"""`make cov_gate`: the tier's coverage against the goals.

Merges every `00_test_results/*/cov_summary.json` of the tier, prints each
goal cell with its count, and exits 1 if one of `model.GOALS` was never
hit (the holes are listed) or a test's coverage model failed.

    python3 -m uvm.coverage.gate [--results-dir DIR] [--tests "t1 t2 ..."]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter

from uvm.coverage.model import GOALS, NOT_REACHABLE


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", default="00_test_results")
    ap.add_argument("--tests", default="")
    a = ap.parse_args(argv)
    want = set(a.tests.split())
    total: Counter = Counter()
    per_test = 0
    bad_model = []
    for f in sorted(glob.glob(os.path.join(a.results_dir, "*", "cov_summary.json"))):
        name = os.path.basename(os.path.dirname(f))
        if want and name not in want:
            continue
        d = json.load(open(f))
        per_test += 1
        total.update(d.get("bins", {}))
        if d.get("bins", {}).get("cg_model_error"):
            bad_model.append(name)
    holes = [g for g in GOALS if not total.get(g)]
    groups = sorted({g.split(".")[0] for g in GOALS})
    print(f"cov_gate: {per_test} tests, {len(GOALS)} goal cells, "
          f"{len(GOALS) - len(holes)} hit ({100 * (len(GOALS) - len(holes)) / len(GOALS):.1f} %)")
    for grp in groups:
        cells = [g for g in GOALS if g.startswith(grp + ".")]
        hit = sum(1 for g in cells if total.get(g))
        print(f"  {grp:20s} {hit:3d}/{len(cells):3d}")
    if holes:
        print("holes:")
        for h in holes:
            print(f"  {h}")
    if bad_model:
        print(f"coverage model failed in: {bad_model}")
    print("not goals (unreachable, with the reason):")
    for k, v in NOT_REACHABLE.items():
        print(f"  {k}: {v}")
    return 1 if holes or bad_model else 0


if __name__ == "__main__":
    sys.exit(main())
