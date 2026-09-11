"""Data survival across the operations a storage backend performs.

Separate from the lifecycle tests on purpose: those ask whether an operation
succeeds, these ask whether the data is still there afterwards. A backend can
pass every lifecycle test and still lose writes.
"""

from conftest import needs_lxc, needs_images, needs_snapshots
from helpers.data_guard import DataGuard
from helpers.wait import wait_for_task


@needs_images
class TestVMDataIntegrity:
    def test_survives_stop_start(self, create_vm):
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        guard = DataGuard(vm.agent()).seed()
        vm.shutdown()
        vm.start()
        vm.wait_agent()
        guard.guest = vm.agent()
        guard.verify("after a stop/start cycle")

    @needs_snapshots
    def test_survives_snapshot_and_rollback(self, create_vm, pve, node):
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        guard = DataGuard(vm.agent()).seed()
        wait_for_task(pve, pve.create(f"/nodes/{node}/qemu/{vm.vmid}/snapshot",
                                      snapname="integrity"))
        vm.shutdown()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm.vmid}/snapshot/integrity/rollback"), 300)
        vm.start()
        vm.wait_agent()
        guard.guest = vm.agent()
        guard.verify("after snapshot and rollback")

    def test_fio_verifies_its_own_writes(self, create_vm):
        """fio's crc32c verification catches corruption the checksums of a few
        files would miss, because it writes and reads back across the whole
        volume rather than a handful of blocks."""
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        out = vm.agent().run(
            "fio --name=verify --filename=/srv/fio-verify.dat --size=256M "
            "--rw=randwrite --bs=4k --verify=crc32c --do_verify=1 "
            "--verify_fatal=1 --group_reporting 2>&1 | tail -20", timeout=900)
        assert "err= 0" in out or "Run status" in out, (
            f"fio reported a verification failure:\n{out[-1500:]}"
        )

    def test_many_files_survive(self, create_vm):
        """A different shape of the same question: lots of small files rather
        than a few large ones, which exercises metadata rather than extents."""
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        agent = vm.agent()
        agent.run("mkdir -p /srv/many && cd /srv/many && "
                  "for i in $(seq 1 400); do echo content-$i > f$i; done && sync")
        before = agent.run("cd /srv/many && md5sum f* | md5sum").split()[0]
        vm.shutdown()
        vm.start()
        vm.wait_agent()
        after = vm.agent().run("cd /srv/many && md5sum f* | md5sum").split()[0]
        assert before == after, "the set of small files changed across a restart"


@needs_lxc
class TestCTDataIntegrity:
    def test_survives_stop_start(self, create_ct):
        ct = create_ct(start=True)
        guard = DataGuard(ct.exec()).seed()
        ct.stop()
        ct.start()
        guard.guest = ct.exec()
        guard.verify("after a stop/start cycle")

    @needs_snapshots
    def test_survives_snapshot_and_rollback(self, create_ct, pve, node):
        ct = create_ct(start=True)
        guard = DataGuard(ct.exec()).seed()
        wait_for_task(pve, pve.create(f"/nodes/{node}/lxc/{ct.vmid}/snapshot",
                                      snapname="integrity"), 300)
        ct.stop()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/lxc/{ct.vmid}/snapshot/integrity/rollback"), 300)
        ct.start()
        guard.guest = ct.exec()
        guard.verify("after snapshot and rollback")
