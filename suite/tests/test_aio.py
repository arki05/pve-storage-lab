"""The same storage through all three of QEMU's I/O backends.

PVE picks an aio mode per disk - io_uring by default on a modern install, with
threads and native still selectable and still used in the field. They take
different paths into the kernel, so a storage can be perfectly well behaved
through one and not through another: io_uring submits via its own ring,
native uses libaio and requires O_DIRECT, threads falls back to a worker pool
and buffered I/O.

That difference is not hypothetical here. A bcachefs read error at offset 0 of
every VM image was first suspected to be an io_uring interaction, and ruling
that out meant setting all three by hand. This class is that experiment kept:
a backend that misbehaves in exactly one mode is otherwise invisible, because
everything else in the suite runs whatever the default happens to be.

Deliberately a small set. Parametrising every VM test three ways would roughly
triple the VM half of a run to re-answer the same question; these are the
operations where the I/O path is actually load-bearing - a verified write, an
unclean stop, and a snapshot taken underneath live writes.
"""

import pytest

from conftest import needs_images, needs_snapshots
from helpers.data_guard import DataGuard
from helpers.load import VerifiedLoad
from helpers.wait import wait_for, wait_for_task

pytestmark = needs_images

AIO_MODES = ["io_uring", "threads", "native"]
MARKER = "/srv/durable-aio.txt"
CONTENT = "written-and-fsynced"


@pytest.mark.parametrize("aio", AIO_MODES)
class TestAioModes:
    def test_data_survives_a_round_trip(self, create_vm, aio):
        """The plain claim: what was written reads back."""
        vm = create_vm(aio=aio)
        vm.start()
        vm.wait_agent()
        guard = DataGuard(vm.agent()).seed()
        vm.shutdown()
        vm.start()
        vm.wait_agent()
        guard.guest = vm.agent()
        guard.verify(f"after a restart with aio={aio}")

    def test_fsynced_data_survives_a_hard_stop(self, create_vm, pve, node, aio):
        """Where a write-cache bug in one backend would actually show."""
        vm = create_vm(aio=aio)
        vm.start()
        vm.wait_agent()
        agent = vm.agent()
        agent.run(f"printf '%s' '{CONTENT}' > {MARKER} && sync {MARKER} && sync")

        wait_for_task(pve, pve.create(f"/nodes/{node}/qemu/{vm.vmid}/status/stop"))
        wait_for(lambda: vm.status() == "stopped", timeout=120,
                 desc=f"VM {vm.vmid} to stop hard")
        vm.start()
        vm.wait_agent()
        out = vm.agent().run(f"cat {MARKER}")
        assert out.strip() == CONTENT, (
            f"data fsynced before a hard stop did not survive with aio={aio}: "
            f"got {out.strip()!r}")

    @needs_snapshots
    def test_snapshot_under_verified_io(self, create_vm, pve, node, aio):
        """A snapshot taken while the guest is writing, with the writes
        verified rather than merely issued."""
        vm = create_vm(aio=aio)
        vm.start()
        vm.wait_agent()
        load = VerifiedLoad(vm.agent(), runtime=120).start().settle()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm.vmid}/snapshot",
            snapname=f"under-{aio}", _timeout=1800), timeout=1800)
        load.wait()
        load.verify(f"during a snapshot with aio={aio}")
