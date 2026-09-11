"""Fill patterns, as a test dimension.

The shape of the data in a volume changes how a storage backend behaves, and
several classes of bug only appear for one shape:

  random        incompressible. The only honest way to test a size limit -
                a compressing backend charges a zero fill almost nothing, so
                a "quota not enforced" failure may be measuring the compressor.

  compressible  a short random line repeated. The repeat period has to be well
                below the compression block size (ZFS records at 128 KiB,
                bcachefs extents) or the compressor never sees the repetition -
                repeating a 1 MiB random block looks compressible to a human
                and is incompressible to lz4. This shape makes logical and
                physical size diverge sharply, which is where "the volume holds
                more than its nominal size" comes from.

  sparse        actual holes, not written zeros. VM images are mostly holes,
                and rsync, qemu-img and tar disagree about preserving them. A
                volume that fits on the source and inflates to full size on the
                destination is the same class of bug as the compression one and
                far more likely in practice.

Written zeros sit between compressible and sparse: ZFS elides all-zero blocks
into holes, so they behave like sparse rather than like compressible data.
That is why zeros are not one of the shapes - they measure a special case
rather than a class.
"""

PATTERNS = ("random", "compressible", "sparse")


def fill_command(path: str, size_mb: int, pattern: str) -> str:
    """A shell command that writes `size_mb` of `pattern` to `path`."""
    if pattern == "random":
        return (f"dd if=/dev/urandom of={path} bs=1M count={size_mb} "
                f"conv=fsync status=none")

    if pattern == "compressible":
        # An ~88-byte random line repeated: a period far below any compression
        # block, so it collapses, while still being distinct per file so the
        # checksum means something.
        return (f"line=$(head -c 64 /dev/urandom | base64 | tr -d '\\n'); "
                f"yes \"$line\" | head -c {size_mb}M > {path}; sync")

    if pattern == "sparse":
        # Mostly hole, with real data at both ends and in the middle, so there
        # is something to checksum and the holes are unambiguous.
        return (
            f"truncate -s {size_mb}M {path}; "
            f"dd if=/dev/urandom of={path} bs=64k count=1 conv=notrunc,fsync "
            f"  status=none; "
            f"dd if=/dev/urandom of={path} bs=64k count=1 seek={size_mb * 8} "
            f"  conv=notrunc,fsync status=none; "
            f"dd if=/dev/urandom of={path} bs=64k count=1 seek={size_mb * 16 - 1} "
            f"  conv=notrunc,fsync status=none; sync"
        )

    raise ValueError(f"unknown payload pattern: {pattern}")


def occupancy(guest, path: str) -> tuple[int, int]:
    """(apparent bytes, allocated bytes) for a file inside a guest.

    A sparse file has allocated far below apparent. After a move that does not
    preserve holes the two converge, which is how inflation is detected.
    """
    out = guest.run(f"stat -c '%s %b %B' {path}").split()
    apparent = int(out[0])
    allocated = int(out[1]) * int(out[2])
    return apparent, allocated
