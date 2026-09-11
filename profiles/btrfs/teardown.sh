#!/usr/bin/env bash
set -euo pipefail
pvesm remove lab-btrfs 2>/dev/null || true
umount /mnt/lab-btrfs 2>/dev/null || true
