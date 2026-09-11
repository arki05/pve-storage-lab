#!/usr/bin/env bash
# SSH into a lab node.  ssh.sh [--name lab] [--node N] [command...]
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

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

port_var="NODE${NODE}_SSH_PORT"
port="${!port_var:-$NODE_SSH_PORT}"
[[ -n "$port" ]] || die "no node $NODE in lab '$NAME'"

mapfile -t SSH_OPTS < <(node_ssh_opts)
exec ssh "${SSH_OPTS[@]}" -i "$NODE_SSH_KEY" -p "$port" root@127.0.0.1 "$@"
