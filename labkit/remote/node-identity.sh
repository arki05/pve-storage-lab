#!/usr/bin/env bash
set -euo pipefail
HOSTNAME="$1"; NODE="$2"
MGMT_IF="$3"; MGMT_IP="$4"; MGMT_GW="$5"; MGMT_DNS="$6"
GUEST_IF="$7"; CLUSTER_IF="$8"; CLUSTER_IP="$9"

rm -f /root/.lab-node-identity

OLD=$(hostname)
if [ "$OLD" != "$HOSTNAME" ]; then
    # /etc/pve/local is a symlink into /etc/pve/nodes/<name>, so the directory
    # has to move and pmxcfs has to be restarted to see it. Safe here because
    # the node is standalone; considerably less so once it has joined.
    systemctl stop pveproxy pvedaemon pvestatd pve-cluster corosync 2>/dev/null || true
    hostnamectl set-hostname "$HOSTNAME"
    echo "$HOSTNAME" > /etc/hostname
    sed -i '/127.0.1.1/d' /etc/hosts
    sed -i "/\b${OLD}\b/d" /etc/hosts
    echo "$MGMT_IP $HOSTNAME.local $HOSTNAME" >> /etc/hosts
    systemctl start pve-cluster
    for _ in $(seq 1 30); do systemctl is-active --quiet pve-cluster && break; sleep 2; done
    systemctl is-active --quiet pve-cluster || {
        echo "pve-cluster did not start after renaming to $HOSTNAME" >&2
        journalctl -u pve-cluster -n 20 --no-pager >&2 || true
        exit 1
    }

    # Guest configs move, they do not copy. /etc/pve is pmxcfs, where a VMID is
    # globally unique: the same config cannot exist under two node directories,
    # so cp fails with "File exists" - and cp -a fails outright, because pmxcfs
    # rejects attribute preservation. Losing every guest config quietly is a
    # bad way to find that out; the baked VM template is what disappears, and
    # it surfaces much later as every VM test skipping.
    for dir in qemu-server lxc; do
        [ -d "/etc/pve/nodes/$OLD/$dir" ] || continue
        mkdir -p "/etc/pve/nodes/$HOSTNAME/$dir"
        for f in "/etc/pve/nodes/$OLD/$dir"/*.conf; do
            [ -e "$f" ] || continue
            dest="/etc/pve/nodes/$HOSTNAME/$dir/$(basename "$f")"
            [ -e "$dest" ] && continue
            mv "$f" "$dest" || { echo "failed to move $f" >&2; exit 1; }
        done
    done
    rm -rf "/etc/pve/nodes/$OLD" 2>/dev/null || true
    systemctl start corosync pvestatd pvedaemon pveproxy 2>/dev/null || true
fi

cat > /etc/network/interfaces <<IFACES
auto lo
iface lo inet loopback

# Management. Its own network per node, reached only through the host's port
# forward, so no two nodes share an address.
auto $MGMT_IF
iface $MGMT_IF inet static
        address $MGMT_IP/24
        gateway $MGMT_GW

# The bridge guests attach to. No address on purpose: the node does not live
# here, so a guest can never take its address or its DHCP lease.
auto $GUEST_IF
iface $GUEST_IF inet manual

auto vmbr0
iface vmbr0 inet manual
        bridge-ports $GUEST_IF
        bridge-stp off
        bridge-fd 0

# Corosync and inter-node traffic.
auto $CLUSTER_IF
iface $CLUSTER_IF inet static
        address $CLUSTER_IP/24

source /etc/network/interfaces.d/*
IFACES

printf 'nameserver %s\n' "$MGMT_DNS" > /etc/resolv.conf

# Its own SSH host keys. Every node image starts from the same base, so without
# this they all present identical keys - and PVE pins a node's key in
# /etc/pve/nodes/<node>/ssh_known_hosts when it joins, then refuses the proxied
# request with "Host key verification failed".
rm -f /etc/ssh/ssh_host_*
ssh-keygen -A >/dev/null
systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true

# Only node 1 keeps the baked guests. A joining node adopts the cluster's
# /etc/pve, and pvecm refuses to join one that already has guests - so every
# node after the first must be empty. Nothing is lost: the cluster's guests are
# node 1's, and tests clone from there.
if [ "$NODE" != 1 ]; then
    for vmid in $(qm list 2>/dev/null | awk 'NR>1 {print $1}'); do
        qm destroy "$vmid" --purge >/dev/null 2>&1 || true
    done
    for vmid in $(pct list 2>/dev/null | awk 'NR>1 {print $1}'); do
        pct destroy "$vmid" --purge >/dev/null 2>&1 || true
    done
fi

touch /root/.lab-node-identity
