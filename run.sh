#!/usr/bin/env bash
# Run the storage suite against a lab node.
#
# Brings a lab up if one is not running, applies a storage profile, copies the
# suite onto the node and runs pytest there. Tests execute on the node itself:
# they use pvesh as root, so there are no credentials anywhere in this repo or
# in CI.
#
# A profile is a directory containing:
#   setup.sh           - runs on the node as root; registers the storage and
#                        appends STORAGE_NAME=... to $LAB_TEST_CONFIG
#   capabilities.env   - what the storage claims to support; copied verbatim
#   teardown.sh        - optional, runs on the node after the suite
#
#   run.sh --profile-dir ./profiles/lvm-thin [--extra-tests DIR] [-- pytest args]

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

ROOT="$(lab_root)"
NAME="${LAB_NAME:-lab}"
PROFILE_DIR=""
EXTRA_TESTS=""
KEEP=0
PYTEST_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)        NAME="$2";        shift 2 ;;
        --profile-dir) PROFILE_DIR="$2"; shift 2 ;;
        --extra-tests) EXTRA_TESTS="$2"; shift 2 ;;
        --keep)        KEEP=1;           shift ;;
        --)            shift; PYTEST_ARGS=("$@"); break ;;
        -h|--help)     sed -n '2,16p' "$0"; exit 0 ;;
        *)             PYTEST_ARGS+=("$1"); shift ;;
    esac
done

[[ -n "$PROFILE_DIR" ]] || die "--profile-dir is required"
PROFILE_DIR="$(cd "$PROFILE_DIR" && pwd)"
[[ -x "$PROFILE_DIR/setup.sh" ]] || die "no executable setup.sh in $PROFILE_DIR"
[[ -z "$EXTRA_TESTS" ]] || EXTRA_TESTS="$(cd "$EXTRA_TESTS" && pwd)"

STATE="$(lab_state_dir)"
LAB="$STATE/labs/$NAME"

if [[ ! -f "$LAB/qemu.pid" ]] || ! kill -0 "$(cat "$LAB/qemu.pid" 2>/dev/null)" 2>/dev/null; then
    log_info "no lab running; starting one"
    "$ROOT/lab/up.sh" --name "$NAME" >&2
fi
# shellcheck disable=SC1091
source "$LAB/lab.env"

mapfile -t SSH_OPTS < <(node_ssh_opts)
node_ssh() {
    ssh "${SSH_OPTS[@]}" -i "$NODE_SSH_KEY" -p "$NODE_SSH_PORT" root@127.0.0.1 "$@"
}
node_push() {
    tar cz -C "$1" . | node_ssh "mkdir -p $2 && tar xz -C $2"
}

# ── Ship the suite and the profile ───────────────────────────────────────────

log_info "copying suite to the node"
node_ssh "rm -rf /root/lab-suite /root/lab-profile && mkdir -p /root/lab-suite /root/lab-profile"
node_push "$ROOT/suite" /root/lab-suite
node_push "$PROFILE_DIR" /root/lab-profile
if [[ -n "$EXTRA_TESTS" ]]; then
    log_info "adding plugin tests from $EXTRA_TESTS"
    node_push "$EXTRA_TESTS" /root/lab-suite/tests
fi

# ── Apply the profile ────────────────────────────────────────────────────────

log_info "applying storage profile: $(basename "$PROFILE_DIR")"
node_ssh "bash -s" <<'REMOTE'
set -euo pipefail
export LAB_TEST_CONFIG=/root/lab-test.env
: > "$LAB_TEST_CONFIG"
chmod +x /root/lab-profile/*.sh 2>/dev/null || true
bash /root/lab-profile/setup.sh
[ -f /root/lab-profile/capabilities.env ] && cat /root/lab-profile/capabilities.env >> "$LAB_TEST_CONFIG"
grep -q '^STORAGE_NAME=' "$LAB_TEST_CONFIG" || {
    echo "profile did not write STORAGE_NAME to $LAB_TEST_CONFIG" >&2
    exit 1
}
echo "--- test config ---"
grep -v '^#' "$LAB_TEST_CONFIG" | grep .
REMOTE

# ── Run ──────────────────────────────────────────────────────────────────────

RESULTS="$LAB/results"
mkdir -p "$RESULTS"

log_info "running the suite"
status=0
node_ssh "bash -s" <<REMOTE || status=\$?
set -uo pipefail
cd /root/lab-suite
python3 -m pytest -v -ra --junitxml=/root/lab-suite/junit.xml ${PYTEST_ARGS[*]:-} tests
REMOTE

node_ssh "cat /root/lab-suite/junit.xml" > "$RESULTS/junit.xml" 2>/dev/null || true

# Anything reproducing a failure needs to know exactly what it ran against.
node_ssh "bash -s" > "$RESULTS/environment.txt" <<'REMOTE' || true
echo "pve=$(pveversion | head -1)"
echo "kernel=$(uname -r)"
echo "date=$(date -Is)"
grep -v '^#' /root/lab-test.env | grep . || true
REMOTE

if [[ -x "$PROFILE_DIR/teardown.sh" && $KEEP -eq 0 ]]; then
    log_info "tearing down the profile"
    node_ssh "bash /root/lab-profile/teardown.sh" || log_warn "teardown reported an error"
fi

log_info "results in $RESULTS"
[[ $status -eq 0 ]] && log_info "suite passed" || log_error "suite failed (exit $status)"
exit $status
