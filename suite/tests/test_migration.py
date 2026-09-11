"""Migration between nodes.

These skip on a single-node lab, which is the default: the backends this suite
targets are local, so a second node buys a full data copy per test and little
coverage. They are here so that pointing the harness at a two-node lab covers
migration immediately rather than needing tests written first.

Offline migration of a local volume is an rsync of the volume to the other
node, so it exercises the same metadata-copying path as move-volume - which is
exactly where backends that keep state in extended attributes come apart.
"""

from conftest import needs_lxc, needs_images, needs_snapshots
from helpers.data_guard import DataGuard
from helpers.wait import wait_for, wait_for_task


def _migrate(pve, node, kind, vmid, target, online=False, timeout=1800):
    wait_for_task(pve, pve.create(f"/nodes/{node}/{kind}/{vmid}/migrate",
                                  target=target, online=1 if online else 0),
                  timeout=timeout)
    # The migrate task finishes before the guest's lock is released, so
    # starting it straight afterwards fails with "VM is locked (migrate)".
    # Waiting on the task is not enough on its own.
    def unlocked() -> bool:
        return not pve.get(f"/nodes/{target}/{kind}/{vmid}/config").get("lock")
    wait_for(unlocked, timeout=180, interval=2,
             desc=f"{kind} {vmid} lock to clear after migrating to {target}")


@needs_images
class TestVMMigration:
    def test_offline_migrate(self, create_vm, pve, node, node2):
        vm = create_vm()
        _migrate(pve, node, "qemu", vm.vmid, node2)
        vm.node = node2
        assert vm.status() == "stopped"

    def test_offline_migrate_preserves_data(self, create_vm, pve, node, node2):
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        guard = DataGuard(vm.agent()).seed()
        vm.shutdown()

        _migrate(pve, node, "qemu", vm.vmid, node2)
        vm.node = node2
        vm.start()
        vm.wait_agent()
        guard.guest = vm.agent()
        guard.verify("after offline migration")

    def test_round_trip_preserves_data(self, create_vm, pve, node, node2):
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        guard = DataGuard(vm.agent()).seed()
        vm.shutdown()

        _migrate(pve, node, "qemu", vm.vmid, node2)
        vm.node = node2
        _migrate(pve, node2, "qemu", vm.vmid, node)
        vm.node = node

        vm.start()
        vm.wait_agent()
        guard.guest = vm.agent()
        guard.verify("after migrating there and back")

    @needs_snapshots
    def test_snapshot_survives_migration(self, create_vm, pve, node, node2):
        """Snapshots are backend state rather than config, so they are the
        thing most likely to be silently dropped by a migration."""
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        guard = DataGuard(vm.agent()).seed()
        wait_for_task(pve, pve.create(f"/nodes/{node}/qemu/{vm.vmid}/snapshot",
                                      snapname="premigrate"))
        vm.shutdown()

        _migrate(pve, node, "qemu", vm.vmid, node2)
        vm.node = node2

        names = [s["name"] for s in pve.get(f"/nodes/{node2}/qemu/{vm.vmid}/snapshot")]
        assert "premigrate" in names, f"the snapshot did not survive migration: {names}"

        wait_for_task(pve, pve.create(
            f"/nodes/{node2}/qemu/{vm.vmid}/snapshot/premigrate/rollback"), 300)
        vm.start()
        vm.wait_agent()
        guard.guest = vm.agent()
        guard.verify("after rolling back a migrated snapshot")


@needs_lxc
class TestCTMigration:
    def test_offline_migrate_preserves_data(self, create_ct, pve, node, node2):
        ct = create_ct(start=True)
        guard = DataGuard(ct.exec()).seed()
        ct.stop()

        _migrate(pve, node, "lxc", ct.vmid, node2)
        ct.node = node2
        ct.start()
        guard.guest = ct.exec()
        guard.verify("after offline migration")

    def test_round_trip_preserves_data(self, create_ct, pve, node, node2):
        ct = create_ct(start=True)
        guard = DataGuard(ct.exec()).seed()
        ct.stop()

        _migrate(pve, node, "lxc", ct.vmid, node2)
        ct.node = node2
        _migrate(pve, node2, "lxc", ct.vmid, node)
        ct.node = node

        ct.start()
        guard.guest = ct.exec()
        guard.verify("after migrating there and back")
