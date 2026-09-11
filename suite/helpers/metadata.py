"""Metadata, not just contents.

A move that preserves every byte but loses ownership, permissions, hardlinks,
symlinks or extended attributes is broken - and a checksum of file contents
passes it happily. This is the class the bcachefs xattr bug belonged to, and it
was only noticed because rsync happened to fail loudly rather than quietly drop
something.

The fixture is deliberately small and specific. A handful of files with known,
unusual attributes catches more than hashing a whole tree does, and costs
almost nothing to verify.
"""

from __future__ import annotations

FIXTURE = "/srv/meta"

# Each entry is a shell fragment creating one thing worth preserving, and the
# fingerprint below reads back exactly what it set.
BUILD = r"""
set -e
rm -rf {root}; mkdir -p {root}
cd {root}

# Ownership and a non-trivial mode.
echo owned > owned.txt
chown 1234:5678 owned.txt
chmod 4711 owned.txt

# A hardlink pair: two names, one inode. A copy that does not preserve links
# turns this into two files and doubles the space.
echo linked > link-target.txt
ln link-target.txt link-second.txt

# A symlink, including a deliberately dangling one.
ln -s link-target.txt symlink.txt
ln -s /nonexistent dangling.txt

# An extended attribute in the user namespace, which every filesystem here
# supports - unlike the filesystem-internal ones.
echo attred > xattr.txt
setfattr -n user.lab.marker -v "kept" xattr.txt

# A file with holes, and a timestamp far in the past.
truncate -s 4M sparse.bin
dd if=/dev/urandom of=sparse.bin bs=4k count=1 conv=notrunc status=none
touch -d '2001-02-03 04:05:06' sparse.bin

# A directory with a sticky bit and a nested empty directory.
mkdir -p dir/empty
chmod 1777 dir
"""

# stat-only: no file contents are read, so this stays cheap enough to run after
# every operation.
FINGERPRINT = (
    "cd {root} && "
    "find . -printf '%p|%y|%m|%U|%G|%s|%l|%i\\n' | sort && "
    "echo '--links--' && "
    "find . -type f -links +1 -printf '%p|%n\\n' | sort && "
    "echo '--xattr--' && "
    "getfattr -R -d -m 'user\\.' --absolute-names . 2>/dev/null "
    "| grep -v '^#' | grep . | sort && "
    "echo '--times--' && "
    "find . -printf '%p|%T@\\n' | sort"
)


class MetadataFixture:
    """Create files with known metadata, and check it survived."""

    def __init__(self, guest, root: str = FIXTURE):
        self.guest = guest
        self.root = root
        self.expected: str | None = None

    def create(self) -> "MetadataFixture":
        self.guest.run(BUILD.format(root=self.root), timeout=180)
        self.expected = self._read()
        return self

    def _read(self) -> str:
        # Inode numbers are in the listing for the hardlink check, but they are
        # not stable across a copy - strip them from the comparison.
        raw = self.guest.run(FINGERPRINT.format(root=self.root), timeout=180)
        lines = []
        for line in raw.splitlines():
            parts = line.split("|")
            lines.append("|".join(parts[:-1]) if len(parts) == 8 else line)
        return "\n".join(lines)

    def verify(self, context: str = "") -> None:
        assert self.expected is not None, "create() was never called"
        actual = self._read()
        if actual == self.expected:
            return

        expected_lines = self.expected.splitlines()
        actual_lines = actual.splitlines()
        changed = [
            f"  expected: {e}\n  actual:   {a}"
            for e, a in zip(expected_lines, actual_lines) if e != a
        ][:8]
        missing = set(expected_lines) - set(actual_lines)
        suffix = f" ({context})" if context else ""
        raise AssertionError(
            f"file metadata changed{suffix} - contents may be intact but "
            f"ownership, modes, links, xattrs or timestamps are not:\n"
            + "\n".join(changed)
            + (f"\n  missing entirely: {sorted(missing)[:5]}" if missing else "")
        )
