"""Moving volumes between two storages.

Proving a backend works on its own says nothing about what happens when data
crosses between two of them, and that is where the interesting failures are:
one backend's assumptions about layout, extended attributes, sparseness or
size only get tested when something else has to read or reproduce them.

Every pair runs in both directions on purpose. A backend that reads a foreign
volume correctly may still write its own wrongly, and the two are different
code paths.

The central assertion is not "the move succeeds". Some pairs legitimately
cannot round-trip - a volume on a compressing backend can hold more logical
data than its nominal size, and that does not fit on a backend that does not
compress. What must always hold is that a move either succeeds completely, or
**fails cleanly**: the source is untouched, the guest still runs, and the data
is still there. A move that reports success and delivers a truncated
filesystem is the real hazard.
"""

import pytest

from conftest import needs_lxc, needs_images, storage_pairs
from helpers.data_guard import DataGuard
from helpers.payloads import PATTERNS
from helpers.pve import PVEError
from helpers.volumes import assert_relocated, volumes_for
from helpers.wait import wait_for_task

pytestmark = pytest.mark.crossstorage

PAIRS = storage_pairs()
# The id spells out the whole round trip, so zfs>btrfs>zfs and btrfs>zfs>btrfs
# are visibly two different tests rather than one pair listed twice.
pair_param = pytest.mark.parametrize(
    "source,dest", PAIRS, ids=[f"{a}>{b}>{a}" for a, b in PAIRS])


class MoveRefused(Exception):
    """The backend declined the move. Not automatically a defect - what
    matters is what it left behind."""


def _move(pve, node, guest, key, dest, timeout=1800):
    kind = "qemu" if guest.kind == "qemu" else "lxc"
    endpoint = "move_disk" if kind == "qemu" else "move_volume"
    param = {"disk": key} if kind == "qemu" else {"volume": key}
    try:
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/{kind}/{guest.vmid}/{endpoint}",
            storage=dest, delete=1, **param,
        ), timeout=timeout)
    except (PVEError, RuntimeError) as exc:
        raise MoveRefused(str(exc)) from exc


def _assert_clean_refusal(pve, guest, key, source, dest, reason, restart):
    """A refused move must leave the guest exactly as it was."""
    configured = guest.config().get(key, "")
    assert configured.startswith(f"{source}:"), (
        f"the move {source} -> {dest} failed, and the config no longer points "
        f"at the source either: {configured!r}. The guest has been left "
        f"between two storages.\n{reason}"
    )
    assert volumes_for(pve, source, guest.vmid), (
        f"the move {source} -> {dest} failed and the source volume is gone. "
        f"Data was destroyed by a move that did not complete.\n{reason}"
    )
    leftover = volumes_for(pve, dest, guest.vmid)
    assert not leftover, (
        f"the move {source} -> {dest} failed but left volumes behind on the "
        f"destination: {leftover}. They will block the next attempt.\n{reason}"
    )
    restart()


@needs_lxc
@pair_param
@pytest.mark.parametrize("pattern", PATTERNS)
class TestContainerAcrossStorages:
    def test_volume_round_trip(self, create_ct, pve, node, source, dest, pattern):
        ct = create_ct(on=source, start=True, disk_gb=2)
        guard = DataGuard(ct.exec()).seed(size_mb=6, count=2, pattern=pattern)
        ct.stop()

        def restart_and_verify(context):
            ct.start()
            guard.guest = ct.exec()
            guard.verify(context)
            guard.verify_sparse(context)
            ct.stop()

        try:
            _move(pve, node, ct, "rootfs", dest)
        except MoveRefused as exc:
            _assert_clean_refusal(
                pve, ct, "rootfs", source, dest, str(exc),
                lambda: restart_and_verify(f"after a refused {source} -> {dest}"))
            pytest.fail(
                f"{source} -> {dest} refused the move ({pattern} payload). It "
                f"failed cleanly, so no data was lost, but the pair cannot "
                f"round-trip:\n{exc}")

        assert_relocated(pve, ct, "rootfs", source, dest, f"{source} -> {dest}")
        restart_and_verify(f"on {dest}")

        _move(pve, node, ct, "rootfs", source)
        assert_relocated(pve, ct, "rootfs", dest, source, f"{dest} -> {source}")
        restart_and_verify(f"back on {source}")


@needs_images
@pair_param
class TestVMAcrossStorages:
    def test_disk_round_trip(self, create_vm, pve, node, source, dest):
        vm = create_vm(on=source)
        vm.start()
        vm.wait_agent()
        guard = DataGuard(vm.agent()).seed(size_mb=8, count=2)
        vm.shutdown()

        def restart_and_verify(context):
            vm.start()
            vm.wait_agent()
            guard.guest = vm.agent()
            guard.verify(context)
            vm.shutdown()

        try:
            _move(pve, node, vm, "scsi0", dest)
        except MoveRefused as exc:
            _assert_clean_refusal(
                pve, vm, "scsi0", source, dest, str(exc),
                lambda: restart_and_verify(f"after a refused {source} -> {dest}"))
            pytest.fail(f"{source} -> {dest} refused the move:\n{exc}")

        assert_relocated(pve, vm, "scsi0", source, dest, f"{source} -> {dest}")
        restart_and_verify(f"on {dest}")

        _move(pve, node, vm, "scsi0", source)
        assert_relocated(pve, vm, "scsi0", dest, source, f"{dest} -> {source}")
        restart_and_verify(f"back on {source}")


@needs_lxc
@pair_param
class TestBackupAcrossStorages:
    def test_backup_on_one_restores_onto_the_other(self, create_ct, pve, node,
                                                   source, dest):
        """A backup is the other way data crosses between backends, and it is
        the one people rely on in an emergency. It has to restore somewhere
        other than where it came from."""
        ct = create_ct(on=source, start=True, disk_gb=2)
        guard = DataGuard(ct.exec()).seed(size_mb=6, count=2)
        ct.stop()

        wait_for_task(pve, pve.create(
            f"/nodes/{node}/vzdump", vmid=ct.vmid, storage="local",
            mode="stop", compress="zstd", remove=0,
        ), timeout=1800)
        dumps = [c for c in pve.storage_content("local", content="backup")
                 if c.get("vmid") == ct.vmid]
        archive = sorted(dumps, key=lambda c: c.get("ctime", 0))[-1]["volid"]

        restore_id = pve.nextid()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/lxc", vmid=restore_id, ostemplate=archive,
            storage=dest, restore=1, force=1, password="pvelab",
        ), timeout=1800)

        from conftest import LabCT
        restored = LabCT(pve, node, restore_id)
        try:
            assert restored.config()["rootfs"].startswith(f"{dest}:")
            restored.start()
            guard.guest = restored.exec()
            guard.verify(f"restored from {source} onto {dest}")
        finally:
            restored.destroy()
