#!/usr/bin/env bash
# btrfs storage. The closest upstream analogue to the bcachefs plugin: a
# container rootfs is a subvolume, snapshots are native, and sizes are enforced
# with qgroups only when the storage declares quotas. Worth having for its own
# sake, and as the comparison point for anything bcachefs-shaped.
set -euo pipefail
CONFIG="${LAB_TEST_CONFIG:-/root/lab-test.env}"
NAME="${BTRFS_STORAGE:-lab-btrfs}"
MOUNT=/mnt/lab-btrfs

read -r -a DISKS <<< "${LAB_DISKS:-}"
[ "${#DISKS[@]}" -ge 1 ] || { echo "no disks assigned (LAB_DISKS='${LAB_DISKS:-}')" >&2; exit 1; }

mkdir -p "$MOUNT"
if ! findmnt -rno TARGET "$MOUNT" >/dev/null 2>&1; then
    for d in "${DISKS[@]}"; do wipefs -aq "$d" || true; done
    if [ "${#DISKS[@]}" -ge 2 ]; then
        mkfs.btrfs -f -d raid1 -m raid1 "${DISKS[@]}" >/dev/null
    else
        mkfs.btrfs -f "${DISKS[0]}" >/dev/null
    fi
    mount "${DISKS[0]}" "$MOUNT"
fi

pvesm status 2>/dev/null | grep -q "^${NAME}" || \
    pvesm add btrfs "$NAME" --path "$MOUNT" --content images,rootdir

cat >> "$CONFIG" <<CFG
STORAGE_NAME=$NAME
STORAGE_TYPE=btrfs
BTRFS_MOUNT=$MOUNT
CFG
