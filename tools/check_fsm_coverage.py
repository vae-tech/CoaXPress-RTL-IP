#!/usr/bin/env python3
"""Fail if a registered FSM collected nothing across a bench's whole run.

    check_fsm_coverage.py [--benches <dir>] <fsm_coverage.json> [...]

`fsm_coverage.register_fsm` raises when a state or clock path does not
resolve, which catches a registration moved out from under a testbench.
The other half is a path that still resolves but whose clock never ticks,
or an FSM whose enum no longer matches the RTL: the report then shows
0/N states and reads like an FSM nobody exercised.

Two things fail here, per FSM:

* no state was ever hit — the sampler ran and recorded nothing;
* a state value outside the registered enum was observed (printed by the
  collector as ``<n>``) — the state list is stale.

* a designed arc (``arcs=`` of ``register_fsm``) that was never taken —
  an exit the benches claim to cover and do not.

With ``--benches <dir>``, every ``<dir>/*/cxp_*/test_*.py`` that calls
``register_fsm(`` must have left an ``fsm_coverage.json``; a missing file
fails the gate (the bench did not run, or its collector was lost).

Uncovered *states* are not a failure: that is the metric the report is
for.  A waived state that was reached is already flagged by the
collector's own summary and fails here too, since a stale waiver hides a
real path.
"""
import json
import sys
from pathlib import Path


def check(path: Path) -> list[str]:
    bad: list[str] = []
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{path}: unreadable ({exc})"]
    module = doc.get("module", path.parent.name)
    for fsm in doc.get("fsms", []):
        name = fsm.get("name", "?")
        hits = fsm.get("state_hits", [])
        if not any(hits):
            bad.append(
                f"{module}: FSM {name!r} recorded no state at all — the "
                "registration collects nothing"
            )
        stale = [s for s in fsm.get("states", []) if s.startswith("<")]
        if stale:
            bad.append(
                f"{module}: FSM {name!r} reached state value(s) "
                f"{', '.join(stale)} outside its registered enum — the "
                "state list is stale"
            )
        dead = [f"{a['from']}->{a['to']}" for a in fsm.get("arcs", [])
                if a.get("designed") and not a.get("hits")]
        if dead:
            bad.append(
                f"{module}: FSM {name!r} never took designed arc(s) "
                f"{', '.join(dead)}"
            )
        if fsm.get("waived_hit"):
            bad.append(
                f"{module}: FSM {name!r} reached waived state(s) "
                f"{', '.join(fsm['waived_hit'])} — remove the waiver"
            )
    return bad


def expected(bench_dir: Path) -> list[Path]:
    """fsm_coverage.json path of every bench that registers an FSM."""
    want = []
    for tb in sorted(bench_dir.glob("*/cxp_*/test_*.py")):
        if "register_fsm(" in tb.read_text(encoding="utf-8", errors="replace"):
            want.append(tb.parent / "fsm_coverage.json")
    return want


def main(argv: list[str]) -> int:
    bad: list[str] = []
    if argv[:1] == ["--benches"]:
        want = expected(Path(argv[1]))
        argv = argv[2:]
        bad += [f"{p.parent.name}: registers an FSM but left no {p.name}"
                for p in want if not p.is_file()]
    paths = [Path(p) for p in argv if Path(p).is_file()]
    if not paths and not bad:
        print("check_fsm_coverage: no fsm_coverage.json files found",
              file=sys.stderr)
        return 0        # a bench tree with no registered FSM is fine
    for p in sorted(paths):
        bad += check(p)
    for line in bad:
        print(f"FSMCOV  {line}")
    print(f"check_fsm_coverage: {len(paths)} files, {len(bad)} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
