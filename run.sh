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
#                        appends STORAGE_NAME=... to $LAB_TEST_CONFIG. If
#                        --source-dir was given, the working tree is at
#                        /root/lab-source and the profile should build and
#                        install from it rather than from a repository.
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
SOURCE_DIR=""
KEEP=0
PYTEST_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)        NAME="$2";        shift 2 ;;
        --profile-dir) PROFILE_DIR="$2"; shift 2 ;;
        --extra-tests) EXTRA_TESTS="$2"; shift 2 ;;
        --source-dir)  SOURCE_DIR="$2";  shift 2 ;;
        --keep)        KEEP=1;           shift ;;
        --)            shift; PYTEST_ARGS=("$@"); break ;;
        -h|--help)     sed -n '2,16p' "$0"; exit 0 ;;
        *)             PYTEST_ARGS+=("$1"); shift ;;
    esac
done

[[ -n "$PROFILE_DIR" ]] || die "--profile-dir is required"
PROFILE_DIR="$(cd "$PROFILE_DIR" && pwd)"
# A profile that lives at <repo>/test/profile would otherwise be called
# "profile", which is a poor name for a published image. Let it say so.
if [[ -f "$PROFILE_DIR/name" ]]; then
    PROFILE_NAME="$(tr -d '[:space:]' < "$PROFILE_DIR/name")"
else
    PROFILE_NAME="$(basename "$PROFILE_DIR")"
fi
[[ -x "$PROFILE_DIR/setup.sh" ]] || die "no executable setup.sh in $PROFILE_DIR"
[[ -z "$EXTRA_TESTS" ]] || EXTRA_TESTS="$(cd "$EXTRA_TESTS" && pwd)"
[[ -z "$SOURCE_DIR" ]] || SOURCE_DIR="$(cd "$SOURCE_DIR" && pwd)"

STATE="$(lab_state_dir)"
LAB="$STATE/labs/$NAME"

# A profile with a bake.sh gets its expensive setup pre-built into a derived
# image, so repeated runs do not reinstall it.
VARIANT=base
if [[ -f "$PROFILE_DIR/bake.sh" ]]; then
    VARIANT="$PROFILE_NAME"
    "$ROOT/lab/bake-profile.sh" --profile-dir "$PROFILE_DIR" >&2
fi

if [[ ! -f "$LAB/qemu.pid" ]] || ! kill -0 "$(cat "$LAB/qemu.pid" 2>/dev/null)" 2>/dev/null; then
    log_info "no lab running; starting one"
    "$ROOT/lab/up.sh" --name "$NAME" --variant "$VARIANT" >&2
fi
# shellcheck disable=SC1091
source "$LAB/lab.env"

mapfile -t SSH_OPTS < <(node_ssh_opts)
# -n everywhere except the one helper that deliberately pipes in a tarball.
# Anything running on the node is pushed as a *file* and executed, never fed
# through `bash -s`: a remote script read from stdin shares that stdin with
# every command it runs, so one command that reads stdin silently swallows the
# rest of the script and the step "succeeds" having done half its work.
node_ssh() {
    ssh -n "${SSH_OPTS[@]}" -i "$NODE_SSH_KEY" -p "$NODE_SSH_PORT" root@127.0.0.1 "$@"
}
node_push() {
    tar cz -C "$1" . | ssh "${SSH_OPTS[@]}" -i "$NODE_SSH_KEY" -p "$NODE_SSH_PORT" \
        root@127.0.0.1 "mkdir -p $2 && tar xz -C $2"
}
node_run_script() {
    # $1: local script path, $2..: arguments
    local script="$1"; shift
    ssh "${SSH_OPTS[@]}" -i "$NODE_SSH_KEY" -p "$NODE_SSH_PORT" root@127.0.0.1 \
        "cat > /root/.lab-step.sh" < "$script"
    node_ssh "bash /root/.lab-step.sh $*"
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
if [[ -n "$SOURCE_DIR" ]]; then
    # The working tree of the thing under test. A profile that finds this can
    # build and install it instead of pulling the last release - otherwise the
    # lab tests whatever was published, which is exactly the code you are not
    # trying to find bugs in.
    log_info "shipping source under test from $SOURCE_DIR"
    node_ssh "rm -rf /root/lab-source && mkdir -p /root/lab-source"
    node_push "$SOURCE_DIR" /root/lab-source
fi

# ── Apply the profile ────────────────────────────────────────────────────────

log_info "applying storage profile: $PROFILE_NAME"
STEP="$(mktemp)"
cat > "$STEP" <<'REMOTE'
set -euo pipefail
export LAB_TEST_CONFIG=/root/lab-test.env
: > "$LAB_TEST_CONFIG"
rm -f /root/.lab-profile-applied
chmod +x /root/lab-profile/*.sh 2>/dev/null || true
# </dev/null so the profile cannot consume anything it should not.
bash /root/lab-profile/setup.sh </dev/null
if [ -f /root/lab-profile/capabilities.env ]; then
    cat /root/lab-profile/capabilities.env >> "$LAB_TEST_CONFIG"
fi
grep -q '^STORAGE_NAME=' "$LAB_TEST_CONFIG" || {
    echo "profile did not write STORAGE_NAME to $LAB_TEST_CONFIG" >&2
    exit 1
}
echo "--- test config ---"
grep -v '^#' "$LAB_TEST_CONFIG" | grep . || true
touch /root/.lab-profile-applied
REMOTE
node_run_script "$STEP"
rm -f "$STEP"
node_ssh "test -f /root/.lab-profile-applied" \
    || die "the storage profile did not apply cleanly"

# ── Run ──────────────────────────────────────────────────────────────────────

RESULTS="$LAB/results"
mkdir -p "$RESULTS"

log_info "running the suite"
STEP="$(mktemp)"
cat > "$STEP" <<REMOTE
set -uo pipefail
cd /root/lab-suite
python3 -m pytest -v -ra --junitxml=/root/lab-suite/junit.xml ${PYTEST_ARGS[*]:-} tests
REMOTE
status=0
node_run_script "$STEP" || status=$?
rm -f "$STEP"

node_ssh "cat /root/lab-suite/junit.xml" > "$RESULTS/junit.xml" 2>/dev/null || true

# Anything reproducing a failure needs to know exactly what it ran against.
node_ssh "pveversion | head -1; uname -r; date -Is; grep -v '^#' /root/lab-test.env | grep ." \
    > "$RESULTS/environment.txt" 2>/dev/null || true

if [[ -x "$PROFILE_DIR/teardown.sh" && $KEEP -eq 0 ]]; then
    log_info "tearing down the profile"
    node_ssh "bash /root/lab-profile/teardown.sh" || log_warn "teardown reported an error"
fi

log_info "results in $RESULTS"
[[ $status -eq 0 ]] && log_info "suite passed" || log_error "suite failed (exit $status)"
exit $status
