"""Snapshots, and the thing that actually matters about them: rollback."""

import pytest

from conftest import needs_snapshots, needs_lxc, needs_images
from helpers.data_guard import DataGuard
from helpers.wait import wait_for_task

pytestmark = needs_snapshots


@needs_images
class TestVMSnapshot:
    def test_create_and_list(self, create_vm, pve, node):
        vm = create_vm()
        wait_for_task(pve, pve.create(f"/nodes/{node}/qemu/{vm.vmid}/snapshot",
                                      snapname="snap1"))
        names = [s["name"] for s in pve.get(f"/nodes/{node}/qemu/{vm.vmid}/snapshot")]
        assert "snap1" in names

    def test_rollback_restores_data(self, create_vm, pve, node):
        vm = create_vm()
        vm.start()
        vm.wait_agent()

        guard = DataGuard(vm.agent()).seed()
        wait_for_task(pve, pve.create(f"/nodes/{node}/qemu/{vm.vmid}/snapshot",
                                      snapname="before"))

        vm.agent().run("rm -f /srv/guard-*.dat && sync")
        vm.shutdown()

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm.vmid}/snapshot/before/rollback"), 300)
        vm.start()
        vm.wait_agent()
        guard.verify("after rollback")

    def test_rollback_undoes_writes_made_after_the_snapshot(self, create_vm, pve, node):
        vm = create_vm()
        vm.start()
        vm.wait_agent()

        wait_for_task(pve, pve.create(f"/nodes/{node}/qemu/{vm.vmid}/snapshot",
                                      snapname="clean"))
        guard = DataGuard(vm.agent()).seed()
        vm.shutdown()

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm.vmid}/snapshot/clean/rollback"), 300)
        vm.start()
        vm.wait_agent()
        guard.verify_missing("data written after the snapshot survived rollback")

    def test_delete(self, create_vm, pve, node):
        vm = create_vm()
        wait_for_task(pve, pve.create(f"/nodes/{node}/qemu/{vm.vmid}/snapshot",
                                      snapname="doomed"))
        wait_for_task(pve, pve.delete(
            f"/nodes/{node}/qemu/{vm.vmid}/snapshot/doomed"), 300)
        names = [s["name"] for s in pve.get(f"/nodes/{node}/qemu/{vm.vmid}/snapshot")]
        assert "doomed" not in names

    def test_multiple_snapshots_are_independent(self, create_vm, pve, node):
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        agent = vm.agent()

        for i in range(3):
            agent.write_file(f"/srv/stage{i}.txt", f"stage {i}")
            agent.run("sync")
            wait_for_task(pve, pve.create(
                f"/nodes/{node}/qemu/{vm.vmid}/snapshot", snapname=f"s{i}"))

        vm.shutdown()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm.vmid}/snapshot/s1/rollback"), 300)
        vm.start()
        vm.wait_agent()
        agent = vm.agent()

        assert "stage 0" in agent.read_file("/srv/stage0.txt")
        assert "stage 1" in agent.read_file("/srv/stage1.txt")
        assert agent.exec("test -e /srv/stage2.txt")["exitcode"] != 0, (
            "a snapshot taken after the rollback target was still visible"
        )


@needs_lxc
class TestCTSnapshot:
    def test_create_and_list(self, create_ct, pve, node):
        ct = create_ct(start=True)
        wait_for_task(pve, pve.create(f"/nodes/{node}/lxc/{ct.vmid}/snapshot",
                                      snapname="snap1"), 300)
        names = [s["name"] for s in pve.get(f"/nodes/{node}/lxc/{ct.vmid}/snapshot")]
        assert "snap1" in names

    def test_rollback_restores_data(self, create_ct, pve, node):
        ct = create_ct(start=True)
        guard = DataGuard(ct.exec()).seed()
        wait_for_task(pve, pve.create(f"/nodes/{node}/lxc/{ct.vmid}/snapshot",
                                      snapname="before"), 300)
        ct.exec().run("rm -f /srv/guard-*.dat && sync")
        ct.stop()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/lxc/{ct.vmid}/snapshot/before/rollback"), 300)
        ct.start()
        guard.verify("after rollback")

    def test_delete(self, create_ct, pve, node):
        ct = create_ct(start=True)
        wait_for_task(pve, pve.create(f"/nodes/{node}/lxc/{ct.vmid}/snapshot",
                                      snapname="doomed"), 300)
        wait_for_task(pve, pve.delete(
            f"/nodes/{node}/lxc/{ct.vmid}/snapshot/doomed"), 300)
        names = [s["name"] for s in pve.get(f"/nodes/{node}/lxc/{ct.vmid}/snapshot")]
        assert "doomed" not in names
