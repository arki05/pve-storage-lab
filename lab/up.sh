#!/usr/bin/env bash
# Boot a lab node from the base image.
#
# The node runs on a throwaway qcow2 overlay, so the base image is never
# modified and "reset to pristine" is `down.sh --reset` - a delete and a
# re-create, not a reinstall. Test disks are raw files attached with stable
# serials, so a profile can address them as /dev/disk/by-id/virtio-labdiskN
# regardless of enumeration order.
#
# The NIC's MAC is pinned and the image names the interface after it
# (enx<mac>), so the name cannot move when test disks are added. Devices also
# sit at fixed PCI slots - system disk 0x10, NIC 0x11, test disks 0x12 upward -
# which keeps the system disk at vda and gives the MAC pinning a second line of
# defence. The default naming scheme derives the name from PCI geography, so
# without this, attaching a test disk renames the NIC, vmbr0 ends up bridging a
# port that no longer exists, and the node boots to a login prompt that is
# completely unreachable. bootindex=0 likewise stops SeaBIOS guessing.
#
# Networking is user-mode with port forwards only: no bridge, no tap device,
# and therefore no elevated privileges beyond /dev/kvm. That is what lets the
# whole lab run inside an unprivileged container.
#
# With --nodes 2 the lab becomes a PVE cluster, which is what migration needs.
# The second NIC each node gets is a QEMU socket netdev - a raw L2 link between
# QEMU processes, entirely in userspace. No tap, no bridge, so no /dev/net/tun
# and no NET_ADMIN: /dev/kvm stays the only elevated thing the runner has.
#
# Both nodes keep the same management MAC and address. Each node's user-mode
# network is its own isolated segment, so there is nothing to collide - which
# means the image needs no per-node variation for management at all. Only the
# cluster NIC differs.
#
#   up.sh [--name lab] [--nodes 2] [--disks 4] [--disk-size 8G] [--mem 6144]
#         [--cpus 4] [--reuse]

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

NAME="${LAB_NAME:-lab}"
NODES="${LAB_NODES:-1}"
PVE_VERSION="${PVE_VERSION:-9.2-1}"
# A profile with expensive setup bakes it into a derived image; boot that
# instead of the base so the cost is paid once rather than every lab.
IMAGE_VARIANT="${IMAGE_VARIANT:-base}"
DISKS="${LAB_DISKS:-4}"
DISK_SIZE="${LAB_DISK_SIZE:-8G}"
MEM="${LAB_MEM:-6144}"
CPUS="${LAB_CPUS:-4}"
SSH_PORT="${LAB_SSH_PORT:-25522}"
GUI_PORT="${LAB_GUI_PORT:-28006}"
# Must match the MAC baked into the image: the node names its NIC after it.
LAB_MAC="${LAB_MAC:-52:54:00:1a:b0:01}"
# Must match the static address baked into the image. Guests get DHCP from
# .20 upward so they can never take the address the forwards point at.
LAB_NODE_IP="${LAB_NODE_IP:-10.0.2.10}"
# The cluster link. Multicast rather than a point-to-point pair so the same
# invocation works for any node count.
CLUSTER_MCAST="${LAB_CLUSTER_MCAST:-230.0.0.1:24000}"
CLUSTER_NET="${LAB_CLUSTER_NET:-10.9.9}"
FRESH=0
REUSE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)      NAME="$2";      shift 2 ;;
        --nodes)     NODES="$2";     shift 2 ;;
        --disks)     DISKS="$2";     shift 2 ;;
        --disk-size) DISK_SIZE="$2"; shift 2 ;;
        --mem)       MEM="$2";       shift 2 ;;
        --cpus)      CPUS="$2";      shift 2 ;;
        --ssh-port)  SSH_PORT="$2";  shift 2 ;;
        --fresh)     FRESH=1;        shift ;;
        --reuse)     REUSE=1;        shift ;;
        --variant)   IMAGE_VARIANT="$2"; shift 2 ;;
        -h|--help)   sed -n '2,17p' "$0"; exit 0 ;;
        *)           die "unknown argument: $1" ;;
    esac
done

require_cmd qemu-system-x86_64 qemu-img ssh

STATE="$(lab_state_dir)"
IMAGE="$STATE/images/pve-${PVE_VERSION}-${IMAGE_VARIANT}.qcow2"
SSH_KEY="$STATE/id_ed25519"
LAB="$STATE/labs/$NAME"

[[ -f "$IMAGE" ]] || die "base image missing: $IMAGE (run lab/build-image.sh first)"

running_nodes=0
for ((i = 1; i <= NODES; i++)); do
    pid="$LAB/node$i/qemu.pid"
    [[ -f "$pid" ]] && kill -0 "$(cat "$pid")" 2>/dev/null && running_nodes=$((running_nodes + 1))
done

if [[ $running_nodes -gt 0 ]]; then
    if [[ $FRESH -eq 0 ]]; then
        log_info "lab '$NAME' already running ($running_nodes node(s))"
        cat "$LAB/lab.env"
        exit 0
    fi
    log_info "lab '$NAME' running; tearing it down first (--fresh)"
    "$(lab_root)/lab/down.sh" --name "$NAME" --reset
fi

[[ $FRESH -eq 1 ]] && rm -rf "$LAB"
mkdir -p "$LAB"

mapfile -t SSH_OPTS < <(node_ssh_opts)

for ((i = 1; i <= NODES; i++)); do
    ND="$LAB/node$i"
    mkdir -p "$ND"

    # Starting a lab that is not currently running discards the previous one's
    # overlay and disks unless --reuse is given. They are stale in two ways:
    # they hold a previous run's filesystems, and they accumulate - test disks
    # fill as tests write to them, so a handful of runs quietly consumed 21 GB
    # of the runner before this existed. A lab is meant to be throwaway.
    if [[ $REUSE -eq 0 ]]; then
        rm -f "$ND"/system.qcow2 "$ND"/disk*.raw
    fi

    if [[ ! -f "$ND/system.qcow2" ]]; then
        log_info "node$i: creating overlay on $(basename "$IMAGE")"
        qemu-img create -f qcow2 -b "$IMAGE" -F qcow2 "$ND/system.qcow2" >/dev/null
    fi

    disk_args=()
    for d in $(seq 1 "$DISKS"); do
        f="$ND/disk${d}.raw"
        [[ -f "$f" ]] || qemu-img create -f raw "$f" "$DISK_SIZE" >/dev/null
        disk_args+=(
            -drive "file=$f,if=none,id=d$d,format=raw,cache=unsafe"
            -device "virtio-blk-pci,drive=d$d,serial=labdisk$d,addr=$(printf '0x%x' $((0x12 + d)))"
        )
    done

    # A second NIC only when there is a cluster to form. Its slot is fixed at
    # 0x12 and the test disks start at 0x13 either way, so the device layout
    # does not change between one node and several.
    cluster_args=()
    if [[ $NODES -gt 1 ]]; then
        cluster_args=(
            -netdev "socket,id=clus,mcast=$CLUSTER_MCAST"
            -device "virtio-net-pci,netdev=clus,addr=0x12,mac=52:54:00:1a:c1:0$i"
        )
    fi

    ssh_port=$((SSH_PORT + (i - 1) * 10))
    gui_port=$((GUI_PORT + (i - 1) * 10))

    log_info "node$i: booting ($CPUS cpus, ${MEM}M, $DISKS x $DISK_SIZE test disks)"
    qemu-system-x86_64 \
        -enable-kvm -cpu host -machine q35 \
        -smp "$CPUS" -m "$MEM" \
        -drive file="$ND/system.qcow2",if=none,id=sys,format=qcow2 \
        -device virtio-blk-pci,drive=sys,serial=labsystem,addr=0x10,bootindex=0 \
        -netdev user,id=n0,net=10.0.2.0/24,host=10.0.2.2,dhcpstart=10.0.2.20,hostfwd=tcp:127.0.0.1:"$ssh_port"-"$LAB_NODE_IP":22,hostfwd=tcp:127.0.0.1:"$gui_port"-"$LAB_NODE_IP":8006 \
        -device virtio-net-pci,netdev=n0,addr=0x11,mac="$LAB_MAC" \
        "${cluster_args[@]}" \
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

{
    echo "LAB_NAME=$NAME"
    echo "LAB_DIR=$LAB"
    echo "LAB_NODE_COUNT=$NODES"
    # NODE_SSH_PORT is node 1, so everything written for a single-node lab
    # keeps working unchanged.
    echo "NODE_SSH_PORT=$SSH_PORT"
    echo "NODE_GUI_PORT=$GUI_PORT"
    echo "NODE_SSH_KEY=$SSH_KEY"
    for ((i = 1; i <= NODES; i++)); do
        echo "NODE${i}_SSH_PORT=$((SSH_PORT + (i - 1) * 10))"
        echo "NODE${i}_GUI_PORT=$((GUI_PORT + (i - 1) * 10))"
        echo "NODE${i}_CLUSTER_IP=${CLUSTER_NET}.$i"
    done
    echo "LAB_DISKS=$DISKS"
    echo "LAB_DISK_SIZE=$DISK_SIZE"
    echo "LAB_CLUSTER_NET=$CLUSTER_NET"
    echo "PVE_VERSION=$PVE_VERSION"
    echo "IMAGE_VARIANT=$IMAGE_VARIANT"
} > "$LAB/lab.env"

if [[ $NODES -gt 1 ]]; then
    log_info "forming a cluster across $NODES nodes"
    "$(lab_root)/lab/cluster.sh" --name "$NAME"
fi

log_info "lab '$NAME' is up"
cat "$LAB/lab.env"
