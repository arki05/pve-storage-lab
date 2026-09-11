"""Clones. Linked clones are where backends diverge most."""

from conftest import needs_linked_clone, needs_lxc, needs_images
from helpers.data_guard import DataGuard
from helpers.wait import wait_for_task
from conftest import LabVM, LabCT


@needs_images
class TestVMClone:
    def test_full_clone_is_independent(self, create_vm, pve, node, storage):
        source = create_vm()
        source.start()
        source.wait_agent()
        guard = DataGuard(source.agent()).seed()
        source.shutdown()

        clone_id = pve.nextid()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{source.vmid}/clone",
            newid=clone_id, name=f"clone-{clone_id}", full=1, storage=storage,
        ), timeout=900)
        clone = LabVM(pve, node, clone_id)
        try:
            clone.start()
            clone.wait_agent()
            guard.guest = clone.agent()
            guard.verify("in the clone")

            # Writing in the clone must not reach back into the source.
            clone.agent().run("rm -f /srv/guard-0.dat && sync")
            clone.shutdown()

            source.start()
            source.wait_agent()
            assert source.agent().exec("test -e /srv/guard-0.dat")["exitcode"] == 0, (
                "deleting in the clone also removed the file in the source"
            )
        finally:
            clone.destroy()


@needs_lxc
class TestCTClone:
    def test_full_clone_is_independent(self, create_ct, pve, node, storage):
        source = create_ct(start=True)
        guard = DataGuard(source.exec()).seed()
        source.stop()

        clone_id = pve.nextid()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/lxc/{source.vmid}/clone",
            newid=clone_id, hostname=f"clone-{clone_id}", full=1, storage=storage,
        ), timeout=900)
        clone = LabCT(pve, node, clone_id)
        try:
            clone.start()
            guard.guest = clone.exec()
            guard.verify("in the clone")
        finally:
            clone.destroy()


@needs_linked_clone
@needs_lxc
class TestCTLinkedClone:
    def test_linked_clone_sees_source_data(self, create_ct, pve, node):
        """A linked clone shares the source's data until written to. The
        source must be a template for PVE to allow it."""
        source = create_ct(start=True)
        guard = DataGuard(source.exec()).seed()
        source.stop()
        wait_for_task(pve, pve.create(f"/nodes/{node}/lxc/{source.vmid}/template"), 300)

        clone_id = pve.nextid()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/lxc/{source.vmid}/clone",
            newid=clone_id, hostname=f"linked-{clone_id}", full=0,
        ), timeout=600)
        clone = LabCT(pve, node, clone_id)
        try:
            clone.start()
            guard.guest = clone.exec()
            guard.verify("in the linked clone")
        finally:
            clone.destroy()
