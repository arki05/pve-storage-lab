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
# Phases can be run separately, which is what CI wants: a failing step then
# names the thing that broke instead of burying it in one long log. `prepare`
# writes a plan into the lab directory that the later phases read, so they do
# not need the profile arguments again.
#
#   run.sh --profile-dir ./profiles/zfs --phase prepare
#   run.sh --phase suite --only zfs
#   run.sh --phase report
#   run.sh --phase teardown
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
PHASE=all
ONLY=""
NODES="${LAB_NODES:-1}"
PYTEST_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)        NAME="$2";              shift 2 ;;
        --profile-dir) PROFILE_DIRS+=("$2");   shift 2 ;;
        --cross)       CROSS=1;                shift ;;
        --keep)        KEEP=1;                 shift ;;
        --nodes)       NODES="$2";             shift 2 ;;
        --phase)       PHASE="$2";             shift 2 ;;
        --only)        ONLY="$2";              shift 2 ;;
        --)            shift; PYTEST_ARGS=("$@"); break ;;
        -h|--help)     sed -n '2,27p' "$0"; exit 0 ;;
        *)             PYTEST_ARGS+=("$1");    shift ;;
    esac
done

case "$PHASE" in
    all|prepare|suite|report|teardown) ;;
    *) die "unknown phase: $PHASE (all, prepare, suite, report, teardown)" ;;
esac

STATE="$(lab_state_dir)"
LAB="$STATE/labs/$NAME"
PLAN="$LAB/plan.env"
RESULTS="$LAB/results"

# Phases after prepare read the plan rather than re-deriving it, so a caller
# does not have to repeat the profile arguments - and cannot accidentally pass
# different ones half way through a run.
if [[ "$PHASE" == all || "$PHASE" == prepare ]]; then
    [[ ${#PROFILE_DIRS[@]} -gt 0 ]] || die "at least one --profile-dir is required"
else
    [[ -f "$PLAN" ]] || die "no plan at $PLAN - run --phase prepare first"
    # shellcheck disable=SC1090
    source "$PLAN"
    read -r -a PROFILE_DIRS <<< "$PLAN_PROFILE_DIRS"
    [[ $CROSS -eq 1 ]] || CROSS="$PLAN_CROSS"
fi

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

run_prepare=0; run_suites=0; run_report=0; run_teardown=0
case "$PHASE" in
    all)      run_prepare=1; run_suites=1; run_report=1; run_teardown=1 ;;
    prepare)  run_prepare=1 ;;
    suite)    run_suites=1 ;;
    report)   run_report=1 ;;
    teardown) run_teardown=1 ;;
esac

# ── Derived images ───────────────────────────────────────────────────────────
#
# Profiles with expensive setup bake it into an image. Several such profiles
# chain: each bakes onto the previous one's result, so a lab with bcachefs and
# zfs pays for each once rather than on every run.

VARIANT=base
for i in "${!P_DIR[@]}"; do
    [[ $run_prepare -eq 1 ]] || break
    [[ -f "${P_DIR[$i]}/bake.sh" ]] || continue
    next="${VARIANT}+${P_NAME[$i]}"
    [[ "$VARIANT" == base ]] && next="${P_NAME[$i]}"
    "$ROOT/lab/bake-profile.sh" --profile-dir "${P_DIR[$i]}" \
        --base "$VARIANT" --variant "$next" >&2
    VARIANT="$next"
done

# ── Lab ──────────────────────────────────────────────────────────────────────

if [[ $run_prepare -eq 1 ]]; then
    # node1's pidfile: a multi-node lab keeps each node in its own directory,
    # and checking the old single-node path made this think the lab was always
    # down and start it again on every phase.
    node1_pid="$LAB/node1/qemu.pid"
    [[ -f "$node1_pid" ]] || node1_pid="$LAB/qemu.pid"
    if [[ ! -f "$node1_pid" ]] || ! kill -0 "$(cat "$node1_pid" 2>/dev/null)" 2>/dev/null; then
        log_info "no lab running; starting one ($total_disks test disks, $NODES node(s))"
        "$ROOT/lab/up.sh" --name "$NAME" --variant "$VARIANT" \
            --disks "$total_disks" --nodes "$NODES" >&2
    fi
fi
[[ -f "$LAB/lab.env" ]] || die "lab '$NAME' is not up"
# shellcheck disable=SC1091
source "$LAB/lab.env"

mapfile -t SSH_OPTS < <(node_ssh_opts)
# -n everywhere except the helpers that deliberately pipe something in.
# Anything running on the node is pushed as a file and executed, never fed
# through `bash -s`: a remote script read from stdin shares that stdin with
# every command it runs, so one command that reads stdin silently swallows the
# rest of the script and the step "succeeds" having done half its work.
NODE_COUNT="${LAB_NODE_COUNT:-1}"
node_port() {
    local var="NODE${1}_SSH_PORT"
    echo "${!var:-$NODE_SSH_PORT}"
}
# The *_at helpers take a node index. The plain ones mean node 1, which is
# where the suite runs and where anything cluster-wide only needs doing once.
node_ssh_at() {
    local i="$1"; shift
    ssh -n "${SSH_OPTS[@]}" -i "$NODE_SSH_KEY" -p "$(node_port "$i")" root@127.0.0.1 "$@"
}
node_push_at() {
    local i="$1"
    tar cz -C "$2" . | ssh "${SSH_OPTS[@]}" -i "$NODE_SSH_KEY" -p "$(node_port "$i")" \
        root@127.0.0.1 "mkdir -p $3 && tar xz -C $3"
}
node_run_script_at() {
    local i="$1" script="$2"; shift 2
    ssh "${SSH_OPTS[@]}" -i "$NODE_SSH_KEY" -p "$(node_port "$i")" root@127.0.0.1 \
        "cat > /root/.lab-step.sh" < "$script"
    node_ssh_at "$i" "bash /root/.lab-step.sh $*"
}
node_ssh()        { node_ssh_at 1 "$@"; }
node_push()       { node_push_at 1 "$@"; }
node_run_script() { node_run_script_at 1 "$@"; }

# ── Ship everything ──────────────────────────────────────────────────────────

if [[ $run_prepare -eq 1 ]]; then
log_info "copying suite to the node"
node_ssh "rm -rf /root/lab-suite && mkdir -p /root/lab-suite"
node_push "$ROOT/suite" /root/lab-suite

for i in "${!P_DIR[@]}"; do
    pname="${P_NAME[$i]}"
    # Every node: a local storage needs its own filesystem on each, and the
    # profile's own guard keeps the cluster-wide `pvesm add` from running twice.
    for ((n = 1; n <= NODE_COUNT; n++)); do
        node_ssh_at "$n" "rm -rf /root/lab-profile-$pname && mkdir -p /root/lab-profile-$pname"
        node_push_at "$n" "${P_DIR[$i]}" "/root/lab-profile-$pname"
    done
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

fi   # run_prepare: ship

# ── Apply the profiles ───────────────────────────────────────────────────────

STORAGES=()
if [[ $run_prepare -eq 1 ]]; then
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
    for ((n = 1; n <= NODE_COUNT; n++)); do
        [[ $NODE_COUNT -gt 1 ]] && log_info "  node$n"
        node_run_script_at "$n" "$STEP"
        node_ssh_at "$n" "test -f /root/.lab-profile-applied-$pname" \
            || die "storage profile '$pname' did not apply cleanly on node$n"
    done
    rm -f "$STEP"

    STORAGES+=("$(node_ssh "sed -n 's/^STORAGE_NAME=//p' /root/lab-test-$pname.env | tail -1")")
done

# The cross-storage suite needs to know every storage that exists.
node_ssh "printf 'STORAGES=%s\n' '$(IFS=,; echo "${STORAGES[*]}")' > /root/lab-storages.env"

# Record the plan so the later phases need no arguments of their own.
mkdir -p "$LAB"
{
    printf 'PLAN_PROFILE_DIRS=%q\n' "${P_DIR[*]}"
    printf 'PLAN_CROSS=%s\n' "$CROSS"
    printf 'PLAN_STORAGES=%q\n' "${STORAGES[*]}"
} > "$PLAN"
log_info "plan written to $PLAN"
fi   # run_prepare: apply

# ── Run ──────────────────────────────────────────────────────────────────────

mkdir -p "$RESULTS"
# Only a whole-run invocation clears previous results. A per-suite phase must
# add to them, or the report at the end would only ever see the last suite.
[[ "$PHASE" == all ]] && { rm -rf "$RESULTS"; mkdir -p "$RESULTS"; }
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

if [[ $run_suites -eq 1 ]]; then
    # Re-ship the suite even when prepare is not running. Without this a phase
    # invocation tests whatever the node happened to have, which silently hides
    # every change made since the lab came up.
    if [[ $run_prepare -eq 0 ]]; then
        log_info "refreshing the suite on the node"
        node_ssh "rm -rf /root/lab-suite && mkdir -p /root/lab-suite"
        node_push "$ROOT/suite" /root/lab-suite
    fi
    for i in "${!P_DIR[@]}"; do
        pname="${P_NAME[$i]}"
        # --only picks one suite out of the plan, so CI can give each storage
        # its own step and name the one that failed.
        [[ -z "$ONLY" || "$ONLY" == "$pname" ]] || continue
        if [[ -n "${P_TESTS[$i]}" ]]; then
            node_ssh "cp -r /root/lab-tests-$pname/. /root/lab-suite/tests/"
        fi
        run_suite "$pname" "/root/lab-test-$pname.env" "tests"
    done

    if [[ $CROSS -eq 1 ]] && [[ -z "$ONLY" || "$ONLY" == cross ]]; then
        read -r -a planned <<< "${PLAN_STORAGES:-${STORAGES[*]}}"
        if [[ ${#planned[@]} -lt 2 ]]; then
            log_warn "--cross needs at least two storages; skipping"
        else
            run_suite "cross" "/root/lab-test-${P_NAME[0]}.env" "-m crossstorage tests"
        fi
    fi
fi

# ── Collect ──────────────────────────────────────────────────────────────────
#
# Anything reproducing a failure needs to know exactly what it ran against.
if [[ $run_report -eq 1 ]]; then
node_ssh "pveversion | head -1; uname -r; date -Is; cat /root/lab-test-*.env" \
    > "$RESULTS/environment.txt" 2>/dev/null || true
cp "$LAB/console.log" "$RESULTS/console.log" 2>/dev/null || true
fi

if [[ $run_teardown -eq 1 && $KEEP -eq 0 ]]; then
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
if [[ $run_report -eq 1 ]]; then
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
fi

# A phase that only ran suites reports pytest's own status; the verdict comes
# later, from the report phase, once every suite's results are in.
exit $status
