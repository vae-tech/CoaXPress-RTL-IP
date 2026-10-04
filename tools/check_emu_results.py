#!/usr/bin/env python3
"""Gate on an emulator validation campaign's results.json.

    check_emu_results.py RESULTS.json [--expect-fail FILE] [--allow-not-run "ID ..."]

`cxp validate` exits non-zero only for FAIL and ERROR, so a campaign whose
cases all skipped exits 0.  This gate reads the verdicts instead:

* FAIL / ERROR fails, unless the case is in the expected-fail file
  (``{"CXP-CAM-INIT-001": "N-05: ..."}``) — an open RTL finding the case is
  red for.  Such a case is listed as TAGGED; if it passes, the tag is stale
  and the gate fails.
* NOT RUN fails for a validation-plan case unless allow-listed (a case the
  device cannot run by design, e.g. GEN-009 on a non-IIDC2 device).
* A campaign with no result at all fails.
"""
import argparse
import json
import sys


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--expect-fail", default="")
    ap.add_argument("--allow-not-run", default="")
    a = ap.parse_args(argv)
    try:
        with open(a.results) as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"check_emu_results: no results ({exc})")
        return 2
    tags: dict[str, str] = {}
    if a.expect_fail:
        with open(a.expect_fail) as f:
            tags = json.load(f)
    allow = set(a.allow_not_run.split())
    bad = 0
    counts: dict[str, int] = {}
    seen = set()
    for r in doc.get("results", []):
        cid, v = r.get("id", "?"), r.get("verdict", "?")
        seen.add(cid)
        counts[v] = counts.get(v, 0) + 1
        if cid in tags:
            if v in ("FAIL", "ERROR"):
                print(f"TAGGED  {cid}: {v} — {tags[cid]}")
            else:
                bad += 1
                print(f"STALE   {cid}: {v} but tagged — {tags[cid]}")
        elif v in ("FAIL", "ERROR"):
            bad += 1
            print(f"FAIL    {cid}: {v} — {r.get('summary', '')}")
        elif v == "NOT RUN" and cid not in allow:
            bad += 1
            print(f"NOTRUN  {cid}: {r.get('summary', '')}")
    for cid in sorted(set(tags) - seen):
        print(f"MISSING {cid}: tagged but not in the campaign")
        bad += 1
    if not seen:
        print("check_emu_results: the campaign ran no case")
        return 2
    print("check_emu_results: " + ", ".join(f"{k} {n}" for k, n in sorted(counts.items()))
          + f"; {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
