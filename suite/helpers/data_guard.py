"""Turn "did the operation succeed?" into "did it succeed without losing data?".

Seed known files, record their checksums, run the operation, verify. Dropping
a DataGuard into a test is the difference between catching a storage backend
that silently drops writes and not.
"""


from helpers.payloads import fill_command, occupancy


class DataGuard:
    def __init__(self, guest):
        self.guest = guest
        self.checksums: dict[str, str] = {}
        self.occupancy: dict[str, tuple[int, int]] = {}
        self.pattern = "random"

    def seed(self, size_mb: int = 8, count: int = 3,
             base_path: str = "/srv/guard", pattern: str = "random") -> "DataGuard":
        """Seed files of a given shape. The shape matters: see helpers.payloads
        for which classes of bug each one exposes."""
        self.pattern = pattern
        self.guest.run(f"mkdir -p {base_path.rsplit('/', 1)[0]}")
        for i in range(count):
            path = f"{base_path}-{i}.dat"
            self.guest.run(fill_command(path, size_mb, pattern), timeout=300)
            self.checksums[path] = self.guest.md5(path)
            self.occupancy[path] = occupancy(self.guest, path)
        # Push everything past any writeback cache above the storage layer.
        # Without this, an offline migration or a move can legitimately lose
        # the most recent writes and the test would be blaming the backend
        # for the test's own omission.
        self.guest.run("sync")
        return self

    def verify(self, context: str = "") -> None:
        suffix = f" ({context})" if context else ""
        for path, expected in self.checksums.items():
            actual = self.guest.md5(path)
            assert expected == actual, (
                f"data corruption in {path}{suffix}: "
                f"expected {expected}, got {actual}"
            )

    def verify_sparse(self, context: str = "", slack: float = 1.5) -> None:
        """Assert sparse files did not inflate.

        Contents surviving is not the whole story for a sparse file: a copy
        that writes the holes out as real zeros preserves every checksum and
        silently turns a 1 GiB mostly-empty image into 1 GiB of allocated
        blocks. On a volume sized for the sparse version, that does not fit.
        """
        if self.pattern != "sparse":
            return
        suffix = f" ({context})" if context else ""
        for path, (apparent, allocated_before) in self.occupancy.items():
            _, allocated_now = occupancy(self.guest, path)
            budget = max(int(allocated_before * slack), 1 << 20)
            assert allocated_now <= budget, (
                f"{path} inflated{suffix}: allocated {allocated_now} bytes, was "
                f"{allocated_before} of {apparent} apparent - the holes were "
                f"written out as real blocks"
            )

    def verify_missing(self, context: str = "") -> None:
        """Assert the seeded files are gone - for rollback-to-before-seed."""
        suffix = f" ({context})" if context else ""
        for path in self.checksums:
            out = self.guest.exec(f"test -e {path}")
            assert out["exitcode"] != 0, (
                f"{path} still present{suffix}; rollback did not take effect"
            )
