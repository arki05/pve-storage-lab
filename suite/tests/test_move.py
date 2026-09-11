"""Moving a volume between storages.

This is the path that reads every extended attribute and re-creates the volume
elsewhere, so it is where backends that store metadata out-of-band tend to
fail - and it fails late, after copying gigabytes.
"""

import pytest

from conftest import needs_lxc, needs_images
from helpers.data_guard import DataGuard
from helpers.volumes import assert_relocated
from helpers.wait import wait_for_task

OTHER_STORAGE = "local"


@pytest.fixture(autouse=True)
def _move_target(pve, node):
    """A move needs somewhere to move to. Skip rather than fail if the lab has
    no second storage that can hold the volume type under test."""
    for entry in pve.get(f"/nodes/{node}/storage"):
        if entry["storage"] != OTHER_STORAGE:
            continue
        content = entry.get("content", "")
        if "images" in content and "rootdir" in content:
            return
        pytest.skip(f"{OTHER_STORAGE} cannot hold disks (content: {content})")
    pytest.skip(f"no storage named {OTHER_STORAGE}")


@needs_images
class TestVMMove:
    def test_move_disk_off_and_back(self, create_vm, pve, node, storage):
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        guard = DataGuard(vm.agent()).seed()
        vm.shutdown()

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm.vmid}/move_disk",
            disk="scsi0", storage=OTHER_STORAGE, delete=1,
        ), timeout=1800)
        assert_relocated(pve, vm, "scsi0", storage, OTHER_STORAGE, "moving off")

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm.vmid}/move_disk",
            disk="scsi0", storage=storage, delete=1,
        ), timeout=1800)
        assert_relocated(pve, vm, "scsi0", OTHER_STORAGE, storage, "moving back")

        vm.start()
        vm.wait_agent()
        guard.verify("after a move round trip")


@needs_lxc
class TestCTMove:
    def test_move_volume_off_and_back(self, create_ct, pve, node, storage):
        ct = create_ct(start=True)
        guard = DataGuard(ct.exec()).seed()
        ct.stop()

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/lxc/{ct.vmid}/move_volume",
            volume="rootfs", storage=OTHER_STORAGE, delete=1,
        ), timeout=1800)
        assert_relocated(pve, ct, "rootfs", storage, OTHER_STORAGE, "moving off")

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/lxc/{ct.vmid}/move_volume",
            volume="rootfs", storage=storage, delete=1,
        ), timeout=1800)
        assert_relocated(pve, ct, "rootfs", OTHER_STORAGE, storage, "moving back")

        ct.start()
        guard.guest = ct.exec()
        guard.verify("after a move round trip")
