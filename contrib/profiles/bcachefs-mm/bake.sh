#!/usr/bin/env bash
# Baked once: bcachefs itself is a DKMS module, and the plugin is a release deb.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

install -d -m 0755 /etc/apt/keyrings
curl -fsSL -o /etc/apt/keyrings/bcachefs.asc https://apt.bcachefs.org/apt.bcachefs.org.asc
echo "deb [signed-by=/etc/apt/keyrings/bcachefs.asc] https://apt.bcachefs.org/trixie bcachefs-tools-release main" \
    > /etc/apt/sources.list.d/bcachefs.list

APT="apt-get -o DPkg::Lock::Timeout=600"
$APT update -qq
$APT install -y -qq "proxmox-headers-$(uname -r)" || $APT install -y -qq proxmox-default-headers
$APT install -y -qq bcachefs-tools bcachefs-kernel-dkms attr curl

modprobe bcachefs
grep -qw bcachefs /proc/filesystems || { echo "bcachefs not registered" >&2; exit 1; }

VERSION=1.0.0
DEB="libpve-storage-plugin-bcachefs-perl_${VERSION}_all.deb"
curl -fsSL -o "/tmp/$DEB" \
    "https://github.com/MmAaXx500/pve-storage-plugin-bcachefs/releases/download/v${VERSION}/${DEB}"
$APT install -y -qq "/tmp/$DEB"

bcachefs version
dpkg -l | grep -E 'bcachefs'
