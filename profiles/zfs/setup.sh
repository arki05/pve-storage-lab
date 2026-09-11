#!/usr/bin/env bash
# ZFS pool storage. ZFS ships with Proxmox, so this needs no bake step.
set -euo pipefail
CONFIG="${LAB_TEST_CONFIG:-/root/lab-test.env}"
NAME="${ZFS_STORAGE:-lab-zfs}"
POOL="${ZFS_POOL:-labpool}"

read -r -a DISKS <<< "${LAB_DISKS:-}"
[ "${#DISKS[@]}" -ge 1 ] || { echo "no disks assigned (LAB_DISKS='${LAB_DISKS:-}')" >&2; exit 1; }

modprobe zfs
if ! zpool list "$POOL" >/dev/null 2>&1; then
    for d in "${DISKS[@]}"; do wipefs -aq "$d" || true; done
    # A mirror when there are two or more devices: single-vdev pools hide any
    # bug that only shows up once ZFS has a choice of where to read from.
    if [ "${#DISKS[@]}" -ge 2 ]; then
        zpool create -f -o ashift=12 "$POOL" mirror "${DISKS[@]}"
    else
        zpool create -f -o ashift=12 "$POOL" "${DISKS[0]}"
    fi
fi
zfs set compression=lz4 "$POOL"

pvesm status 2>/dev/null | grep -q "^${NAME}" || \
    pvesm add zfspool "$NAME" --pool "$POOL" --content images,rootdir --sparse 1

cat >> "$CONFIG" <<CFG
STORAGE_NAME=$NAME
STORAGE_TYPE=zfspool
ZFS_POOL=$POOL
CFG
