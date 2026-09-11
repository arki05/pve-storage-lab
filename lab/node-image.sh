#!/usr/bin/env bash
# Derive a node image from the base image.
#
# The base image is generic: one hostname, management on DHCP, no cluster NIC.
# A node image is that base plus one node's identity - its hostname, its three
# MAC-derived interfaces and their addresses - baked in, so the node boots
# correct rather than being corrected afterwards.
#
# It matters that the rename happens here. Renaming a PVE node means moving
# /etc/pve/nodes/<name> and restarting pmxcfs, which is safe on a standalone
# node and awkward on one that has already joined a cluster. Doing it while
# building the image means cluster formation never has to.
#
# The image is a qcow2 overlay on the base, so a node costs a few megabytes.
#
#   node-image.sh --node 2 [--variant base] [--force]

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/../lib/nodes.sh"

PVE_VERSION="${PVE_VERSION:-9.2-1}"
NODE=""
VARIANT="base"
FORCE=0
MEM="${NODE_IMAGE_MEM:-4096}"
CPUS="${NODE_IMAGE_CPUS:-2}"
# Derived from the node index: two builders running at once would otherwise
# contend for the same forward, and the loser boots a machine nobody can reach.
BUILD_PORT="${NODE_IMAGE_PORT:-0}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --node)    NODE="$2";    shift 2 ;;
        --variant) VARIANT="$2"; shift 2 ;;
        --force)   FORCE=1;      shift ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *)         die "unknown argument: $1" ;;
    esac
done
[[ -n "$NODE" ]] || die "--node is required"
[[ "$BUILD_PORT" != 0 ]] || BUILD_PORT=$((25590 + NODE))

STATE="$(lab_state_dir)"
BASE="$STATE/images/pve-${PVE_VERSION}-${VARIANT}.qcow2"
NODE_IMAGE="$STATE/images/pve-${PVE_VERSION}-${VARIANT}-node${NODE}.qcow2"
SSH_KEY="$STATE/id_ed25519"

[[ -f "$BASE" ]] || die "base image missing: $BASE"
if [[ -f "$NODE_IMAGE" && $FORCE -eq 0 ]]; then
    log_info "node image already built: $(basename "$NODE_IMAGE")"
    exit 0
fi

HOSTNAME="$(node_hostname "$NODE")"
MGMT_MAC="$(node_mgmt_mac "$NODE")";       MGMT_IF="$(node_mgmt_ifname "$NODE")"
GUEST_MAC="$(node_guest_mac "$NODE")";     GUEST_IF="$(node_guest_ifname "$NODE")"
CLUSTER_MAC="$(node_cluster_mac "$NODE")"; CLUSTER_IF="$(node_cluster_ifname "$NODE")"
MGMT_IP="$(node_mgmt_ip "$NODE")"
MGMT_NET="$(node_mgmt_net "$NODE")"
MGMT_GW="$(node_mgmt_gw "$NODE")"
MGMT_DNS="$(node_mgmt_dns "$NODE")"

rm -f "$NODE_IMAGE"
qemu-img create -f qcow2 -b "$BASE" -F qcow2 "$NODE_IMAGE" >/dev/null

mapfile -t SSH_OPTS < <(node_ssh_opts)
nssh()       { ssh -n "${SSH_OPTS[@]}" -i "$SSH_KEY" -p "$BUILD_PORT" root@127.0.0.1 "$@"; }
nssh_stdin() { ssh    "${SSH_OPTS[@]}" -i "$SSH_KEY" -p "$BUILD_PORT" root@127.0.0.1 "$@"; }

PIDFILE=$(mktemp -u)
boot() {
    # $1..$3: the MACs to present. The first boot uses the base image's own,
    # because that is what its interfaces file still names; the second uses the
    # node's real ones, which is what proves the new config works.
    qemu-system-x86_64 \
        -enable-kvm -cpu host -machine q35 -smp "$CPUS" -m "$MEM" \
        -drive file="$NODE_IMAGE",if=none,id=sys,format=qcow2 \
        -device virtio-blk-pci,drive=sys,addr=0x10,bootindex=0 \
        -netdev user,id=mgmt,net="$MGMT_NET",host="$MGMT_GW",dhcpstart="$MGMT_IP",hostfwd=tcp:127.0.0.1:"$BUILD_PORT"-"$MGMT_IP":22 \
        -device virtio-net-pci,netdev=mgmt,addr=0x11,mac="$1" \
        -netdev user,id=guest,net="$(node_guest_net "$NODE")",host="$(node_guest_gw "$NODE")" \
        -device virtio-net-pci,netdev=guest,addr=0x12,mac="$2" \
        -netdev socket,id=clus,mcast=230.0.0.9:24999 \
        -device virtio-net-pci,netdev=clus,addr=0x13,mac="$3" \
        -display none -serial file:"${NODE_IMAGE%.qcow2}.console.log" \
        -pidfile "$PIDFILE" -daemonize
}
halt() {
    [[ -f "$PIDFILE" ]] || return 0
    local pid; pid=$(cat "$PIDFILE")
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PIDFILE"
}
wait_ssh() {
    for _ in $(seq 1 120); do
        nssh true 2>/dev/null && return 0
        sleep 5
    done
    return 1
}
trap halt EXIT

# ── First boot: still the base image's identity ──────────────────────────────

log_info "node$NODE: booting the base image to personalise it"
boot "$LAB_BASE_MGMT_MAC" "$LAB_BASE_GUEST_MAC" "$CLUSTER_MAC"
wait_ssh || die "node$NODE: base image did not come up"

log_info "node$NODE: writing identity ($HOSTNAME, $MGMT_IP, cluster $(node_cluster_ip "$NODE"))"
STEP="$(mktemp)"
# Quoted heredoc: nothing expands here. Build-time values arrive as arguments
# instead. The alternative - an unquoted heredoc with \$ escapes on everything
# meant for runtime - is a constant source of variables that silently expand on
# the wrong side, and the failures it produces ("cp: missing file operand",
# "vmid: unbound variable") say nothing about the cause.
cat > "$STEP" <<'REMOTE'
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
REMOTE
nssh_stdin "cat > /root/.lab-node-step.sh" < "$STEP"
rm -f "$STEP"
nssh "bash /root/.lab-node-step.sh '$HOSTNAME' '$NODE' '$MGMT_IF' '$MGMT_IP' '$MGMT_GW' '$MGMT_DNS' '$GUEST_IF' '$CLUSTER_IF' '$(node_cluster_ip "$NODE")'" || true
nssh "test -f /root/.lab-node-identity" || die "node$NODE: identity step did not complete"

nssh "systemctl poweroff" 2>/dev/null || true
for _ in $(seq 1 60); do
    [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null || break
    sleep 2
done
halt

# ── Second boot: the node's own MACs, which is the real test ─────────────────

log_info "node$NODE: rebooting on its own MACs to verify"
boot "$MGMT_MAC" "$GUEST_MAC" "$CLUSTER_MAC"
wait_ssh || die "node$NODE: unreachable on its own identity - see ${NODE_IMAGE%.qcow2}.console.log"

[[ "$(nssh hostname)" == "$HOSTNAME" ]] || die "node$NODE: hostname did not stick"
nssh "ip -4 -o addr show $MGMT_IF | grep -q $MGMT_IP" \
    || die "node$NODE: management address is not $MGMT_IP on $MGMT_IF"
nssh "ip -4 -o addr show $CLUSTER_IF | grep -q $(node_cluster_ip "$NODE")" \
    || die "node$NODE: cluster address missing on $CLUSTER_IF"
nssh "ip -br link show vmbr0 | grep -q UP" || die "node$NODE: vmbr0 is down"
nssh "! ip -4 -o addr show vmbr0 | grep -q inet" \
    || die "node$NODE: vmbr0 has an address; the node is on the guest bridge"
nssh "test -d /etc/pve/nodes/$HOSTNAME" || die "node$NODE: /etc/pve/nodes/$HOSTNAME missing"
# The LXC template is storage content rather than a guest, so every node keeps
# it. The VM template is a guest and lives only on node 1 - a joining node has
# to be empty. Both are why a lab starts in twenty seconds instead of five
# minutes, and losing either shows up much later as tests quietly skipping.
nssh "pveam list local | grep -q amd64" \
    || die "node$NODE: the baked LXC template did not survive the rename"
if [[ "$NODE" == 1 ]]; then
    nssh "qm list | grep -q lab-guest-template" \
        || die "node$NODE: the baked VM template did not survive the rename"
else
    nssh "test -z \"\$(qm list 2>/dev/null | awk 'NR>1')\"" \
        || die "node$NODE: still has guests; pvecm add will refuse to join it"
fi
log_info "node$NODE: verified"

nssh "fstrim -av" >/dev/null 2>&1 || true
nssh "systemctl poweroff" 2>/dev/null || true
for _ in $(seq 1 60); do
    [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null || break
    sleep 2
done
trap - EXIT
halt

log_info "node$NODE: built $(basename "$NODE_IMAGE") ($(du -h "$NODE_IMAGE" | cut -f1))"
