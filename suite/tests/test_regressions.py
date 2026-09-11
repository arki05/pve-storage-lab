"""Regression tests for things that actually broke once.

Every entry here started as a failure someone hit, not as a category someone
thought of. New ones get added when something surprising happens, which is the
only reliable way a suite stays honest about a backend's real weak spots.
"""

import subprocess
import time

import pytest

from conftest import needs_images, needs_tpm, needs_lxc
from helpers.data_guard import DataGuard
from helpers.wait import wait_for, wait_for_task


@needs_images
@needs_tpm
class TestTPMState:
    def test_ovmf_vm_with_tpm_starts(self, create_vm, pve, node, storage):
        """A TPM state volume is a tiny volume with unusual size and access
        patterns, and backends have refused to allocate it while handling every
        ordinary disk correctly."""
        vm = create_vm()
        pve.set(f"/nodes/{node}/qemu/{vm.vmid}/config",
                bios="ovmf", efidisk0=f"{storage}:1,efitype=4m,pre-enrolled-keys=0",
                tpmstate0=f"{storage}:1,version=v2.0")
        vm.start()
        assert vm.status() == "running"


class TestDaemonResilience:
    """PVE daemons re-read storage configuration on restart. A plugin that
    caches state in a daemon can work perfectly until one of them restarts -
    which happens on every package upgrade."""

    def _storage_active(self, pve, node, storage) -> bool:
        entries = {e["storage"]: e for e in pve.get(f"/nodes/{node}/storage")}
        return bool(entries.get(storage, {}).get("active"))

    def test_pvedaemon_restart_preserves_storage(self, pve, node, storage):
        subprocess.run(["systemctl", "restart", "pvedaemon"], check=True, timeout=120)
        wait_for(lambda: self._storage_active(pve, node, storage), timeout=90,
                 desc=f"{storage} to be active after pvedaemon restarted")

    def test_pveproxy_restart_preserves_storage(self, pve, node, storage):
        subprocess.run(["systemctl", "restart", "pveproxy"], check=True, timeout=120)
        wait_for(lambda: self._storage_active(pve, node, storage), timeout=90,
                 desc=f"{storage} to be active after pveproxy restarted")

    def test_full_daemon_restart_sequence_preserves_storage(self, pve, node, storage):
        for unit in ("pvestatd", "pvedaemon", "pveproxy", "pvescheduler"):
            subprocess.run(["systemctl", "restart", unit], check=True, timeout=120)
        wait_for(lambda: self._storage_active(pve, node, storage), timeout=120,
                 desc=f"{storage} to be active after restarting every daemon")

    @needs_lxc
    def test_running_guest_survives_a_daemon_restart(self, create_ct, pve, node):
        ct = create_ct(start=True)
        guard = DataGuard(ct.exec()).seed()
        for unit in ("pvestatd", "pvedaemon"):
            subprocess.run(["systemctl", "restart", unit], check=True, timeout=120)
        time.sleep(5)
        assert ct.status() == "running", "the container stopped when a daemon restarted"
        guard.verify("after a daemon restart")


class TestFilesystemConsistency:
    """Ask the backend whether it is still intact.

    Everything else here checks data the tests themselves wrote. This catches
    damage nothing happened to read back - which is the only way a silent
    corruption gets noticed at all.

    The command comes from the profile, because only it knows how to check its
    own filesystem, and whether that can be done while mounted.
    """

    def test_the_filesystem_reports_itself_clean(self, config):
        command = config.get("CONSISTENCY_CHECK")
        if not command:
            pytest.skip("profile declares no consistency check")
        result = subprocess.run(["bash", "-c", command], capture_output=True,
                                text=True, timeout=3600)
        assert result.returncode == 0, (
            f"the storage reports itself damaged after this run:\n"
            f"$ {command}\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
