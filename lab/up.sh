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
#   up.sh [--name lab] [--disks 4] [--disk-size 8G] [--mem 6144] [--cpus 4]

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

NAME="${LAB_NAME:-lab}"
PVE_VERSION="${PVE_VERSION:-9.2-1}"
DISKS="${LAB_DISKS:-4}"
DISK_SIZE="${LAB_DISK_SIZE:-8G}"
MEM="${LAB_MEM:-6144}"
CPUS="${LAB_CPUS:-4}"
SSH_PORT="${LAB_SSH_PORT:-25522}"
GUI_PORT="${LAB_GUI_PORT:-28006}"
# Must match the MAC baked into the image: the node names its NIC after it.
LAB_MAC="${LAB_MAC:-52:54:00:1a:b0:01}"
FRESH=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)      NAME="$2";      shift 2 ;;
        --disks)     DISKS="$2";     shift 2 ;;
        --disk-size) DISK_SIZE="$2"; shift 2 ;;
        --mem)       MEM="$2";       shift 2 ;;
        --cpus)      CPUS="$2";      shift 2 ;;
        --ssh-port)  SSH_PORT="$2";  shift 2 ;;
        --fresh)     FRESH=1;        shift ;;
        -h|--help)   sed -n '2,17p' "$0"; exit 0 ;;
        *)           die "unknown argument: $1" ;;
    esac
done

require_cmd qemu-system-x86_64 qemu-img ssh

STATE="$(lab_state_dir)"
IMAGE="$STATE/images/pve-${PVE_VERSION}-base.qcow2"
SSH_KEY="$STATE/id_ed25519"
LAB="$STATE/labs/$NAME"

[[ -f "$IMAGE" ]] || die "base image missing: $IMAGE (run lab/build-image.sh first)"

if [[ -f "$LAB/qemu.pid" ]] && kill -0 "$(cat "$LAB/qemu.pid")" 2>/dev/null; then
    if [[ $FRESH -eq 0 ]]; then
        log_info "lab '$NAME' already running"
        cat "$LAB/lab.env"
        exit 0
    fi
    log_info "lab '$NAME' running; tearing it down first (--fresh)"
    "$(lab_root)/lab/down.sh" --name "$NAME" --reset
fi

[[ $FRESH -eq 1 ]] && rm -rf "$LAB"
mkdir -p "$LAB"

# ── Disks ────────────────────────────────────────────────────────────────────

if [[ ! -f "$LAB/system.qcow2" ]]; then
    log_info "creating overlay on $(basename "$IMAGE")"
    qemu-img create -f qcow2 -b "$IMAGE" -F qcow2 "$LAB/system.qcow2" >/dev/null
fi

disk_args=()
for i in $(seq 1 "$DISKS"); do
    f="$LAB/disk${i}.raw"
    [[ -f "$f" ]] || qemu-img create -f raw "$f" "$DISK_SIZE" >/dev/null
    disk_args+=(
        -drive "file=$f,if=none,id=d$i,format=raw,cache=unsafe"
        -device "virtio-blk-pci,drive=d$i,serial=labdisk$i,addr=$(printf '0x%x' $((0x11 + i)))"
    )
done

# ── Boot ─────────────────────────────────────────────────────────────────────

log_info "booting node ($CPUS cpus, ${MEM}M, $DISKS x $DISK_SIZE test disks)"
qemu-system-x86_64 \
    -enable-kvm -cpu host -machine q35 \
    -smp "$CPUS" -m "$MEM" \
    -drive file="$LAB/system.qcow2",if=none,id=sys,format=qcow2 \
    -device virtio-blk-pci,drive=sys,serial=labsystem,addr=0x10,bootindex=0 \
    "${disk_args[@]}" \
    -netdev user,id=n0,hostfwd=tcp:127.0.0.1:"$SSH_PORT"-:22,hostfwd=tcp:127.0.0.1:"$GUI_PORT"-:8006 \
    -device virtio-net-pci,netdev=n0,addr=0x11,mac="$LAB_MAC" \
    -display none -serial file:"$LAB/console.log" \
    -pidfile "$LAB/qemu.pid" -daemonize

mapfile -t SSH_OPTS < <(node_ssh_opts)
log_info "waiting for SSH on 127.0.0.1:$SSH_PORT"
up=0
for _ in $(seq 1 120); do
    if ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" -p "$SSH_PORT" root@127.0.0.1 true 2>/dev/null; then
        up=1; break
    fi
    sleep 5
done
[[ $up -eq 1 ]] || die "node did not come up - see $LAB/console.log"

cat > "$LAB/lab.env" <<EOF
LAB_NAME=$NAME
LAB_DIR=$LAB
NODE_SSH_PORT=$SSH_PORT
NODE_GUI_PORT=$GUI_PORT
NODE_SSH_KEY=$SSH_KEY
LAB_DISKS=$DISKS
LAB_DISK_SIZE=$DISK_SIZE
PVE_VERSION=$PVE_VERSION
EOF

log_info "lab '$NAME' is up"
cat "$LAB/lab.env"
