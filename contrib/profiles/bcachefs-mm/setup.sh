#!/usr/bin/env bash
# Formats a multi-device bcachefs and registers it through the other plugin.
set -euo pipefail
CONFIG="${LAB_TEST_CONFIG:-/root/lab-test.env}"
NAME="${BCACHEFS_STORAGE:-lab-bcachefs-mm}"
MOUNT=/mnt/lab-bcachefs-mm

read -r -a DISKS <<< "${LAB_DISKS:-}"
[ "${#DISKS[@]}" -ge 1 ] || { echo "need at least 1 disk" >&2; exit 1; }

modprobe bcachefs
mkdir -p "$MOUNT"

if ! findmnt -rno TARGET "$MOUNT" >/dev/null 2>&1; then
    for d in "${DISKS[@]}"; do wipefs -aq "$d" || true; done
    # No --prjquota: this plugin has no project-quota path, so enabling them
    # would test something it never reads.
    bcachefs format --force "${DISKS[@]}"
    mount -t bcachefs "$(IFS=:; echo "${DISKS[*]}")" "$MOUNT"
fi
findmnt -rno TARGET "$MOUNT" >/dev/null || { echo "bcachefs did not mount" >&2; exit 1; }

if ! pvesm status --storage "$NAME" >/dev/null 2>&1; then
    pvesm add bcachefs "$NAME" --path "$MOUNT" --content images,rootdir
fi

echo "STORAGE_NAME=$NAME" >> "$CONFIG"
echo "BCACHEFS_MOUNT=$MOUNT" >> "$CONFIG"
