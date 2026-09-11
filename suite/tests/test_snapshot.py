"""Snapshots, and the thing that actually matters about them: rollback."""

import pytest

from conftest import (needs_snapshots, needs_lxc, needs_images,
                      needs_rollback_past_newer)
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

    def _stage(self, vm, pve, node, count=3):
        """Write a marker, snapshot, repeat. Leaves the guest stopped."""
        vm.start()
        vm.wait_agent()
        agent = vm.agent()
        for i in range(count):
            agent.write_file(f"/srv/stage{i}.txt", f"stage {i}")
            agent.run("sync")
            wait_for_task(pve, pve.create(
                f"/nodes/{node}/qemu/{vm.vmid}/snapshot", snapname=f"s{i}"))
        vm.shutdown()

    @needs_rollback_past_newer
    def test_rollback_past_a_newer_snapshot_keeps_it(self, create_vm, pve, node):
        """Roll back to s1 while s2 exists, and keep s2.

        Only some backends can do this. ZFS refuses - it can roll back to the
        most recent snapshot only - so this is capability-gated rather than
        being a failure there.
        """
        vm = create_vm()
        self._stage(vm, pve, node)

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm.vmid}/snapshot/s1/rollback"), 300)
        vm.start()
        vm.wait_agent()
        agent = vm.agent()

        assert "stage 0" in agent.read_file("/srv/stage0.txt")
        assert "stage 1" in agent.read_file("/srv/stage1.txt")
        assert agent.exec("test -e /srv/stage2.txt")["exitcode"] != 0, (
            "a snapshot taken after the rollback target was still visible")

        names = [s["name"] for s in
                 pve.get(f"/nodes/{node}/qemu/{vm.vmid}/snapshot")]
        assert "s2" in names, (
            "rolling back to s1 destroyed the newer snapshot s2; this backend "
            "should have kept it")

    def test_rollback_after_discarding_newer_snapshots(self, create_vm, pve, node):
        """The portable form: drop the newer snapshots first, then roll back.

        Works on every backend, because it never asks one to roll back past a
        snapshot it still holds.
        """
        vm = create_vm()
        self._stage(vm, pve, node)

        wait_for_task(pve, pve.delete(
            f"/nodes/{node}/qemu/{vm.vmid}/snapshot/s2"), 300)
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm.vmid}/snapshot/s1/rollback"), 300)
        vm.start()
        vm.wait_agent()
        agent = vm.agent()

        assert "stage 0" in agent.read_file("/srv/stage0.txt")
        assert "stage 1" in agent.read_file("/srv/stage1.txt")
        assert agent.exec("test -e /srv/stage2.txt")["exitcode"] != 0


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
