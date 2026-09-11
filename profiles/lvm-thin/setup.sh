#!/usr/bin/env bash
# Reference profile: the lvm-thin storage that every PVE install already has.
# Useful as a control - if the suite fails here, the problem is the suite.
set -euo pipefail
CONFIG="${LAB_TEST_CONFIG:-/root/lab-test.env}"

pvesm status | grep -q '^local-lvm' || { echo "local-lvm is missing" >&2; exit 1; }
pvesm set local-lvm --content images,rootdir

cat >> "$CONFIG" <<CFG
STORAGE_NAME=local-lvm
STORAGE_TYPE=lvmthin
CFG
