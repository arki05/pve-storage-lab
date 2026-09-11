"""Bringing a lab up and down.

A lab is N nodes, each booting its own node image on a throwaway overlay.
Resetting a node is a delete, not a reinstall.
"""

from __future__ import annotations

import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import nodes as ident
from . import state
from .qemu import Disk, Machine, SocketNet, UserNet
from .ssh import NodeSSH

log = logging.getLogger("lab")


class LabError(RuntimeError):
    pass


def _overlay(base: Path, target: Path) -> None:
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-b", str(base), "-F", "qcow2",
         str(target)],
        check=True, capture_output=True)


def _raw(path: Path, size: str) -> None:
    subprocess.run(["qemu-img", "create", "-f", "raw", str(path), size],
                   check=True, capture_output=True)


def machine_for(node: ident.Node, workdir: Path, image: Path, disks: list[Disk],
                mem: int, cpus: int, cluster: bool) -> Machine:
    """The lab's standard machine shape. Every caller gets the same device
    layout, which is the point of there being one of these."""
    nets = [
        UserNet(mac=node.mgmt_mac, addr=0x11, net=node.mgmt_net, gw=node.mgmt_gw,
                forwards=[(node.ssh_port, node.mgmt_ip, 22),
                          (node.gui_port, node.mgmt_ip, 8006)]),
        UserNet(mac=node.guest_mac, addr=0x12, net=ident.GUEST_NET,
                gw=ident.GUEST_GW, ident="guest"),
    ]
    if cluster:
        nets.append(SocketNet(mac=node.cluster_mac, addr=0x13,
                              mcast=ident.CLUSTER_MCAST))
    return Machine(name=f"node{node.index}", image=image, workdir=workdir,
                   mem=mem, cpus=cpus, nets=nets, disks=disks)


def up(name: str = "lab", count: int = 1, disks: int = 4, disk_size: str = "8G",
       mem: int = 6144, cpus: int = 4, variant: str = "base",
       version: str = "9.2-1", reuse: bool = False, fresh: bool = False,
       ensure_image=None) -> dict:
    """Boot a lab. Nodes boot in parallel - there is nothing sequential about
    them until the cluster forms."""
    lab = state.lab_dir(name)
    key = state.ssh_key()
    wanted = ident.nodes(count)

    running = [n for n in wanted
               if Machine(f"node{n.index}", Path("/"), lab / f"node{n.index}").running()]
    if running and not fresh:
        log.info("lab '%s' already running (%d node(s))", name, len(running))
        return read_env(name)
    if running:
        down(name, reset=True)

    lab.mkdir(parents=True, exist_ok=True)

    # Node images are built first, and serially. They are a prerequisite rather
    # than part of booting, and the builder boots a machine of its own - two at
    # once would contend for the same port and the same scratch files.
    if ensure_image is not None:
        for node in wanted:
            ensure_image(node.index, variant)

    def boot(node: ident.Node) -> None:
        workdir = lab / f"node{node.index}"
        workdir.mkdir(parents=True, exist_ok=True)
        image = state.image_path(version, variant, node.index)
        if not image.exists():
            raise LabError(f"node image missing: {image}")

        system = workdir / "system.qcow2"
        if not reuse:
            # A stopped lab's disks are stale twice over: they hold the
            # previous run's filesystems, and they accumulate, because test
            # disks fill as tests write to them.
            system.unlink(missing_ok=True)
            for old in workdir.glob("disk*.raw"):
                old.unlink()
        if not system.exists():
            _overlay(image, system)

        attached = []
        for d in range(1, disks + 1):
            path = workdir / f"disk{d}.raw"
            if not path.exists():
                _raw(path, disk_size)
            attached.append(Disk(path, f"labdisk{d}"))

        machine = machine_for(node, workdir, system, attached, mem, cpus,
                              cluster=count > 1)
        log.info("node%d (%s): booting - %d cpus, %dM, %d x %s, mgmt %s",
                 node.index, node.hostname, cpus, mem, disks, disk_size,
                 node.mgmt_ip)
        machine.start()

        conn = NodeSSH(node.ssh_port, key)
        if not conn.wait(timeout=600):
            raise LabError(
                f"node{node.index} did not come up - see {machine.console}")
        log.info("node%d: up on 127.0.0.1:%d", node.index, node.ssh_port)

    with ThreadPoolExecutor(max_workers=max(1, count)) as pool:
        for result in pool.map(boot, wanted):
            _ = result

    env = {
        "LAB_NAME": name,
        "LAB_DIR": str(lab),
        "LAB_NODE_COUNT": str(count),
        "NODE_SSH_PORT": str(wanted[0].ssh_port),
        "NODE_GUI_PORT": str(wanted[0].gui_port),
        "NODE_SSH_KEY": str(key),
        "LAB_DISKS": str(disks),
        "LAB_DISK_SIZE": disk_size,
        "PVE_VERSION": version,
        "IMAGE_VARIANT": variant,
    }
    for node in wanted:
        env[f"NODE{node.index}_NAME"] = node.hostname
        env[f"NODE{node.index}_SSH_PORT"] = str(node.ssh_port)
        env[f"NODE{node.index}_GUI_PORT"] = str(node.gui_port)
        env[f"NODE{node.index}_MGMT_IP"] = node.mgmt_ip
        env[f"NODE{node.index}_CLUSTER_IP"] = node.cluster_ip
    (lab / "lab.env").write_text(
        "".join(f"{k}={v}\n" for k, v in env.items()))
    return env


def read_env(name: str) -> dict:
    path = state.lab_dir(name) / "lab.env"
    if not path.exists():
        raise LabError(f"lab '{name}' is not up")
    env = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            env[key] = value
    return env


def connection(name: str, node: int = 1) -> NodeSSH:
    env = read_env(name)
    port = int(env.get(f"NODE{node}_SSH_PORT", env["NODE_SSH_PORT"]))
    return NodeSSH(port, Path(env["NODE_SSH_KEY"]))


def down(name: str = "lab", reset: bool = False) -> None:
    lab = state.lab_dir(name)
    if not lab.exists():
        log.info("no such lab: %s", name)
        return
    for workdir in sorted(lab.glob("node*")):
        machine = Machine(workdir.name, Path("/"), workdir)
        if machine.running():
            log.info("stopping %s (pid %s)", workdir.name, machine.pid())
        machine.kill()
    if reset:
        log.info("resetting lab '%s' to pristine", name)
        for pattern in ("node*/system.qcow2", "node*/disk*.raw",
                        "node*/*.console.log", "lab.env", "plan.env"):
            for path in lab.glob(pattern):
                path.unlink(missing_ok=True)
