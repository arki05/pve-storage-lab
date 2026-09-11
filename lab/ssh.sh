#!/usr/bin/env bash
# SSH into a lab node.  ssh.sh [--name lab] [--node N] [command...]
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/../lib/nodes.sh"

NAME="${LAB_NAME:-lab}"
NODE=1
while true; do
    case "${1:-}" in
        --name) NAME="$2"; shift 2 ;;
        --node) NODE="$2"; shift 2 ;;
        *)      break ;;
    esac
done

LAB="$(lab_state_dir)/labs/$NAME"
[[ -f "$LAB/lab.env" ]] || die "lab '$NAME' is not up"
# shellcheck disable=SC1091
source "$LAB/lab.env"

port="$(node_ssh_port "$NODE")"

mapfile -t SSH_OPTS < <(node_ssh_opts)
exec ssh "${SSH_OPTS[@]}" -i "$NODE_SSH_KEY" -p "$port" root@127.0.0.1 "$@"
