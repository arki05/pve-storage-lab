#!/usr/bin/env python3
"""Print what each backend claims to support.

The capabilities drive the suite's skip logic, so this is also a statement of
what was and was not tested - and the differences are worth reading on their
own. "Roll back to last Tuesday" works on some of these and not others, and
that is a design constraint, not a bug in either.

    tools/capabilities.py [--markdown] [profile-dir ...]
"""

import sys
from pathlib import Path

PREFIXES = ("SUPPORTS_", "ENFORCES_", "ROLLBACK_", "RESIZE_")


def read(path: Path) -> dict[str, str]:
    values = {}
    caps = path / "capabilities.env"
    if not caps.exists():
        return values
    for line in caps.read_text().splitlines():
        line = line.strip()
        if line.startswith(PREFIXES) and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def name_of(path: Path) -> str:
    env = path / "profile.env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("NAME="):
                return line.partition("=")[2].strip()
    return path.name


def main(argv: list[str]) -> int:
    markdown = "--markdown" in argv
    dirs = [Path(a) for a in argv[1:] if not a.startswith("--")]
    if not dirs:
        root = Path(__file__).resolve().parent.parent
        dirs = sorted(p for p in (root / "profiles").iterdir() if p.is_dir())

    data = {name_of(d): read(d) for d in dirs}
    keys = sorted({k for v in data.values() for k in v})
    names = list(data)

    def cell(value: str) -> str:
        if not markdown:
            return value
        return {"true": "yes", "false": "no"}.get(value, value)

    if markdown:
        print("| capability | " + " | ".join(names) + " |")
        print("|---" * (len(names) + 1) + "|")
        for key in keys:
            row = " | ".join(cell(data[n].get(key, "–")) for n in names)
            print(f"| `{key}` | {row} |")
    else:
        print(f"{'capability':34}" + "".join(f"{n:>11}" for n in names))
        for key in keys:
            print(f"{key:34}" + "".join(f"{data[n].get(key, '-'):>11}" for n in names))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
