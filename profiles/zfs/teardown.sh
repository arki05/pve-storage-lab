#!/usr/bin/env bash
set -euo pipefail
pvesm remove lab-zfs 2>/dev/null || true
zpool destroy -f labpool 2>/dev/null || true
