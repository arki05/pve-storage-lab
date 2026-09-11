#!/usr/bin/env bash
# Reference profile: a plain directory storage on one of the lab's test disks.
# Formats /dev/disk/by-id/virtio-labdisk1 as ext4 - the least capable backend
# the suite supports, so it exercises the skip logic as much as the tests.
set -euo pipefail
CONFIG="${LAB_TEST_CONFIG:-/root/lab-test.env}"
MOUNT=/mnt/lab-dir
NAME=lab-dir

# The lab assigns disks; do not go looking for them. With several profiles in
# one lab, whichever one globbed for labdisk1 would take a disk that belongs to
# another.
set -- $LAB_DISKS
DISK="${1:-}"
[ -b "$DISK" ] || { echo "no disk assigned (LAB_DISKS='$LAB_DISKS')" >&2; exit 1; }

if ! findmnt -rno TARGET "$MOUNT" >/dev/null 2>&1; then
    blkid "$DISK" >/dev/null 2>&1 || mkfs.ext4 -q -F "$DISK"
    mkdir -p "$MOUNT"
    mount "$DISK" "$MOUNT"
fi

pvesm status | grep -q "^${NAME}" || \
    pvesm add dir "$NAME" --path "$MOUNT" --content images,rootdir --shared 0

cat >> "$CONFIG" <<CFG
STORAGE_NAME=$NAME
STORAGE_TYPE=dir
CFG
