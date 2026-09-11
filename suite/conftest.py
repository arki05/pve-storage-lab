"""Fixtures for the generic storage suite.

The suite is backend-agnostic: it is told which storage to exercise and what
that storage claims to support, and everything else is discovered. A plugin
under test supplies a profile that registers the storage and declares its
capabilities; nothing here knows about any particular backend.
"""

import os
import time
from pathlib import Path

import pytest

from helpers.pve import PVE, PVEError
from helpers.guest import GuestAgent, ContainerExec
from helpers.wait import wait_for, wait_for_task

CONFIG_FILE = Path(os.environ.get("LAB_TEST_CONFIG", "/root/lab-test.env"))


def _load_config() -> dict[str, str]:
    config: dict[str, str] = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            config[key.strip()] = value.strip()
    # The environment wins, so a single test can be re-run against a
    # different storage without rewriting the file.
    config.update({k: v for k, v in os.environ.items() if k in config})
    return config


CONFIG = _load_config()


def pytest_configure(config):
    # The parameter has to be called `config`: pluggy matches hook arguments by
    # name, and anything else fails validation at collection time.
    for marker, description in CAPABILITY_MARKERS.items():
        config.addinivalue_line("markers", f"{marker}: {description}")


def cap(name: str) -> bool:
    """What a profile claims. Read for reporting, not for skipping."""
    return CONFIG.get(name, "false").strip().lower() in ("1", "true", "yes")


# Capabilities are *tags*, not gates.
#
# They used to be skipif conditions, and that had a failure mode worse than any
# bug they hid: a capability wrongly declared false meant the tests for it were
# never run, and nothing said so. Linked clones went untested on bcachefs for
# exactly that reason - the plugin supported them all along.
#
# So every test runs against every backend. A backend that genuinely cannot do
# something declares it in expectations.toml, where the failure is reported and
# does not block - and where a declaration that stops being true is flagged
# loudly. "We expected this to fail and it passed" is a finding worth having,
# and a skip can never produce one.
#
# Tags remain useful for selection: `-m "not vmstate"` skips the slow ones
# deliberately, which is a choice rather than an accident.
needs_snapshots = pytest.mark.snapshots
needs_linked_clone = pytest.mark.linked_clone
needs_backup = pytest.mark.backup
needs_lxc = pytest.mark.lxc
needs_images = pytest.mark.images
needs_size_enforcement = pytest.mark.size_enforcement
needs_rollback_past_newer = pytest.mark.rollback_past_newer
needs_vmstate = pytest.mark.vmstate
needs_tpm = pytest.mark.tpm
needs_guest_visible_vm_size = pytest.mark.vm_resize_visible
needs_guest_visible_ct_size = pytest.mark.ct_resize_visible
needs_snapshot_migration = pytest.mark.snapshot_migration

CAPABILITY_MARKERS = {
    "snapshots": "takes and rolls back snapshots",
    "linked_clone": "clones that share the base rather than copying it",
    "backup": "vzdump and restore",
    "lxc": "container volumes",
    "images": "VM disk images",
    "size_enforcement": "a volume's stated size is a limit",
    "rollback_past_newer": "rolling back past a newer snapshot, keeping it",
    "vmstate": "snapshots that include guest memory",
    "tpm": "TPM state volumes",
    "vm_resize_visible": "a VM disk resize is visible inside the guest",
    "ct_resize_visible": "a container rootfs resize is visible inside it",
    "crossstorage": "moves volumes between two storages",
    "snapshot_migration": "migrating a guest that has snapshots",
}


# ── Session fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _record_markers(request, record_property):
    """Put each test's markers into the junit output.

    Expectations then declare a limitation by capability - `marker =
    "vmstate"` - rather than by matching test names, which drift the moment a
    test is renamed or split.
    """
    names = sorted(m.name for m in request.node.iter_markers()
                   if m.name in CAPABILITY_MARKERS)
    if names:
        record_property("markers", ",".join(names))


@pytest.fixture(scope="session")
def config() -> dict[str, str]:
    return CONFIG


@pytest.fixture(scope="session")
def pve() -> PVE:
    return PVE()


@pytest.fixture(scope="session")
def node(pve) -> str:
    return pve.node()


@pytest.fixture(scope="session")
def nodes(pve) -> list[str]:
    return sorted(entry["node"] for entry in pve.get("/nodes"))


@pytest.fixture(scope="session")
def node2(pve) -> str:
    """A node other than the one the tests run on, for migration.

    The lab is single-node by default - these backends are local, so a second
    node costs a full data copy and adds little - but the tests exist so a
    two-node lab covers them immediately.
    """
    other = pve.other_node()
    if other is None:
        pytest.skip("migration needs a second node; this lab has one")
    return other


def _storage_list() -> list[str]:
    """Every storage registered in this lab, in the order the profiles ran.

    Written by run.sh rather than discovered from PVE: the lab knows which
    storages it registered, and `pvesm status` would also return the built-in
    ones that no profile owns.
    """
    path = Path("/root/lab-storages.env")
    if not path.exists():
        return []
    for line in path.read_text().splitlines():
        if line.startswith("STORAGES="):
            return [s for s in line.partition("=")[2].strip().split(",") if s]
    return []


@pytest.fixture(scope="session")
def storages() -> list[str]:
    found = _storage_list()
    if len(found) < 2:
        pytest.skip("cross-storage tests need at least two storages")
    return found


def storage_pairs() -> list[tuple[str, str]]:
    """Ordered pairs, deduplicated only by a != b.

    Direction is not a duplicate. A round trip starting on zfs
    (zfs -> btrfs -> zfs) and one starting on btrfs (btrfs -> zfs -> btrfs)
    exercise different code on both ends: each backend writes a volume in one
    and reads a foreign one in the other, and a backend that reads correctly
    may still write its own wrongly.

    So both orders are kept, and nothing beyond that is generated: N storages
    give N*(N-1) round trips, each appearing exactly once.
    """
    found = _storage_list()
    return [(a, b) for a in found for b in found if a != b]


@pytest.fixture(scope="session")
def storage(config) -> str:
    name = config.get("STORAGE_NAME")
    if not name:
        pytest.skip("no STORAGE_NAME configured - was a profile applied?")
    return name


@pytest.fixture(scope="session")
def vm_template(pve, node, config) -> int:
    """The VM template baked into the lab image."""
    vmid = int(config.get("GUEST_TEMPLATE_VMID", 900))
    try:
        cfg = pve.get(f"/nodes/{node}/qemu/{vmid}/config")
    except PVEError:
        pytest.skip(f"VM template {vmid} is missing from the lab image")
    if not cfg.get("template"):
        pytest.skip(f"VMID {vmid} exists but is not a template")
    return vmid


@pytest.fixture(scope="session")
def ct_template(pve, node) -> str:
    """The LXC template baked into the lab image."""
    for item in pve.storage_content("local", content="vztmpl"):
        if "debian-13" in item.get("volid", ""):
            return item["volid"]
    pytest.skip("no Debian 13 LXC template in the lab image")


# ── Guest lifecycle ──────────────────────────────────────────────────────────
#
# Named Lab* rather than Test*: pytest tries to collect any class named Test*
# and warns that it cannot, because these take constructor arguments.


class LabGuest:
    kind = ""

    def __init__(self, pve: PVE, node: str, vmid: int):
        self.pve = pve
        self.node = node
        self.vmid = vmid

    @property
    def _base(self) -> str:
        return f"/nodes/{self.node}/{self.kind}/{self.vmid}"

    def status(self) -> str:
        return self.pve.get(f"{self._base}/status/current").get("status", "unknown")

    def config(self) -> dict:
        return self.pve.get(f"{self._base}/config")

    def start(self, timeout: int = 120) -> None:
        wait_for_task(self.pve, self.pve.create(f"{self._base}/status/start"), timeout)
        wait_for(lambda: self.status() == "running", timeout=timeout,
                 desc=f"{self.kind} {self.vmid} to start")

    def stop(self, timeout: int = 90) -> None:
        wait_for_task(self.pve, self.pve.create(f"{self._base}/status/stop"), timeout)
        wait_for(lambda: self.status() == "stopped", timeout=timeout,
                 desc=f"{self.kind} {self.vmid} to stop")

    def destroy(self) -> None:
        # A rollback or a migration releases the config lock a moment after
        # its task ends, and stop refuses while it is held - so the stop was
        # swallowed and delete then failed with "is running".
        try:
            wait_for(lambda: not self.config().get("lock"), timeout=120,
                     desc=f"{self.kind} {self.vmid} lock to clear before destroy")
        except Exception:                      # noqa: BLE001 - best effort
            pass
        try:
            if self.status() == "running":
                self.stop()
        except Exception:                      # noqa: BLE001
            pass
        time.sleep(1)
        error = None
        try:
            wait_for_task(self.pve, self.pve.delete(self._base, purge=1), 180)
        except Exception as exc:               # noqa: BLE001
            error = exc
        self.assert_gone(error)

    def assert_gone(self, error=None) -> None:
        """Config presence is the source of truth. A destroy that leaves the
        volume behind is a backend bug that otherwise surfaces much later, as
        the *next* test failing to allocate the VMID it was just handed."""
        try:
            self.pve.get(f"{self._base}/status/current")
        except PVEError:
            return
        suffix = f" (destroy raised: {error})" if error else ""
        raise AssertionError(f"{self.kind} {self.vmid} still exists after destroy{suffix}")


class LabVM(LabGuest):
    kind = "qemu"

    def agent(self) -> GuestAgent:
        # Bound to the node the guest is on right now, which a migration
        # changes underneath the test.
        return GuestAgent(self.vmid, self.node)

    def wait_agent(self, timeout: int = 240) -> None:
        def ping() -> bool:
            self.pve.create(f"{self._base}/agent/ping")
            return True
        wait_for(ping, timeout=timeout, interval=2,
                 desc=f"guest agent on VM {self.vmid}")

    def shutdown(self, timeout: int = 120) -> None:
        """Graceful, so the guest flushes its cache. Falls back to a hard stop."""
        try:
            wait_for_task(self.pve, self.pve.create(f"{self._base}/status/shutdown"),
                          timeout)
        except Exception:                      # noqa: BLE001
            self.pve.create(f"{self._base}/status/stop")
        wait_for(lambda: self.status() == "stopped", timeout=timeout,
                 desc=f"VM {self.vmid} to shut down")


class LabCT(LabGuest):
    kind = "lxc"

    def exec(self) -> ContainerExec:
        return ContainerExec(self.vmid, self.node)


# ── Factories ────────────────────────────────────────────────────────────────


@pytest.fixture
def create_vm(pve, node, storage, vm_template):
    created: list[LabVM] = []

    def _create(name: str = "lab-vm", memory: int = 1024, cores: int = 2,
                full: bool = True, on: str | None = None) -> LabVM:
        # `on` selects a storage other than the one under test, which is what
        # the cross-storage tests need.
        target = on or storage
        vmid = pve.nextid()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm_template}/clone",
            newid=vmid, name=f"{name}-{vmid}", full=full, storage=target,
        ), timeout=600)
        pve.set(f"/nodes/{node}/qemu/{vmid}/config", memory=memory, cores=cores)
        vm = LabVM(pve, node, vmid)
        created.append(vm)
        return vm

    yield _create
    for vm in created:
        vm.destroy()


@pytest.fixture
def create_ct(pve, node, storage, ct_template):
    created: list[LabCT] = []

    def _create(name: str = "lab-ct", memory: int = 512, disk_gb: int = 2,
                start: bool = False, on: str | None = None, **extra) -> LabCT:
        target = on or storage
        vmid = pve.nextid()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/lxc",
            vmid=vmid, hostname=f"{name}-{vmid}", ostemplate=ct_template,
            storage=target, rootfs=f"{target}:{disk_gb}", memory=memory,
            cores=1, net0="name=eth0,bridge=vmbr0,ip=dhcp", password="pvelab",
            unprivileged=1, start=0, **extra,
        ), timeout=600)
        ct = LabCT(pve, node, vmid)
        created.append(ct)
        if start:
            ct.start()
        return ct

    yield _create
    for ct in created:
        ct.destroy()
