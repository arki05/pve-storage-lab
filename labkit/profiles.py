"""Reading a storage profile.

A profile describes itself in profile.env, so `--profile-dir <dir>` is the only
thing a caller needs to name. That matters once several profiles share one lab:
per-profile test directories and source trees cannot be passed as single global
flags any more.

  NAME    what this storage is called in results and image names
  DISKS   how many lab test disks it wants (default 1)
  TESTS   optional, relative to the profile dir - extra pytest files
  SOURCE  optional, relative to the profile dir - a working tree the profile
          builds and installs instead of using a published package

DISKS is the reason this exists. Profiles used to hardcode virtio-labdisk1,
which works only while exactly one profile exists and silently steals another
storage's disk the moment one does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def _read(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    env = path / "profile.env"
    if not env.exists():
        return values
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def profile_name(path: Path) -> str:
    return _read(path).get("NAME", Path(path).name)


@dataclass(frozen=True)
class Profile:
    path: Path
    name: str
    disks: int
    tests: Path | None
    source: Path | None
    has_bake: bool

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        path = Path(path).resolve()
        values = _read(path)

        def optional(key: str) -> Path | None:
            rel = values.get(key)
            if not rel:
                return None
            resolved = (path / rel).resolve()
            return resolved if resolved.exists() else None

        return cls(
            path=path,
            name=values.get("NAME", path.name),
            disks=int(values.get("DISKS", 1)),
            tests=optional("TESTS"),
            source=optional("SOURCE"),
            has_bake=(path / "bake.sh").exists(),
        )


def assign_disks(profiles: list[Profile]) -> dict[str, list[str]]:
    """Give each profile a disjoint range of the lab's test disks."""
    assignment: dict[str, list[str]] = {}
    offset = 0
    for profile in profiles:
        assignment[profile.name] = [
            f"/dev/disk/by-id/virtio-labdisk{offset + i}"
            for i in range(1, profile.disks + 1)
        ]
        offset += profile.disks
    return assignment
