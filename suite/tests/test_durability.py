"""Surviving an unclean stop.

Every other test here shuts a guest down politely. A power cut does not, and
the guarantee that matters is narrower and stronger than "the data came back":

  data written with fsync, before the machine died, must still be there.

That is the promise the storage stack actually makes, and it is where journal
replay and write-cache bugs live. Nothing else in this suite would notice a
backend that acknowledged an fsync and then lost the write, because everything
else flushes on the way out.

Deliberately *not* asserted: that un-fsynced writes survive. They may or may
not, and a backend is not wrong either way - claiming otherwise would make this
a test of luck.
"""

import pytest

from conftest import needs_images, needs_lxc
from helpers.wait import wait_for

MARKER = "/srv/durable.txt"
CONTENT = "written-and-fsynced"


def _write_durable(guest) -> None:
    # sync on the file, then on the filesystem: after this returns, the storage
    # has said the data is safe.
    guest.run(f"printf '%s' '{CONTENT}' > {MARKER} && sync {MARKER} && sync")


@needs_lxc
class TestContainerDurability:
    def test_fsynced_data_survives_a_hard_stop(self, create_ct, pve, node):
        ct = create_ct(start=True)
        _write_durable(ct.exec())

        # A hard stop, not a shutdown: no unmount, no flush, nothing given a
        # chance to tidy up.
        pve.create(f"/nodes/{node}/lxc/{ct.vmid}/status/stop")
        wait_for(lambda: ct.status() == "stopped", timeout=120,
                 desc="the container to be killed")

        ct.start()
        content = ct.exec().run(f"cat {MARKER}").strip()
        assert content == CONTENT, (
            f"fsynced data did not survive an unclean stop: got {content!r}. "
            f"The write was acknowledged before the container was killed.")

    def test_the_filesystem_is_usable_after_a_hard_stop(self, create_ct, pve, node):
        """A volume that comes back read-only, or needs a manual check, is a
        failure even if the data is technically intact."""
        ct = create_ct(start=True)
        _write_durable(ct.exec())
        pve.create(f"/nodes/{node}/lxc/{ct.vmid}/status/stop")
        wait_for(lambda: ct.status() == "stopped", timeout=120,
                 desc="the container to be killed")

        ct.start()
        ct.exec().run("echo still-writable > /srv/after-crash.txt && sync")
        assert "still-writable" in ct.exec().run("cat /srv/after-crash.txt")


@needs_images
class TestVMDurability:
    def test_fsynced_data_survives_a_hard_stop(self, create_vm, pve, node):
        vm = create_vm()
        vm.start()
        vm.wait_agent()
        _write_durable(vm.agent())

        pve.create(f"/nodes/{node}/qemu/{vm.vmid}/status/stop")
        wait_for(lambda: vm.status() == "stopped", timeout=120,
                 desc="the guest to be killed")

        vm.start()
        vm.wait_agent()
        content = vm.agent().run(f"cat {MARKER}").strip()
        assert content == CONTENT, (
            f"fsynced data did not survive an unclean stop: got {content!r}")

    def test_repeated_hard_stops_do_not_accumulate_damage(self, create_vm, pve,
                                                          node):
        """One crash is recoverable almost everywhere. The interesting question
        is whether the third one still is."""
        vm = create_vm()
        for round_number in range(3):
            vm.start()
            vm.wait_agent()
            agent = vm.agent()
            agent.run(f"printf 'round-{round_number}' > {MARKER} && "
                      f"sync {MARKER} && sync")
            pve.create(f"/nodes/{node}/qemu/{vm.vmid}/status/stop")
            wait_for(lambda: vm.status() == "stopped", timeout=120,
                     desc=f"the guest to be killed (round {round_number})")

        vm.start()
        vm.wait_agent()
        content = vm.agent().run(f"cat {MARKER}").strip()
        assert content == "round-2", (
            f"after three unclean stops the last fsynced write is missing: "
            f"got {content!r}")
