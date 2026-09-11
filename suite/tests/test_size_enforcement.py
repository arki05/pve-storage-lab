"""A volume's stated size must be a limit, not a suggestion.

This is a general storage property, not a bcachefs one. Every backend enforces
it differently - lvm-thin by the LV size, ZFS by a refquota, a directory
storage by the size of the raw image, bcachefs by a project quota - and a
backend that gets it wrong hands out a volume that can consume the whole pool.

The failure mode is quiet: everything works, nothing errors, and the pool fills
up. It deserves a test at this level rather than in any one plugin's suite.
"""

import pytest

from conftest import (needs_lxc, needs_images, needs_size_enforcement,
                      needs_snapshots)
from helpers.wait import wait_for_task

pytestmark = needs_size_enforcement

# Incompressible data, deliberately. Filling with /dev/zero tests nothing on a
# backend that compresses: ZFS with lz4 shrinks 2.5 GiB of zeros to almost
# nothing and the refquota is never reached, so the test "fails" while the
# storage is behaving perfectly. Only random data measures a size limit.
#
# Written in chunks so the limit arrives as a write error rather than as one
# large allocation the backend might reject up front.
FILL = ("for i in $(seq 1 {chunks}); do "
        "  dd if=/dev/urandom of={path}/fill-$i bs=1M count=64 conv=fsync "
        "    status=none || exit 42; "
        "done")


@needs_lxc
class TestContainerSizeEnforcement:
    def test_cannot_write_past_the_rootfs_size(self, create_ct):
        ct = create_ct(start=True, disk_gb=1)
        result = ct.exec().exec(FILL.format(chunks=40, path="/root"), timeout=900)
        assert result["exitcode"] != 0, (
            "wrote 2.5 GiB into a 1 GiB rootfs without hitting a limit - the "
            "configured size is not enforced, so one container can fill the pool"
        )

    def test_enforcement_survives_a_restart(self, create_ct):
        """A limit applied at creation but not re-applied on start is a limit
        that quietly disappears the first time the guest is rebooted."""
        ct = create_ct(start=True, disk_gb=1)
        ct.stop()
        ct.start()
        result = ct.exec().exec(FILL.format(chunks=40, path="/root"), timeout=900)
        assert result["exitcode"] != 0, (
            "the size limit was not enforced after a restart"
        )

    @needs_snapshots
    def test_enforcement_survives_a_snapshot_rollback(self, create_ct, pve, node):
        """Rollback can leave the volume in a different internal state than it
        was created in - on some backends a snapshot rather than a master
        volume - and enforcement can be skipped in that state."""
        ct = create_ct(start=True, disk_gb=1)
        wait_for_task(pve, pve.create(f"/nodes/{node}/lxc/{ct.vmid}/snapshot",
                                      snapname="sized"), 300)
        ct.stop()
        wait_for_task(pve, pve.create(
            f"/nodes/{node}/lxc/{ct.vmid}/snapshot/sized/rollback"), 300)
        ct.start()
        result = ct.exec().exec(FILL.format(chunks=40, path="/root"), timeout=900)
        assert result["exitcode"] != 0, (
            "the size limit stopped being enforced after a snapshot rollback"
        )


@needs_images
class TestVMSizeEnforcement:
    def test_disk_is_the_size_it_claims(self, create_vm, pve, node):
        """A VM disk is enforced by construction, so this checks the simpler
        property: the guest sees the size the config advertises."""
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        seen = int(vm.agent().run("lsblk -bdno SIZE /dev/sda").strip())
        declared = vm.config()["scsi0"]
        size = [p[5:] for p in declared.split(",") if p.startswith("size=")][0]
        unit = size[-1]
        want = int(float(size[:-1]) * {"G": 1 << 30, "M": 1 << 20, "T": 1 << 40}[unit])
        # Allow a little slack for rounding between the two representations.
        assert abs(seen - want) < (64 << 20), (
            f"the guest sees {seen} bytes but the config says {want}"
        )
