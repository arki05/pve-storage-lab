"""Backup and restore, including snapshot-mode backup of a running guest."""

import time

from conftest import needs_backup, needs_lxc, needs_images, needs_snapshots, LabVM, LabCT
from helpers.data_guard import DataGuard
from helpers.wait import wait_for_task

pytestmark = needs_backup

BACKUP_STORAGE = "local"


def _newest_backup(pve, vmid: int) -> str:
    dumps = [c for c in pve.storage_content(BACKUP_STORAGE, content="backup")
             if c.get("vmid") == vmid]
    assert dumps, f"no backup found for {vmid}"
    return sorted(dumps, key=lambda c: c.get("ctime", 0))[-1]["volid"]


@needs_images
class TestVMBackup:
    def test_backup_restore_roundtrip(self, create_vm, pve, node, storage):
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        guard = DataGuard(vm.agent()).seed()
        vm.shutdown()

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/vzdump", vmid=vm.vmid, storage=BACKUP_STORAGE,
            mode="stop", compress="zstd", remove=0,
        ), timeout=1800)
        archive = _newest_backup(pve, vm.vmid)

        restore_id = pve.nextid()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu", vmid=restore_id, archive=archive,
            storage=storage, force=1,
        ), timeout=1800)
        restored = LabVM(pve, node, restore_id)
        try:
            restored.start()
            restored.wait_agent()
            guard.guest = restored.agent()
            guard.verify("after restore")
        finally:
            restored.destroy()

    @needs_snapshots
    def test_snapshot_mode_backup_of_a_running_vm(self, create_vm, pve, node):
        """Snapshot mode must not stop the guest and must not corrupt it."""
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        DataGuard(vm.agent()).seed()

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/vzdump", vmid=vm.vmid, storage=BACKUP_STORAGE,
            mode="snapshot", compress="zstd", remove=0,
        ), timeout=1800)

        assert vm.status() == "running", "snapshot-mode backup stopped the guest"
        _newest_backup(pve, vm.vmid)


@needs_lxc
class TestCTBackup:
    def test_backup_restore_roundtrip(self, create_ct, pve, node, storage):
        ct = create_ct(start=True)
        guard = DataGuard(ct.exec()).seed()
        ct.stop()

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/vzdump", vmid=ct.vmid, storage=BACKUP_STORAGE,
            mode="stop", compress="zstd", remove=0,
        ), timeout=1800)
        archive = _newest_backup(pve, ct.vmid)

        restore_id = pve.nextid()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/lxc", vmid=restore_id, ostemplate=archive,
            storage=storage, restore=1, force=1, password="pvelab",
        ), timeout=1800)
        restored = LabCT(pve, node, restore_id)
        try:
            restored.start()
            guard.guest = restored.exec()
            guard.verify("after restore")
        finally:
            restored.destroy()

    @needs_snapshots
    def test_snapshot_mode_backup_of_a_running_ct(self, create_ct, pve, node):
        """The path that needs a snapshot of a *mounted* volume - historically
        the weakest spot for path-backed container storage."""
        ct = create_ct(start=True)
        DataGuard(ct.exec()).seed()

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/vzdump", vmid=ct.vmid, storage=BACKUP_STORAGE,
            mode="snapshot", compress="zstd", remove=0,
        ), timeout=1800)

        assert ct.status() == "running", "snapshot-mode backup stopped the container"
        _newest_backup(pve, ct.vmid)
