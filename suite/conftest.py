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


def cap(name: str) -> bool:
    return CONFIG.get(name, "false").strip().lower() in ("1", "true", "yes")


needs_snapshots = pytest.mark.skipif(
    not cap("SUPPORTS_SNAPSHOTS"), reason="storage does not support snapshots")
needs_linked_clone = pytest.mark.skipif(
    not cap("SUPPORTS_LINKED_CLONE"), reason="storage does not support linked clones")
needs_backup = pytest.mark.skipif(
    not cap("SUPPORTS_BACKUP"), reason="storage does not support backups")
needs_lxc = pytest.mark.skipif(
    not cap("SUPPORTS_LXC"), reason="storage does not support containers")
needs_images = pytest.mark.skipif(
    not cap("SUPPORTS_IMAGES"), reason="storage does not support VM images")
# Not every backend expresses a volume's size as a filesystem the guest can
# see. A quota-backed container rootfs is a directory on a much larger
# filesystem: the limit is real and enforced, but `df` inside the guest reports
# the whole filesystem, so a resize is invisible from in there.
# Size enforcement is universal, not a bcachefs concern: lvm-thin enforces with
# the LV size, ZFS with a refquota, a directory storage with the size of the raw
# image, bcachefs with a project quota. A backend that hands out a volume whose
# stated size is not a limit is a backend that will silently fill its pool.
needs_size_enforcement = pytest.mark.skipif(
    not cap("ENFORCES_VOLUME_SIZE"),
    reason="storage does not enforce volume sizes")
needs_tpm = pytest.mark.skipif(
    not cap("SUPPORTS_TPM"), reason="storage does not support TPM state volumes")
needs_guest_visible_size = pytest.mark.skipif(
    not cap("RESIZE_VISIBLE_IN_GUEST"),
    reason="volume size is not visible to the guest on this backend")


# ── Session fixtures ─────────────────────────────────────────────────────────


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
                full: bool = True) -> LabVM:
        vmid = pve.nextid()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm_template}/clone",
            newid=vmid, name=f"{name}-{vmid}", full=full, storage=storage,
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
                start: bool = False, **extra) -> LabCT:
        vmid = pve.nextid()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/lxc",
            vmid=vmid, hostname=f"{name}-{vmid}", ostemplate=ct_template,
            storage=storage, rootfs=f"{storage}:{disk_gb}", memory=memory,
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
