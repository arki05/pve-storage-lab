"""Building images.

Three layers, each cached at a different lifetime:

  base    a generic PVE install with the templates baked in. Never booted as a
          lab node - it exists to derive from.
  variant base plus a profile's expensive setup, if it has any. A bcachefs
          profile builds a DKMS module here, once, instead of on every run.
  node    a variant plus one node's identity. A qcow2 overlay costing about
          15 MB, so a node is cheap even though it is a whole machine.

A node image is why nodes boot correct rather than being corrected afterwards,
and why the PVE rename happens once - on a standalone node, which is the safe
time - instead of during cluster formation.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from . import nodes as ident
from . import state
from .qemu import Machine, SocketNet, UserNet
from .ssh import NodeSSH

log = logging.getLogger("images")
REMOTE = Path(__file__).resolve().parent / "remote"


class ImageError(RuntimeError):
    pass


def _overlay(base: Path, target: Path) -> None:
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "-b", str(base),
                    "-F", "qcow2", str(target)], check=True, capture_output=True)


def _flatten(source: Path, target: Path) -> None:
    """Collapse an overlay into a standalone, compressed image."""
    tmp = target.with_suffix(".tmp")
    subprocess.run(["qemu-img", "convert", "-O", "qcow2", "-c", str(source),
                    str(tmp)], check=True)
    tmp.replace(target)


# Image builds get their own multicast group, so a build never joins the
# segment of a lab that happens to be running.
BUILD_MCAST = "230.0.0.9:24999"


def _build_machine(name: str, image: Path, workdir: Path, port: int,
                   mgmt_ip: str, mgmt_net: str, mgmt_gw: str,
                   mgmt_mac: str, guest_mac: str, mem: int, cpus: int,
                   cluster_mac: str | None = None) -> Machine:
    """A machine for building an image.

    All three NICs, always. The base image's vmbr0 bridges the guest NIC, so
    booting without it leaves the bridge with no port - the node comes up, sits
    at a login prompt, and says nothing about why it is unreachable. And a node
    image configures a cluster address, which cannot exist on an interface that
    was never attached.
    """
    nets = [
        UserNet(mac=mgmt_mac, addr=0x11, net=mgmt_net, gw=mgmt_gw,
                dhcpstart=mgmt_ip, forwards=[(port, mgmt_ip, 22)]),
        UserNet(mac=guest_mac, addr=0x12, net=ident.GUEST_NET,
                gw=ident.GUEST_GW, ident="guest"),
    ]
    if cluster_mac:
        nets.append(SocketNet(mac=cluster_mac, addr=0x13, mcast=BUILD_MCAST))
    return Machine(name=name, image=image, workdir=workdir, mem=mem, cpus=cpus,
                   nets=nets, system_cache="unsafe")


def node_image(index: int, variant: str = "base", version: str = "9.2-1",
               force: bool = False, mem: int = 4096, cpus: int = 2) -> Path:
    """Derive a node image from a variant image.

    Two boots. The first presents the base image's MACs, because the base's
    interfaces file still names them; the second presents the node's own, which
    is what proves the new configuration works rather than assuming it.
    """
    node = ident.Node(index)
    base = state.image_path(version, variant)
    target = state.image_path(version, variant, index)
    if not base.exists():
        raise ImageError(f"image missing: {base}")
    if target.exists() and not force:
        log.info("node%d: image already built (%s)", index, target.name)
        return target

    workdir = state.state_dir() / "build" / f"node{index}"
    workdir.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    _overlay(base, target)

    # Derived from the index: two builders at once would otherwise contend for
    # one forward, and the loser boots a machine nobody can reach.
    port = 25590 + index
    key = state.ssh_key()
    conn = NodeSSH(port, key)

    def boot(mgmt_mac: str, guest_mac: str) -> Machine:
        machine = _build_machine(
            f"nodeimage{index}", target, workdir, port, node.mgmt_ip,
            node.mgmt_net, node.mgmt_gw, mgmt_mac, guest_mac, mem, cpus,
            cluster_mac=node.cluster_mac)
        machine.start()
        if not conn.wait(timeout=600):
            machine.kill()
            raise ImageError(
                f"node{index}: unreachable - see {machine.console}")
        return machine

    log.info("node%d: personalising the base image", index)
    machine = boot(ident.BASE_MGMT_MAC, ident.BASE_GUEST_MAC)
    try:
        log.info("node%d: writing identity (%s, %s, cluster %s)",
                 index, node.hostname, node.mgmt_ip, node.cluster_ip)
        conn.run_script(
            REMOTE / "node-identity.sh",
            node.hostname, str(index),
            node.mgmt_ifname, node.mgmt_ip, node.mgmt_gw, node.mgmt_dns,
            node.guest_ifname, node.cluster_ifname, node.cluster_ip,
            check=False)
        if not conn.ok("test -f /root/.lab-node-identity"):
            raise ImageError(f"node{index}: identity step did not complete")
        conn.run("systemctl poweroff", check=False)
        machine.wait_until_stopped()
    finally:
        machine.kill()

    log.info("node%d: rebooting on its own MACs to verify", index)
    machine = boot(node.mgmt_mac, node.guest_mac)
    try:
        _verify_node(conn, node, index)
        conn.run("fstrim -av", check=False)
        conn.run("systemctl poweroff", check=False)
        machine.wait_until_stopped()
    finally:
        machine.kill()

    size = target.stat().st_size / (1 << 20)
    log.info("node%d: built %s (%.0fM)", index, target.name, size)
    return target


def _verify_node(conn: NodeSSH, node: ident.Node, index: int) -> None:
    """Assert the things that have silently gone wrong before."""
    checks = [
        (f"[ \"$(hostname)\" = {node.hostname} ]",
         "hostname did not stick"),
        (f"ip -4 -o addr show {node.mgmt_ifname} | grep -q {node.mgmt_ip}",
         f"management address is not {node.mgmt_ip} on {node.mgmt_ifname}"),
        (f"ip -4 -o addr show {node.cluster_ifname} | grep -q {node.cluster_ip}",
         f"cluster address missing on {node.cluster_ifname}"),
        ("ip -br link show vmbr0 | grep -q UP", "vmbr0 is down"),
        # The node must not live on the guest bridge: that is what stops a
        # guest taking its address or its DHCP lease.
        ("! ip -4 -o addr show vmbr0 | grep -q inet",
         "vmbr0 has an address; the node is on the guest bridge"),
        (f"test -d /etc/pve/nodes/{node.hostname}",
         f"/etc/pve/nodes/{node.hostname} missing"),
        # The LXC template is storage content, so every node keeps it. The VM
        # template is a guest and lives only on node 1 - pvecm refuses to join
        # a node that has any. Both are why a lab starts in twenty seconds
        # rather than five minutes, and losing either shows up much later as
        # tests quietly skipping.
        ("pveam list local | grep -q amd64",
         "the baked LXC template did not survive the rename"),
    ]
    if index == 1:
        checks.append(("qm list | grep -q lab-guest-template",
                       "the baked VM template did not survive the rename"))
    else:
        checks.append(("test -z \"$(qm list 2>/dev/null | awk 'NR>1')\"",
                       "still has guests; pvecm add will refuse to join it"))

    for command, message in checks:
        if not conn.ok(command):
            raise ImageError(f"node{index}: {message}")
    log.info("node%d: verified", index)


def bake_profile(profile_dir: Path, base_variant: str = "base",
                 out_variant: str | None = None, version: str = "9.2-1",
                 force: bool = False, mem: int = 6144, cpus: int = 4) -> str:
    """Bake a profile's expensive setup into a derived image.

    Some backends need real work before they can be tested at all - a DKMS
    module built against the running kernel, packages from a third-party
    repository. That is minutes per run, and because every lab boots a
    throwaway overlay, "per run" means every single time.

    So a profile may split its setup in two: bake.sh once, into an image, for
    anything belonging to the machine; setup.sh every lab, for anything
    belonging to the disks, which are recreated each time.

    Bakes chain. A second profile with expensive setup builds on the first
    one's result rather than on the base, so a lab with two of them pays for
    each once instead of on every run.
    """
    from .profiles import profile_name

    name = profile_name(profile_dir)
    out_variant = out_variant or name
    base = state.image_path(version, base_variant)
    target = state.image_path(version, out_variant)

    if not (profile_dir / "bake.sh").exists():
        log.info("profile '%s' has no bake.sh; nothing to pre-build", name)
        return base_variant
    if not base.exists():
        raise ImageError(f"image missing: {base}")
    if target.exists() and not force:
        log.info("derived image already built: %s", target.name)
        return out_variant

    workdir = state.state_dir() / "build" / f"bake-{out_variant}"
    workdir.mkdir(parents=True, exist_ok=True)
    scratch = workdir / "node.qcow2"
    scratch.unlink(missing_ok=True)
    _overlay(base, scratch)

    port = 25589
    conn = NodeSSH(port, state.ssh_key())
    machine = _build_machine("bake", scratch, workdir, port,
                             ident.Node(1).mgmt_ip, ident.Node(1).mgmt_net,
                             ident.Node(1).mgmt_gw, ident.BASE_MGMT_MAC,
                             ident.BASE_GUEST_MAC, mem, cpus)

    log.info("baking profile '%s' onto %s", name, base_variant)
    machine.start()
    try:
        if not conn.wait(timeout=600):
            raise ImageError(f"bake node did not come up - see {machine.console}")

        conn.run(f"rm -rf /root/lab-profile && mkdir -p /root/lab-profile")
        conn.push_dir(profile_dir, "/root/lab-profile")
        # </dev/null so the profile cannot consume anything it should not.
        conn.run("rm -f /root/.lab-bake-ready; chmod +x /root/lab-profile/*.sh "
                 "2>/dev/null || true", check=False)
        conn.run("bash /root/lab-profile/bake.sh </dev/null "
                 "&& touch /root/.lab-bake-ready", check=False, timeout=5400)
        if not conn.ok("test -f /root/.lab-bake-ready"):
            raise ImageError(f"profile '{name}' bake did not complete")

        conn.run("fstrim -av", check=False)
        conn.run("systemctl poweroff", check=False)
        machine.wait_until_stopped()
    finally:
        machine.kill()

    log.info("flattening into a standalone image")
    _flatten(scratch, target)
    scratch.unlink(missing_ok=True)
    log.info("built %s (%.1fG)", target.name, target.stat().st_size / (1 << 30))
    return out_variant
