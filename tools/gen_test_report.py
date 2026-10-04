#!/usr/bin/env python3
"""Aggregate cocotb JUnit results.xml files into one self-contained HTML report.

Walks <tb-dir>/*/results.xml (one per module), parses with stdlib xml.etree,
and writes a single HTML file with a summary, per-module tables, status
filtering, and inlined failure messages. No external dependencies.

Usage:
    python3 tools/gen_test_report.py                 # defaults: scan src/tb_unit, write src/tb_unit/tb_unit_test_report.html
    python3 tools/gen_test_report.py --tb-dir src/tb_unit --out report.html
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class TestCase:
    name: str
    classname: str
    file: str
    lineno: int
    time_s: float
    sim_time_ns: float
    ratio: float
    status: str   # "pass" | "fail" | "error" | "skip"
    message: str  # failure/error/skip detail (may be empty)


@dataclass
class FsmEntry:
    """One FSM's coverage, loaded from a module's fsm_coverage.json."""
    name: str
    states: list[str]
    state_hits: list[int]
    states_covered: int
    states_total: int
    arcs: list[dict]
    arcs_observed: int
    arcs_total: int | None
    waived: list[str] = field(default_factory=list)
    waived_hit: list[str] = field(default_factory=list)
    arcs_covered: int = 0
    arcs_extra: int = 0

    @property
    def state_pct(self) -> float:
        return 100.0 * self.states_covered / self.states_total if self.states_total else 0.0

    @property
    def arc_pct(self) -> float:
        return 100.0 * self.arcs_covered / self.arcs_total if self.arcs_total else 100.0


@dataclass
class ModuleResult:
    name: str
    xml_path: Path
    cases: list[TestCase] = field(default_factory=list)
    random_seed: str = ""
    fsms: list[FsmEntry] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.status == "pass")

    @property
    def failed(self) -> int:
        return sum(1 for c in self.cases if c.status == "fail")

    @property
    def errored(self) -> int:
        return sum(1 for c in self.cases if c.status == "error")

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.cases if c.status == "skip")

    @property
    def wall_s(self) -> float:
        return sum(c.time_s for c in self.cases)

    @property
    def sim_ns(self) -> float:
        return sum(c.sim_time_ns for c in self.cases)

    @property
    def status(self) -> str:
        if self.failed or self.errored:
            return "fail"
        if self.total == 0:
            return "empty"
        return "pass"


def _float(s: str | None, default: float = 0.0) -> float:
    try:
        return float(s) if s else default
    except ValueError:
        return default


def _int(s: str | None, default: int = 0) -> int:
    try:
        return int(s) if s else default
    except ValueError:
        return default


def parse_results(xml_path: Path) -> ModuleResult:
    module = ModuleResult(name=xml_path.parent.name, xml_path=xml_path)
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as exc:
        module.cases.append(TestCase(
            name="<parse error>", classname="", file=str(xml_path), lineno=0,
            time_s=0.0, sim_time_ns=0.0, ratio=0.0,
            status="error", message=f"XML parse failed: {exc}",
        ))
        return module

    for ts in root.iter("testsuite"):
        seed = ts.find("./property[@name='random_seed']")
        if seed is not None and seed.get("value"):
            module.random_seed = seed.get("value", "")
        for tc in ts.iter("testcase"):
            failure = tc.find("failure")
            error = tc.find("error")
            skipped = tc.find("skipped")
            if failure is not None:
                status = "fail"
                msg = f"{failure.get('error_type', 'Failure')}: {failure.get('error_msg', '') or (failure.text or '')}"
            elif error is not None:
                status = "error"
                msg = f"{error.get('error_type', 'Error')}: {error.get('error_msg', '') or (error.text or '')}"
            elif skipped is not None:
                status = "skip"
                msg = skipped.get("message", "") or (skipped.text or "")
            else:
                status = "pass"
                msg = ""
            module.cases.append(TestCase(
                name=tc.get("name", ""),
                classname=tc.get("classname", ""),
                file=tc.get("file", ""),
                lineno=_int(tc.get("lineno")),
                time_s=_float(tc.get("time")),
                sim_time_ns=_float(tc.get("sim_time_ns")),
                ratio=_float(tc.get("ratio_time")),
                status=status,
                message=msg,
            ))
    return module


def parse_fsm_coverage(path: Path) -> list[FsmEntry]:
    """Load one module's fsm_coverage.json (written by common/fsm_coverage.py)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries: list[FsmEntry] = []
    for f in doc.get("fsms", []):
        entries.append(FsmEntry(
            name=str(f.get("name", "?")),
            states=list(f.get("states", [])),
            state_hits=list(f.get("state_hits", [])),
            states_covered=_int(str(f.get("states_covered", 0))),
            states_total=_int(str(f.get("states_total", 0))),
            arcs=list(f.get("arcs", [])),
            arcs_observed=_int(str(f.get("arcs_observed", 0))),
            arcs_total=f.get("arcs_total"),
            waived=list(f.get("waived", [])),
            waived_hit=list(f.get("waived_hit", [])),
            arcs_covered=_int(str(f.get("arcs_covered", 0))),
            arcs_extra=_int(str(f.get("arcs_extra", 0))),
        ))
    return entries


def discover(tb_dir: Path) -> list[ModuleResult]:
    modules: list[ModuleResult] = []
    for p in sorted(tb_dir.glob("*/cxp_*/results.xml")):
        module = parse_results(p)
        fsm_json = p.parent / "fsm_coverage.json"
        if fsm_json.is_file():
            module.fsms = parse_fsm_coverage(fsm_json)
        modules.append(module)
    return modules


def fmt_sim_ns(ns: float) -> str:
    if ns >= 1e9:
        return f"{ns / 1e9:.3f} s"
    if ns >= 1e6:
        return f"{ns / 1e6:.3f} ms"
    if ns >= 1e3:
        return f"{ns / 1e3:.3f} us"
    return f"{ns:.0f} ns"


def fmt_wall(s: float) -> str:
    if s >= 60:
        m, sec = divmod(s, 60)
        return f"{int(m)}m {sec:.2f}s"
    if s >= 1:
        return f"{s:.3f} s"
    return f"{s * 1000:.1f} ms"


CSS = """
:root {
  --bg: #0f1115; --panel: #161a22; --panel2: #1d2230; --border: #2a3142;
  --fg: #e6e8ee; --muted: #8a93a6; --accent: #4f9cf9;
  --pass: #2ea043; --fail: #f85149; --error: #d29922; --skip: #6e7681;
  --pass-bg: #0f2417; --fail-bg: #2a0f12; --error-bg: #2a210f; --skip-bg: #1b1f27;
}
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: var(--bg); color: var(--fg); }
header { padding: 20px 28px; border-bottom: 1px solid var(--border); background: var(--panel); position: sticky; top: 0; z-index: 10; }
h1 { font-size: 18px; margin: 0 0 4px; font-weight: 600; }
.sub { color: var(--muted); font-size: 12px; }
.cards { display: flex; gap: 12px; margin-top: 14px; flex-wrap: wrap; }
.card { background: var(--panel2); border: 1px solid var(--border); border-radius: 6px; padding: 10px 14px; min-width: 110px; }
.card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
.card .value { font-size: 20px; font-weight: 600; margin-top: 2px; }
.card.pass .value { color: var(--pass); }
.card.fail .value { color: var(--fail); }
.card.error .value { color: var(--error); }
.card.skip .value { color: var(--skip); }
.filters { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
.filter { background: var(--panel2); border: 1px solid var(--border); color: var(--fg); padding: 4px 10px; border-radius: 12px; font-size: 12px; cursor: pointer; user-select: none; }
.filter.active { background: var(--accent); border-color: var(--accent); color: #0b1220; font-weight: 600; }
main { padding: 20px 28px 60px; }
.module { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 14px; overflow: hidden; }
.module summary { padding: 12px 16px; cursor: pointer; display: flex; align-items: center; gap: 12px; list-style: none; }
.module summary::-webkit-details-marker { display: none; }
.module summary::before { content: "▸"; color: var(--muted); transition: transform 0.15s; display: inline-block; width: 12px; }
.module[open] summary::before { transform: rotate(90deg); }
.module summary .name { font-weight: 600; font-size: 15px; }
.module summary .counts { margin-left: auto; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
.pill.pass  { background: var(--pass-bg);  color: var(--pass);  border: 1px solid var(--pass);  }
.pill.fail  { background: var(--fail-bg);  color: var(--fail);  border: 1px solid var(--fail);  }
.pill.error { background: var(--error-bg); color: var(--error); border: 1px solid var(--error); }
.pill.skip  { background: var(--skip-bg);  color: var(--skip);  border: 1px solid var(--skip);  }
.pill.empty { background: var(--skip-bg);  color: var(--skip);  border: 1px solid var(--skip);  }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 8px 12px; border-top: 1px solid var(--border); font-variant-numeric: tabular-nums; }
th { background: var(--panel2); color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
td.name { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 13px; }
td.loc { color: var(--muted); font-size: 12px; font-family: ui-monospace, Menlo, Consolas, monospace; }
td.num { text-align: right; color: var(--muted); }
tr.fail td.num, tr.error td.num { color: var(--fg); }
tr.hidden { display: none; }
.msg { white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; color: var(--fail); background: var(--fail-bg); padding: 8px 12px; border-top: 1px dashed var(--border); }
tr.error + tr.detail .msg { color: var(--error); background: var(--error-bg); }
.empty-note { padding: 12px 16px; color: var(--muted); font-style: italic; border-top: 1px solid var(--border); }
.fsmbody { padding: 2px 16px 14px; }
.fsm { padding: 12px 0; border-top: 1px solid var(--border); }
.fsm:first-child { border-top: none; }
.fsm-top { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; }
.fsm-name { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 13px; font-weight: 600; min-width: 185px; }
.fsm-metric { display: flex; align-items: center; gap: 8px; }
.fsm-bar { flex: 0 0 90px; height: 8px; background: var(--panel2); border: 1px solid var(--border); border-radius: 4px; overflow: hidden; }
.fsm-bar-fill { display: block; height: 100%; }
.fsm-bar-fill.ok   { background: var(--pass); }
.fsm-bar-fill.warn { background: var(--error); }
.fsm-bar-fill.bad  { background: var(--fail); }
.fsm-stat { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.fsm-chiprow { display: flex; gap: 10px; margin-top: 8px; align-items: baseline; }
.fsm-rowlabel { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; flex: 0 0 74px; }
.fsm-states { display: flex; flex-wrap: wrap; gap: 4px; flex: 1; }
.st { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11px; padding: 1px 8px; border-radius: 10px; border: 1px solid; }
.st.hit    { background: var(--pass-bg); color: var(--pass); border-color: var(--pass); }
.st.miss   { background: var(--fail-bg); color: var(--fail); border-color: var(--fail); }
.st.waived { background: var(--skip-bg); color: var(--skip); border-color: var(--skip); }
.st.extra  { background: var(--panel2);  color: var(--accent); border-color: var(--accent); }
"""

JS = """
const filters = document.querySelectorAll('.filter');
filters.forEach(f => f.addEventListener('click', () => {
  filters.forEach(x => x.classList.remove('active'));
  f.classList.add('active');
  const want = f.dataset.filter;
  document.querySelectorAll('tr.case').forEach(row => {
    const s = row.dataset.status;
    const show = want === 'all' || s === want;
    row.classList.toggle('hidden', !show);
    const detail = row.nextElementSibling;
    if (detail && detail.classList.contains('detail')) detail.classList.toggle('hidden', !show);
  });
  document.querySelectorAll('details.module').forEach(d => {
    if (d.classList.contains('fsmpanel')) return;   // always-visible FSM panel
    const visible = d.querySelectorAll('tr.case:not(.hidden)').length;
    d.style.display = (want === 'all' || visible > 0) ? '' : 'none';
    if (want !== 'all' && visible > 0) d.open = true;
  });
}));
"""


def _bar_cls(pct: float) -> str:
    return "ok" if pct >= 99.999 else ("warn" if pct >= 50 else "bad")


def render_one_fsm(module_name: str, f: FsmEntry) -> str:
    """Render one FSM block: state + transition bars and two chip rows."""
    sp, ap = f.state_pct, f.arc_pct
    p: list[str] = ["<div class='fsm'><div class='fsm-top'>"]
    p.append(f"<span class='fsm-name'>{html.escape(module_name)}</span>")

    s_stat = f"states {f.states_covered}/{f.states_total}"
    if f.waived:
        s_stat += f" (+{len(f.waived)} waived)"
    p.append("<span class='fsm-metric'>"
             f"<span class='fsm-bar'><span class='fsm-bar-fill {_bar_cls(sp)}' "
             f"style='width:{sp:.0f}%'></span></span>"
             f"<span class='fsm-stat'>{s_stat} ({sp:.0f}%)</span></span>")

    a_stat = f"transitions {f.arcs_covered}/{f.arcs_total or 0}"
    if f.arcs_extra:
        a_stat += f" (+{f.arcs_extra} extra)"
    p.append("<span class='fsm-metric'>"
             f"<span class='fsm-bar'><span class='fsm-bar-fill {_bar_cls(ap)}' "
             f"style='width:{ap:.0f}%'></span></span>"
             f"<span class='fsm-stat'>{a_stat} ({ap:.0f}%)</span></span>")
    p.append("</div>")

    # State chips.
    p.append("<div class='fsm-chiprow'><span class='fsm-rowlabel'>states</span>"
             "<div class='fsm-states'>")
    hits = list(f.state_hits) + [0] * max(0, len(f.states) - len(f.state_hits))
    waived, waived_hit = set(f.waived), set(f.waived_hit)
    for name, hit in zip(f.states, hits):
        if name in waived_hit:
            cls, tip = "miss", f"waived but reached ({hit} hits) — remove waiver"
        elif name in waived:
            cls, tip = "waived", "unreachable by design (waived)"
        elif hit > 0:
            cls, tip = "hit", f"{hit} hit(s)"
        else:
            cls, tip = "miss", "never reached"
        p.append(f"<span class='st {cls}' title='{html.escape(tip)}'>"
                 f"{html.escape(name)}</span>")
    p.append("</div></div>")

    # Transition chips — every designed arc, plus any observed extra arc.
    p.append("<div class='fsm-chiprow'><span class='fsm-rowlabel'>transitions</span>"
             "<div class='fsm-states'>")
    for a in f.arcs:
        hit = a.get("hits", 0)
        if a.get("designed") is False:
            cls, tip = "extra", f"observed, not in the designed graph ({hit} hits)"
        elif hit > 0:
            cls, tip = "hit", f"{hit} hit(s)"
        else:
            cls, tip = "miss", "designed transition, never taken"
        label = f"{a.get('from', '?')}→{a.get('to', '?')}"
        p.append(f"<span class='st {cls}' title='{html.escape(tip)}'>"
                 f"{html.escape(label)}</span>")
    p.append("</div></div></div>")
    return "".join(p)


def render_fsm_section(modules: list[ModuleResult]) -> str:
    """Render the consolidated, always-visible FSM-coverage panel.

    One block per FSM: state- and transition-coverage bars, the full set of
    state chips, and the full designed transition graph as arc chips (green
    = exercised, red = never taken, blue = observed but outside the designed
    graph).  Lives at the top of the report so coverage is visible without
    expanding any module, and is exempt from the pass/fail row filter.
    """
    fsm_mods = [m for m in modules if m.fsms]
    if not fsm_mods:
        return ""
    s_total = sum(f.states_total for m in fsm_mods for f in m.fsms)
    s_cov   = sum(f.states_covered for m in fsm_mods for f in m.fsms)
    a_total = sum((f.arcs_total or 0) for m in fsm_mods for f in m.fsms)
    a_cov   = sum(f.arcs_covered for m in fsm_mods for f in m.fsms)
    n_fsm   = sum(len(m.fsms) for m in fsm_mods)
    denom   = s_total + a_total
    pct     = 100.0 * (s_cov + a_cov) / denom if denom else 0.0
    full    = s_cov >= s_total and a_cov >= a_total

    p: list[str] = ["<details class='module fsmpanel' open><summary>"]
    p.append(f"<span class='pill {'pass' if full else 'error'}'>{pct:.0f}%</span>")
    p.append("<span class='name'>FSM coverage</span>")
    p.append(f"<span class='counts'>{s_cov}/{s_total} states &middot; "
             f"{a_cov}/{a_total} transitions &middot; {n_fsm} FSM(s)</span></summary>")
    p.append("<div class='fsmbody'>")
    for m in fsm_mods:
        for f in m.fsms:
            p.append(render_one_fsm(m.name, f))
    p.append("</div></details>")
    return "".join(p)


def render_html(modules: list[ModuleResult], tb_dir: Path) -> str:
    total = sum(m.total for m in modules)
    passed = sum(m.passed for m in modules)
    failed = sum(m.failed for m in modules)
    errored = sum(m.errored for m in modules)
    skipped = sum(m.skipped for m in modules)
    wall = sum(m.wall_s for m in modules)
    sim = sum(m.sim_ns for m in modules)
    fsm_states_total = sum(f.states_total for m in modules for f in m.fsms)
    fsm_states_cov = sum(f.states_covered for m in modules for f in m.fsms)
    fsm_arcs_total = sum((f.arcs_total or 0) for m in modules for f in m.fsms)
    fsm_arcs_cov = sum(f.arcs_covered for m in modules for f in m.fsms)
    fsm_count = sum(len(m.fsms) for m in modules)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    parts: list[str] = []
    parts.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    parts.append("<title>CXP testbench report</title>")
    parts.append(f"<style>{CSS}</style></head><body>")
    parts.append("<header>")
    parts.append("<h1>CoaXPress testbench report</h1>")
    parts.append(
        f"<div class='sub'>{html.escape(str(tb_dir))} · {len(modules)} module(s) · generated {generated}</div>"
    )
    parts.append("<div class='cards'>")
    parts.append(f"<div class='card'><div class='label'>Tests</div><div class='value'>{total}</div></div>")
    parts.append(f"<div class='card pass'><div class='label'>Passed</div><div class='value'>{passed}</div></div>")
    parts.append(f"<div class='card fail'><div class='label'>Failed</div><div class='value'>{failed}</div></div>")
    parts.append(f"<div class='card error'><div class='label'>Errors</div><div class='value'>{errored}</div></div>")
    parts.append(f"<div class='card skip'><div class='label'>Skipped</div><div class='value'>{skipped}</div></div>")
    parts.append(f"<div class='card'><div class='label'>Wall</div><div class='value'>{html.escape(fmt_wall(wall))}</div></div>")
    parts.append(f"<div class='card'><div class='label'>Sim time</div><div class='value'>{html.escape(fmt_sim_ns(sim))}</div></div>")
    if fsm_count:
        parts.append(
            f"<div class='card'><div class='label'>FSM states</div>"
            f"<div class='value'>{fsm_states_cov}/{fsm_states_total}</div></div>"
        )
        parts.append(
            f"<div class='card'><div class='label'>FSM transitions</div>"
            f"<div class='value'>{fsm_arcs_cov}/{fsm_arcs_total}</div></div>"
        )
    parts.append("</div>")
    parts.append("<div class='filters'>")
    for key, label in [("all", "All"), ("fail", "Failed"), ("error", "Errors"), ("skip", "Skipped"), ("pass", "Passed")]:
        cls = "filter active" if key == "all" else "filter"
        parts.append(f"<span class='{cls}' data-filter='{key}'>{label}</span>")
    parts.append("</div>")
    parts.append("</header><main>")

    parts.append(render_fsm_section(modules))

    for m in modules:
        pill_cls = m.status if m.status in ("pass", "fail") else ("empty" if m.status == "empty" else "fail")
        pill_text = m.status.upper() if m.total else "EMPTY"
        counts = f"{m.passed}/{m.total} passed"
        if m.failed:
            counts += f" · {m.failed} failed"
        if m.errored:
            counts += f" · {m.errored} errors"
        if m.skipped:
            counts += f" · {m.skipped} skipped"
        if m.total:
            counts += f" · {fmt_wall(m.wall_s)} wall · {fmt_sim_ns(m.sim_ns)} sim"

        open_attr = " open" if m.status != "pass" else ""
        parts.append(f"<details class='module'{open_attr}><summary>")
        parts.append(f"<span class='pill {pill_cls}'>{pill_text}</span>")
        parts.append(f"<span class='name'>{html.escape(m.name)}</span>")
        parts.append(f"<span class='counts'>{html.escape(counts)}</span>")
        parts.append("</summary>")

        if not m.cases:
            parts.append(f"<div class='empty-note'>No testcases in {html.escape(str(m.xml_path))}</div>")
            parts.append("</details>")
            continue

        parts.append("<table><thead><tr>")
        parts.append("<th style='width:90px'>Status</th><th>Test</th><th>Location</th>")
        parts.append("<th style='text-align:right'>Wall</th><th style='text-align:right'>Sim</th><th style='text-align:right'>Ratio</th>")
        parts.append("</tr></thead><tbody>")
        for c in m.cases:
            rel = c.file
            try:
                rel = str(Path(c.file).resolve().relative_to(tb_dir.resolve().parent))
            except (ValueError, OSError):
                rel = Path(c.file).name if c.file else ""
            parts.append(f"<tr class='case {c.status}' data-status='{c.status}'>")
            parts.append(f"<td><span class='pill {c.status}'>{c.status.upper()}</span></td>")
            parts.append(f"<td class='name'>{html.escape(c.name)}</td>")
            parts.append(f"<td class='loc'>{html.escape(rel)}:{c.lineno}</td>")
            parts.append(f"<td class='num'>{html.escape(fmt_wall(c.time_s))}</td>")
            parts.append(f"<td class='num'>{html.escape(fmt_sim_ns(c.sim_time_ns))}</td>")
            parts.append(f"<td class='num'>{c.ratio:,.0f}×</td>")
            parts.append("</tr>")
            if c.message:
                parts.append(f"<tr class='detail' data-status='{c.status}'><td colspan='6'><div class='msg'>{html.escape(c.message)}</div></td></tr>")
        parts.append("</tbody></table>")
        if m.random_seed:
            parts.append(f"<div class='empty-note'>random_seed = {html.escape(m.random_seed)}</div>")
        parts.append("</details>")

    parts.append("</main>")
    parts.append(f"<script>{JS}</script>")
    parts.append("</body></html>")
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Aggregate cocotb results.xml into one HTML report.")
    p.add_argument("--tb-dir", type=Path, default=here.parent / "src" / "tb_unit",
                   help="Directory containing <group>/cxp_<module>/results.xml (default: src/tb_unit).")
    p.add_argument("--out", type=Path, default=None,
                   help="Output HTML path (default: <tb-dir>/tb_unit_test_report.html).")
    args = p.parse_args(argv)

    tb_dir = args.tb_dir.resolve()
    out = (args.out or (tb_dir / "tb_unit_test_report.html")).resolve()

    if not tb_dir.is_dir():
        print(f"error: tb-dir not found: {tb_dir}", file=sys.stderr)
        return 2

    modules = discover(tb_dir)
    if not modules:
        print(f"warning: no */results.xml under {tb_dir} — run `make` first?", file=sys.stderr)

    out.write_text(render_html(modules, tb_dir), encoding="utf-8")

    total = sum(m.total for m in modules)
    failed = sum(m.failed + m.errored for m in modules)
    print(f"wrote {out}  ({len(modules)} modules, {total} tests, {failed} failing)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
