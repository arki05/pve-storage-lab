"""What a lab node is called and addressed.

Everything about a node derives from its index, so there is one place to look
and nothing to keep in sync. Three roles, three NICs, three addresses:

  management  what the host forwards to, and the node's own address. Its own
              network per node, so nothing is shared between nodes and no
              guest can reach it.
  guest       the bridge guests attach to. vmbr0, with no host address - that
              separation is what stops a guest taking the node's lease.
  cluster     corosync and inter-node traffic, over a QEMU socket netdev.

Interface names follow from the MACs, because the image names every NIC after
its MAC rather than its PCI slot: a name derived from a PCI slot moves the
moment a test disk is added.
"""

from dataclasses import dataclass

# The base image's own MACs. A node image's first boot presents these, because
# the base's interfaces file still names them; the second boot uses the node's
# own and proves the new configuration works.
BASE_MGMT_MAC = "52:54:00:1a:b0:01"
BASE_GUEST_MAC = "52:54:00:1a:b1:01"

GUEST_NET = "10.0.99.0/24"
GUEST_GW = "10.0.99.2"
GUEST_HOST_IP = "10.0.99.1"   # only the base image holds this; node images do not
CLUSTER_MCAST = "230.0.0.1:24000"

BASE_SSH_PORT = 25522
BASE_GUI_PORT = 28006


def _ifname(mac: str) -> str:
    return "enx" + mac.replace(":", "")


@dataclass(frozen=True)
class Node:
    """One lab node. Immutable, derived entirely from its index."""

    index: int

    @property
    def hostname(self) -> str:
        return f"pve-node{self.index}"

    # MAC prefixes: b0 management, b1 guest, c1 cluster; last octet the index.
    @property
    def mgmt_mac(self) -> str:
        return f"52:54:00:1a:b0:{self.index:02d}"

    @property
    def guest_mac(self) -> str:
        return f"52:54:00:1a:b1:{self.index:02d}"

    @property
    def cluster_mac(self) -> str:
        return f"52:54:00:1a:c1:{self.index:02d}"

    @property
    def mgmt_ifname(self) -> str:
        return _ifname(self.mgmt_mac)

    @property
    def guest_ifname(self) -> str:
        return _ifname(self.guest_mac)

    @property
    def cluster_ifname(self) -> str:
        return _ifname(self.cluster_mac)

    # Management on a per-node /24, so two nodes never share an address. They
    # could - the networks are isolated - but PVE takes a node's ssh address
    # from resolving its hostname, and one stale hosts entry then makes a node
    # reach itself and deadlock on a lock it already holds.
    @property
    def mgmt_subnet(self) -> str:
        return f"10.0.{1 + self.index}"

    @property
    def mgmt_net(self) -> str:
        return f"{self.mgmt_subnet}.0/24"

    @property
    def mgmt_gw(self) -> str:
        return f"{self.mgmt_subnet}.2"

    @property
    def mgmt_dns(self) -> str:
        return f"{self.mgmt_subnet}.3"

    @property
    def mgmt_ip(self) -> str:
        return f"{self.mgmt_subnet}.10"

    @property
    def cluster_ip(self) -> str:
        return f"10.9.9.{self.index}"

    # Ports ten apart, so a node has room for more forwards later.
    @property
    def ssh_port(self) -> int:
        return BASE_SSH_PORT + (self.index - 1) * 10

    @property
    def gui_port(self) -> int:
        return BASE_GUI_PORT + (self.index - 1) * 10


def nodes(count: int) -> list[Node]:
    return [Node(i) for i in range(1, count + 1)]
