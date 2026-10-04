#!/usr/bin/env python3
"""Fail if any cocotb JUnit results file has a failure/error, or one is missing.

    check_results.py <results.xml> [<results.xml> ...]
    check_results.py --benches <dir> [--pending "a b"] [<results.xml> ...]
    check_results.py --results-dir <dir> --expect "t1 t2 ..."

Used as the gate in the repo-root Makefile (`make tb`, `make uvm`) and at
the end of every src/verif/ regression tier: the per-TB cocotb runs and the
tier loops keep going on failure, so this reads what they left behind and
turns it into one exit code.

With --benches, every <dir>/<group>/cxp_<name>/Makefile is a bench that must have
left <dir>/cxp_<name>/results.xml; a bench that failed to build leaves none
and fails the gate.  --pending names benches that are parked on purpose:
they are skipped and listed on every run so they are not forgotten.

With --results-dir plus --expect, every named test must have left
<dir>/<name>/results.xml with at least one passing case; a test whose
simulation never started leaves none and fails the gate.  That is how a
PyUVM tier turns "one of my tests failed" into a non-zero exit.

A skipped case fails the gate unless it is named in --allow-skip
("<file-stem>.<test>", a bare test name, or "<bench-dir>:*" for every
skip of one bench, e.g. a second build that runs one test): a filter left in the
environment or a stray `skip=True` must not shrink a regression silently.

Every `expected_fail.json` next to a results.xml (written by
`cxp_testcase.cxp_test(finding=...)`) is listed as TAGGED: those tests are
red for a named RTL finding and pass only while they stay red.
"""
import argparse
import glob
import json
import os
import sys
import xml.etree.ElementTree as ET


def expected_results(bench_dir: str, pending: set[str]) -> tuple[list[str], list[str]]:
    """(results.xml path per bench that must run, pending bench names found)."""
    want, parked = [], []
    for mk in sorted(glob.glob(os.path.join(bench_dir, "*", "cxp_*", "Makefile"))):
        d = os.path.dirname(mk)
        name = os.path.basename(d)[len("cxp_"):]
        if name in pending:
            parked.append(name)
        else:
            want.append(os.path.join(d, "results.xml"))
    return want, parked


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--benches", help="directory whose */cxp_*/Makefile are expected benches")
    ap.add_argument("--pending", default="", help="bench names parked on purpose")
    ap.add_argument("--results-dir", help="directory holding <name>/results.xml for --expect")
    ap.add_argument("--expect", default="", help="test names that must each have left a results.xml")
    ap.add_argument("--allow-skip", default="",
                    help="test names (or <file-stem>.<test>) allowed to be skipped")
    ap.add_argument("results", nargs="*")
    a = ap.parse_args(argv)

    pending = set(a.pending.split())
    paths = [p for p in a.results if os.path.isfile(p)]
    missing: list[str] = []
    if a.expect:
        if not a.results_dir:
            print("check_results: --expect needs --results-dir", file=sys.stderr)
            return 2
        want = [os.path.join(a.results_dir, t, "results.xml") for t in a.expect.split()]
        missing = [p for p in want if not os.path.isfile(p)]
        paths = [p for p in want if os.path.isfile(p)]
    if a.benches:
        want, parked = expected_results(a.benches, pending)
        missing = [p for p in want if not os.path.isfile(p)]
        keep = {os.path.normpath(p) for p in want}
        paths = [p for p in paths if os.path.normpath(p) in keep]
        for name in parked:
            print(f"PENDING  {name}: parked, not run")
    if not paths and not missing:
        print("check_results: no results.xml files found", file=sys.stderr)
        return 2

    allow = set(a.allow_skip.split())
    total = bad = skipped = tagged = 0
    for p in missing:
        print(f"MISSING  {p}: bench did not build or run")
    for p in sorted(paths):
        cases = ET.parse(p).getroot().iter("testcase")
        for tc in cases:
            total += 1
            name = f"{tc.get('classname')}.{tc.get('name')}"
            if tc.find("failure") is not None or tc.find("error") is not None:
                bad += 1
                print(f"FAIL  {p}: {name}")
            elif tc.find("skipped") is not None:
                bench = os.path.basename(os.path.dirname(os.path.abspath(p)))
                if tc.get("name") in allow or name in allow or f"{bench}:*" in allow:
                    print(f"SKIP  {p}: {name} (allow-listed)")
                else:
                    skipped += 1
                    print(f"SKIPPED  {p}: {name} — not allow-listed, counts as failed")
        tags = os.path.join(os.path.dirname(p), "expected_fail.json")
        if os.path.isfile(tags):
            try:
                with open(tags) as f:
                    found = json.load(f)
            except (OSError, ValueError):
                found = {}
            for t, why in sorted(found.items()):
                tagged += 1
                print(f"TAGGED  {os.path.dirname(p) or '.'}: {t} — {why}")
    bad += skipped
    summary = f"check_results: {len(paths)} files, {total} tests, {bad} failed"
    if skipped:
        summary += f" ({skipped} skipped)"
    if tagged:
        summary += f", {tagged} tagged"
    if a.benches:
        summary += f", {len(missing)} missing, {len(pending)} pending"
    elif a.expect:
        summary += f", {len(missing)} missing"
    print(summary)
    return 1 if bad or missing or total == 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
