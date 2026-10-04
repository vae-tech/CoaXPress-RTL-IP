#!/usr/bin/env python3
"""Aggregate per-test cocotb results.xml + coverage JSON into one HTML report.

The PyUVM regression at src/verif/ archives each test's artifacts under
``00_test_results/<UVM_TESTNAME>/``.  This script walks that tree, parses
the JUnit XML with stdlib xml.etree, optionally folds the per-test
coverage JSON dumps into a global bin tally, and writes a single
self-contained HTML page (no external dependencies, dark theme to match
the unit-TB report at src/tb_unit/tb_unit_test_report.html).

It also gives every validation-plan test case a verdict.  Each test's
results.xml carries the plan IDs its class serves (``plan`` /
``plan_partial`` properties, written by ``CxpTopTest``); the static map of
the plan (``docs/verification/validation/plan_map.json``) supplies the rest.  The
verdicts go to ``plan_status.json`` and a "Validation plan" section of
the page.

Usage:
    python3 gen_uvm_test_report.py                       # writes src/verif/uvm_test_report.html
    python3 gen_uvm_test_report.py --results-dir 00_test_results --out report.html
    python3 gen_uvm_test_report.py --plan-status-only    # table + JSON, no HTML
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
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
    message: str


@dataclass
class TestResult:
    """One UVM test's results (matches one subdir of 00_test_results/)."""
    name: str
    xml_path: Path
    cov_path: Path | None
    cases: list[TestCase] = field(default_factory=list)
    random_seed: str = ""
    cov_bins: dict[str, int] = field(default_factory=dict)
    # Testsuite properties CxpTopTest writes (see base_test.py).
    props: dict[str, str] = field(default_factory=dict)

    def _list(self, key: str) -> list[str]:
        return [x for x in self.props.get(key, "").split(",") if x]

    @property
    def plan(self) -> list[str]:
        return self._list("plan")

    @property
    def plan_partial(self) -> list[str]:
        return self._list("plan_partial")

    @property
    def tags(self) -> list[str]:
        return self._list("expect_fail")

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


def parse_results(xml_path: Path) -> list[TestCase]:
    cases: list[TestCase] = []
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as exc:
        cases.append(TestCase(
            name="<parse error>", classname="", file=str(xml_path), lineno=0,
            time_s=0.0, sim_time_ns=0.0, ratio=0.0,
            status="error", message=f"XML parse failed: {exc}",
        ))
        return cases

    for ts in root.iter("testsuite"):
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
            cases.append(TestCase(
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
    return cases


def _properties(xml_path: Path) -> dict[str, str]:
    """Testsuite-level <property name= value=> pairs (first suite wins)."""
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return {}
    out: dict[str, str] = {}
    for ts in root.iter("testsuite"):
        for prop in ts.findall("./property"):
            out.setdefault(prop.get("name", ""), prop.get("value", ""))
    return out


def discover(results_dir: Path) -> list[TestResult]:
    out: list[TestResult] = []
    for sub in sorted(p for p in results_dir.iterdir() if p.is_dir()):
        xml = sub / "results.xml"
        if not xml.is_file():
            continue
        cov = sub / "cov_summary.json"
        result = TestResult(
            name=sub.name,
            xml_path=xml,
            cov_path=cov if cov.is_file() else None,
        )
        result.cases = parse_results(xml)
        result.props = _properties(xml)
        result.random_seed = result.props.get("random_seed", "")
        if result.cov_path is not None:
            try:
                payload = json.loads(result.cov_path.read_text())
                bins = payload.get("bins", {})
                if isinstance(bins, dict):
                    result.cov_bins = {str(k): int(v) for k, v in bins.items()}
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        out.append(result)
    return out


# ---------------------------------------------------------------------------
# Validation-plan status (rule V2 of prompts/uvm_validation_plan_followup.md)
#
#   PASS        every mapped test green and untagged, and at least one of
#               them runs the plan procedure in full
#   PARTIAL     green, but a mapped test carries an EXPECT_FAIL tag or only
#               part of the procedure runs anywhere
#   FAIL        a mapped test is red
#   NOT TESTED  nothing mapped to the row ran
#   N/A, HW     rows the plan map marks N/A to the IP or lab-only keep that
#               status; a simulation never makes them PASS
# ---------------------------------------------------------------------------
VERDICTS = ("PASS", "PARTIAL", "FAIL", "NOT TESTED", "N/A", "HW")
_STATIC_VERDICT = {"NA": "N/A", "HW": "HW"}


def load_plan_map(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["rows"]


def plan_status(results: list[TestResult], plan_rows: list[dict]) -> dict:
    by_id: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        if r.status == "empty":
            continue                      # nothing ran: it maps nothing
        for pid in r.plan:
            by_id[pid].append({
                "test": r.props.get("uvm_test", r.name),
                "result": r.status,       # "pass" | "fail"
                "partial": pid in r.plan_partial,
                "tags": r.tags,
            })
    known = {row["id"] for row in plan_rows}
    rows = []
    for row in plan_rows:
        tests = sorted(by_id.get(row["id"], []), key=lambda t: t["test"])
        if row["status"] in _STATIC_VERDICT:
            verdict = _STATIC_VERDICT[row["status"]]
        elif not tests:
            verdict = "NOT TESTED"
        elif any(t["result"] != "pass" for t in tests):
            verdict = "FAIL"
        elif any(t["tags"] for t in tests) or all(t["partial"] for t in tests):
            verdict = "PARTIAL"
        else:
            verdict = "PASS"
        rows.append({"id": row["id"], "title": row["title"],
                     "proposal_status": row["status"], "verdict": verdict,
                     "tests": tests, "closed_by": row["closed_by"]})
    counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in VERDICTS}
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "counts": counts,
        "unknown_ids": sorted(set(by_id) - known),
        "unmapped_failures": sorted(r.name for r in results
                                    if r.status == "fail" and not r.plan),
        "rows": rows,
    }


def print_plan_table(st: dict) -> None:
    w = max(len(r["id"]) for r in st["rows"])
    print(f"{'plan test':<{w}}  {'verdict':<10}  {'map':<4}  tests")
    for r in st["rows"]:
        tests = " ".join(
            t["test"] + ("!" if t["result"] != "pass" else "")
            + ("~" if t["partial"] else "") + ("*" if t["tags"] else "")
            for t in r["tests"]) or "-"
        print(f"{r['id']:<{w}}  {r['verdict']:<10}  {r['proposal_status']:<4}  {tests}")
    print("  (test! = red, test~ = runs part of the procedure, test* = carries an EXPECT_FAIL tag)")
    print("  " + " · ".join(f"{v} {n}" for v, n in st["counts"].items())
          + f" · total {len(st['rows'])}")
    if st["unknown_ids"]:
        print("  WARNING: PLAN names IDs the plan does not have: "
              + ", ".join(st["unknown_ids"]))
    if st["unmapped_failures"]:
        print("  note: red tests that map to no plan row: "
              + ", ".join(st["unmapped_failures"]))


_VERDICT_CLS = {"PASS": "pass", "PARTIAL": "error", "FAIL": "fail",
                "NOT TESTED": "skip", "N/A": "empty", "HW": "empty"}


def render_plan_section(st: dict) -> str:
    parts = ["<details class='module' open><summary>",
             "<span class='pill pass'>PLAN</span>",
             "<span class='name'>Validation plan</span>",
             "<span class='counts'>" + html.escape(" · ".join(
                 f"{v} {n}" for v, n in st["counts"].items())) + "</span>",
             "</summary>"]
    if st["unknown_ids"]:
        parts.append("<div class='msg'>PLAN names IDs the plan does not have: "
                     + html.escape(", ".join(st["unknown_ids"])) + "</div>")
    parts.append("<table><thead><tr><th style='width:150px'>Plan test</th><th>Title</th>"
                 "<th style='width:110px'>Verdict</th><th>Map</th><th>Mapped tests</th>"
                 "</tr></thead><tbody>")
    for r in st["rows"]:
        cls = _VERDICT_CLS[r["verdict"]]
        tests = "<br>".join(
            html.escape(t["test"])
            + (" <span class='pill fail'>red</span>" if t["result"] != "pass" else "")
            + (" <span class='pill skip'>part</span>" if t["partial"] else "")
            + (" <span class='pill error' title='" + html.escape(", ".join(t["tags"]))
               + "'>tagged</span>" if t["tags"] else "")
            for t in r["tests"]) or "<span class='sub'>—</span>"
        parts.append(
            f"<tr class='case {cls}'><td class='name'>{html.escape(r['id'])}</td>"
            f"<td>{html.escape(r['title'])}</td>"
            f"<td><span class='pill {cls}'>{html.escape(r['verdict'])}</span></td>"
            f"<td class='loc'>{html.escape(r['proposal_status'])}</td><td>{tests}</td></tr>")
    parts.append("</tbody></table></details>")
    return "".join(parts)


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
.cov-block { padding: 12px 16px; border-top: 1px solid var(--border); }
.cov-block h3 { margin: 0 0 8px; font-size: 13px; color: var(--muted); font-weight: 500; text-transform: uppercase; letter-spacing: 0.05em; }
.cov-bins { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 4px 16px; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
.cov-bins .bin { display: flex; justify-content: space-between; gap: 12px; padding: 1px 0; }
.cov-bins .bin .k { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cov-bins .bin .v { color: var(--fg); }
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
    const visible = d.querySelectorAll('tr.case:not(.hidden)').length;
    d.style.display = (want === 'all' || visible > 0) ? '' : 'none';
    if (want !== 'all' && visible > 0) d.open = true;
  });
}));
"""


def render_html(results: list[TestResult], results_dir: Path,
                plan: dict | None = None) -> str:
    total = sum(r.total for r in results)
    passed = sum(r.passed for r in results)
    failed = sum(r.failed for r in results)
    errored = sum(r.errored for r in results)
    skipped = sum(r.skipped for r in results)
    wall = sum(r.wall_s for r in results)
    sim = sum(r.sim_ns for r in results)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Merge coverage bins across every test.
    global_bins: dict[str, int] = defaultdict(int)
    for r in results:
        for k, v in r.cov_bins.items():
            global_bins[k] += v

    parts: list[str] = []
    parts.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    parts.append("<title>CXP PyUVM regression report</title>")
    parts.append(f"<style>{CSS}</style></head><body>")
    parts.append("<header>")
    parts.append("<h1>CoaXPress PyUVM regression report</h1>")
    parts.append(
        f"<div class='sub'>{html.escape(str(results_dir))} · "
        f"{len(results)} test(s) · generated {generated}</div>"
    )
    parts.append("<div class='cards'>")
    parts.append(f"<div class='card'><div class='label'>Tests</div><div class='value'>{total}</div></div>")
    parts.append(f"<div class='card pass'><div class='label'>Passed</div><div class='value'>{passed}</div></div>")
    parts.append(f"<div class='card fail'><div class='label'>Failed</div><div class='value'>{failed}</div></div>")
    parts.append(f"<div class='card error'><div class='label'>Errors</div><div class='value'>{errored}</div></div>")
    parts.append(f"<div class='card skip'><div class='label'>Skipped</div><div class='value'>{skipped}</div></div>")
    parts.append(f"<div class='card'><div class='label'>Wall</div><div class='value'>{html.escape(fmt_wall(wall))}</div></div>")
    parts.append(f"<div class='card'><div class='label'>Sim time</div><div class='value'>{html.escape(fmt_sim_ns(sim))}</div></div>")
    parts.append(f"<div class='card'><div class='label'>Cov bins</div><div class='value'>{len(global_bins)}</div></div>")
    parts.append("</div>")
    parts.append("<div class='filters'>")
    for key, label in [("all", "All"), ("fail", "Failed"), ("error", "Errors"),
                       ("skip", "Skipped"), ("pass", "Passed")]:
        cls = "filter active" if key == "all" else "filter"
        parts.append(f"<span class='{cls}' data-filter='{key}'>{label}</span>")
    parts.append("</div>")
    parts.append("</header><main>")
    if plan is not None:
        parts.append(render_plan_section(plan))

    for r in results:
        pill_cls = r.status if r.status in ("pass", "fail") else ("empty" if r.status == "empty" else "fail")
        pill_text = r.status.upper() if r.total else "EMPTY"
        counts = f"{r.passed}/{r.total} passed"
        if r.failed:
            counts += f" · {r.failed} failed"
        if r.errored:
            counts += f" · {r.errored} errors"
        if r.skipped:
            counts += f" · {r.skipped} skipped"
        if r.total:
            counts += f" · {fmt_wall(r.wall_s)} wall · {fmt_sim_ns(r.sim_ns)} sim"
        if r.cov_bins:
            counts += f" · {len(r.cov_bins)} cov bins"

        open_attr = " open" if r.status != "pass" else ""
        parts.append(f"<details class='module'{open_attr}><summary>")
        parts.append(f"<span class='pill {pill_cls}'>{pill_text}</span>")
        parts.append(f"<span class='name'>{html.escape(r.name)}</span>")
        parts.append(f"<span class='counts'>{html.escape(counts)}</span>")
        parts.append("</summary>")

        if not r.cases:
            parts.append(f"<div class='empty-note'>No testcases in {html.escape(str(r.xml_path))}</div>")
            parts.append("</details>")
            continue

        parts.append("<table><thead><tr>")
        parts.append("<th style='width:90px'>Status</th><th>Test</th><th>Location</th>")
        parts.append("<th style='text-align:right'>Wall</th><th style='text-align:right'>Sim</th><th style='text-align:right'>Ratio</th>")
        parts.append("</tr></thead><tbody>")
        for c in r.cases:
            rel = c.file
            try:
                rel = str(Path(c.file).resolve().relative_to(results_dir.resolve().parent.parent))
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
                parts.append(
                    f"<tr class='detail' data-status='{c.status}'>"
                    f"<td colspan='6'><div class='msg'>{html.escape(c.message)}</div></td></tr>"
                )
        parts.append("</tbody></table>")
        if r.random_seed:
            parts.append(f"<div class='empty-note'>random_seed = {html.escape(r.random_seed)}</div>")
        if r.cov_bins:
            parts.append("<div class='cov-block'><h3>Coverage bins</h3><div class='cov-bins'>")
            for k in sorted(r.cov_bins):
                parts.append(
                    f"<div class='bin'><span class='k'>{html.escape(k)}</span>"
                    f"<span class='v'>{r.cov_bins[k]}</span></div>"
                )
            parts.append("</div></div>")
        parts.append("</details>")

    # Global coverage rollup at the bottom (collapsed by default).
    if global_bins:
        parts.append("<details class='module'><summary>")
        parts.append("<span class='pill pass'>COV</span>")
        parts.append("<span class='name'>Global coverage rollup</span>")
        parts.append(
            f"<span class='counts'>{len(global_bins)} bins · "
            f"{sum(global_bins.values())} total hits</span>"
        )
        parts.append("</summary>")
        parts.append("<div class='cov-block'><div class='cov-bins'>")
        for k in sorted(global_bins):
            parts.append(
                f"<div class='bin'><span class='k'>{html.escape(k)}</span>"
                f"<span class='v'>{global_bins[k]}</span></div>"
            )
        parts.append("</div></div></details>")

    parts.append("</main>")
    parts.append(f"<script>{JS}</script>")
    parts.append("</body></html>")
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(
        description="Aggregate PyUVM per-test results.xml + cov_summary.json into one HTML report."
    )
    p.add_argument("--results-dir", type=Path, default=here.parent / "src" / "verif" / "00_test_results",
                   help="Directory containing <test_name>/results.xml subdirs "
                        "(default: <script-dir>/00_test_results).")
    p.add_argument("--out", type=Path, default=None,
                   help="Output HTML path (default: uvm_test_report.html beside <results-dir>).")
    p.add_argument("--plan-map", type=Path,
                   default=here.parent / "docs" / "verification" / "validation" / "plan_map.json",
                   help="Static plan map exported by docs/verification/validation/gen_validation_proposal.py.")
    p.add_argument("--plan-status", type=Path, default=here.parent / "src" / "verif" / "plan_status.json",
                   help="Where to write the per-plan-test verdicts (default: src/verif/plan_status.json).")
    p.add_argument("--plan-status-only", action="store_true",
                   help="Write plan_status.json and print the table; no HTML, exit 0.")
    args = p.parse_args(argv)

    results_dir = args.results_dir.resolve()
    out = (args.out or (results_dir.parent / "uvm_test_report.html")).resolve()

    if not results_dir.is_dir():
        if not args.plan_status_only:
            print(f"error: results-dir not found: {results_dir}", file=sys.stderr)
            return 2
        results = []                     # nothing ran: every row NOT TESTED
    else:
        results = discover(results_dir)
    if not results:
        print(f"warning: no <name>/results.xml under {results_dir} — run `make` first?",
              file=sys.stderr)

    plan = None
    if args.plan_map.is_file():
        plan = plan_status(results, load_plan_map(args.plan_map))
        args.plan_status.write_text(json.dumps(plan, indent=1) + "\n", encoding="utf-8")
    else:
        print(f"warning: no plan map at {args.plan_map}; no plan status", file=sys.stderr)
        if args.plan_status_only:
            return 2
    if args.plan_status_only:
        print_plan_table(plan)
        print(f"wrote {args.plan_status}")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(results, results_dir, plan), encoding="utf-8")

    total = sum(r.total for r in results)
    failed = sum(r.failed + r.errored for r in results)
    print(f"wrote {out}  ({len(results)} tests, {total} cases, {failed} failing)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
