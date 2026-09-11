#!/usr/bin/env bash
# Boot a lab: one node, or a cluster of them.
#
# Each node boots from its own node image - the base image plus that node's
# identity, built by lab/node-image.sh - so a node comes up correct rather than
# being corrected afterwards. Node images are qcow2 overlays on the base, so a
# node costs a few megabytes, and the lab itself boots a throwaway overlay on
# top of that. Resetting a node is a delete, not a reinstall.
#
# Each node has three NICs, which is what a real PVE node has:
#
#   management  its own user-mode network per node, reached only through the
#               host's port forward. Nothing is shared between nodes.
#   guest       vmbr0, with no host address. Guests live here and the node does
#               not, so a guest can never take the node's address.
#   cluster     a QEMU socket netdev - a raw L2 link between QEMU processes,
#               entirely in userspace. No tap and no bridge, so /dev/kvm stays
#               the only elevated thing the runner needs.
#
# Test disks are raw files attached with stable serials, so a profile addresses
# them as /dev/disk/by-id/virtio-labdiskN rather than guessing at vdb. The
# system disk carries bootindex=0 so SeaBIOS does not have to guess either.
#
#   up.sh [--name lab] [--nodes 2] [--disks 4] [--disk-size 8G]
#         [--mem 6144] [--cpus 4] [--variant base] [--fresh] [--reuse]

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/../lib/nodes.sh"

ROOT="$(lab_root)"
NAME="${LAB_NAME:-lab}"
NODES="${LAB_NODES:-1}"
PVE_VERSION="${PVE_VERSION:-9.2-1}"
IMAGE_VARIANT="${IMAGE_VARIANT:-base}"
DISKS="${LAB_DISKS:-4}"
DISK_SIZE="${LAB_DISK_SIZE:-8G}"
MEM="${LAB_MEM:-6144}"
CPUS="${LAB_CPUS:-4}"
CLUSTER_MCAST="${LAB_CLUSTER_MCAST:-230.0.0.1:24000}"
FRESH=0
REUSE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)      NAME="$2";          shift 2 ;;
        --nodes)     NODES="$2";         shift 2 ;;
        --disks)     DISKS="$2";         shift 2 ;;
        --disk-size) DISK_SIZE="$2";     shift 2 ;;
        --mem)       MEM="$2";           shift 2 ;;
        --cpus)      CPUS="$2";          shift 2 ;;
        --variant)   IMAGE_VARIANT="$2"; shift 2 ;;
        --fresh)     FRESH=1;            shift ;;
        --reuse)     REUSE=1;            shift ;;
        -h|--help)   sed -n '2,25p' "$0"; exit 0 ;;
        *)           die "unknown argument: $1" ;;
    esac
done

require_cmd qemu-system-x86_64 qemu-img ssh

STATE="$(lab_state_dir)"
SSH_KEY="$STATE/id_ed25519"
LAB="$STATE/labs/$NAME"

# ── Already running? ─────────────────────────────────────────────────────────

running=0
for ((i = 1; i <= NODES; i++)); do
    pid="$LAB/node$i/qemu.pid"
    [[ -f "$pid" ]] && kill -0 "$(cat "$pid")" 2>/dev/null && running=$((running + 1))
done
if [[ $running -gt 0 ]]; then
    if [[ $FRESH -eq 0 ]]; then
        log_info "lab '$NAME' already running ($running node(s))"
        cat "$LAB/lab.env"
        exit 0
    fi
    log_info "lab '$NAME' running; tearing it down first (--fresh)"
    "$ROOT/lab/down.sh" --name "$NAME" --reset
fi
[[ $FRESH -eq 1 ]] && rm -rf "$LAB"
mkdir -p "$LAB"

mapfile -t SSH_OPTS < <(node_ssh_opts)

# ── Boot ─────────────────────────────────────────────────────────────────────

for ((i = 1; i <= NODES; i++)); do
    "$ROOT/lab/node-image.sh" --node "$i" --variant "$IMAGE_VARIANT" >&2

    NODE_IMAGE="$STATE/images/pve-${PVE_VERSION}-${IMAGE_VARIANT}-node${i}.qcow2"
    ND="$LAB/node$i"
    mkdir -p "$ND"

    # A stopped lab's disks are discarded unless --reuse. They are stale twice
    # over: they hold the previous run's filesystems, and they accumulate -
    # test disks fill as tests write to them.
    [[ $REUSE -eq 0 ]] && rm -f "$ND"/system.qcow2 "$ND"/disk*.raw

    [[ -f "$ND/system.qcow2" ]] || \
        qemu-img create -f qcow2 -b "$NODE_IMAGE" -F qcow2 "$ND/system.qcow2" >/dev/null

    disk_args=()
    for d in $(seq 1 "$DISKS"); do
        f="$ND/disk${d}.raw"
        [[ -f "$f" ]] || qemu-img create -f raw "$f" "$DISK_SIZE" >/dev/null
        disk_args+=(
            -drive "file=$f,if=none,id=d$d,format=raw,cache=unsafe"
            -device "virtio-blk-pci,drive=d$d,serial=labdisk$d,addr=$(printf '0x%x' $((0x13 + d)))"
        )
    done

    ssh_port="$(node_ssh_port "$i")"
    gui_port="$(node_gui_port "$i")"
    mgmt_ip="$(node_mgmt_ip "$i")"

    log_info "node$i ($(node_hostname "$i")): booting - $CPUS cpus, ${MEM}M, $DISKS x $DISK_SIZE, mgmt $mgmt_ip"
    qemu-system-x86_64 \
        -enable-kvm -cpu host -machine q35 \
        -smp "$CPUS" -m "$MEM" \
        -drive file="$ND/system.qcow2",if=none,id=sys,format=qcow2 \
        -device virtio-blk-pci,drive=sys,serial=labsystem,addr=0x10,bootindex=0 \
        -netdev user,id=mgmt,net="$(node_mgmt_net "$i")",host="$(node_mgmt_gw "$i")",hostfwd=tcp:127.0.0.1:"$ssh_port"-"$mgmt_ip":22,hostfwd=tcp:127.0.0.1:"$gui_port"-"$mgmt_ip":8006 \
        -device virtio-net-pci,netdev=mgmt,addr=0x11,mac="$(node_mgmt_mac "$i")" \
        -netdev user,id=guest,net="$(node_guest_net "$i")",host="$(node_guest_gw "$i")" \
        -device virtio-net-pci,netdev=guest,addr=0x12,mac="$(node_guest_mac "$i")" \
        -netdev socket,id=clus,mcast="$CLUSTER_MCAST" \
        -device virtio-net-pci,netdev=clus,addr=0x13,mac="$(node_cluster_mac "$i")" \
        "${disk_args[@]}" \
        -display none -serial file:"$ND/console.log" \
        -pidfile "$ND/qemu.pid" -daemonize

    log_info "node$i: waiting for SSH on 127.0.0.1:$ssh_port"
    up=0
    for _ in $(seq 1 120); do
        if ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" -p "$ssh_port" root@127.0.0.1 true 2>/dev/null; then
            up=1; break
        fi
        sleep 5
    done
    [[ $up -eq 1 ]] || die "node$i did not come up - see $ND/console.log"
done

# ── Record ───────────────────────────────────────────────────────────────────

{
    echo "LAB_NAME=$NAME"
    echo "LAB_DIR=$LAB"
    echo "LAB_NODE_COUNT=$NODES"
    # NODE_SSH_PORT is node 1, so anything written for a single-node lab keeps
    # working unchanged.
    echo "NODE_SSH_PORT=$(node_ssh_port 1)"
    echo "NODE_GUI_PORT=$(node_gui_port 1)"
    echo "NODE_SSH_KEY=$SSH_KEY"
    for ((i = 1; i <= NODES; i++)); do
        echo "NODE${i}_NAME=$(node_hostname "$i")"
        echo "NODE${i}_SSH_PORT=$(node_ssh_port "$i")"
        echo "NODE${i}_GUI_PORT=$(node_gui_port "$i")"
        echo "NODE${i}_MGMT_IP=$(node_mgmt_ip "$i")"
        echo "NODE${i}_CLUSTER_IP=$(node_cluster_ip "$i")"
    done
    echo "LAB_DISKS=$DISKS"
    echo "LAB_DISK_SIZE=$DISK_SIZE"
    echo "PVE_VERSION=$PVE_VERSION"
    echo "IMAGE_VARIANT=$IMAGE_VARIANT"
} > "$LAB/lab.env"

if [[ $NODES -gt 1 ]]; then
    "$ROOT/lab/cluster.sh" --name "$NAME"
fi

log_info "lab '$NAME' is up"
cat "$LAB/lab.env"
