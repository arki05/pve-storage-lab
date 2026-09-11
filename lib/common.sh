# Shared helpers for pve-storage-lab. Source, don't execute.
# shellcheck shell=bash

_log() { printf '[%s] %-5s %s\n' "$(date +%H:%M:%S)" "$1" "${*:2}" >&2; }
log_info()  { _log INFO  "$@"; }
log_warn()  { _log WARN  "$@"; }
log_error() { _log ERROR "$@"; }
die()       { log_error "$@"; exit 1; }

require_cmd() {
    for c in "$@"; do
        command -v "$c" >/dev/null 2>&1 || die "missing required command: $c"
    done
}

lab_root() {
    cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
}

# Everything the lab creates lives under one directory so a run leaves no
# trace elsewhere and CI can cache or wipe it wholesale.
lab_state_dir() {
    echo "${PVE_LAB_STATE:-${HOME}/.local/share/pve-storage-lab}"
}

# SSH into the node. The node is reachable only through a forwarded port on
# localhost, so there is no host key worth checking and no network to trust.
node_ssh_opts() {
    printf '%s\n' \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR \
        -o ConnectTimeout=5 \
        -o BatchMode=yes
}
