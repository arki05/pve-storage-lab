#!/usr/bin/env bash
# Turn a multi-node lab into a PVE cluster.
#
# Migration is the only reason this exists: PVE has no way to move a guest
# between unclustered nodes. Everything else the suite does works fine on one.
#
# Every node boots from the same image, so they all start out called `pve-lab`
# on 10.0.2.10. That is fine for management - each node's user-mode network is
# its own isolated segment - but a cluster needs distinct identities, so every
# node after the first is renamed and all of them get an address on the cluster
# link.
#
# Node 1 keeps the image's hostname untouched, which keeps a single-node lab
# byte-identical to what it was before any of this existed.
#
#   cluster.sh [--name lab]

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

NAME="${LAB_NAME:-lab}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)    NAME="$2"; shift 2 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
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
node_port() { local i="$1"; local var="NODE${i}_SSH_PORT"; echo "${!var}"; }
node_ip()   { local i="$1"; local var="NODE${i}_CLUSTER_IP"; echo "${!var}"; }
node_name() { local i="$1"; [[ $i -eq 1 ]] && echo "pve-lab" || echo "pve-lab-node$i"; }

nssh() {
    local i="$1"; shift
    ssh -n "${SSH_OPTS[@]}" -i "$NODE_SSH_KEY" -p "$(node_port "$i")" root@127.0.0.1 "$@"
}
nrun() {
    # Scripts go over as files and are executed, never piped to `bash -s`: a
    # remote script read from stdin shares that stdin with everything it runs.
    local i="$1" script="$2"; shift 2
    ssh "${SSH_OPTS[@]}" -i "$NODE_SSH_KEY" -p "$(node_port "$i")" root@127.0.0.1 \
        "cat > /root/.lab-cluster-step.sh" < "$script"
    nssh "$i" "bash /root/.lab-cluster-step.sh $*"
}

if nssh 1 "pvecm status" >/dev/null 2>&1; then
    log_info "cluster already formed"
    exit 0
fi

# ── Per-node identity and the cluster link ───────────────────────────────────

hosts_block=""
for ((i = 1; i <= NODES; i++)); do
    hosts_block+="$(node_ip "$i") $(node_name "$i").local $(node_name "$i")\n"
done

STEP="$(mktemp)"
cat > "$STEP" <<'REMOTE'
set -euo pipefail
NEW_NAME="$1"; CLUSTER_IP="$2"; HOSTS_BLOCK="$3"

# The cluster NIC is the second virtio NIC. It is named after its MAC, like
# every NIC in this image, so it can be found without caring which PCI slot it
# landed in.
IFACE=$(ls /sys/class/net | grep '^enx5254001ac1' | head -1)
[ -n "$IFACE" ] || { echo "no cluster NIC found" >&2; exit 1; }

if ! grep -q "iface $IFACE" /etc/network/interfaces; then
    cat >> /etc/network/interfaces <<EOF

auto $IFACE
iface $IFACE inet static
        address $CLUSTER_IP/24
EOF
    ifup "$IFACE" 2>/dev/null || ifreload -a 2>/dev/null || true
fi

OLD_NAME=$(hostname)

write_hosts() {
    # pmxcfs refuses to start if it cannot resolve its own hostname, so the
    # entry has to exist *before* pve-cluster is started - not after.
    #
    # Every existing entry for this node's names goes first, including the
    # installer's. Every node in this lab has the same management address,
    # because each one's user-mode network is its own isolated segment - so a
    # peer that resolves a node name to 10.0.2.10 reaches *itself*. PVE stores
    # a node's address in /etc/pve/.members from exactly this lookup and then
    # uses it to ssh between nodes, so leaving the installer's line in place
    # makes a node migrate a guest to itself: it tries to take a lock it
    # already holds and deadlocks, reported as "can't lock file ... got
    # timeout" and a guest left locked.
    sed -i '/127.0.1.1/d' /etc/hosts
    for name in "$@"; do
        [ -n "$name" ] || continue
        sed -i "/[[:space:]]${name}\([[:space:]]\|\.\|\$\)/d" /etc/hosts
    done
    printf "%b" "$HOSTS_BLOCK" >> /etc/hosts
    awk '!seen[$0]++' /etc/hosts > /tmp/hosts.dedup && mv /tmp/hosts.dedup /etc/hosts
}

if [ "$OLD_NAME" != "$NEW_NAME" ]; then
    # Renaming a PVE node is more than hostnamectl: /etc/pve/nodes/<name> is
    # created by the installer and /etc/pve/local is a symlink into it, so the
    # directory has to move and pmxcfs has to be restarted to pick it up.
    systemctl stop pveproxy pvedaemon pvestatd pve-cluster corosync 2>/dev/null || true
    hostnamectl set-hostname "$NEW_NAME"
    echo "$NEW_NAME" > /etc/hostname
    write_hosts "$OLD_NAME" "$NEW_NAME"

    systemctl start pve-cluster
    for _ in $(seq 1 30); do
        systemctl is-active --quiet pve-cluster && break
        sleep 2
    done
    systemctl is-active --quiet pve-cluster || {
        echo "pve-cluster did not start after renaming to $NEW_NAME" >&2
        journalctl -u pve-cluster -n 20 --no-pager >&2 || true
        exit 1
    }

    if [ -d "/etc/pve/nodes/$OLD_NAME" ]; then
        mkdir -p "/etc/pve/nodes/$NEW_NAME"
        cp -a "/etc/pve/nodes/$OLD_NAME/." "/etc/pve/nodes/$NEW_NAME/" 2>/dev/null || true
        rm -rf "/etc/pve/nodes/$OLD_NAME" 2>/dev/null || true
    fi
    systemctl start corosync pvestatd pvedaemon pveproxy 2>/dev/null || true
else
    write_hosts "$NEW_NAME"
fi

touch /root/.lab-identity-done
REMOTE

for ((i = 1; i <= NODES; i++)); do
    log_info "node$i: identity and cluster link ($(node_name "$i") on $(node_ip "$i"))"
    nssh "$i" "rm -f /root/.lab-identity-done"
    nrun "$i" "$STEP" "$(node_name "$i")" "$(node_ip "$i")" "'$hosts_block'"
    nssh "$i" "test -f /root/.lab-identity-done" \
        || die "node$i identity step did not complete"
    resolved=$(nssh "$i" "getent hosts $(node_name "$i") | awk '{print \$1}' | head -1")
    [[ "$resolved" == "$(node_ip "$i")" ]] || die \
        "node$i resolves its own name to '$resolved', expected $(node_ip "$i"). \
Every node shares the management address, so peers would ssh to themselves."
done
rm -f "$STEP"

# ── Form the cluster ─────────────────────────────────────────────────────────

log_info "node1: creating cluster '$NAME'"
nssh 1 "pvecm create '$NAME' --link0 '$(node_ip 1)'" >/dev/null
sleep 5

FP=$(nssh 1 "openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -fingerprint -sha256" \
    | sed 's/.*=//')
[[ -n "$FP" ]] || die "could not read node1's certificate fingerprint"

for ((i = 2; i <= NODES; i++)); do
    log_info "node$i: joining"
    # pvecm authenticates the join over SSH with the root password; the lab's
    # is baked into the image.
    nssh "$i" "echo '${ROOT_PASSWORD:-pvelab123}' | pvecm add '$(node_ip 1)' \
        --link0 '$(node_ip "$i")' --fingerprint '$FP'" >/dev/null
    sleep 10
done

# ── Wait for quorum ──────────────────────────────────────────────────────────

log_info "waiting for quorum"
for _ in $(seq 1 60); do
    if nssh 1 "pvecm status 2>/dev/null | grep -q 'Quorate:.*Yes'"; then
        log_info "cluster is quorate"
        nssh 1 "pvecm nodes" || true
        exit 0
    fi
    sleep 5
done
nssh 1 "pvecm status" || true
die "cluster did not reach quorum"
