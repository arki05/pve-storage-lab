"""RAM snapshots: saving a running guest's memory to the storage under test.

Two things are being checked, and only the first is about the guest.

A snapshot taken with `vmstate` writes the guest's whole memory as one large
sequential volume. Rolling back has to bring the guest back *mid-execution* -
same process, same state - rather than merely restoring a disk and rebooting.
That is a stronger claim than any other snapshot test makes.

For the storage it is a workload nothing else here produces: a single write of
gigabytes, allocated and freed on every snapshot. `vmstatestorage` is pointed
explicitly at the storage under test, because otherwise PVE chooses one itself
and the backend being tested never sees the state volume at all.
"""

import time

import pytest

from conftest import needs_images, needs_snapshots, needs_vmstate
from helpers.data_guard import DataGuard
from helpers.load import VerifiedLoad
from helpers.wait import wait_for, wait_for_task

pytestmark = [needs_images, needs_snapshots, needs_vmstate]

COUNTER = "/srv/counter"


def _snapshot(pve, node, vmid, name, storage):
    """Snapshot including guest memory, with the state volume pinned to the
    storage under test."""
    wait_for_task(pve, pve.create(
        f"/nodes/{node}/qemu/{vmid}/snapshot", _timeout=1800,
        snapname=name, vmstate=1), timeout=1800)


def _start_counter(agent) -> None:
    """A process whose state lives only in RAM.

    Rolling back a vmstate snapshot must restore this counter to where it was
    when the snapshot was taken - which a disk-only rollback cannot do, because
    the process would have been restarted.
    """
    # Written to a temporary file and renamed, because `echo > file` truncates
    # first: a read landing in that window returns an empty file, which read
    # as 0 and made the rollback look like it had restored nothing.
    agent.run(
        f"nohup bash -c 'i=0; while true; do i=$((i+1)); "
        f"echo $i > {COUNTER}.tmp; mv {COUNTER}.tmp {COUNTER}; sleep 1; done' "
        f">/dev/null 2>&1 & echo started")


def _counter(agent) -> int:
    """Never silently 0. A blank read means the file was caught mid-write, and
    treating that as a value is how a working rollback got reported as one
    that restored only the disk."""
    for _ in range(5):
        raw = agent.run(f"cat {COUNTER}").strip()
        if raw.isdigit():
            return int(raw)
        time.sleep(0.5)
    raise AssertionError(f"{COUNTER} never read back as a number (last: {raw!r})")


class TestVMStateSnapshots:
    def test_state_volume_lands_on_the_storage_under_test(
            self, create_vm, pve, node, storage):
        vm = create_vm()
        pve.set(f"/nodes/{node}/qemu/{vm.vmid}/config", vmstatestorage=storage)
        vm.start()
        vm.wait_agent()

        _snapshot(pve, node, vm.vmid, "withram", storage)

        volumes = [entry["volid"] for entry in pve.storage_content(storage)
                   if "state" in entry["volid"]]
        assert volumes, (
            f"no VM state volume on {storage} after a vmstate snapshot - "
            f"vmstatestorage was set to it, so the state went somewhere else")

    def test_rollback_restores_the_running_process(self, create_vm, pve, node,
                                                   storage):
        """The claim a RAM snapshot actually makes."""
        vm = create_vm()
        pve.set(f"/nodes/{node}/qemu/{vm.vmid}/config", vmstatestorage=storage)
        vm.start()
        vm.wait_agent()
        agent = vm.agent()

        _start_counter(agent)
        wait_for(lambda: _counter(vm.agent()) > 2, timeout=60,
                 desc="the counter to start")
        at_snapshot = _counter(vm.agent())
        _snapshot(pve, node, vm.vmid, "running", storage)

        # Let it run well past where the snapshot was taken.
        wait_for(lambda: _counter(vm.agent()) > at_snapshot + 10, timeout=120,
                 desc="the counter to advance past the snapshot")
        before_rollback = _counter(vm.agent())

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm.vmid}/snapshot/running/rollback",
            _timeout=1800), timeout=1800)

        # A vmstate rollback resumes the guest; it does not reboot it.
        wait_for(lambda: vm.status() == "running", timeout=180,
                 desc="the guest to resume after rollback")
        vm.wait_agent()

        after = _counter(vm.agent())
        assert after < before_rollback, (
            f"the counter did not go backwards ({after} >= {before_rollback}): "
            f"the guest's memory was not restored, only its disk")
        assert after >= at_snapshot - 2, (
            f"the counter went back further than the snapshot ({after} vs "
            f"{at_snapshot}): the restored state predates the snapshot")

    def test_rollback_under_verified_io(self, create_vm, pve, node, storage):
        """Snapshot and roll back while the guest is writing.

        The disk and the memory are captured at the same instant, so a write
        that was in flight has to be consistent between the two. fio verifies
        its own writes, which is the only way to see that from outside.
        """
        vm = create_vm()
        pve.set(f"/nodes/{node}/qemu/{vm.vmid}/config", vmstatestorage=storage)
        vm.start()
        vm.wait_agent()

        load = VerifiedLoad(vm.agent(), runtime=180).start().settle()
        _snapshot(pve, node, vm.vmid, "underload", storage)
        time.sleep(10)

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm.vmid}/snapshot/underload/rollback",
            _timeout=1800), timeout=1800)
        wait_for(lambda: vm.status() == "running", timeout=180,
                 desc="the guest to resume after rollback")
        vm.wait_agent()

        load.guest = vm.agent()
        load.verify("across a vmstate snapshot and rollback")

    def test_deleting_the_snapshot_frees_the_state_volume(
            self, create_vm, pve, node, storage):
        """A state volume is gigabytes. One leaked per snapshot fills a pool
        quietly, and nothing else here would notice."""
        vm = create_vm()
        pve.set(f"/nodes/{node}/qemu/{vm.vmid}/config", vmstatestorage=storage)
        vm.start()
        vm.wait_agent()

        before = {e["volid"] for e in pve.storage_content(storage)}
        _snapshot(pve, node, vm.vmid, "doomed", storage)
        wait_for_task(pve, pve.delete(
            f"/nodes/{node}/qemu/{vm.vmid}/snapshot/doomed", _timeout=1800),
            timeout=1800)

        after = {e["volid"] for e in pve.storage_content(storage)}
        leaked = [v for v in after - before if "state" in v]
        assert not leaked, f"the state volume outlived its snapshot: {leaked}"
