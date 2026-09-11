#!/usr/bin/env bash
# Stop a lab node. With --reset, also delete its overlay and test disks so the
# next up.sh starts from the pristine base image.
#
#   down.sh [--name lab] [--reset]

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

NAME="${LAB_NAME:-lab}"
RESET=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)    NAME="$2"; shift 2 ;;
        --reset)   RESET=1;   shift ;;
        -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
        *)         die "unknown argument: $1" ;;
    esac
done

LAB="$(lab_state_dir)/labs/$NAME"
[[ -d "$LAB" ]] || { log_info "no such lab: $NAME"; exit 0; }

if [[ -f "$LAB/qemu.pid" ]]; then
    pid=$(cat "$LAB/qemu.pid")
    if kill -0 "$pid" 2>/dev/null; then
        log_info "stopping lab '$NAME' (pid $pid)"
        kill "$pid"
        for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$LAB/qemu.pid"
fi

if [[ $RESET -eq 1 ]]; then
    log_info "resetting lab '$NAME' to pristine"
    rm -f "$LAB"/system.qcow2 "$LAB"/disk*.raw "$LAB"/lab.env "$LAB"/console.log
fi
