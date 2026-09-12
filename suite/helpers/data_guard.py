"""Turn "did the operation succeed?" into "did it succeed without losing data?".

Seed known files, record their checksums, run the operation, verify. Dropping
a DataGuard into a test is the difference between catching a storage backend
that silently drops writes and not.
"""


from helpers.payloads import fill_command, occupancy


# Written by the guest itself on every boot, so they say nothing about whether
# a copy preserved anything. lost+found is here because a round trip through a
# storage that allocates a raw ext4 image brings one back with it.
VOLATILE = (
    "run", "tmp", "var/tmp", "var/log", "var/cache", "var/lib/dhcp",
    "var/lib/systemd", "lost+found",
)


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

    def tree_fingerprint(self, path: str = "/", deep: bool = False) -> str:
        """A digest of a whole tree.

        Cheap by default: stat only, no file contents. A Debian rootfs is tens
        of thousands of files, and hashing all of them after every operation
        would add hours across a full matrix for very little signal. The
        stat-only pass still catches files vanishing, changing size, or
        changing ownership or mode - which is what a broken copy usually does.

        `deep=True` hashes contents as well. Worth it once per pair in the
        cross-storage tests, not after every operation.

        Volatile paths are pruned. A guest that is stopped and started again
        writes to them whatever the storage did, so including them made this
        compare a running rootfs against itself across a reboot - which no
        backend can satisfy. Measured on an untouched container: a plain
        restart with no move at all changed fifteen entries, all of them
        systemd-private temp directories with fresh random names, a grown
        wtmp, a rotated journal and a rewritten DHCP lease.
        """
        prune = " -o ".join(f"-path ./{d}" for d in VOLATILE)
        if deep:
            command = (f"cd {path} && find . -xdev \\( {prune} \\) -prune -o "
                       f"-type f -print0 | sort -z | "
                       f"xargs -0 md5sum 2>/dev/null | md5sum")
        else:
            command = (f"cd {path} && find . -xdev \\( {prune} \\) -prune -o "
                       f"-printf '%p|%y|%m|%U|%G|%s\n' | sort | md5sum")
        return self.guest.run(command, timeout=900).split()[0]

    def verify_missing(self, context: str = "") -> None:
        """Assert the seeded files are gone - for rollback-to-before-seed."""
        suffix = f" ({context})" if context else ""
        for path in self.checksums:
            out = self.guest.exec(f"test -e {path}")
            assert out["exitcode"] != 0, (
                f"{path} still present{suffix}; rollback did not take effect"
            )
