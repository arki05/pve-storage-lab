"""Writing verified data while something disruptive happens.

A test that stops the guest, does the operation and compares checksums proves
the data at rest survived. It says nothing about writes that were *in flight*
when the operation happened - and those are what a live migration, a snapshot
of a running guest or an online resize actually risk.

fio verifies its own writes: with --verify=crc32c --do_verify=1 it records a
checksum per block and reads them back. If the operation loses or reorders a
write, fio reports the corruption itself, which is far more sensitive than
hashing a few files afterwards.
"""

from __future__ import annotations

import time


class VerifiedLoad:
    """Run fio with verification inside a guest, in the background."""

    def __init__(self, guest, path: str = "/srv/fio-load.dat",
                 size_mb: int = 256, runtime: int = 90, rate: str | None = "20m"):
        self.guest = guest
        self.path = path
        self.size_mb = size_mb
        self.runtime = runtime
        # Throttled on purpose. The question is whether writes *in flight*
        # survive, not whether the guest can outrun the operation: an
        # unthrottled randwrite dirties blocks faster than a live migration's
        # drive-mirror converges, and the test then fails for convergence
        # rather than correctness. That is a real thing to test, but it is a
        # different test - pass rate=None for it.
        self.rate = rate
        self.output = f"{path}.out"

    def start(self) -> "VerifiedLoad":
        throttle = f"--rate={self.rate} " if self.rate else ""
        self.guest.run(
            f"nohup fio --name=load --filename={self.path} "
            f"--size={self.size_mb}M --rw=randwrite --bs=4k "
            f"--verify=crc32c --do_verify=1 --verify_fatal=1 "
            f"{throttle}"
            f"--time_based --runtime={self.runtime} "
            f"--output={self.output} >/dev/null 2>&1 & echo started")
        return self

    def settle(self, seconds: int = 8) -> "VerifiedLoad":
        """Let it get going, so the operation really does land mid-write."""
        time.sleep(seconds)
        return self

    def running(self) -> bool:
        return self.guest.exec("pgrep -x fio")["exitcode"] == 0

    def wait(self, timeout: int = 300) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.running():
                return
            time.sleep(2)

    def verify(self, context: str = "") -> None:
        """Assert fio saw no corruption.

        `guest` may have been reassigned - a live migration moves the guest to
        another node, and the agent has to follow it.
        """
        self.wait()
        out = self.guest.read_file(self.output)
        suffix = f" ({context})" if context else ""
        bad = [line for line in out.splitlines()
               if "verify" in line.lower() and "fail" in line.lower()]
        assert not bad, (
            f"fio reported verification failures{suffix} - writes in flight "
            f"were lost or reordered:\n" + "\n".join(bad[:8]))
        assert "err= 0" in out or "Run status" in out, (
            f"fio did not finish cleanly{suffix}:\n{out[-1500:]}")
