#!/usr/bin/env bash
# Teach a node about its peers.
#
# Every name resolves to a *cluster* address. That is not a detail: the
# management networks are isolated per node and cannot route between them, so
# the cluster link is the only path - and PVE takes a node's ssh address from
# exactly this lookup and stores it in /etc/pve/.members. Leave a management
# address in place and a node reaches itself, tries to take a lock it already
# holds, and deadlocks.
#
#   cluster-hosts.sh "<name> <name> ..." "<ip> <name>.local <name>\n..."
set -euo pipefail
NAMES="$1"; BLOCK="$2"

sed -i '/127.0.1.1/d' /etc/hosts
for name in $NAMES; do
    sed -i "/[[:space:]]${name}\([[:space:]]\|\.\|$\)/d" /etc/hosts
done
printf "%b" "$BLOCK" >> /etc/hosts
awk '!seen[$0]++' /etc/hosts > /tmp/hosts.dedup && mv /tmp/hosts.dedup /etc/hosts
