#!/usr/bin/env bash
# Join a multi-node lab into a PVE cluster.
#
# Migration is the only reason this exists: PVE cannot move a guest between
# unclustered nodes. Everything else works on one node.
#
# Each node already knows who it is - hostname, interfaces and addresses come
# from its node image - so this only has to teach the nodes about each other
# and run pvecm. In particular no node is renamed here: renaming a PVE node
# means moving /etc/pve/nodes/<name> and restarting pmxcfs, which is safe on a
# standalone node and awkward on one that has joined, so it happens while the
# node image is built instead.
#
#   cluster.sh [--name lab]

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/../lib/nodes.sh"

NAME="${LAB_NAME:-lab}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)    NAME="$2"; shift 2 ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *)         die "unknown argument: $1" ;;
    esac
done

LAB="$(lab_state_dir)/labs/$NAME"
[[ -f "$LAB/lab.env" ]] || die "lab '$NAME' is not up"
# shellcheck disable=SC1091
source "$LAB/lab.env"

NODES="${LAB_NODE_COUNT:-1}"
[[ $NODES -gt 1 ]] || { log_info "single node; nothing to cluster"; exit 0; }

mapfile -t SSH_OPTS < <(node_ssh_opts)
nssh() {
    local i="$1"; shift
    ssh -n "${SSH_OPTS[@]}" -i "$NODE_SSH_KEY" -p "$(node_ssh_port "$i")" \
        root@127.0.0.1 "$@"
}

if nssh 1 "pvecm status" >/dev/null 2>&1; then
    log_info "cluster already formed"
    exit 0
fi

# ── Teach the nodes about each other ─────────────────────────────────────────
#
# Every name resolves to a *cluster* address. That is not a detail: the
# management networks are isolated per node and cannot route between them, so
# the cluster link is the only path, and PVE takes a node's ssh address from
# exactly this lookup and stores it in /etc/pve/.members.

hosts_block=""
for ((i = 1; i <= NODES; i++)); do
    hosts_block+="$(node_cluster_ip "$i") $(node_hostname "$i").local $(node_hostname "$i")\n"
done

for ((i = 1; i <= NODES; i++)); do
    log_info "node$i: peers"
    STEP="$(mktemp)"
    cat > "$STEP" <<REMOTE
set -euo pipefail
# Drop any entry for a node name first - including this node's own, written
# when its image was built against its management address. Leaving it in place
# would have peers resolve a node to an address on a network they cannot reach.
for name in $(for ((j = 1; j <= NODES; j++)); do printf '%s ' "$(node_hostname "$j")"; done); do
    sed -i "/[[:space:]]\${name}\\([[:space:]]\\|\\.\\|\\\$\\)/d" /etc/hosts
done
printf "%b" "$hosts_block" >> /etc/hosts
awk '!seen[\$0]++' /etc/hosts > /tmp/hosts.dedup && mv /tmp/hosts.dedup /etc/hosts
REMOTE
    ssh "${SSH_OPTS[@]}" -i "$NODE_SSH_KEY" -p "$(node_ssh_port "$i")" root@127.0.0.1 \
        "cat > /root/.lab-hosts.sh" < "$STEP"
    rm -f "$STEP"
    nssh "$i" "bash /root/.lab-hosts.sh"

    resolved=$(nssh "$i" "getent hosts $(node_hostname "$i") | awk '{print \$1}' | head -1")
    [[ "$resolved" == "$(node_cluster_ip "$i")" ]] || die \
        "node$i resolves its own name to '$resolved', expected $(node_cluster_ip "$i")"
done

# ── Form ─────────────────────────────────────────────────────────────────────

log_info "node1: creating cluster '$NAME'"
nssh 1 "pvecm create '$NAME' --link0 '$(node_cluster_ip 1)'" >/dev/null
sleep 5

FP=$(nssh 1 "openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -fingerprint -sha256" \
    | sed 's/.*=//')
[[ -n "$FP" ]] || die "could not read node1's certificate fingerprint"

for ((i = 2; i <= NODES; i++)); do
    log_info "node$i: joining"
    nssh "$i" "echo '${ROOT_PASSWORD:-pvelab123}' | pvecm add '$(node_cluster_ip 1)' \
        --link0 '$(node_cluster_ip "$i")' --fingerprint '$FP'" >/dev/null
    sleep 10
done

log_info "waiting for quorum"
for _ in $(seq 1 60); do
    if nssh 1 "pvecm status 2>/dev/null | grep -q 'Quorate:.*Yes'"; then
        # Quorum alone is not proof: a one-node cluster is quorate too.
        seen=$(nssh 1 "pvecm nodes 2>/dev/null | grep -c '^ *[0-9]'" || echo 0)
        if [[ "$seen" -ge "$NODES" ]]; then
            log_info "cluster is quorate with $seen nodes"
            nssh 1 "pvecm nodes" || true

            # Every node image regenerates its SSH host keys, so the
            # ssh_known_hosts PVE inherited from the base image names the right
            # node with the wrong key. updatecerts rebuilds it from the keys
            # that actually exist. Without this, any API call one node proxies
            # to another fails with "Host key verification failed" - which
            # surfaces as migrations timing out, not as an SSH error.
            for ((n = 1; n <= NODES; n++)); do
                nssh "$n" "pvecm updatecerts -f" >/dev/null 2>&1 || true
                nssh "$n" "systemctl restart pvedaemon pveproxy" >/dev/null 2>&1 || true
            done
            sleep 5

            # Prove it, rather than finding out during a migration. Each node
            # must be able to proxy an API call to every other.
            for ((n = 1; n <= NODES; n++)); do
                for ((m = 1; m <= NODES; m++)); do
                    [[ $n -eq $m ]] && continue
                    nssh "$n" "pvesh get /nodes/$(node_hostname "$m")/status --output-format json >/dev/null" \
                        || die "node$n cannot reach node$m through the API"
                done
            done
            log_info "cross-node API verified"
            exit 0
        fi
    fi
    sleep 5
done
nssh 1 "pvecm status" || true
die "cluster did not reach quorum with $NODES nodes"
