"""Turn "did the operation succeed?" into "did it succeed without losing data?".

Seed known files, record their checksums, run the operation, verify. Dropping
a DataGuard into a test is the difference between catching a storage backend
that silently drops writes and not.
"""


class DataGuard:
    def __init__(self, guest):
        self.guest = guest
        self.checksums: dict[str, str] = {}

    def seed(self, size_mb: int = 8, count: int = 3,
             base_path: str = "/srv/guard") -> "DataGuard":
        self.guest.run(f"mkdir -p {base_path.rsplit('/', 1)[0]}")
        for i in range(count):
            path = f"{base_path}-{i}.dat"
            self.guest.run(
                f"dd if=/dev/urandom of={path} bs=1M count={size_mb} "
                f"conv=fsync status=none"
            )
            self.checksums[path] = self.guest.md5(path)
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

    def verify_missing(self, context: str = "") -> None:
        """Assert the seeded files are gone - for rollback-to-before-seed."""
        suffix = f" ({context})" if context else ""
        for path in self.checksums:
            out = self.guest.exec(f"test -e {path}")
            assert out["exitcode"] != 0, (
                f"{path} still present{suffix}; rollback did not take effect"
            )
