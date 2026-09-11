"""Joining a multi-node lab into a PVE cluster.

Migration is the only reason this exists: PVE cannot move a guest between
unclustered nodes. Everything else works on one node.

Each node already knows who it is - hostname, interfaces and addresses come
from its node image - so this only teaches the nodes about each other and runs
pvecm. In particular no node is renamed here: renaming a PVE node means moving
/etc/pve/nodes/<name> and restarting pmxcfs, which is safe on a standalone node
and awkward on one that has joined, so it happens while the image is built.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from . import nodes as ident
from . import state
from .ssh import NodeSSH

log = logging.getLogger("cluster")
REMOTE = Path(__file__).resolve().parent / "remote"

ROOT_PASSWORD = "pvelab123"


class ClusterError(RuntimeError):
    pass


def form(name: str = "lab", count: int = 2) -> None:
    if count < 2:
        log.info("single node; nothing to cluster")
        return

    key = state.ssh_key()
    members = ident.nodes(count)
    conns = {n.index: NodeSSH(n.ssh_port, key) for n in members}

    if conns[1].ok("pvecm status"):
        log.info("cluster already formed")
        return

    names = " ".join(n.hostname for n in members)
    block = "".join(f"{n.cluster_ip} {n.hostname}.local {n.hostname}\\n"
                    for n in members)

    for node in members:
        log.info("node%d: peers", node.index)
        conns[node.index].run_script(REMOTE / "cluster-hosts.sh", names, block)
        resolved = conns[node.index].run(
            f"getent hosts {node.hostname} | awk '{{print $1}}' | head -1").strip()
        if resolved != node.cluster_ip:
            raise ClusterError(
                f"node{node.index} resolves its own name to '{resolved}', "
                f"expected {node.cluster_ip}")

    log.info("node1: creating cluster '%s'", name)
    conns[1].run(f"pvecm create '{name}' --link0 '{members[0].cluster_ip}'",
                 timeout=300)
    time.sleep(5)

    fingerprint = conns[1].run(
        "openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -fingerprint -sha256"
    ).strip().split("=")[-1]
    if not fingerprint:
        raise ClusterError("could not read node1's certificate fingerprint")

    for node in members[1:]:
        log.info("node%d: joining", node.index)
        conns[node.index].run(
            f"echo '{ROOT_PASSWORD}' | pvecm add '{members[0].cluster_ip}' "
            f"--link0 '{node.cluster_ip}' --fingerprint '{fingerprint}'",
            timeout=600)
        time.sleep(10)

    _wait_quorate(conns[1], count)
    _repair_known_hosts(conns, members)
    _verify_cross_node(conns, members)


def _wait_quorate(conn: NodeSSH, count: int, timeout: int = 300) -> None:
    log.info("waiting for quorum")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if conn.ok("pvecm status 2>/dev/null | grep -q 'Quorate:.*Yes'"):
            # Quorum alone is not proof: a one-node cluster is quorate too.
            seen = conn.run("pvecm nodes 2>/dev/null | grep -c '^ *[0-9]' || true")
            if int(seen.strip() or 0) >= count:
                log.info("cluster is quorate with %s nodes", seen.strip())
                return
        time.sleep(5)
    raise ClusterError(f"cluster did not reach quorum with {count} nodes")


def _repair_known_hosts(conns: dict[int, NodeSSH], members) -> None:
    """Rebuild PVE's ssh_known_hosts from the keys that actually exist.

    Every node image regenerates its SSH host keys, so the file PVE inherited
    from the base image names the right node with the wrong key. Any API call
    one node proxies to another then fails with "Host key verification failed"
    - which surfaces as migrations timing out, not as an SSH error.
    """
    for node in members:
        conns[node.index].run("pvecm updatecerts -f", check=False, timeout=300)
        conns[node.index].run("systemctl restart pvedaemon pveproxy",
                              check=False, timeout=300)
    time.sleep(5)


def _verify_cross_node(conns: dict[int, NodeSSH], members) -> None:
    """Prove it, rather than finding out during a migration."""
    for source in members:
        for target in members:
            if source.index == target.index:
                continue
            if not conns[source.index].ok(
                    f"pvesh get /nodes/{target.hostname}/status "
                    f"--output-format json >/dev/null"):
                raise ClusterError(
                    f"node{source.index} cannot reach node{target.index} "
                    f"through the API")
    log.info("cross-node API verified")
