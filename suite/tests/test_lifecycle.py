"""The floor: can the backend allocate, run and free a guest at all."""

import pytest

from conftest import needs_lxc, needs_images
from helpers.data_guard import DataGuard
from helpers.pve import PVEError
from helpers.wait import wait_for_task


@needs_images
class TestVMLifecycle:
    def test_create_lands_on_the_storage(self, create_vm, storage, pve, node):
        vm = create_vm()
        cfg = vm.config()
        assert cfg["scsi0"].startswith(f"{storage}:"), (
            f"disk was allocated on the wrong storage: {cfg['scsi0']}"
        )

    def test_start_stop(self, create_vm):
        vm = create_vm()
        vm.start()
        assert vm.status() == "running"
        vm.stop()
        assert vm.status() == "stopped"

    def test_guest_agent_responds(self, create_vm):
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        assert "Linux" in vm.agent().run("uname -s")

    def test_disk_io(self, create_vm):
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        DataGuard(vm.agent()).seed().verify()

    def test_destroy_frees_the_volume(self, pve, node, storage, vm_template):
        """A destroy that leaves the volume behind does not fail here - it
        fails much later, when a backend with an allocate-time existence check
        refuses the next guest that is handed the same VMID."""
        vmid = pve.nextid()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm_template}/clone",
            newid=vmid, name=f"leakcheck-{vmid}", full=1, storage=storage,
        ), timeout=600)
        wait_for_task(pve, pve.delete(f"/nodes/{node}/qemu/{vmid}", purge=1), 180)

        leaked = [c["volid"] for c in pve.storage_content(storage)
                  if f"-{vmid}-" in c["volid"]]
        assert not leaked, f"destroy left volumes behind: {leaked}"


@needs_lxc
class TestCTLifecycle:
    def test_create_lands_on_the_storage(self, create_ct, storage):
        ct = create_ct()
        assert ct.config()["rootfs"].startswith(f"{storage}:")

    def test_start_stop(self, create_ct):
        ct = create_ct(start=True)
        assert ct.status() == "running"
        ct.stop()
        assert ct.status() == "stopped"

    def test_exec(self, create_ct):
        ct = create_ct(start=True)
        assert "Linux" in ct.exec().run("uname -s")

    def test_disk_io(self, create_ct):
        ct = create_ct(start=True)
        DataGuard(ct.exec()).seed().verify()

    def test_destroy_frees_the_volume(self, pve, node, storage, ct_template):
        vmid = pve.nextid()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/lxc", vmid=vmid, hostname=f"leakcheck-{vmid}",
            ostemplate=ct_template, storage=storage, rootfs=f"{storage}:2",
            memory=512, cores=1, password="pvelab", unprivileged=1, start=0,
        ), timeout=600)
        wait_for_task(pve, pve.delete(f"/nodes/{node}/lxc/{vmid}", purge=1), 180)

        leaked = [c["volid"] for c in pve.storage_content(storage)
                  if f"-{vmid}-" in c["volid"]]
        assert not leaked, f"destroy left volumes behind: {leaked}"
