"""Cocotb FSM state / transition coverage collector.

Verilator cannot run SystemVerilog ``covergroup``s, so FSM coverage for these
testbenches is gathered in Python instead.  This module samples a DUT's state
register on every rising clock edge, accumulates the set of states and
state-to-state transitions exercised *across all tests in a module run*, and
writes an ``fsm_coverage.json`` artifact next to ``results.xml`` that
``gen_test_report.py`` folds into the HTML report.

Usage
-----
A testbench registers each FSM once, at import time::

    from fsm_coverage import register_fsm

    register_fsm(
        name="rx_packet_parser",
        states=["ST_IDLE_HUNT", "ST_TRIG_DATA",
                "ST_LONG_TYPE", "ST_LONG_BODY"],
        state_path="cxp_rx_packet_parser_i.state_q",  # relative to `dut`
        clk_path="rx_clk",
    )

That is the whole integration: :func:`cxp_testcase.cxp_test` starts a sampler
for every registered FSM at the head of each test, stops it at the end, and
flushes the JSON.  ``states`` is the enum in declaration order — state value
``i`` maps to ``states[i]`` (every CXP FSM enum is auto-numbered from 0).
``state_path`` / ``clk_path`` are dotted paths resolved against the cocotb
``dut`` handle: for a wrapper TB the state lives under the DUT instance
(``cxp_<m>_i.state_q``); for a direct-TOPLEVEL TB it is simply ``state_q``.

Metrics
-------
* **State coverage** — ``states_covered / states_total`` (a hard percentage;
  the denominator is the enum, which is always known).  States that are dead
  by design — unreachable RTL, e.g. a packet type removed by a spec revision
  — can be waived with the optional ``unreachable=`` argument: they drop out
  of the denominator and are reported separately.  If a waived state is ever
  reached the collector flags it, so a stale waiver cannot hide a real path.
* **Transition coverage** — ``arcs_covered / arcs_total`` against the FSM's
  designed transition graph, passed as ``(from, to)`` state-name pairs via
  the ``arcs=`` argument.  Any arc observed that is *not* in that graph — a
  global override such as a §8.7 abandon, or a genuine bug — is reported
  separately as an "extra" arc rather than counted.  Self-loops (staying in
  a state) are not arcs — they are already captured by the state hit count.

Staleness
---------
Reaching an internal ``state_q`` needs the signal visible to the VPI: under
Verilator that requires ``--public-flat-rw`` (added by
``common/cocotb_sim.mk``); Questa exposes internals by default.  A path that
does not resolve raises: a registration that silently collects nothing is
worse than no registration, because the report then shows an FSM at 0 %
that nobody can tell from a genuinely unexercised one.  ``tools/
check_fsm_coverage.py`` closes the other half — an FSM whose path resolves
but which never recorded a sample fails ``make tb``.
"""

from __future__ import annotations

import atexit
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cocotb
from cocotb.triggers import RisingEdge


# =============================================================================
# Registry data model
# =============================================================================
@dataclass
class _FsmSpec:
    """Static description of one FSM, supplied by :func:`register_fsm`."""

    name: str
    states: list[str]          # enum names, indexed by state value
    state_path: str            # dotted path to state_q, relative to `dut`
    clk_path: str              # dotted path to the sampling clock
    arcs: set[tuple[int, int]] | None = None   # legal transitions, if known
    unreachable: set[str] = field(default_factory=set)   # waived dead states


@dataclass
class _FsmData:
    """Accumulated coverage for one FSM, persisting across every test."""

    spec: _FsmSpec
    state_hits: dict[int, int] = field(default_factory=dict)
    arc_hits: dict[tuple[int, int], int] = field(default_factory=dict)

    def record(self, cur: int, prev: int | None) -> None:
        self.state_hits[cur] = self.state_hits.get(cur, 0) + 1
        if prev is not None and prev != cur:
            self.arc_hits[(prev, cur)] = self.arc_hits.get((prev, cur), 0) + 1


# FSM name -> accumulator.  Module-global so coverage survives between the
# independent test coroutines of a single `make` run.
_REGISTRY: dict[str, _FsmData] = {}
_atexit_armed = False


# =============================================================================
# Public registration API
# =============================================================================
def register_fsm(
    name: str,
    states: list[str],
    state_path: str,
    clk_path: str,
    *,
    arcs: list[tuple[str, str]] | None = None,
    unreachable: list[str] | None = None,
) -> None:
    """Register an FSM for coverage collection.

    Call once per FSM, at module import time, in the testbench's
    ``test_*.py``.  See the module docstring for argument semantics.

    ``arcs`` is the FSM's designed transition graph as a list of
    ``(from_state, to_state)`` name pairs — the denominator for transition
    coverage.  ``unreachable`` waives enum states that are dead by design
    (no RTL path reaches them) — they drop out of the state-coverage
    denominator.  Each waived name should be justified by a comment at the
    call site.
    """
    global _atexit_armed
    states = list(states)
    _idx = {s: i for i, s in enumerate(states)}
    arc_set: set[tuple[int, int]] | None = None
    if arcs:
        arc_set = set()
        for fr, to in arcs:
            if fr in _idx and to in _idx:
                arc_set.add((_idx[fr], _idx[to]))
            else:
                cocotb.log.warning(
                    "fsm_coverage: FSM %r — arc (%r, %r) names not in the "
                    "state list; ignored", name, fr, to)
    spec = _FsmSpec(
        name=name,
        states=states,
        state_path=state_path,
        clk_path=clk_path,
        arcs=arc_set,
        unreachable=set(unreachable or ()),
    )
    _REGISTRY[name] = _FsmData(spec=spec)
    if not _atexit_armed:
        atexit.register(_at_exit)
        _atexit_armed = True


# =============================================================================
# Sampling — driven by cxp_testcase.cxp_test
# =============================================================================
def _resolve(dut, path: str):
    """Walk a dotted *path* of cocotb handles from *dut*; None if absent."""
    obj = dut
    for part in path.split("."):
        try:
            obj = getattr(obj, part)
        except Exception:          # AttributeError + back-end-specific errors
            return None
    return obj


def _handles(dut, spec: _FsmSpec):
    """Resolve an FSM's (clk, state) handles, or say why it is stale."""
    clk = _resolve(dut, spec.clk_path)
    sig = _resolve(dut, spec.state_path)
    if clk is None or sig is None:
        raise AssertionError(
            f"fsm_coverage: FSM {spec.name!r} — cannot resolve state "
            f"{spec.state_path!r} / clk {spec.clk_path!r}.  The registration "
            "is stale (or Verilator was built without --public-flat-rw); it "
            "would report 0 hits and look like an unexercised FSM."
        )
    return clk, sig


async def _sampler(dut, data: _FsmData, clk, sig) -> None:
    """Sample one FSM's state on every rising clock edge until killed."""
    prev: int | None = None
    while True:
        await RisingEdge(clk)
        try:
            cur = int(sig.value)
        except (ValueError, TypeError):
            # X/Z (typically pre-reset) — drop the arc across the gap.
            prev = None
            continue
        data.record(cur, prev)
        prev = cur


def start_all(dut) -> list:
    """Spawn a sampler for every registered FSM; return the task handles.

    Raises if a registered path does not resolve — see "Staleness".
    """
    tasks = []
    for data in _REGISTRY.values():
        clk, sig = _handles(dut, data.spec)
        tasks.append(cocotb.start_soon(_sampler(dut, data, clk, sig)))
    return tasks


def stop_all(tasks: list) -> None:
    """Stop the per-test sampler tasks created by :func:`start_all`."""
    for task in tasks:
        try:
            task.cancel()
        except Exception:
            pass


# =============================================================================
# JSON artifact
# =============================================================================
def _output_path() -> Path:
    """``fsm_coverage.json`` next to the cocotb results file (TB directory)."""
    results = os.environ.get("COCOTB_RESULTS_FILE")
    if results:
        return Path(results).resolve().parent / "fsm_coverage.json"
    return Path.cwd() / "fsm_coverage.json"


def _fsm_to_dict(data: _FsmData) -> dict:
    spec = data.spec
    n = len(spec.states)

    def label(value: int) -> str:
        return spec.states[value] if 0 <= value < n else f"<{value}>"

    # States actually observed outside the declared enum range (should not
    # happen, but surfacing it beats silently dropping a real hit).
    extra = sorted(v for v in data.state_hits if not 0 <= v < n)
    states = spec.states + [f"<{v}>" for v in extra]
    hits = [data.state_hits.get(i, 0) for i in range(n)]
    hits += [data.state_hits[v] for v in extra]

    # Waived (dead-by-design) states drop out of the coverage denominator.
    # A waived state that is nonetheless reached is reported in waived_hit
    # so a stale waiver cannot mask a real path.
    waived = [s for s in spec.states if s in spec.unreachable]
    waived_set = set(waived)
    waived_hit = [s for s, h in zip(states, hits) if s in waived_set and h > 0]
    reachable = [(s, h) for s, h in zip(states, hits) if s not in waived_set]
    covered = sum(1 for _, h in reachable if h > 0)

    # Transition coverage.  When a designed transition graph is registered,
    # every designed arc is emitted (hit or not) plus any "extra" arc that
    # was observed but is not in the graph (a global override or a bug);
    # otherwise just the observed arcs are emitted.
    def arc_dict(a, b, hits, designed):
        return {"from": label(a), "to": label(b), "hits": hits,
                "designed": designed}

    if spec.arcs is not None:
        designed = sorted(spec.arcs)
        arcs = [arc_dict(a, b, data.arc_hits.get((a, b), 0), True)
                for (a, b) in designed]
        extra = sorted(set(data.arc_hits) - spec.arcs)
        arcs += [arc_dict(a, b, data.arc_hits[(a, b)], False)
                 for (a, b) in extra]
        arcs_covered = sum(1 for (a, b) in designed
                           if data.arc_hits.get((a, b), 0) > 0)
        arcs_total = len(designed)
        arcs_extra = len(extra)
    else:
        arcs = [arc_dict(a, b, data.arc_hits[(a, b)], None)
                for (a, b) in sorted(data.arc_hits)]
        arcs_covered = len(data.arc_hits)
        arcs_total = None
        arcs_extra = 0

    return {
        "name": spec.name,
        "states": states,
        "state_hits": hits,
        "waived": waived,
        "waived_hit": waived_hit,
        "states_covered": covered,
        "states_total": len(reachable),
        "arcs": arcs,
        "arcs_observed": len(data.arc_hits),
        "arcs_covered": arcs_covered,
        "arcs_total": arcs_total,
        "arcs_extra": arcs_extra,
    }


def flush() -> None:
    """Write the accumulated coverage to ``fsm_coverage.json`` (idempotent)."""
    if not _REGISTRY:
        return
    doc = {
        "module": _output_path().parent.name,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "fsms": [_fsm_to_dict(d) for d in _REGISTRY.values()],
    }
    try:
        _output_path().write_text(json.dumps(doc, indent=2), encoding="utf-8")
    except OSError as exc:
        cocotb.log.warning("fsm_coverage: could not write JSON (%s)", exc)


def _print_summary() -> None:
    """Print a compact per-FSM summary once, at process exit."""
    if not _REGISTRY:
        return
    lines = ["", "=" * 64, f"FSM coverage — {_output_path().parent.name}", "-" * 64]
    for data in _REGISTRY.values():
        d = _fsm_to_dict(data)
        sc, st = d["states_covered"], d["states_total"]
        pct = 100.0 * sc / st if st else 0.0
        ac, at = d["arcs_covered"], d["arcs_total"]
        trans = f"{ac}/{at}" if at else str(ac)
        waived = set(d["waived"])
        uncovered = [s for s, h in zip(d["states"], d["state_hits"])
                     if h == 0 and s not in waived]
        miss_arcs = [f"{a['from']}->{a['to']}" for a in d["arcs"]
                     if a["designed"] and a["hits"] == 0]
        line = f"  {d['name']:<22} states {sc}/{st} ({pct:5.1f}%)  arcs {trans}"
        if waived:
            line += f"  [{len(waived)} waived]"
        if d["arcs_extra"]:
            line += f"  [+{d['arcs_extra']} extra]"
        lines.append(line)
        if uncovered:
            lines.append("    uncovered states: " + ", ".join(uncovered))
        if miss_arcs:
            lines.append("    uncovered arcs: " + ", ".join(miss_arcs))
        if d["waived_hit"]:
            lines.append("    WARNING: waived state(s) actually reached — "
                         "remove the waiver: " + ", ".join(d["waived_hit"]))
    lines.append("=" * 64)
    print("\n".join(lines))


def _at_exit() -> None:
    flush()
    _print_summary()
