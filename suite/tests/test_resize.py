"""Growing a volume, and the guest actually seeing the new size."""

from conftest import (needs_lxc, needs_images,
                      needs_guest_visible_vm_size, needs_guest_visible_ct_size)
from helpers.data_guard import DataGuard
from helpers.wait import wait_for, wait_for_task


def _disk_gb(guest, path="/") -> float:
    out = guest.run(f"df -BG --output=size {path} | tail -1")
    return float(out.strip().rstrip("G"))


@needs_images
class TestVMResize:
    def test_resize_preserves_data(self, create_vm, pve, node):
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        guard = DataGuard(vm.agent()).seed()
        vm.shutdown()

        pve.set(f"/nodes/{node}/qemu/{vm.vmid}/resize", disk="scsi0", size="+2G")
        vm.start()
        vm.wait_agent()
        guard.verify("after resize")

    @needs_guest_visible_vm_size
    def test_guest_sees_the_new_size(self, create_vm, pve, node):
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        before = int(vm.agent().run("lsblk -bdno SIZE /dev/sda").strip())

        pve.set(f"/nodes/{node}/qemu/{vm.vmid}/resize", disk="scsi0", size="+2G")

        def grown() -> bool:
            vm.agent().run("echo 1 > /sys/class/block/sda/device/rescan")
            return int(vm.agent().run("lsblk -bdno SIZE /dev/sda").strip()) > before

        wait_for(grown, timeout=90, desc="the guest to see the larger disk")


@needs_lxc
class TestCTResize:
    def test_resize_preserves_data(self, create_ct, pve, node):
        ct = create_ct(start=True, disk_gb=2)
        guard = DataGuard(ct.exec()).seed()

        pve.set(f"/nodes/{node}/lxc/{ct.vmid}/resize", disk="rootfs", size="+1G")
        guard.verify("after resize")

    @needs_guest_visible_ct_size
    def test_container_sees_the_new_size(self, create_ct, pve, node):
        ct = create_ct(start=True, disk_gb=2)
        before = _disk_gb(ct.exec())
        pve.set(f"/nodes/{node}/lxc/{ct.vmid}/resize", disk="rootfs", size="+2G")
        wait_for(lambda: _disk_gb(ct.exec()) > before, timeout=90,
                 desc="the container to see the larger rootfs")
