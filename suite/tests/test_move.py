"""Moving a volume between storages.

This is the path that reads every extended attribute and re-creates the volume
elsewhere, so it is where backends that store metadata out-of-band tend to
fail - and it fails late, after copying gigabytes.
"""

from conftest import needs_lxc, needs_images
from helpers.data_guard import DataGuard
from helpers.wait import wait_for_task

OTHER_STORAGE = "local"


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
        assert vm.config()["scsi0"].startswith(f"{OTHER_STORAGE}:")

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm.vmid}/move_disk",
            disk="scsi0", storage=storage, delete=1,
        ), timeout=1800)
        assert vm.config()["scsi0"].startswith(f"{storage}:")

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
        assert ct.config()["rootfs"].startswith(f"{OTHER_STORAGE}:")

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/lxc/{ct.vmid}/move_volume",
            volume="rootfs", storage=storage, delete=1,
        ), timeout=1800)
        assert ct.config()["rootfs"].startswith(f"{storage}:")

        ct.start()
        guard.guest = ct.exec()
        guard.verify("after a move round trip")
