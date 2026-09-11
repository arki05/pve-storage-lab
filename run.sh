#!/usr/bin/env bash
# Run the storage suite against a lab node.
#
# Brings a lab up if one is not running, applies one or more storage profiles,
# copies the suite onto the node and runs pytest there. Tests execute on the
# node itself and talk to PVE through pvesh as root, so no credentials exist in
# this repository or in CI.
#
# With one profile this tests that storage. With several, the suite runs once
# per storage - each with its own capabilities - because proving one backend
# works says nothing about the others. `--cross` then adds the pairwise tests:
# move a volume from every storage to every other and back, and check the data
# is unchanged.
#
#   run.sh --profile-dir ./profiles/zfs
#   run.sh --profile-dir ./profiles/zfs --profile-dir ./profiles/lvm-thin --cross
#
# A profile is a directory containing:
#   profile.env        - NAME, DISKS, and optional TESTS / SOURCE paths
#   setup.sh           - runs on the node as root; registers the storage and
#                        appends STORAGE_NAME=... to $LAB_TEST_CONFIG. Its
#                        disks arrive in $LAB_DISKS; it must not go looking for
#                        any others.
#   capabilities.env   - what the storage claims to support; drives skip logic
#   bake.sh            - optional, run once into a derived image
#   teardown.sh        - optional, runs on the node after the suite

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/lib/profile.sh"

ROOT="$(lab_root)"
NAME="${LAB_NAME:-lab}"
PROFILE_DIRS=()
CROSS=0
KEEP=0
PYTEST_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)        NAME="$2";              shift 2 ;;
        --profile-dir) PROFILE_DIRS+=("$2");   shift 2 ;;
        --cross)       CROSS=1;                shift ;;
        --keep)        KEEP=1;                 shift ;;
        --)            shift; PYTEST_ARGS=("$@"); break ;;
        -h|--help)     sed -n '2,27p' "$0"; exit 0 ;;
        *)             PYTEST_ARGS+=("$1");    shift ;;
    esac
done

[[ ${#PROFILE_DIRS[@]} -gt 0 ]] || die "at least one --profile-dir is required"

# ── Resolve the profiles ─────────────────────────────────────────────────────

declare -a P_DIR P_NAME P_DISKS P_TESTS P_SOURCE P_FIRSTDISK
total_disks=0
variant_parts=()

for dir in "${PROFILE_DIRS[@]}"; do
    dir="$(cd "$dir" && pwd)"
    [[ -x "$dir/setup.sh" || -f "$dir/setup.sh" ]] || die "no setup.sh in $dir"
    pname="$(profile_name "$dir")"
    want="$(profile_disks "$dir")"

    P_DIR+=("$dir")
    P_NAME+=("$pname")
    P_DISKS+=("$want")
    P_TESTS+=("$(profile_path "$dir" TESTS)")
    P_SOURCE+=("$(profile_path "$dir" SOURCE)")
    P_FIRSTDISK+=("$((total_disks + 1))")
    total_disks=$((total_disks + want))

    [[ -f "$dir/bake.sh" ]] && variant_parts+=("$pname")
done

# Every profile gets at least one disk's worth of headroom in the lab even if
# it wants none, so a lab is never created with zero.
[[ $total_disks -gt 0 ]] || total_disks=1

# ── Derived images ───────────────────────────────────────────────────────────
#
# Profiles with expensive setup bake it into an image. Several such profiles
# chain: each bakes onto the previous one's result, so a lab with bcachefs and
# zfs pays for each once rather than on every run.

VARIANT=base
for i in "${!P_DIR[@]}"; do
    [[ -f "${P_DIR[$i]}/bake.sh" ]] || continue
    next="${VARIANT}+${P_NAME[$i]}"
    [[ "$VARIANT" == base ]] && next="${P_NAME[$i]}"
    "$ROOT/lab/bake-profile.sh" --profile-dir "${P_DIR[$i]}" \
        --base "$VARIANT" --variant "$next" >&2
    VARIANT="$next"
done

# ── Lab ──────────────────────────────────────────────────────────────────────

STATE="$(lab_state_dir)"
LAB="$STATE/labs/$NAME"

if [[ ! -f "$LAB/qemu.pid" ]] || ! kill -0 "$(cat "$LAB/qemu.pid" 2>/dev/null)" 2>/dev/null; then
    log_info "no lab running; starting one ($total_disks test disks)"
    "$ROOT/lab/up.sh" --name "$NAME" --variant "$VARIANT" --disks "$total_disks" >&2
fi
# shellcheck disable=SC1091
source "$LAB/lab.env"

mapfile -t SSH_OPTS < <(node_ssh_opts)
# -n everywhere except the helpers that deliberately pipe something in.
# Anything running on the node is pushed as a file and executed, never fed
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
    local script="$1"; shift
    ssh "${SSH_OPTS[@]}" -i "$NODE_SSH_KEY" -p "$NODE_SSH_PORT" root@127.0.0.1 \
        "cat > /root/.lab-step.sh" < "$script"
    node_ssh "bash /root/.lab-step.sh $*"
}

# ── Ship everything ──────────────────────────────────────────────────────────

log_info "copying suite to the node"
node_ssh "rm -rf /root/lab-suite && mkdir -p /root/lab-suite"
node_push "$ROOT/suite" /root/lab-suite

for i in "${!P_DIR[@]}"; do
    pname="${P_NAME[$i]}"
    node_ssh "rm -rf /root/lab-profile-$pname && mkdir -p /root/lab-profile-$pname"
    node_push "${P_DIR[$i]}" "/root/lab-profile-$pname"
    if [[ -n "${P_TESTS[$i]}" ]]; then
        log_info "adding $pname tests from ${P_TESTS[$i]}"
        node_ssh "mkdir -p /root/lab-tests-$pname"
        node_push "${P_TESTS[$i]}" "/root/lab-tests-$pname"
    fi
    if [[ -n "${P_SOURCE[$i]}" ]]; then
        log_info "shipping $pname source under test from ${P_SOURCE[$i]}"
        node_ssh "rm -rf /root/lab-source-$pname && mkdir -p /root/lab-source-$pname"
        node_push "${P_SOURCE[$i]}" "/root/lab-source-$pname"
    fi
done

# ── Apply the profiles ───────────────────────────────────────────────────────

STORAGES=()
for i in "${!P_DIR[@]}"; do
    pname="${P_NAME[$i]}"
    first="${P_FIRSTDISK[$i]}"
    want="${P_DISKS[$i]}"
    disks=""
    for ((d = first; d < first + want; d++)); do
        disks+="/dev/disk/by-id/virtio-labdisk$d "
    done

    log_info "applying storage profile: $pname (disks: ${disks:-none})"
    STEP="$(mktemp)"
    cat > "$STEP" <<REMOTE
set -euo pipefail
export LAB_TEST_CONFIG=/root/lab-test-$pname.env
export LAB_DISKS="${disks% }"
export LAB_SOURCE=/root/lab-source-$pname
: > "\$LAB_TEST_CONFIG"
rm -f /root/.lab-profile-applied-$pname
chmod +x /root/lab-profile-$pname/*.sh 2>/dev/null || true
bash /root/lab-profile-$pname/setup.sh </dev/null
if [ -f /root/lab-profile-$pname/capabilities.env ]; then
    cat /root/lab-profile-$pname/capabilities.env >> "\$LAB_TEST_CONFIG"
fi
grep -q '^STORAGE_NAME=' "\$LAB_TEST_CONFIG" || {
    echo "profile $pname did not write STORAGE_NAME" >&2
    exit 1
}
echo "--- $pname ---"
grep -v '^#' "\$LAB_TEST_CONFIG" | grep . || true
touch /root/.lab-profile-applied-$pname
REMOTE
    node_run_script "$STEP"
    rm -f "$STEP"
    node_ssh "test -f /root/.lab-profile-applied-$pname" \
        || die "storage profile '$pname' did not apply cleanly"

    STORAGES+=("$(node_ssh "sed -n 's/^STORAGE_NAME=//p' /root/lab-test-$pname.env | tail -1")")
done

# The cross-storage suite needs to know every storage that exists.
node_ssh "printf 'STORAGES=%s\n' '$(IFS=,; echo "${STORAGES[*]}")' > /root/lab-storages.env"

# ── Run ──────────────────────────────────────────────────────────────────────

RESULTS="$LAB/results"
rm -rf "$RESULTS"; mkdir -p "$RESULTS"
status=0

# Shell-quoted, because these are interpolated into a script that runs on the
# node: an unquoted `-k "a or b"` arrives as three separate arguments and
# pytest tries to collect a file called "or".
EXTRA_ARGS=""
[[ ${#PYTEST_ARGS[@]} -gt 0 ]] && EXTRA_ARGS="$(printf '%q ' "${PYTEST_ARGS[@]}")"

run_suite() {
    # $1 label, $2 config file on the node, $3.. pytest target paths
    local label="$1" config="$2"; shift 2
    log_info "running suite: $label"
    local step; step="$(mktemp)"
    cat > "$step" <<REMOTE
set -uo pipefail
cd /root/lab-suite
export LAB_TEST_CONFIG=$config
python3 -m pytest -v -ra \\
    --junitxml=/root/lab-junit-$label.xml \\
    $EXTRA_ARGS $*
REMOTE
    local rc=0
    node_run_script "$step" || rc=$?
    rm -f "$step"
    node_ssh "cat /root/lab-junit-$label.xml" > "$RESULTS/junit-$label.xml" 2>/dev/null || true
    [[ $rc -eq 0 ]] || status=$rc
    return 0
}

for i in "${!P_DIR[@]}"; do
    pname="${P_NAME[$i]}"
    targets="tests"
    if [[ -n "${P_TESTS[$i]}" ]]; then
        node_ssh "cp -r /root/lab-tests-$pname/. /root/lab-suite/tests/"
    fi
    run_suite "$pname" "/root/lab-test-$pname.env" "$targets"
done

if [[ $CROSS -eq 1 ]]; then
    if [[ ${#STORAGES[@]} -lt 2 ]]; then
        log_warn "--cross needs at least two storages; skipping"
    else
        run_suite "cross" "/root/lab-test-${P_NAME[0]}.env" "-m crossstorage tests"
    fi
fi

# ── Collect ──────────────────────────────────────────────────────────────────
#
# Anything reproducing a failure needs to know exactly what it ran against.
node_ssh "pveversion | head -1; uname -r; date -Is; cat /root/lab-test-*.env" \
    > "$RESULTS/environment.txt" 2>/dev/null || true
cp "$LAB/console.log" "$RESULTS/console.log" 2>/dev/null || true

if [[ $KEEP -eq 0 ]]; then
    for i in "${!P_DIR[@]}"; do
        pname="${P_NAME[$i]}"
        [[ -f "${P_DIR[$i]}/teardown.sh" ]] || continue
        node_ssh "bash /root/lab-profile-$pname/teardown.sh" >/dev/null 2>&1 \
            || log_warn "teardown for $pname reported an error"
    done
fi

# ── Verdict ──────────────────────────────────────────────────────────────────
#
# Not the raw pytest status. A backend with known defects fails the same tests
# every run, and a suite that is always red tells you nothing the day something
# new breaks. report.py separates unexpected failures from declared ones, and
# flags any declared one that has started passing.
EXPECT_ARGS=()
for dir in "${P_DIR[@]}"; do
    [[ -f "$dir/expectations.toml" ]] && EXPECT_ARGS+=("$dir/expectations.toml")
done

log_info "results in $RESULTS"
verdict=0
python3 "$ROOT/tools/report.py" "$RESULTS" "${EXPECT_ARGS[@]}" \
    > "$RESULTS/summary.md" || verdict=$?
cat "$RESULTS/summary.md"

if [[ $verdict -eq 0 ]]; then
    log_info "nothing new broke"
else
    log_error "unexpected failures - see $RESULTS/summary.md"
fi
exit $verdict
