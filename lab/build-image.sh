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
# Three roles, three NICs, each with a pinned MAC so the interface name is
# derived from it rather than from a PCI slot.
#
#   mgmt     what the host forwards to, and the node's own address
#   guest    the bridge guests attach to - vmbr0, with no host address
#   cluster  added later by cluster.sh, only when there is a cluster
#
# Keeping the node's address off the guest bridge is the point. When they share
# a segment a guest can take the node's DHCP lease, and two nodes cannot have
# distinct addresses without re-addressing them after boot. Separating them
# makes both problems impossible rather than guarded against.
LAB_MAC="52:54:00:1a:b0:01"
LAB_IFNAME="enx5254001ab001"
LAB_GUEST_MAC="52:54:00:1a:b1:01"
LAB_GUEST_IFNAME="enx5254001ab101"
# The node takes a fixed address below SLIRP's DHCP range, and guests are
# pushed above it. SLIRP hands out 10.0.2.15 first and hostfwd targets that
# address by default - so the moment a test guest bridged onto vmbr0 it took
# the lease, the node came back on .16 after a reboot, and every port forward
# pointed at an address nobody held.
LAB_NODE_IP="10.0.2.10"
LAB_NET="10.0.2.0/24"
LAB_GW="10.0.2.2"
LAB_DNS="10.0.2.3"
LAB_DHCP_START="10.0.2.20"
# The guest bridge lives on its own network on every node. Nothing routes
# between nodes here - the cluster link does that - so the same numbers on
# each node are correct rather than merely convenient.
LAB_GUEST_NET="10.0.99.0/24"
LAB_GUEST_GW="10.0.99.2"
LAB_GUEST_DHCP="10.0.99.20"
# Only the base image holds this; node images do not.
LAB_GUEST_HOST_IP="10.0.99.1"
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
    -e "s|@NODE_CIDR@|${LAB_NODE_IP}/24|" \
    -e "s|@GATEWAY@|${LAB_GW}|" \
    -e "s|@DNS@|${LAB_DNS}|" \
    -e "s|@IFNAME@|${LAB_IFNAME}|" \
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
# The installer takes its address by DHCP and freezes it into a static stanza,
# so it has to be handed LAB_NODE_IP here - hand it anything else and the node
# is unreachable on first boot, before there is any chance to correct it.
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
    -netdev user,id=n0,net="$LAB_NET",host="$LAB_GW",dhcpstart="$LAB_NODE_IP" -device virtio-net-pci,netdev=n0,addr=0x11,mac="$LAB_MAC" \
    -netdev user,id=n1,net="$LAB_GUEST_NET",host="$LAB_GUEST_GW" -device virtio-net-pci,netdev=n1,addr=0x12,mac="$LAB_GUEST_MAC" \
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
    -netdev user,id=n0,net="$LAB_NET",host="$LAB_GW",dhcpstart="$LAB_NODE_IP",hostfwd=tcp:127.0.0.1:"$SSH_PORT"-"$LAB_NODE_IP":22 \
    -device virtio-net-pci,netdev=n0,addr=0x11,mac="$LAB_MAC" \
    -netdev user,id=n1,net="$LAB_GUEST_NET",host="$LAB_GUEST_GW" \
    -device virtio-net-pci,netdev=n1,addr=0x12,mac="$LAB_GUEST_MAC" \
    -display none -serial file:"$WORK/firstboot.log" \
    -pidfile "$WORK/qemu.pid" -daemonize

cleanup() {
    local pid
    [[ -f "$WORK/qemu.pid" ]] && pid=$(cat "$WORK/qemu.pid") && kill "$pid" 2>/dev/null || true
}
trap cleanup EXIT

mapfile -t SSH_OPTS < <(node_ssh_opts)
# -n matters: without it ssh reads the caller's stdin, and when the caller is
# itself a script being piped into `bash -s`, ssh eats the rest of the script.
# Bash then simply runs out of input and exits 0, so the step "succeeds"
# having done nothing. node_ssh_stdin is the deliberate exception, for
# heredocs.
node_ssh()       { ssh -n "${SSH_OPTS[@]}" -i "$SSH_KEY" -p "$SSH_PORT" root@127.0.0.1 "$@"; }
node_ssh_stdin() { ssh    "${SSH_OPTS[@]}" -i "$SSH_KEY" -p "$SSH_PORT" root@127.0.0.1 "$@"; }

log_info "waiting for SSH"
for _ in $(seq 1 120); do
    node_ssh true 2>/dev/null && break
    sleep 5
done
node_ssh true 2>/dev/null || die "node never came up - see $WORK/firstboot.log"
log_info "node is up"

node_ssh_stdin "bash -s $LAB_IFNAME $LAB_MAC $LAB_NODE_IP $LAB_GW $LAB_DNS $LAB_GUEST_IFNAME $LAB_GUEST_HOST_IP" <<'REMOTE'
set -e
IFNAME="$1"; MAC="$2"; NODE_IP="$3"; GW="$4"; DNS="$5"; GUEST_IFNAME="$6"; GUEST_HOST_IP="$7"
[ -n "$IFNAME" ] && [ -n "$MAC" ] && [ -n "$NODE_IP" ] || { echo "args not passed through" >&2; exit 1; }
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
# `local` ships as iso,vztmpl,backup,import - it cannot hold a disk, so there
# is nowhere for a move test to move a volume to. Adding images and rootdir
# gives the suite a second, always-present storage. snippets is enabled too:
# `qm set --cicustom` accepts a snippet volume whose content type is disabled
# and then silently ignores it.
pvesm set local --content iso,vztmpl,backup,import,images,rootdir,snippets

# Handy inside the node for every profile and test run.
$APT install -y -qq --no-install-recommends python3-pytest python3-requests jq fio
$APT clean

# Without a serial console a node that fails to boot is completely silent -
# lab/up.sh writes console.log and it stays empty. Costs nothing, saves an
# afternoon.
sed -i 's|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT="quiet console=tty0 console=ttyS0,115200"|' /etc/default/grub
grep -q GRUB_TERMINAL /etc/default/grub || printf 'GRUB_TERMINAL="console serial"\nGRUB_SERIAL_COMMAND="serial --speed=115200"\n' >> /etc/default/grub
update-grub

# The installer puts the management NIC into vmbr0 and the node's address on
# the bridge, which entangles the node with its guests: a guest can take the
# node's DHCP lease, and two nodes cannot hold distinct addresses without being
# re-addressed after boot. Split the roles instead.
#
# Management goes back to DHCP, which is safe here precisely because it is now
# alone on its segment - the only client, so the lease is whatever the lab
# hands out first, deterministically. vmbr0 moves to its own NIC and carries no
# host address at all.
grep -q "$NODE_IP" /etc/network/interfaces || {
    echo "installer did not configure $NODE_IP" >&2
    exit 1
}

cat > /etc/network/interfaces <<EOF
auto lo
iface lo inet loopback

# Management. Alone on its own user-mode network, so DHCP is deterministic and
# no guest can ever take this address. A node image replaces this with a static
# address and its own MAC-derived name.
auto $IFNAME
iface $IFNAME inet dhcp

# The bridge guests attach to. No address: the node does not live here, which
# is what keeps a guest from ever taking the node's lease.
auto $GUEST_IFNAME
iface $GUEST_IFNAME inet manual

# An address here, unlike on a node. The base image is never booted as a lab
# node - it exists to build node images from, and building one means talking to
# the guest it boots to bake the VM template. A node image drops this, which is
# what keeps a node off the guest segment.
auto vmbr0
iface vmbr0 inet static
        address $GUEST_HOST_IP/24
        bridge-ports $GUEST_IFNAME
        bridge-stp off
        bridge-fd 0

source /etc/network/interfaces.d/*
EOF
# Deliberately not applied here. This rewrites the very interface the SSH
# session is riding on - bringing it up live drops the connection mid-script.
# The reboot below applies it, and verifies it.

# Name the NIC after its (pinned) MAC instead of its PCI slot. The default
# NamePolicy ends in `path`, which encodes PCI bus/slot geography - so simply
# attaching another test disk renamed the NIC, left vmbr0 bridging a port that
# no longer existed, and the node booted to a login prompt with an address in
# its banner and no reachable network. `mac` is immune to PCI topology.
# Matched on the driver rather than on one MAC: a two-node lab gives each node
# a second NIC for the cluster link, and anything not covered here falls back
# to the default policy - which ends at `path`, derives the name from the PCI
# slot, and moves the moment a test disk is added.
cat > /etc/systemd/network/10-lab-net.link <<EOF
[Match]
Driver=virtio_net

[Link]
NamePolicy=mac
EOF
# The rename happens in early userspace, so the rule has to be in the initramfs.
update-initramfs -u

# Zero the free space so the qcow2 compacts well - the image gets shipped.
fstrim -av || true
REMOTE

# Reboot before baking anything. The templates are baked *through* this
# network - the node talks to the guest it boots over vmbr0 - so the network
# has to be the final one first. It also means a broken network fails here,
# rather than as a mysterious timeout during the bake.
log_info "rebooting onto the final network"
node_ssh 'systemctl reboot' 2>/dev/null || true
sleep 20
up=0
for _ in $(seq 1 60); do
    node_ssh true 2>/dev/null && { up=1; break; }
    sleep 5
done
[[ $up -eq 1 ]] || die "node unreachable after the interface rename - see $WORK/firstboot.log"

node_ssh "ip -br link show $LAB_IFNAME" >/dev/null 2>&1 \
    || die "management interface $LAB_IFNAME did not appear after reboot"
node_ssh "ip -br link show $LAB_GUEST_IFNAME" >/dev/null 2>&1 \
    || die "guest interface $LAB_GUEST_IFNAME did not appear after reboot"
node_ssh "ip -4 -o addr show $LAB_IFNAME | grep -q inet" \
    || die "$LAB_IFNAME has no address after reboot"
# vmbr0 must be up and must NOT carry an address: the node lives on the
# management NIC, and anything here would put it back on the guest segment.
node_ssh "ip -br link show vmbr0 | grep -q UP" || die "vmbr0 is not up"
node_ssh "ip -4 -o addr show vmbr0 | grep -q $LAB_GUEST_HOST_IP" \
    || die "vmbr0 does not carry $LAB_GUEST_HOST_IP; the bake guest would be unreachable"
log_info "verified: management on $LAB_IFNAME, vmbr0 bridging $LAB_GUEST_IFNAME"


# ── Bake in the guest templates ──────────────────────────────────────────────
#
# A lab boots on a throwaway overlay, so anything created per-run is created
# every run. Downloading a cloud image and booting it to install the guest
# agent costs ~5 minutes; doing it once here makes it free. Both templates live
# on node-local storage, never on the storage under test - tests full-clone
# from them, which still exercises the target backend.

log_info "baking in the LXC template"
node_ssh_stdin 'bash -s' <<'REMOTE'
set -e
pveam update >/dev/null
# The architecture matters: sorting the whole list picks the arm64 build,
# which installs happily and then cannot start a container.
tmpl=$(pveam available --section system | awk '{print $2}' \
        | grep '^debian-13-standard.*amd64' | sort -V | tail -1)
[ -n "$tmpl" ] || { echo "no debian-13 LXC template offered" >&2; exit 1; }
pveam download local "$tmpl" >/dev/null
pveam list local | grep -q amd64 || { echo "no amd64 template downloaded" >&2; exit 1; }
pveam list local
touch /root/.lab-ct-template-ready
REMOTE
node_ssh "test -f /root/.lab-ct-template-ready" \
    || die "LXC template bake did not complete"

log_info "baking in the VM template (downloads a cloud image and boots it once)"
node_ssh_stdin "bash -s $GUEST_TEMPLATE_VMID" <<'REMOTE'
set -e
VMID="$1"
BAKE_IP=10.0.99.11         # on the guest network, below its DHCP range
IMG=/var/lib/vz/template/debian-13-genericcloud-amd64.qcow2
[ -f "$IMG" ] || curl -fsSL -o "$IMG" \
    https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2

[ -f /root/.ssh/id_ed25519 ] || ssh-keygen -q -t ed25519 -N '' -f /root/.ssh/id_ed25519

# local-lvm, not the storage under test: tests clone onto their own storage,
# which still exercises the target backend on every clone.
qm create "$VMID" --name lab-guest-template --ostype l26 --cpu host \
    --cores 2 --memory 1024 --scsihw virtio-scsi-single --agent enabled=1 \
    --serial0 socket --net0 virtio,bridge=vmbr0
qm set "$VMID" --scsi0 "local-lvm:0,import-from=$IMG,discard=on"
qm set "$VMID" --ide2 local-lvm:cloudinit
# A static address and an injected key, so the guest is reachable at a known
# place the moment cloud-init finishes. The obvious alternative - a cicustom
# snippet that installs the packages - fails silently: `snippets` is not an
# enabled content type on `local` by default, so the snippet is ignored and
# the guest gets templated unprovisioned, which only surfaces later as tests
# timing out waiting for an agent that was never installed.
qm set "$VMID" --ciuser root --cipassword pvelab \
    --sshkeys /root/.ssh/id_ed25519.pub \
    --ipconfig0 "ip=${BAKE_IP}/24,gw=10.0.99.2" --nameserver 10.0.99.3 \
    --boot order=scsi0
qm start "$VMID"

SSHOPT="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o BatchMode=yes -i /root/.ssh/id_ed25519"
SSHQ="ssh -n $SSHOPT"          # never reads this script's stdin
SSHIN="ssh $SSHOPT"            # for the one call that is fed a heredoc
up=0
for _ in $(seq 1 90); do
    $SSHQ "root@$BAKE_IP" true 2>/dev/null && { up=1; break; }
    sleep 4
done
[ "$up" = 1 ] || { echo "bake guest never became reachable at $BAKE_IP" >&2; exit 1; }

# Debian's genericcloud image ships without qemu-guest-agent; the suite needs
# it for every VM test.
$SSHIN "root@$BAKE_IP" 'bash -s' <<'GUEST'
set -e
export DEBIAN_FRONTEND=noninteractive
# The guest is reachable as soon as sshd starts, which is well before
# cloud-init has finished - and cloud-init runs apt. Wait for it, then stop
# the timers that would start another one behind us.
cloud-init status --wait >/dev/null 2>&1 || true
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl stop apt-daily.service apt-daily-upgrade.service 2>/dev/null || true
for _ in $(seq 1 60); do
    fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || break
    sleep 5
done
apt-get() { command apt-get -o DPkg::Lock::Timeout=600 "$@"; }
apt-get update -qq
apt-get install -y -qq qemu-guest-agent fio
systemctl enable qemu-guest-agent
# Clones have no cloud-init drive, so leaving cloud-init enabled only costs
# every boot a datasource search that cannot succeed.
touch /etc/cloud/cloud-init.disabled
command -v fio >/dev/null || { echo "fio missing" >&2; exit 1; }
GUEST

# Prove the agent actually answers before templating. This is the check whose
# absence let an unprovisioned template ship.
$SSHQ "root@$BAKE_IP" 'systemctl start qemu-guest-agent' || true
agent_ok=0
for _ in $(seq 1 45); do
    qm agent "$VMID" ping >/dev/null 2>&1 && { agent_ok=1; break; }
    sleep 2
done
[ "$agent_ok" = 1 ] || { echo "guest agent did not answer after install" >&2; exit 1; }

$SSHQ "root@$BAKE_IP" 'systemctl poweroff' 2>/dev/null || true
for _ in $(seq 1 90); do
    qm status "$VMID" | grep -q stopped && break
    sleep 2
done
qm status "$VMID" | grep -q stopped || { echo "bake guest never powered off" >&2; exit 1; }

qm set "$VMID" --delete ide2
qm template "$VMID"
qm config "$VMID" | grep -q '^template: 1' || { echo "not a template" >&2; exit 1; }
touch /root/.lab-vm-template-ready
echo "VM template ready"
REMOTE
node_ssh "test -f /root/.lab-vm-template-ready" \
    || die "VM template bake did not complete"

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
