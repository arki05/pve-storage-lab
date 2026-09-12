#!/usr/bin/env bash
set -uo pipefail
pvesm remove "${BCACHEFS_STORAGE:-lab-bcachefs-mm}" 2>/dev/null || true
umount /mnt/lab-bcachefs-mm 2>/dev/null || true
