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

With --badge and --html it also writes a shields.io endpoint and a standalone
page, which is what turns the three buckets into something a README can show
and a reader can click into. The badge says "N passed · N known · N failing"
rather than pass/fail, because pass/fail is the question this tool exists to
reject.
"""

import argparse
import fnmatch
import html
import json
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


PAGE_CSS = """
:root { color-scheme: light dark; --fg: #1a1a1a; --dim: #666; --line: #d8d8d8;
        --bg: #fff; --ok: #1a7f37; --known: #9a6700; --bad: #cf222e; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e6e6e6; --dim: #9a9a9a; --line: #333; --bg: #0d1117;
          --ok: #3fb950; --known: #d29922; --bad: #f85149; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 2rem 1rem; background: var(--bg); color: var(--fg);
       font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
       Helvetica, Arial, sans-serif; }
main { max-width: 60rem; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
.sub { color: var(--dim); margin: 0 0 1.5rem; font-size: .9rem; }
.counts { display: flex; flex-wrap: wrap; gap: .75rem; margin: 0 0 2rem; padding: 0;
          list-style: none; }
.counts li { border: 1px solid var(--line); border-radius: .5rem; padding: .6rem 1rem;
             min-width: 7rem; }
.counts .n { display: block; font-size: 1.6rem; font-weight: 600; line-height: 1.1; }
.counts .l { color: var(--dim); font-size: .8rem; text-transform: uppercase;
             letter-spacing: .04em; }
.ok .n { color: var(--ok); } .known .n { color: var(--known); } .bad .n { color: var(--bad); }
h2 { font-size: 1.05rem; margin: 2rem 0 .5rem; }
h2 .dot { display: inline-block; width: .6rem; height: .6rem; border-radius: 50%;
          margin-right: .5rem; }
p.note { color: var(--dim); margin: 0 0 .75rem; font-size: .9rem; }
table { border-collapse: collapse; width: 100%; font-size: .88rem; }
th, td { text-align: left; vertical-align: top; padding: .5rem .6rem;
         border-bottom: 1px solid var(--line); }
th { color: var(--dim); font-weight: 600; font-size: .78rem; text-transform: uppercase;
     letter-spacing: .04em; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .92em; }
.kind { display: inline-block; border-radius: .3rem; padding: .05rem .4rem;
        font-size: .78rem; border: 1px solid var(--line); white-space: nowrap; }
footer { margin-top: 3rem; color: var(--dim); font-size: .82rem;
         border-top: 1px solid var(--line); padding-top: 1rem; }
@media (max-width: 40rem) { table, thead, tbody, tr, th, td { display: block; }
  th { display: none; } td { border: 0; padding: .15rem .6rem; }
  tr { border-bottom: 1px solid var(--line); padding: .5rem 0; display: block; } }
"""


def write_badge(path: Path, counts: dict) -> None:
    """A shields.io endpoint. Three numbers, not a verdict: "passing" would be
    a lie the day a known bug is the only thing failing, and "failing" would be
    a lie every other day."""
    if counts["failing"]:
        colour = "red"
    elif counts["known"]:
        colour = "yellow"
    else:
        colour = "brightgreen"
    message = f"{counts['passed']} passed"
    if counts["known"]:
        message += f" \u00b7 {counts['known']} known"
    if counts["failing"]:
        message += f" \u00b7 {counts['failing']} failing"
    path.write_text(json.dumps({
        "schemaVersion": 1,
        "label": "storage lab",
        "message": message,
        "color": colour,
    }) + "\n")


def write_page(path: Path, counts: dict, failures, stale, expected_failures,
               environment: str = "") -> None:
    def esc(value) -> str:
        return html.escape(str(value))

    def table(headers, rows) -> list[str]:
        out = ["<table>", "<thead><tr>"]
        out += [f"<th>{esc(h)}</th>" for h in headers]
        out += ["</tr></thead>", "<tbody>"]
        for row in rows:
            out.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
        out += ["</tbody>", "</table>"]
        return out

    body = [
        "<!doctype html>", '<html lang="en">', "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Storage lab results</title>",
        f"<style>{PAGE_CSS}</style>", "</head>", "<body>", "<main>",
        "<h1>Storage lab results</h1>",
        '<p class="sub">Every test runs against every backend. What a backend '
        "genuinely cannot do is declared, with a reason, rather than skipped - "
        "so a limitation is visible here instead of being invisible in a skip "
        "count.</p>",
        '<ul class="counts">',
        f'<li class="ok"><span class="n">{counts["passed"]}</span>'
        '<span class="l">passed</span></li>',
        f'<li class="known"><span class="n">{counts["known"]}</span>'
        '<span class="l">known issues</span></li>',
        f'<li class="bad"><span class="n">{counts["failing"]}</span>'
        '<span class="l">failing</span></li>',
        "</ul>",
    ]

    if failures:
        body += ['<h2><span class="dot" style="background:var(--bad)"></span>'
                 "Failing</h2>",
                 '<p class="note">Not declared anywhere. These block the run.</p>']
        body += table(["suite", "test", "message"],
                      [(f"<code>{esc(l)}</code>", f"<code>{esc(t)}</code>",
                        esc((m or "").splitlines()[0][:300]))
                       for l, t, m in failures])

    if stale:
        body += ['<h2><span class="dot" style="background:var(--known)"></span>'
                 "Stale declarations</h2>",
                 '<p class="note">Declared as known failures, but they passed. '
                 "Remove the entry, and whatever workaround went with it.</p>"]
        body += table(["suite", "test", "declared in"],
                      [(f"<code>{esc(l)}</code>", f"<code>{esc(t)}</code>",
                        f"<code>{esc(e['_source'])}</code>")
                       for l, t, e in stale])

    if expected_failures:
        body += ['<h2><span class="dot" style="background:var(--known)"></span>'
                 "Known issues</h2>",
                 '<p class="note">Declared, with a reason. A '
                 "<code>limitation</code> is something the backend cannot do "
                 "and never will; a <code>known-bug</code> is something it "
                 "should do and does not. Either way the test still runs, so "
                 "the day it starts passing this page says so.</p>"]
        body += table(["suite", "test", "kind", "why"],
                      [(f"<code>{esc(l)}</code>", f"<code>{esc(t)}</code>",
                        f'<span class="kind">{esc(e.get("kind", "?"))}</span>',
                        esc(" ".join(e.get("reason", "").split())))
                       for l, t, e in expected_failures])

    if not failures and not stale:
        body.append("<p><strong>Nothing new broke.</strong></p>")

    if environment:
        body += ["<footer>", "<pre><code>" + esc(environment.strip()) +
                 "</code></pre>", "</footer>"]
    body += ["</main>", "</body>", "</html>"]
    path.write_text("\n".join(body) + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report.py", description="Turn junit results into a verdict.")
    parser.add_argument("results_dir", type=Path)
    parser.add_argument("expectations", nargs="*", type=Path,
                        help="extra expectations.toml files, merged")
    parser.add_argument("--badge", type=Path,
                        help="write a shields.io endpoint JSON here")
    parser.add_argument("--html", type=Path,
                        help="write a standalone results page here")
    args = parser.parse_args(argv[1:])

    results_dir = args.results_dir
    expectation_files = [Path(__file__).resolve().parent.parent / "expectations.toml"]
    expectation_files += list(args.expectations)
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

    counts = {"passed": totals["passed"],
              "known": len(expected_failures),
              "failing": len(failures) + len(stale)}
    if args.badge:
        args.badge.parent.mkdir(parents=True, exist_ok=True)
        write_badge(args.badge, counts)
    if args.html:
        environment = ""
        env_file = results_dir / "environment.txt"
        if env_file.is_file():
            environment = env_file.read_text()
        args.html.parent.mkdir(parents=True, exist_ok=True)
        write_page(args.html, counts, failures, stale, expected_failures,
                   environment)

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
