"""Operations in combination.

Individually-correct operations can still be wrong in sequence: a rollback
that forgets a resize, a snapshot taken while the guest is writing, two
allocations racing for the same name. These are the cases that single-operation
tests structurally cannot find.
"""

import time

from conftest import needs_snapshots, needs_lxc, needs_images
from helpers.data_guard import DataGuard
from helpers.wait import wait_for_task


def _size_bytes(pve, node, vmid: int) -> int:
    cfg = pve.get(f"/nodes/{node}/qemu/{vmid}/config")
    # "local-lvm:vm-101-disk-0,size=12G"
    for part in cfg["scsi0"].split(","):
        if part.startswith("size="):
            value = part[5:]
            unit = value[-1]
            return int(float(value[:-1]) * {"G": 1 << 30, "M": 1 << 20, "T": 1 << 40}[unit])
    raise AssertionError(f"no size in {cfg['scsi0']}")


@needs_snapshots
@needs_images
class TestSnapshotAndResize:
    def test_rollback_after_resize_restores_the_old_size(self, create_vm, pve, node):
        vm = create_vm()
        before = _size_bytes(pve, node, vm.vmid)

        wait_for_task(pve, pve.create(f"/nodes/{node}/qemu/{vm.vmid}/snapshot",
                                      snapname="presize"))
        pve.set(f"/nodes/{node}/qemu/{vm.vmid}/resize", disk="scsi0", size="+2G")
        assert _size_bytes(pve, node, vm.vmid) > before

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm.vmid}/snapshot/presize/rollback"), 300)
        assert _size_bytes(pve, node, vm.vmid) == before, (
            "rollback did not restore the pre-resize disk size"
        )

    def test_snapshot_after_resize_keeps_the_new_size(self, create_vm, pve, node):
        vm = create_vm()
        pve.set(f"/nodes/{node}/qemu/{vm.vmid}/resize", disk="scsi0", size="+2G")
        grown = _size_bytes(pve, node, vm.vmid)

        wait_for_task(pve, pve.create(f"/nodes/{node}/qemu/{vm.vmid}/snapshot",
                                      snapname="postsize"))
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/qemu/{vm.vmid}/snapshot/postsize/rollback"), 300)
        assert _size_bytes(pve, node, vm.vmid) == grown, (
            "rollback to a post-resize snapshot lost the resize"
        )


@needs_snapshots
@needs_images
class TestSnapshotUnderLoad:
    def test_snapshot_while_the_guest_is_writing(self, create_vm, pve, node):
        """fio with verify running across the snapshot. If the backend loses
        in-flight writes, fio reports the corruption itself."""
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        agent = vm.agent()

        agent.run(
            "nohup fio --name=load --filename=/srv/fio.dat --size=256M "
            "--rw=randwrite --bs=4k --verify=crc32c --do_verify=1 "
            "--time_based --runtime=45 --output=/srv/fio.out "
            ">/dev/null 2>&1 & echo started"
        )
        time.sleep(8)
        wait_for_task(pve, pve.create(f"/nodes/{node}/qemu/{vm.vmid}/snapshot",
                                      snapname="underload"), 600)

        for _ in range(60):
            if agent.exec("pgrep -x fio")["exitcode"] != 0:
                break
            time.sleep(2)

        out = agent.read_file("/srv/fio.out")
        assert "verify" not in out.lower() or "error" not in out.lower(), (
            f"fio reported verification errors across the snapshot:\n{out[-2000:]}"
        )


@needs_lxc
class TestConcurrentAllocation:
    def test_two_containers_created_back_to_back(self, create_ct, pve, node, storage):
        """Allocation races show up as a second guest reusing a name the first
        already took, or as a volume left behind that blocks the next one."""
        first = create_ct()
        second = create_ct()
        assert first.vmid != second.vmid

        volumes = {c["volid"] for c in pve.storage_content(storage)}
        for ct in (first, second):
            expected = [v for v in volumes if f"-{ct.vmid}-" in v]
            assert expected, f"no volume on {storage} for CT {ct.vmid}"


@needs_lxc
class TestDaemonRestart:
    def test_storage_survives_a_pvestatd_restart(self, create_ct, pve, node, storage):
        """A plugin that caches state in the daemon can look fine until the
        daemon restarts and re-reads the storage from scratch."""
        ct = create_ct(start=True)
        DataGuard(ct.exec()).seed()

        import subprocess
        subprocess.run(["systemctl", "restart", "pvestatd"], check=True, timeout=60)
        time.sleep(5)

        status = {s["storage"]: s for s in pve.get("/nodes/%s/storage" % node)}
        assert status[storage]["active"], f"{storage} is not active after restart"
        assert ct.status() == "running"
