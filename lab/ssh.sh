#!/usr/bin/env bash
# SSH into a lab node.  ssh.sh [--name lab] [command...]
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

NAME="${LAB_NAME:-lab}"
[[ "${1:-}" == "--name" ]] && { NAME="$2"; shift 2; }

LAB="$(lab_state_dir)/labs/$NAME"
[[ -f "$LAB/lab.env" ]] || die "lab '$NAME' is not up"
# shellcheck disable=SC1091
source "$LAB/lab.env"

mapfile -t SSH_OPTS < <(node_ssh_opts)
exec ssh "${SSH_OPTS[@]}" -i "$NODE_SSH_KEY" -p "$NODE_SSH_PORT" root@127.0.0.1 "$@"
