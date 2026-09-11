#!/usr/bin/env bash
# Build a derived image with a profile's expensive setup already applied.
#
# Some backends need real work before they can be tested at all - a DKMS
# module built against the running kernel, packages from a third-party
# repository. That is minutes per run, and because every lab boots a throwaway
# overlay, "per run" means every single time.
#
# So a profile may split its setup in two:
#
#   bake.sh    once, into a derived image: install packages, build modules,
#              add repositories - anything that belongs to the machine
#   setup.sh   every lab: format the test disks, register the storage -
#              anything that belongs to the disks, which are recreated each
#              time a lab comes up
#
# The result is a standalone image, not an overlay, so it can be published and
# pulled the same way the base image is.
#
#   bake-profile.sh --profile-dir DIR [--force]

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

PVE_VERSION="${PVE_VERSION:-9.2-1}"
PROFILE_DIR=""
BASE_VARIANT="base"
OUT_VARIANT=""
FORCE=0
MEM="${BAKE_MEM:-6144}"
CPUS="${BAKE_CPUS:-4}"
SSH_PORT="${BAKE_SSH_PORT:-25523}"
NODE_IP="${LAB_NODE_IP:-10.0.2.10}"
MAC="${LAB_MAC:-52:54:00:1a:b0:01}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile-dir) PROFILE_DIR="$2";   shift 2 ;;
        --base)        BASE_VARIANT="$2"; shift 2 ;;
        --variant)     OUT_VARIANT="$2";  shift 2 ;;
        --force)       FORCE=1;            shift ;;
        -h|--help)     sed -n '2,20p' "$0"; exit 0 ;;
        *)             die "unknown argument: $1" ;;
    esac
done

[[ -n "$PROFILE_DIR" ]] || die "--profile-dir is required"
PROFILE_DIR="$(cd "$PROFILE_DIR" && pwd)"
source "$(dirname "${BASH_SOURCE[0]}")/../lib/profile.sh"
PROFILE="$(profile_name "$PROFILE_DIR")"
[[ -n "$OUT_VARIANT" ]] || OUT_VARIANT="$PROFILE"

STATE="$(lab_state_dir)"
# Bakes chain: a second profile with expensive setup builds on the first one's
# result rather than on the base, so a lab with two such profiles pays for each
# once instead of on every run.
BASE="$STATE/images/pve-${PVE_VERSION}-${BASE_VARIANT}.qcow2"
DERIVED="$STATE/images/pve-${PVE_VERSION}-${OUT_VARIANT}.qcow2"
SSH_KEY="$STATE/id_ed25519"
WORK="$STATE/bake/$OUT_VARIANT"

[[ -f "$BASE" ]] || die "image missing: $BASE (run lab/build-image.sh first)"

if [[ ! -f "$PROFILE_DIR/bake.sh" ]]; then
    log_info "profile '$PROFILE' has no bake.sh; nothing to pre-build"
    exit 0
fi
if [[ -f "$DERIVED" && $FORCE -eq 0 ]]; then
    log_info "derived image already built: $DERIVED (use --force to rebuild)"
    exit 0
fi

mkdir -p "$WORK"
rm -f "$WORK/node.qcow2"
qemu-img create -f qcow2 -b "$BASE" -F qcow2 "$WORK/node.qcow2" >/dev/null

log_info "baking profile '$PROFILE' onto ${BASE_VARIANT}"
qemu-system-x86_64 \
    -enable-kvm -cpu host -machine q35 -smp "$CPUS" -m "$MEM" \
    -drive file="$WORK/node.qcow2",if=none,id=sys,format=qcow2,cache=unsafe \
    -device virtio-blk-pci,drive=sys,addr=0x10,bootindex=0 \
    -netdev user,id=n0,net=10.0.2.0/24,host=10.0.2.2,dhcpstart=10.0.2.20,hostfwd=tcp:127.0.0.1:"$SSH_PORT"-"$NODE_IP":22 \
    -device virtio-net-pci,netdev=n0,addr=0x11,mac="$MAC" \
    -display none -serial file:"$WORK/console.log" \
    -pidfile "$WORK/qemu.pid" -daemonize

cleanup() {
    [[ -f "$WORK/qemu.pid" ]] && kill "$(cat "$WORK/qemu.pid")" 2>/dev/null || true
}
trap cleanup EXIT

mapfile -t SSH_OPTS < <(node_ssh_opts)
# -n so ssh never reads this script's stdin; the heredoc call is the exception.
bake_ssh()       { ssh -n "${SSH_OPTS[@]}" -i "$SSH_KEY" -p "$SSH_PORT" root@127.0.0.1 "$@"; }
bake_ssh_stdin() { ssh    "${SSH_OPTS[@]}" -i "$SSH_KEY" -p "$SSH_PORT" root@127.0.0.1 "$@"; }

log_info "waiting for SSH"
up=0
for _ in $(seq 1 120); do
    bake_ssh true 2>/dev/null && { up=1; break; }
    sleep 5
done
[[ $up -eq 1 ]] || die "node did not come up - see $WORK/console.log"

log_info "running $PROFILE/bake.sh on the node"
tar cz -C "$PROFILE_DIR" . | bake_ssh_stdin "mkdir -p /root/lab-profile && tar xz -C /root/lab-profile"
# Pushed as a file and executed, not piped to `bash -s`: a script read from
# stdin shares it with everything it runs, and one stdin-reading command then
# swallows the rest of the script.
STEP="$(mktemp)"
cat > "$STEP" <<'REMOTE'
set -e
rm -f /root/.lab-bake-ready
chmod +x /root/lab-profile/*.sh 2>/dev/null || true
bash /root/lab-profile/bake.sh </dev/null
touch /root/.lab-bake-ready
REMOTE
ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" -p "$SSH_PORT" root@127.0.0.1 \
    "cat > /root/.lab-bake-step.sh" < "$STEP"
rm -f "$STEP"
bake_ssh "bash /root/.lab-bake-step.sh" || true
# Exit status through ssh has proved unreliable when a remote step consumes
# stdin, so assert on the artefact instead.
bake_ssh "test -f /root/.lab-bake-ready" || die "profile bake did not complete"

bake_ssh "fstrim -av" >/dev/null 2>&1 || true
log_info "shutting down"
bake_ssh "systemctl poweroff" 2>/dev/null || true
for _ in $(seq 1 90); do
    [[ -f "$WORK/qemu.pid" ]] && kill -0 "$(cat "$WORK/qemu.pid")" 2>/dev/null || break
    sleep 2
done
trap - EXIT
cleanup

log_info "flattening into a standalone image"
qemu-img convert -O qcow2 -c "$WORK/node.qcow2" "$DERIVED.tmp"
mv "$DERIVED.tmp" "$DERIVED"
rm -f "$WORK/node.qcow2"

log_info "built $DERIVED ($(du -h "$DERIVED" | cut -f1))"
