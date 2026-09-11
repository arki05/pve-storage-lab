#!/usr/bin/env bash
set -euo pipefail
pvesm remove lab-dir 2>/dev/null || true
umount /mnt/lab-dir 2>/dev/null || true
