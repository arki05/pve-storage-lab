#!/usr/bin/env python3
"""Turn junit results into a verdict and a readable summary.

Exists because "did the suite pass" is the wrong question once a backend has
known defects. A run is good when nothing *new* broke - so the exit status here
is driven by unexpected failures, not by the raw count.

Three buckets come out:

  failures              not listed in expectations. These block.
  expected failures     listed, and failed. Reported, do not block.
  stale expectations    listed, and passed. Surfaced loudly: without this the
                        expectations file rots into a permanent mute button,
                        and a fix nobody noticed keeps its workaround forever.

Writes markdown to stdout, and to $GITHUB_STEP_SUMMARY when running in Actions,
so failures are readable in the job page without downloading an artifact.
"""

import fnmatch
import os
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path


def load_expectations(paths: list[Path]) -> list[dict]:
    entries = []
    for path in paths:
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        for entry in data.get("expected", []):
            entry["_source"] = str(path)
            entries.append(entry)
    return entries


def matches(entry: dict, label: str, test_id: str, markers: set[str]) -> bool:
    scope = entry.get("profile") or entry.get("pair")
    if scope and scope != label:
        return False
    # A capability marker is the durable way to declare a limitation: test
    # names drift when they are renamed or split, capabilities do not.
    wanted = entry.get("marker")
    if wanted and wanted not in markers:
        return False
    if "test" in entry and not fnmatch.fnmatch(test_id, entry["test"]):
        return False
    return bool(wanted) or "test" in entry


def parse_junit(path: Path) -> tuple[str, list[dict]]:
    # junit-<label>.xml
    label = path.stem.removeprefix("junit-")
    results = []
    for case in ET.parse(path).getroot().iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        test_id = f"{classname}::{name}" if classname else name
        outcome = "passed"
        message = ""
        markers: set[str] = set()
        for child in case:
            if child.tag in ("failure", "error"):
                outcome = child.tag
                message = (child.get("message") or "").strip()
            elif child.tag == "skipped":
                outcome = "skipped"
            elif child.tag == "properties":
                for prop in child:
                    if prop.get("name") == "markers":
                        markers = set((prop.get("value") or "").split(","))
        results.append({"id": test_id, "outcome": outcome, "message": message,
                        "markers": markers})
    return label, results


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: report.py RESULTS_DIR [EXTRA_EXPECTATIONS...]", file=sys.stderr)
        return 2

    results_dir = Path(argv[1])
    expectation_files = [Path(__file__).resolve().parent.parent / "expectations.toml"]
    expectation_files += [Path(p) for p in argv[2:]]
    expectations = load_expectations(expectation_files)

    failures, expected_failures, stale = [], [], []
    totals = {"passed": 0, "failed": 0, "skipped": 0}

    junits = sorted(results_dir.glob("junit-*.xml"))
    for junit in junits:
        label, results = parse_junit(junit)
        for result in results:
            entry = next((e for e in expectations
                          if matches(e, label, result["id"],
                                     result.get("markers", set()))), None)
            if result["outcome"] == "skipped":
                totals["skipped"] += 1
                continue
            if result["outcome"] == "passed":
                totals["passed"] += 1
                # An xfail that starts passing is reported by pytest itself;
                # this catches the same thing for expectations declared here.
                if entry is not None:
                    stale.append((label, result["id"], entry))
                continue
            totals["failed"] += 1
            if entry is not None:
                expected_failures.append((label, result["id"], entry))
            else:
                failures.append((label, result["id"], result["message"]))

    lines = ["# Storage suite results", ""]
    if not junits:
        lines.append("**No results found.** The run did not get as far as testing.")
    lines.append(
        f"{totals['passed']} passed, {totals['failed']} failed, "
        f"{totals['skipped']} skipped across {len(junits)} suite(s)."
    )
    lines.append("")

    if failures:
        lines += ["## Failures", "",
                  "| suite | test | message |", "|---|---|---|"]
        for label, test_id, message in failures:
            short = message.replace("|", "\\|").splitlines()[0][:160] if message else ""
            lines.append(f"| `{label}` | `{test_id}` | {short} |")
        lines.append("")

    if stale:
        lines += ["## Stale expectations", "",
                  "These are listed as known failures but **passed**. "
                  "Remove the entry - and whatever workaround goes with it.", "",
                  "| suite | test | declared in |", "|---|---|---|"]
        for label, test_id, entry in stale:
            lines.append(f"| `{label}` | `{test_id}` | `{entry['_source']}` |")
        lines.append("")

    if expected_failures:
        lines += ["## Expected failures", "",
                  "Known, and not blocking.", "",
                  "| suite | test | kind | reason |", "|---|---|---|---|"]
        for label, test_id, entry in expected_failures:
            reason = " ".join(entry.get("reason", "").split())[:220]
            lines.append(
                f"| `{label}` | `{test_id}` | {entry.get('kind', '?')} | {reason} |")
        lines.append("")

    if not failures and not stale:
        lines.append("Nothing new broke.")

    summary = "\n".join(lines)
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as handle:
            handle.write(summary + "\n")

    # Stale expectations block too: an expectation that no longer holds is a
    # claim about the system that has quietly become false, which is exactly
    # the thing this file exists to prevent.
    return 1 if (failures or stale or not junits) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
