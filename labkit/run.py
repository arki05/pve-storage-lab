"""Running the suite against a lab.

Phases can be invoked separately, which is what CI wants: a failing step then
names the thing that broke instead of burying it in one long log. `prepare`
writes a plan into the lab directory and the later phases read it, so they need
no arguments of their own and cannot be handed different ones half way through.

With one profile this tests that storage. With several, the suite runs once per
storage, each with its own capabilities - proving one backend works says
nothing about the others - and `cross` adds the pairwise round trips.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import cluster, images, state
from .lab import connection, read_env
from .lab import up as lab_up
from .profiles import Profile, assign_disks
from .ssh import NodeSSH

log = logging.getLogger("run")
ROOT = Path(__file__).resolve().parent.parent
REMOTE = Path(__file__).resolve().parent / "remote"


class RunError(RuntimeError):
    pass


@dataclass
class Plan:
    name: str
    nodes: int
    cross: bool
    profile_dirs: list[str]
    storages: dict[str, str] = field(default_factory=dict)

    @property
    def profiles(self) -> list[Profile]:
        return [Profile.load(p) for p in self.profile_dirs]

    def save(self) -> None:
        path = state.lab_dir(self.name) / "plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, name: str) -> "Plan":
        path = state.lab_dir(name) / "plan.json"
        if not path.exists():
            raise RunError(f"no plan at {path} - run the prepare phase first")
        return cls(**json.loads(path.read_text()))


def _connections(name: str, count: int) -> list[NodeSSH]:
    return [connection(name, i) for i in range(1, count + 1)]


def prepare(profile_dirs: list[str], name: str = "lab", nodes: int = 1,
            cross: bool = False, disk_size: str = "8G", mem: int = 6144,
            cpus: int = 4) -> Plan:
    profiles = [Profile.load(p) for p in profile_dirs]
    if not profiles:
        raise RunError("at least one profile is required")

    # Bakes chain: a second profile with expensive setup builds on the first
    # one's result, so a lab with two of them pays for each once.
    variant = "base"
    for profile in profiles:
        if profile.has_bake:
            nxt = profile.name if variant == "base" else f"{variant}+{profile.name}"
            variant = images.bake_profile(profile.path, base_variant=variant,
                                          out_variant=nxt)

    total_disks = max(1, sum(p.disks for p in profiles))
    lab_up(name=name, count=nodes, disks=total_disks, disk_size=disk_size,
           mem=mem, cpus=cpus, variant=variant,
           ensure_image=lambda index, v: images.node_image(index, v))
    if nodes > 1:
        cluster.form(name, nodes)

    conns = _connections(name, nodes)
    plan = Plan(name=name, nodes=nodes, cross=cross,
                profile_dirs=[str(p.path) for p in profiles])

    log.info("copying suite to the node")
    conns[0].run("rm -rf /root/lab-suite && mkdir -p /root/lab-suite")
    conns[0].push_dir(ROOT / "suite", "/root/lab-suite")

    disks = assign_disks(profiles)
    for profile in profiles:
        # Every node: a local storage needs its own filesystem on each, and the
        # profile's own guard keeps the cluster-wide `pvesm add` from running
        # twice.
        for conn in conns:
            conn.run(f"rm -rf /root/lab-profile-{profile.name} && "
                     f"mkdir -p /root/lab-profile-{profile.name}")
            conn.push_dir(profile.path, f"/root/lab-profile-{profile.name}")

        if profile.tests:
            log.info("adding %s tests from %s", profile.name, profile.tests)
            conns[0].run(f"mkdir -p /root/lab-tests-{profile.name}")
            conns[0].push_dir(profile.tests, f"/root/lab-tests-{profile.name}")

        # The source under test goes to *every* node. A storage plugin is a
        # Perl module on each node and a patch to each node's pve-container;
        # shipping it to node 1 alone leaves the others running whatever the
        # repository had, and the cluster then behaves differently depending on
        # which node a guest happens to be on.
        if profile.source:
            log.info("shipping %s source under test from %s",
                     profile.name, profile.source)
            for conn in conns:
                conn.run(f"rm -rf /root/lab-source-{profile.name} && "
                         f"mkdir -p /root/lab-source-{profile.name}")
                conn.push_dir(profile.source, f"/root/lab-source-{profile.name}")

        assigned = " ".join(disks[profile.name])
        log.info("applying storage profile: %s (disks: %s)",
                 profile.name, assigned or "none")
        config = f"/root/lab-test-{profile.name}.env"
        for index, conn in enumerate(conns, start=1):
            if nodes > 1:
                log.info("  node%d", index)
            conn.run_script(REMOTE / "apply-profile.sh", profile.name, config,
                            assigned, f"/root/lab-source-{profile.name}",
                            check=False, timeout=5400)
            if not conn.ok(f"test -f /root/.lab-profile-applied-{profile.name}"):
                raise RunError(
                    f"storage profile '{profile.name}' did not apply cleanly "
                    f"on node{index}")

        storage = conns[0].run(
            f"sed -n 's/^STORAGE_NAME=//p' {config} | tail -1").strip()
        plan.storages[profile.name] = storage

    # The cross-storage suite needs to know every storage that exists.
    names = ",".join(plan.storages.values())
    conns[0].run(f"printf 'STORAGES=%s\\n' '{names}' > /root/lab-storages.env")

    plan.save()
    log.info("plan written to %s", state.lab_dir(name) / "plan.json")
    return plan


def run_suites(name: str = "lab", only: str | None = None,
               pytest_args: list[str] | None = None) -> int:
    plan = Plan.load(name)
    conn = connection(name, 1)
    results = state.lab_dir(name) / "results"
    results.mkdir(parents=True, exist_ok=True)
    extra = " ".join(shlex.quote(a) for a in (pytest_args or []))
    status = 0

    # Re-ship the suite: without this a phase invocation tests whatever the
    # node happened to have, which silently hides every change since the lab
    # came up.
    log.info("refreshing the suite on the node")
    conn.run("rm -rf /root/lab-suite && mkdir -p /root/lab-suite")
    conn.push_dir(ROOT / "suite", "/root/lab-suite")

    def one(label: str, config: str, targets: str) -> None:
        nonlocal status
        log.info("running suite: %s", label)
        command = (f"cd /root/lab-suite && LAB_TEST_CONFIG={config} "
                   f"python3 -m pytest -v -ra "
                   f"--junitxml=/root/lab-junit-{label}.xml {extra} {targets}")
        proc = subprocess.run(
            conn._base(False) + [command], text=True, timeout=28800)
        if proc.returncode != 0:
            status = proc.returncode
        junit = conn.run(f"cat /root/lab-junit-{label}.xml", check=False)
        if junit:
            (results / f"junit-{label}.xml").write_text(junit)

    for profile in plan.profiles:
        if only and only != profile.name:
            continue
        if profile.tests:
            conn.run(f"cp -r /root/lab-tests-{profile.name}/. /root/lab-suite/tests/")
        # Cross-storage tests belong to the pairwise pass, not to any one
        # storage's run.
        one(profile.name, f"/root/lab-test-{profile.name}.env",
            "-m 'not crossstorage' tests")

    if plan.cross and (only is None or only == "cross"):
        if len(plan.storages) < 2:
            log.warning("cross needs at least two storages; skipping")
        else:
            first = plan.profiles[0].name
            one("cross", f"/root/lab-test-{first}.env", "-m crossstorage tests")

    return status


def report(name: str = "lab") -> int:
    plan = Plan.load(name)
    conn = connection(name, 1)
    results = state.lab_dir(name) / "results"
    results.mkdir(parents=True, exist_ok=True)

    # Anything reproducing a failure needs to know exactly what it ran against.
    env = conn.run("pveversion | head -1; uname -r; date -Is; "
                   "cat /root/lab-test-*.env", check=False)
    (results / "environment.txt").write_text(env)

    expectations = [str(p.path / "expectations.toml") for p in plan.profiles
                    if (p.path / "expectations.toml").exists()]
    proc = subprocess.run(
        ["python3", str(ROOT / "tools" / "report.py"), str(results), *expectations],
        capture_output=True, text=True)
    (results / "summary.md").write_text(proc.stdout)
    print(proc.stdout)
    if proc.returncode == 0:
        log.info("nothing new broke")
    else:
        log.error("unexpected failures - see %s", results / "summary.md")
    return proc.returncode


def teardown(name: str = "lab") -> None:
    plan = Plan.load(name)
    conns = _connections(name, plan.nodes)
    for profile in plan.profiles:
        if not (profile.path / "teardown.sh").exists():
            continue
        for conn in conns:
            conn.run(f"bash /root/lab-profile-{profile.name}/teardown.sh",
                     check=False)
