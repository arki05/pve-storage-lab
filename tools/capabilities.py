#!/usr/bin/env python3
"""Print what each backend is declared unable to do, and why.

This used to read `SUPPORTS_*` keys out of capabilities.env, back when those
decided which tests ran. They decide nothing now - every test runs against
every backend - and the keys were read by no test at all, so printing them
described a mechanism that no longer existed.

The real statement of what a backend cannot do is expectations.toml: each
entry names a capability marker or a test, says whether it is a `limitation`
(cannot, and never will) or a `known-bug` (should, and does not), and carries
the reason. That is what this prints.

    tools/capabilities.py [--markdown] [profile-dir ...]
"""

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        return tomllib.load(handle).get("expected", [])


def main(argv: list[str]) -> int:
    markdown = "--markdown" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    dirs = [Path(a) for a in args] or sorted(
        p for p in (ROOT / "profiles").iterdir() if p.is_dir())

    rows = []
    for directory in dirs:
        for entry in load(directory / "expectations.toml"):
            rows.append((
                entry.get("profile") or entry.get("pair") or directory.name,
                entry.get("marker") or entry.get("test", "?"),
                entry.get("kind", "?"),
                " ".join(entry.get("reason", "").split()),
            ))
    for entry in load(ROOT / "expectations.toml"):
        rows.append((entry.get("profile") or entry.get("pair") or "(any)",
                     entry.get("marker") or entry.get("test", "?"),
                     entry.get("kind", "?"),
                     " ".join(entry.get("reason", "").split())))

    if not rows:
        print("No declared limitations. Either everything works, or nobody "
              "has run it yet.")
        return 0

    rows.sort()
    if markdown:
        print("| backend | capability | kind | why |")
        print("|---|---|---|---|")
        for backend, what, kind, why in rows:
            why = why.replace("|", "\\|")
            print(f"| `{backend}` | `{what}` | {kind} | {why} |")
    else:
        width = max(len(r[0]) for r in rows)
        for backend, what, kind, why in rows:
            print(f"{backend:<{width}}  {kind:<11} {what}")
            print(f"{'':<{width}}  {'':<11} {why[:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
