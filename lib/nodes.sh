# What a lab node is called and addressed. Source, don't execute.
# shellcheck shell=bash
#
# Everything about a node is derived from its index, so there is one place to
# look and nothing to keep in sync. Three roles, three NICs, three addresses:
#
#   management  what the host forwards to, and the node's own address. On its
#               own network per node, so nothing is shared between nodes and no
#               guest can reach it.
#   guest       the bridge guests attach to. vmbr0, with no host address - that
#               separation is what stops a guest taking the node's lease.
#   cluster     corosync and inter-node traffic, over a QEMU socket netdev.
#
# MAC prefixes: b0 management, b1 guest, c1 cluster; last octet is the index.
# Interface names follow from the MACs, because the image names every NIC after
# its MAC rather than its PCI slot.

node_hostname()     { echo "pve-node$1"; }

node_mgmt_mac()     { printf '52:54:00:1a:b0:%02d' "$1"; }
node_guest_mac()    { printf '52:54:00:1a:b1:%02d' "$1"; }
node_cluster_mac()  { printf '52:54:00:1a:c1:%02d' "$1"; }

_ifname() { echo "enx$(echo "$1" | tr -d ':')"; }
node_mgmt_ifname()    { _ifname "$(node_mgmt_mac "$1")"; }
node_guest_ifname()   { _ifname "$(node_guest_mac "$1")"; }
node_cluster_ifname() { _ifname "$(node_cluster_mac "$1")"; }

# Management lives on a per-node /24 so two nodes never share an address.
node_mgmt_net()     { echo "10.0.$((1 + $1)).0/24"; }
node_mgmt_gw()      { echo "10.0.$((1 + $1)).2"; }
node_mgmt_dns()     { echo "10.0.$((1 + $1)).3"; }
node_mgmt_ip()      { echo "10.0.$((1 + $1)).10"; }

# The guest network is the same on every node. Nothing routes between nodes
# here - the cluster link does that - so identical numbering is correct rather
# than merely convenient.
node_guest_net()    { echo "10.0.99.0/24"; }
node_guest_gw()     { echo "10.0.99.2"; }

node_cluster_ip()   { echo "10.9.9.$1"; }

# Host-side ports, ten apart so a node has room for more forwards later.
node_ssh_port()     { echo $(( ${LAB_BASE_SSH_PORT:-25522} + ($1 - 1) * 10 )); }
node_gui_port()     { echo $(( ${LAB_BASE_GUI_PORT:-28006} + ($1 - 1) * 10 )); }

# What the base image itself was built with. A node image's first boot has to
# present these, because the base's interfaces file still names them; the
# second boot uses the node's own and proves the new config works.
LAB_BASE_MGMT_MAC="52:54:00:1a:b0:01"
LAB_BASE_GUEST_MAC="52:54:00:1a:b1:01"
