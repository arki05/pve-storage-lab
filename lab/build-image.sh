#!/usr/bin/env bash
# Build a bootable Proxmox VE node image for the lab.
#
# Runs Proxmox's own automated installer under QEMU, then boots the result once
# to do the post-install fixups every fresh PVE install needs (repositories,
# updates). The output is a base image that is never written to again: every
# lab run boots a throwaway qcow2 overlay on top of it, so "reset the node"
# costs an `rm` rather than a reinstall.
#
# Only needs /dev/kvm. No Proxmox host, no cluster, no credentials.
#
#   build-image.sh [--pve-version 9.2-1] [--force] [--vnc :1]

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

PVE_VERSION="${PVE_VERSION:-9.2-1}"
DISK_SIZE="${DISK_SIZE:-40G}"
BUILD_MEM="${BUILD_MEM:-4096}"
BUILD_CPUS="${BUILD_CPUS:-4}"
SSH_PORT="${SSH_PORT:-25522}"
# Pinned so the interface name can be derived from it (see LAB_IFNAME).
LAB_MAC="52:54:00:1a:b0:01"
LAB_IFNAME="enx5254001ab001"
# Baked into the image; the suite clones from it rather than building its own.
GUEST_TEMPLATE_VMID="${GUEST_TEMPLATE_VMID:-900}"
ROOT_PASSWORD="${ROOT_PASSWORD:-pvelab123}"
FORCE=0
VNC=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pve-version) PVE_VERSION="$2"; shift 2 ;;
        --disk-size)   DISK_SIZE="$2";   shift 2 ;;
        --force)       FORCE=1;          shift ;;
        --vnc)         VNC="$2";         shift 2 ;;
        -h|--help)     sed -n '2,14p' "$0"; exit 0 ;;
        *)             die "unknown argument: $1" ;;
    esac
done

require_cmd qemu-system-x86_64 qemu-img curl ssh ssh-keygen proxmox-auto-install-assistant

STATE="$(lab_state_dir)"
CACHE="$STATE/cache"
IMAGES="$STATE/images"
WORK="$STATE/build"
mkdir -p "$CACHE" "$IMAGES" "$WORK"

ISO_NAME="proxmox-ve_${PVE_VERSION}.iso"
ISO="$CACHE/$ISO_NAME"
IMAGE="$IMAGES/pve-${PVE_VERSION}-base.qcow2"
SSH_KEY="$STATE/id_ed25519"

if [[ -f "$IMAGE" && $FORCE -eq 0 ]]; then
    log_info "image already built: $IMAGE (use --force to rebuild)"
    exit 0
fi

# ── SSH key ──────────────────────────────────────────────────────────────────
# Baked into the image by the installer, so the node is reachable the moment it
# boots — no cloud-init, no password auth, no guest agent.

if [[ ! -f "$SSH_KEY" ]]; then
    log_info "generating lab SSH key"
    ssh-keygen -t ed25519 -N '' -C 'pve-storage-lab' -f "$SSH_KEY" >/dev/null
fi

# ── ISO ──────────────────────────────────────────────────────────────────────

if [[ ! -f "$ISO" ]]; then
    log_info "downloading $ISO_NAME (~1.5 GB)"
    curl -fSL --progress-bar -o "$ISO.part" \
        "http://download.proxmox.com/iso/$ISO_NAME"
    mv "$ISO.part" "$ISO"
fi

log_info "verifying ISO checksum"
curl -fsSL -o "$CACHE/SHA256SUMS" http://download.proxmox.com/iso/SHA256SUMS
expected=$(awk -v n="$ISO_NAME" '$2 == n || $2 == "*"n {print $1}' "$CACHE/SHA256SUMS" | head -1)
[[ -n "$expected" ]] || die "no checksum published for $ISO_NAME"
actual=$(sha256sum "$ISO" | awk '{print $1}')
[[ "$expected" == "$actual" ]] || die "ISO checksum mismatch: expected $expected, got $actual"
log_info "checksum OK"

# ── Answer file ──────────────────────────────────────────────────────────────

log_info "rendering answer file"
sed -e "s|@FQDN@|pve-lab.local|" \
    -e "s|@ROOT_PASSWORD@|${ROOT_PASSWORD}|" \
    -e "s|@ROOT_SSH_KEY@|$(cat "${SSH_KEY}.pub")|" \
    "$(lab_root)/lab/answer.toml.tpl" > "$WORK/answer.toml"

proxmox-auto-install-assistant validate-answer "$WORK/answer.toml" \
    || die "answer file rejected by the installer tooling"

log_info "preparing auto-install ISO"
rm -f "$WORK/auto.iso"
proxmox-auto-install-assistant prepare-iso "$ISO" \
    --fetch-from iso \
    --answer-file "$WORK/answer.toml" \
    --output "$WORK/auto.iso"

# ── Install ──────────────────────────────────────────────────────────────────

log_info "creating ${DISK_SIZE} system disk"
rm -f "$WORK/node.qcow2"
qemu-img create -f qcow2 "$WORK/node.qcow2" "$DISK_SIZE" >/dev/null

log_info "running the Proxmox installer (10-20 min)"
# -no-reboot turns the installer's final reboot into a clean QEMU exit, which
# is how we know it finished. cache=unsafe is safe here specifically because a
# failed build is thrown away and restarted, never resumed.
qemu_display=(-display none)
[[ -n "$VNC" ]] && qemu_display=(-vnc "$VNC")

timeout 3600 qemu-system-x86_64 \
    -enable-kvm -cpu host -machine q35 \
    -smp "$BUILD_CPUS" -m "$BUILD_MEM" \
    -drive file="$WORK/node.qcow2",if=none,id=sys,format=qcow2,cache=unsafe \
    -device virtio-blk-pci,drive=sys,addr=0x10,bootindex=0 \
    -cdrom "$WORK/auto.iso" -boot d \
    -netdev user,id=n0 -device virtio-net-pci,netdev=n0,addr=0x11,mac="$LAB_MAC" \
    "${qemu_display[@]}" \
    -serial file:"$WORK/install.log" \
    -no-reboot \
    || die "installer failed or timed out - see $WORK/install.log"

log_info "installer finished"

# ── First boot: post-install fixups ──────────────────────────────────────────

log_info "booting the installed system for post-install setup"
qemu-system-x86_64 \
    -enable-kvm -cpu host -machine q35 \
    -smp "$BUILD_CPUS" -m "$BUILD_MEM" \
    -drive file="$WORK/node.qcow2",if=none,id=sys,format=qcow2,cache=unsafe \
    -device virtio-blk-pci,drive=sys,addr=0x10,bootindex=0 \
    -netdev user,id=n0,hostfwd=tcp:127.0.0.1:"$SSH_PORT"-:22 \
    -device virtio-net-pci,netdev=n0,addr=0x11,mac="$LAB_MAC" \
    -display none -serial file:"$WORK/firstboot.log" \
    -pidfile "$WORK/qemu.pid" -daemonize

cleanup() {
    local pid
    [[ -f "$WORK/qemu.pid" ]] && pid=$(cat "$WORK/qemu.pid") && kill "$pid" 2>/dev/null || true
}
trap cleanup EXIT

mapfile -t SSH_OPTS < <(node_ssh_opts)
node_ssh() { ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" -p "$SSH_PORT" root@127.0.0.1 "$@"; }

log_info "waiting for SSH"
for _ in $(seq 1 120); do
    node_ssh true 2>/dev/null && break
    sleep 5
done
node_ssh true 2>/dev/null || die "node never came up - see $WORK/firstboot.log"
log_info "node is up"

node_ssh "bash -s $LAB_IFNAME $LAB_MAC" <<'REMOTE'
set -e
IFNAME="$1"; MAC="$2"
[ -n "$IFNAME" ] && [ -n "$MAC" ] || { echo "IFNAME/MAC not passed through" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

# Debian's apt-daily.timer fires shortly after boot, so a freshly installed
# node is usually already holding the apt lock by the time sshd answers.
# Disable it outright rather than racing it: a lab node running background
# package jobs mid-test is its own kind of flake.
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl stop apt-daily.service apt-daily-upgrade.service 2>/dev/null || true
for _ in $(seq 1 120); do
    fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || break
    sleep 5
done
# A fresh install points at the enterprise repo, which 401s without a
# subscription and makes every later apt call fail.
rm -f /etc/apt/sources.list.d/pve-enterprise.sources /etc/apt/sources.list.d/pve-enterprise.list
rm -f /etc/apt/sources.list.d/ceph.sources /etc/apt/sources.list.d/ceph.list
cat > /etc/apt/sources.list.d/pve-no-subscription.sources <<'EOF'
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF
APT="apt-get -o DPkg::Lock::Timeout=600"
$APT update -qq
$APT -y -qq dist-upgrade
# Handy inside the node for every profile and test run.
$APT install -y -qq --no-install-recommends python3-pytest python3-requests jq fio
$APT clean

# Without a serial console a node that fails to boot is completely silent -
# lab/up.sh writes console.log and it stays empty. Costs nothing, saves an
# afternoon.
sed -i 's|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT="quiet console=tty0 console=ttyS0,115200"|' /etc/default/grub
grep -q GRUB_TERMINAL /etc/default/grub || printf 'GRUB_TERMINAL="console serial"\nGRUB_SERIAL_COMMAND="serial --speed=115200"\n' >> /etc/default/grub
update-grub

# The installer freezes the DHCP address it saw into a static stanza. Put the
# bridge back on DHCP so a node still works if the lab network ever differs.
sed -i 's|^iface vmbr0 inet static|iface vmbr0 inet dhcp|' /etc/network/interfaces
sed -i '/^\s*address 10\./d; /^\s*gateway 10\./d' /etc/network/interfaces

# Name the NIC after its (pinned) MAC instead of its PCI slot. The default
# NamePolicy ends in `path`, which encodes PCI bus/slot geography - so simply
# attaching another test disk renamed the NIC, left vmbr0 bridging a port that
# no longer existed, and the node booted to a login prompt with an address in
# its banner and no reachable network. `mac` is immune to PCI topology.
cat > /etc/systemd/network/10-lab-net.link <<EOF
[Match]
MACAddress=$MAC

[Link]
NamePolicy=mac
EOF
# The rename happens in early userspace, so the rule has to be in the initramfs.
update-initramfs -u
sed -i "s|^\(\s*bridge-ports\).*|\1 $IFNAME|" /etc/network/interfaces
sed -i "s|^iface \(enp\|ens\|eno\|eth\)[^ ]* inet manual|iface $IFNAME inet manual|" /etc/network/interfaces

# Zero the free space so the qcow2 compacts well - the image gets shipped.
fstrim -av || true
REMOTE

# ── Bake in the guest templates ──────────────────────────────────────────────
#
# A lab boots on a throwaway overlay, so anything created per-run is created
# every run. Downloading a cloud image and booting it to install the guest
# agent costs ~5 minutes; doing it once here makes it free. Both templates live
# on node-local storage, never on the storage under test - tests full-clone
# from them, which still exercises the target backend.

log_info "baking in the LXC template"
node_ssh 'bash -s' <<'REMOTE'
set -e
pveam update >/dev/null
tmpl=$(pveam available --section system | awk '{print $2}' | grep '^debian-13-standard' | sort -V | tail -1)
[ -n "$tmpl" ] || { echo "no debian-13 LXC template offered" >&2; exit 1; }
pveam download local "$tmpl" >/dev/null
pveam list local
REMOTE

log_info "baking in the VM template (downloads a cloud image and boots it once)"
node_ssh "bash -s $GUEST_TEMPLATE_VMID" <<'REMOTE'
set -e
VMID="$1"
export DEBIAN_FRONTEND=noninteractive
IMG=/var/lib/vz/template/debian-13-genericcloud-amd64.qcow2
[ -f "$IMG" ] || curl -fsSL -o "$IMG" \
    https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2

mkdir -p /var/lib/vz/snippets
cat > /var/lib/vz/snippets/guest-bake.yaml <<'YAML'
#cloud-config
ssh_pwauth: true
package_update: true
packages:
  - qemu-guest-agent
  - fio
runcmd:
  - systemctl enable --now qemu-guest-agent
  - systemctl enable qemu-guest-agent
  - poweroff
YAML

# local-lvm, not the storage under test: tests clone onto their own storage.
qm create "$VMID" --name lab-guest-template --ostype l26 --cpu host \
    --cores 2 --memory 1024 --scsihw virtio-scsi-single --agent enabled=1 \
    --serial0 socket --net0 virtio,bridge=vmbr0
qm set "$VMID" --scsi0 "local-lvm:0,import-from=$IMG,discard=on"
qm set "$VMID" --ide2 local-lvm:cloudinit
qm set "$VMID" --ciuser root --cipassword pvelab --ipconfig0 ip=dhcp \
    --cicustom user=local:snippets/guest-bake.yaml --boot order=scsi0
qm start "$VMID"

# cloud-init powers the guest off when it is done, which is the signal.
for _ in $(seq 1 180); do
    qm status "$VMID" | grep -q stopped && break
    sleep 5
done
qm status "$VMID" | grep -q stopped || { echo "guest template never finished" >&2; exit 1; }

# cloud-init runcmd runs once per instance-id and clones get fresh ones, so a
# clone would re-run the install and promptly power itself off. Strip it.
qm set "$VMID" --delete cicustom
qm set "$VMID" --delete ide2
qm template "$VMID"
qm config "$VMID" | grep -E 'template|scsi0'
REMOTE

# The rename only takes effect on the next boot, so verify it here rather than
# discovering it in every lab that uses the image.
log_info "rebooting to verify the interface rename"
node_ssh 'systemctl reboot' 2>/dev/null || true
sleep 20
up=0
for _ in $(seq 1 60); do
    node_ssh true 2>/dev/null && { up=1; break; }
    sleep 5
done
[[ $up -eq 1 ]] || die "node unreachable after the interface rename - see $WORK/firstboot.log"

got=$(node_ssh "ip -br link show $LAB_IFNAME >/dev/null 2>&1 && echo ok || echo missing")
[[ "$got" == "ok" ]] || die "interface $LAB_IFNAME did not appear after reboot"
node_ssh "ip -br a show vmbr0 | grep -q 'UP'" || die "vmbr0 is not up after the rename"
log_info "verified: $LAB_IFNAME present, vmbr0 up"

log_info "shutting down"
node_ssh 'systemctl poweroff' 2>/dev/null || true
for _ in $(seq 1 60); do
    [[ -f "$WORK/qemu.pid" ]] && kill -0 "$(cat "$WORK/qemu.pid")" 2>/dev/null || break
    sleep 2
done
trap - EXIT
cleanup

# ── Publish ──────────────────────────────────────────────────────────────────

log_info "compacting image"
qemu-img convert -O qcow2 -c "$WORK/node.qcow2" "$IMAGE.tmp"
mv "$IMAGE.tmp" "$IMAGE"
rm -f "$WORK/node.qcow2"

log_info "built $IMAGE ($(du -h "$IMAGE" | cut -f1))"
