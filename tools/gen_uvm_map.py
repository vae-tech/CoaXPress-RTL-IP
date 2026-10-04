#!/usr/bin/env python3
"""Environment data for docs/verification/cxp_uvm_map.html, from the PyUVM sources.

    python3 tools/gen_uvm_map.py            # rewrite the UVM block in the page
    python3 tools/gen_uvm_map.py --check    # exit 1 if the block is stale

Reads src/verif/ without importing it (pyuvm and cocotb need not be
installed): the Python under uvm/ through `ast`, the tiers and build knobs
from the Makefile, the harness ports from uvm/sv/tb_cxp_top.sv and the test
titles, plan IDs and error-kind descriptions from
docs/verification/cxp_verification_plan.md.  It writes one JSON object:

    scopes     one graph per level (env, each agent): components, the DUT,
               and the nets between them - analysis port -> exports, DUT
               pins driven / sampled, handles one component calls through,
               sequences started on a sequencer, reset callbacks
    comps      every pyuvm class: kind, file, ports, exports, DUT signals,
               ConfigDB keys, error kinds
    tests      every test class: tiers, knobs, overrides, sequences, the
               components it touches, its own check kinds, plan entry
    tiers, dut (harness ports), cov (GOALS / NOT_REACHABLE), decisions

and prints every disagreement it finds between the code, the Makefile and
the plan ("map: ..." lines).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIF = os.path.join(ROOT, "src", "verif")
UVM = os.path.join(VERIF, "uvm")
PAGE = os.path.join(ROOT, "docs", "verification", "cxp_uvm_map.html")
PLAN = os.path.join(ROOT, "docs", "verification", "cxp_verification_plan.md")
TB = os.path.join(UVM, "sv", "tb_cxp_top.sv")
BEGIN, END = "/* UVM:BEGIN (tools/gen_uvm_map.py) */", "/* UVM:END */"

DEC_NAMES: set = set()
MODS: dict = {}
CLK_RE = re.compile(r"_clk_in$|_clk_en$|_rst_n$")
PYUVM_KIND = {
    "uvm_agent": "agent", "uvm_driver": "driver", "uvm_monitor": "monitor",
    "uvm_sequencer": "sequencer", "uvm_scoreboard": "scoreboard",
    "uvm_subscriber": "subscriber", "uvm_component": "component",
    "uvm_sequence": "sequence", "uvm_sequence_item": "item",
    "uvm_test": "test", "uvm_env": "env",
}
EDGE_CALLS = {"RisingEdge", "FallingEdge", "Edge", "First", "ReadOnly"}
REL = lambda p: os.path.relpath(p, ROOT).replace(os.sep, "/")


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def dotted(n) -> str | None:
    """'self.a.b' for a Name / Attribute chain, else None."""
    parts = []
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if isinstance(n, ast.Name):
        parts.append(n.id)
        return ".".join(reversed(parts))
    return None


def strip_self(p: str) -> str:
    for pre in ("self.env.", "env.", "self."):
        if p.startswith(pre):
            return p[len(pre):]
    return p


def doc_first(node) -> str:
    d = ast.get_docstring(node) or ""
    return re.sub(r"\s+", " ", d.split("\n\n")[0]).strip()


def doc_all(node) -> str:
    d = ast.get_docstring(node) or ""
    return "\n\n".join(re.sub(r"\s+", " ", p).strip() for p in d.split("\n\n") if p.strip())


class Mod:
    def __init__(self, path: str):
        self.path = path
        self.name = os.path.relpath(path, VERIF)[:-3].replace(os.sep, ".")
        with open(path, encoding="utf-8") as f:
            self.src = f.read()
        self.tree = ast.parse(self.src)
        for n in ast.walk(self.tree):
            for c in ast.iter_child_nodes(n):
                c._parent = n
        self.imports: dict[str, tuple[str, str]] = {}
        self.funcs: dict[str, ast.AST] = {}
        self.consts: dict[str, ast.AST] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        for n in self.tree.body:
            if isinstance(n, ast.ImportFrom) and n.module:
                for a in n.names:
                    self.imports[a.asname or a.name] = (n.module, a.name)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[n.name] = n
            elif isinstance(n, ast.ClassDef):
                self.classes[n.name] = n
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        self.consts[t.id] = n.value


def load_mods() -> dict[str, Mod]:
    mods = {}
    for dp, _, fs in os.walk(UVM):
        for f in sorted(fs):
            if f.endswith(".py") and f != "__init__.py":
                m = Mod(os.path.join(dp, f))
                mods[m.name] = m
    return mods


def tb_ports() -> tuple[dict, dict]:
    """Harness ports in order: name -> {dir, w, grp, note, to}; params."""
    with open(TB, encoding="utf-8") as f:
        src = f.read()
    hdr = src[src.index("module tb_cxp_top"):src.index(");", src.index(") (")) + 2]
    params = {}
    for m in re.finditer(r"parameter\s+[\w\s\[\]:]*?\s(\w+)\s*=\s*([^,\n]+?)\s*(?:,|\n|\))(?:\s*//\s*(.*))?", hdr):
        v, _, c = m.group(2).partition("//")
        params[m.group(1)] = {"v": v.strip(), "note": (m.group(3) or c).strip()}
    ports, grp = {}, ""
    body = hdr[hdr.index(") (") + 3:]
    pending_note = ""
    for line in body.splitlines():
        s = line.strip()
        g = re.match(r"//\s*-{3,}\s*(.*?)\s*-{3,}", s)
        if g:
            grp, pending_note = g.group(1), ""
            continue
        if s.startswith("//"):
            t = s.lstrip("/ ").strip()
            if t and not grp.startswith(t):
                if re.match(r"^[A-Z]", t) and not pending_note.endswith("("):
                    grp_candidate = t.rstrip(".")
                    if len(grp_candidate) < 70 and not any(p for p in ports if ports[p]["grp"] == grp_candidate):
                        grp = grp_candidate
            continue
        m = re.match(r"(input|output)\s+(?:wire\s+)?(?:logic\s*)?(\[[^\]]+\])?\s*(\w+)\s*,?\s*(?://\s*(.*))?", s)
        if m:
            ports[m.group(3)] = {"dir": "in" if m.group(1) == "input" else "out",
                                 "w": m.group(2) or "", "grp": grp, "note": (m.group(4) or "").strip()}
    # where each port goes
    inst = re.search(r"cxp_device_top\s*#\(.*?\)\s*cxp_device_top_i\s*\((.*?)\);", src, re.S).group(1)
    for m in re.finditer(r"\.(\w+)\s*\(([^()]*)\)", inst):
        expr = m.group(2).strip()
        if expr in ports:
            ports[expr]["to"] = f"cxp_device_top.{m.group(1)}"
    alias = {}
    for m in re.finditer(r"assign\s+(\w+)\s*=\s*([^;]+);", src):
        lhs, rhs = m.group(1), re.sub(r"\s+", " ", m.group(2).strip())
        if lhs in ports and "to" not in ports[lhs]:
            if rhs.startswith("cxp_device_top_i."):
                ports[lhs]["to"] = "tap " + rhs.replace("cxp_device_top_i.", "cxp_device_top.")
                ports[lhs]["tap"] = 1
            elif rhs in alias:
                ports[lhs]["to"] = alias[rhs]
            else:
                ports[lhs]["to"] = "TB: " + rhs
        alias[lhs] = rhs
    for m in re.finditer(r"\.(\w+)\s*\((\w+)\)", inst):
        for p, rhs in list(alias.items()):
            if p in ports and rhs == m.group(2) and ports[p].get("to", "").startswith("TB: "):
                ports[p]["to"] = f"cxp_device_top.{m.group(1)} (via {rhs})"
    tail = src[src.index(");", src.index(") (")) + 2:]
    code = [re.sub(r"//.*", "", ln).strip() for ln in tail.splitlines()]
    for name, p in ports.items():
        if "to" in p:
            continue
        hits = [ln for ln in code if re.search(rf"\b{name}\b", ln) and not ln.startswith("initial")]
        drv = [ln for ln in hits if re.search(rf"(assign\s+{name}\b|\b{name}\s*<=)", ln)]
        use = (drv or hits or [""])[0]
        p["to"] = "TB: " + use.rstrip(";") if use else "nothing in the shell reads it"
        if not use and p["dir"] == "in":
            p["unused"] = 1
    for p in ("app_clk_in", "tx_clk_in", "rx_clk_in"):
        if p in ports:
            ports[p]["to"] = f"cxp_device_top.{p[:-3]} (gated by {p[:-6]}clk_en)"
    return ports, params


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------
class World:
    def __init__(self, mods, ports):
        self.mods, self.ports = mods, ports
        self.cls: dict[str, tuple[Mod, ast.ClassDef]] = {}
        for m in mods.values():
            for k, c in m.classes.items():
                if k in self.cls and not k.startswith("_"):
                    print(f"map: class {k} defined twice ({self.cls[k][0].name}, {m.name})")
                self.cls.setdefault(k, (m, c))
        self._sig_cache = {}

    def bases(self, name):
        m, c = self.cls[name]
        return [b.id if isinstance(b, ast.Name) else (b.attr if isinstance(b, ast.Attribute) else "") for b in c.bases]

    def kind(self, name, seen=()):
        if name in PYUVM_KIND:
            return PYUVM_KIND[name]
        if name not in self.cls or name in seen:
            return None
        for b in self.bases(name):
            k = self.kind(b, seen + (name,))
            if k:
                return k
        return None

    def chain(self, name):
        """name and its local base classes, nearest first."""
        out = [name]
        for b in self.bases(name):
            if b in self.cls:
                out += [x for x in self.chain(b) if x not in out]
        return out

    def method(self, cname, meth):
        for c in self.chain(cname):
            for n in self.cls[c][1].body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == meth:
                    return self.cls[c][0], c, n
        return None

    def resolve_func(self, mod: Mod, name: str):
        if name in mod.funcs:
            return mod, mod.funcs[name]
        if name in mod.imports:
            src, nm = mod.imports[name]
            if src in self.mods and nm in self.mods[src].funcs:
                return self.mods[src], self.mods[src].funcs[nm]
        return None

    # -- DUT access ------------------------------------------------------------
    def fn_signals(self, mod: Mod, fn, cname=None, seen=None) -> dict[str, str]:
        """port -> 'w' (drives) / 'r' (samples), over fn and what it calls."""
        key = (mod.name, cname, fn.name, fn.lineno)
        if key in self._sig_cache:
            return self._sig_cache[key]
        seen = seen if seen is not None else set()
        if key in seen:
            return {}
        seen.add(key)
        out: dict[str, str] = {}

        def put(p, how):
            if out.get(p) != "w":
                out[p] = how
        duts = {"dut"} | {a.arg for a in fn.args.args if a.arg == "dut"}
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                vals = n.value.elts if isinstance(n.value, ast.Tuple) else [n.value]
                tgts = n.targets[0].elts if isinstance(n.targets[0], ast.Tuple) else n.targets
                for t, v in zip(tgts, vals):
                    if isinstance(t, ast.Name) and isinstance(v, ast.Call) and getattr(v.func, "id", "") == "get_dut":
                        duts.add(t.id)
                    if isinstance(t, ast.Name) and isinstance(v, ast.BoolOp) and any(
                            isinstance(x, ast.Call) and getattr(x.func, "id", "") == "get_dut" for x in v.values):
                        duts.add(t.id)
        is_dut = lambda v: (isinstance(v, ast.Name) and v.id in duts) or (
            isinstance(v, ast.Call) and getattr(v.func, "id", "") == "get_dut")
        dyn = False
        for n in ast.walk(fn):
            if isinstance(n, ast.Attribute) and n.attr in self.ports and is_dut(n.value):
                p, par = n.attr, getattr(n, "_parent", None)
                if isinstance(par, ast.Attribute) and par.attr == "value":
                    gp = getattr(par, "_parent", None)
                    w = (isinstance(gp, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and
                         par in (gp.targets if isinstance(gp, ast.Assign) else [gp.target]))
                    put(p, "w" if w else "r")
                elif isinstance(par, ast.Attribute) and par.attr in ("setimmediatevalue",):
                    put(p, "w")
                elif isinstance(par, ast.Call) and getattr(par.func, "id", "") in EDGE_CALLS:
                    put(p, "r")
                else:
                    put(p, "w" if self.ports[p]["dir"] == "in" else "r")
            elif isinstance(n, ast.Call) and getattr(n.func, "id", "") == "getattr" and n.args and is_dut(n.args[0]):
                a = n.args[1] if len(n.args) > 1 else None
                if isinstance(a, ast.Constant) and a.value in self.ports:
                    put(a.value, "r")
                else:
                    dyn = True
            elif isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name):
                    r = self.resolve_func(mod, f.id)
                    if r:
                        for p, h in self.fn_signals(r[0], r[1], None, seen).items():
                            put(p, h)
                elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "self" and cname:
                    r = self.method(cname, f.attr)
                    if r:
                        for p, h in self.fn_signals(r[0], r[2], r[1], seen).items():
                            put(p, h)
        if dyn:   # getattr(dut, name) over names held in strings
            pool = set()
            for n in ast.walk(self.cls[cname][1] if cname else fn):
                if isinstance(n, ast.Name) and n.id in mod.consts:
                    for c in ast.walk(mod.consts[n.id]):
                        if isinstance(c, ast.Constant) and c.value in self.ports:
                            pool.add(c.value)
                if isinstance(n, ast.Constant) and n.value in self.ports:
                    pool.add(n.value)
            for p in sorted(pool):
                put(p, "r")
        self._sig_cache[key] = out
        return out

    def class_signals(self, cname) -> dict[str, str]:
        out = {}
        for c in self.chain(cname):
            m, cd = self.cls[c]
            for n in cd.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for p, h in self.fn_signals(m, n, cname).items():
                        if out.get(p) != "w":
                            out[p] = h
        return out

    # -- structure ---------------------------------------------------------------
    def build_info(self, cname):
        """children, analysis ports, fifos, exports, seq item ports declared in build_phase."""
        info = {"children": {}, "ports": [], "fifos": [], "exports": {}, "item_ports": []}
        for c in reversed(self.chain(cname)):
            m, cd = self.cls[c]
            bp = next((n for n in cd.body if isinstance(n, ast.FunctionDef) and n.name == "build_phase"), None)
            if not bp:
                continue
            for n in ast.walk(bp):
                if not (isinstance(n, ast.Assign) and len(n.targets) == 1):
                    continue
                t = dotted(n.targets[0])
                if not t or not t.startswith("self.") or t.count(".") != 1:
                    continue
                attr = t[5:]
                v = n.value
                if isinstance(v, ast.Call) and isinstance(v.func, ast.Name):
                    fn = v.func.id
                    if fn == "uvm_analysis_port":
                        info["ports"].append(attr)
                    elif fn == "uvm_tlm_analysis_fifo":
                        info["fifos"].append(attr)
                    elif fn == "uvm_seq_item_port":
                        info["item_ports"].append(attr)
                    elif fn in self.cls and self.kind(fn) and self.kind(fn) not in ("sequence", "item"):
                        info["children"][attr] = fn
                elif isinstance(v, ast.Attribute) and v.attr == "analysis_export":
                    info["exports"][attr] = dotted(v.value)[5:]
        if self.kind(cname) == "driver":
            info["item_ports"].insert(0, "seq_item_port")
        if self.kind(cname) == "sequencer":
            info["item_ports"].insert(0, "seq_item_export")
        return info

    def configdb(self, cname):
        keys = set()
        for c in self.chain(cname):
            m, cd = self.cls[c]
            for n in ast.walk(cd):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("get", "set"):
                    v = n.func.value
                    if isinstance(v, ast.Call) and getattr(v.func, "id", "") == "ConfigDB" and len(n.args) >= 3:
                        k = n.args[2]
                        name = k.value if isinstance(k, ast.Constant) else dotted(k)
                        if isinstance(k, ast.Name) and k.id in m.imports:
                            src = self.mods.get(m.imports[k.id][0])
                            if src and k.id in src.consts and isinstance(src.consts[k.id], ast.Constant):
                                name = src.consts[k.id].value
                        keys.add(f"{n.func.attr}:{name}")
        return sorted(keys)

    def err_kinds(self, cname) -> dict[str, str]:
        """error kind -> 'run' or 'final' (raised from _final_check)."""
        out = {}
        for c in self.chain(cname):
            m, cd = self.cls[c]
            for fn in cd.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for n in ast.walk(fn):
                    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "err"
                            and dotted(n.func.value) == "self" and n.args):
                        a = n.args[0]
                        if isinstance(a, ast.Constant):
                            k = a.value
                        elif isinstance(a, ast.JoinedStr):
                            k = "".join(x.value if isinstance(x, ast.Constant) else "{" + (dotted(x.value) or "…") + "}"
                                        for x in a.values)
                        else:
                            k = "{" + (dotted(a) or "…") + "}"
                        out.setdefault(k, "final" if fn.name == "_final_check" else "run")
        return out


# ---------------------------------------------------------------------------
# Makefile, plan, decisions, coverage
# ---------------------------------------------------------------------------
def makefile():
    with open(os.path.join(VERIF, "Makefile"), encoding="utf-8") as f:
        src = f.read().replace("\\\n", " ")
    lists = {}
    for m in re.finditer(r"^(\w+)_TESTS\s*(:=|\+=)\s*(.*)$", src, re.M):
        name, op, rhs = m.group(1), m.group(2), m.group(3).split("#")[0]
        vals = []
        for tok in rhs.split():
            r = re.fullmatch(r"\$\((\w+)_TESTS\)", tok)
            vals += lists.get(r.group(1), []) if r else [tok]
        lists[name] = (lists.get(name, []) + vals) if op == "+=" else vals
    knobs = {m.group(1): dict(kv.split("=") for kv in m.group(2).split())
             for m in re.finditer(r"^KNOBS_(\w+)\s*:=\s*(.*)$", src, re.M)}
    defaults = {m.group(1): m.group(2).strip() for m in re.finditer(r"^(OS_RATIO|RX_CLK_KHZ|RX_LOSS_WORDS|FIFO_DEPTH|ASYNC_CLOCKS|CXP_SEED)\s*\?=\s*(\S+)", src, re.M)}
    targets = {}
    for t in ("smoke", "ci", "feature", "xifc", "nightly", "weekly"):
        m = re.search(rf"^{t}:.*?\n((?:\t.*\n)+)", src, re.M)
        if m:
            uses = re.search(r"\$\((\w+)_TESTS\)", m.group(1))
            targets[t] = {"list": uses.group(1) if uses else "", "seeds": "random" if "urandom" in m.group(1) else "CXP_SEED",
                          "dump": "DUMP=1" in m.group(1), "cov_gate": "cov_gate" in m.group(1)}
    return lists, knobs, defaults, targets


def plan():
    if not os.path.exists(PLAN):
        return {}, {}, ""
    with open(PLAN, encoding="utf-8") as f:
        src = f.read()
    stamp = re.search(r"_Generated from .*?@ `(\w+)`, ([\d-]+)", src)
    tests, area, area_no = {}, "", 99
    for sec in re.split(r"^(?=#{3,4} )", src, flags=re.M):
        h = re.match(r"### 8\.(\d+) (.*)", sec)
        if h:
            area, area_no = h.group(2).strip(), int(h.group(1))
            continue
        h = re.match(r"#### (VP-[A-Z]+-\d+) — `(\w+)`", sec)
        if not h:
            continue
        e = {"vp": h.group(1), "area": area, "area_no": area_no}
        for k, lab in (("title", "Title"), ("cat", "Catalogue"), ("spec", "Spec")):
            m = re.search(rf"\*\*{lab}:\*\* (.*?)(?: · \*\*|  \n|\n)", sec)
            if m and m.group(1).strip():
                e[k] = m.group(1).strip()
        for k, lab in (("desc", "Description"), ("how", "How to test")):
            m = re.search(rf"\*\*{lab}\.\*\* (.*?)\n\n", sec, re.S)
            if m:
                e[k] = re.sub(r"\s+", " ", m.group(1)).strip()
        for k, lab in (("pass", "Pass criteria"), ("fail", "Fail criteria")):
            m = re.search(rf"\*\*{lab}\.\*\*\n\n((?:- .*\n?)+)", sec)
            if m:
                e[k] = [x[2:].strip() for x in m.group(1).strip().splitlines()]
        m = re.search(r"\*\*Tiers:\*\* (.*?)\s*$", sec, re.M)
        e["tiers"] = [t.strip() for t in m.group(1).split(",")] if m else []
        tests[h.group(2)] = e
    kinds = {}
    s33 = src[src.index("### 3.3"):src.index("### 3.4")] if "### 3.3" in src else ""
    for blk in re.split(r"^(?=\*\*`)", s33, flags=re.M):
        h = re.match(r"\*\*`(\w+)", blk)
        if not h:
            continue
        for line in blk.splitlines():
            m = re.match(r"- (\w+)(?: \((final)\))? — (.*)", line)
            if m:
                kinds.setdefault(h.group(1), {})[m.group(1)] = m.group(3).strip()
    return tests, kinds, f"{stamp.group(2)} @ {stamp.group(1)}" if stamp else ""


def decisions(mods):
    m = mods["uvm.common.decisions"]
    lines = m.src.splitlines()
    out, cur = [], None
    for i, line in enumerate(lines):
        h = re.match(r"#\s*(D\d+)\s*—\s*(.*)", line)
        if h:
            cur = {"id": h.group(1), "text": [h.group(2)], "consts": []}
            out.append(cur)
            continue
        if line.startswith("# Recorded."):
            cur = {"id": "R", "text": [], "consts": []}
            out.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("#"):
            t = line[1:].strip()
            if t and not set(t) <= {"-"}:
                cur["text"].append(t)
        else:
            a = re.match(r"(\w+)\s*=\s*(.*?)(?:\s*#\s*(.*))?$", line)
            if a and a.group(1).isupper():
                val = a.group(2)
                j = i
                while val.count("{") > val.count("}") or val.count("(") > val.count(")"):
                    j += 1
                    val += " " + lines[j].strip()
                cur["consts"].append({"name": a.group(1), "value": val, "opts": a.group(3) or ""})
    res = []
    for d in out:
        txt = " ".join(d["text"])
        q = re.split(r"\s+None \|", txt)[0]
        opts = re.search(r"(None \|.*?)\s+Read by:", txt)
        if d["id"] == "R":
            q = re.split(r"\s+Recorded in", txt)[0]
            dec = re.search(r"\):\s*(.*?)\s+Read by", txt)
            rb = re.search(r"Read by:\s*(.*?)\.?$", txt)
            res.append({"id": "R", "q": q.strip(), "opts": "", "readby": rb.group(1) if rb else "",
                        "decided": dec.group(1) if dec else "", "consts": d["consts"]})
            continue
        rb = re.search(r"Read by:\s*(.*?)(?:\s+Decided|$)", txt)
        dec = re.search(r"Decided[:.]?\s*(.*)", txt)
        res.append({"id": d["id"], "q": q.strip(), "opts": opts.group(1) if opts else "",
                    "readby": rb.group(1).strip().rstrip(".") if rb else "",
                    "decided": dec.group(1).strip() if dec else ("" if d["id"] != "R" else txt),
                    "consts": d["consts"]})
    return res


def coverage(mods):
    m = mods["uvm.coverage.model"]
    goals, nr = [], {}
    for n in m.tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
            if n.targets[0].id == "GOALS":
                goals = eval(compile(ast.Expression(n.value), "model.py", "eval"), {"__builtins__": {"range": range}})
            elif n.targets[0].id == "NOT_REACHABLE":
                nr = ast.literal_eval(n.value)
    # every cell name pattern the sampler can write
    sampled = sorted(set(re.findall(r'bins\[f?"(cg_[^"]+)"\]', m.src)))
    groups = {}
    for g in goals:
        groups.setdefault(g.split(".")[0], []).append(g)
    doc = {}
    for c in re.finditer(r"#\s*(cg_\w+):\s*(.*)", m.src):
        doc.setdefault(c.group(1), c.group(2).strip())
    return {"goals": groups, "nr": nr, "sampled": sampled, "doc": doc}


# ---------------------------------------------------------------------------
# Graphs
# ---------------------------------------------------------------------------
def env_scope(w: World, comps: dict, tests: list) -> dict:
    env_m, env_c = w.cls["CxpEnv"]
    binfo = w.build_info("CxpEnv")
    kids = binfo["children"]
    nodes = {"dut": {"bnd": 1}}
    for k, c in kids.items():
        nodes[k] = {"cls": c, "kind": w.kind(c)}
        if w.build_info(c)["children"]:
            nodes[k]["child"] = k
    nodes["test"] = {"cls": "CxpTopTest", "kind": "test"}
    nets = {}

    def net(name, kind, d, l, wdt="", **kw):
        n = nets.setdefault((kind, name), {"n": name, "k": kind, "w": wdt, "d": [], "l": [], **kw})
        for x in d:
            if x not in n["d"]:
                n["d"].append(x)
        for x in l:
            if x not in n["l"]:
                n["l"].append(x)
        return n

    def comp_of(path):          # 'txwire_ag.mon.ap_pkt' -> ('txwire_ag', 'mon.ap_pkt')
        head, _, rest = path.partition(".")
        return head, rest

    cp = next(n for n in env_c.body if isinstance(n, ast.FunctionDef) and n.name == "connect_phase")
    for st in cp.body:
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
            f = st.value.func
            if isinstance(f, ast.Attribute) and f.attr == "connect":
                src, dst = strip_self(dotted(f.value)), strip_self(dotted(st.value.args[0]))
                a, pa = comp_of(src)
                b, pb = comp_of(dst)
                owner = w.build_info(kids[a])["children"].get(pa.split(".")[0], kids[a])
                ttype = comps.get(owner, {}).get("txn", {}).get(pa.split(".")[-1], "")
                net(src, "tlm", [f"{a}:{pa}"], [f"{b}:{pb}"], ttype)
            elif isinstance(f, ast.Attribute) and f.attr == "append":
                lst, cb = strip_self(dotted(f.value)), strip_self(dotted(st.value.args[0]))
                a, pa = comp_of(lst)
                nodes.setdefault("env", {"glue": 1, "cls": "CxpEnv", "kind": "env"})
                net(f"{lst} → {cb}", "cb", [f"{a}:{pa}"], [f"env:{cb}()"])
                r = w.method("CxpEnv", cb)
                for n in ast.walk(r[2]):
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                        p = dotted(n.func)
                        if p and p.startswith("self.") and p.count(".") >= 2:
                            b, pb = comp_of(strip_self(p))
                            if b in kids:
                                net(f"{cb} → {b}.{pb}()", "cb", [f"env:{cb}()"], [f"{b}:{pb}()"])
        elif isinstance(st, ast.Assign):
            t, v = dotted(st.targets[0]), dotted(st.value)
            if t and v:
                a, pa = comp_of(strip_self(t))
                b, pb = comp_of(strip_self(v))
                kind = "seq" if pa.endswith("seqr") else "call"
                net(strip_self(t), kind, [f"{a}:{pa}"], [f"{b}:{pb}"], w.build_info(kids[b])["children"].get(pb, kids.get(b, "")))
    sos = w.method("CxpEnv", "start_of_simulation_phase")
    if sos:
        local = {}
        for st in sos[2].body:
            if isinstance(st, ast.Assign):
                t, v = dotted(st.targets[0]), dotted(st.value)
                if not (t and v):
                    continue
                if isinstance(st.targets[0], ast.Name):
                    local[t] = strip_self(v)
                    continue
                head = v.split(".")[0]
                v = local.get(head, strip_self(v)) + v[len(head):] if head in local else strip_self(v)
                a, pa = comp_of(strip_self(t))
                b, pb = comp_of(v)
                net(strip_self(t), "cb", [f"{b}:{pb}"], [f"{a}:{pa}"], "start_of_simulation")
    # DUT pins
    ports = w.ports
    for k, c in list(kids.items()) + [("env", "CxpEnv")]:
        sig = {}
        for cls in [c] + list(w.build_info(c)["children"].values()):
            for p, h in comps[cls]["sig"].items():
                sig.setdefault(p, set()).add(h)
        if k == "env":
            sig = {p: {h} for p, h in w.class_signals("CxpEnv").items()}
            if sig:
                nodes.setdefault("env", {"glue": 1, "cls": "CxpEnv", "kind": "env"})
        for p, hs in sig.items():
            sub = [s for s, cls in w.build_info(c)["children"].items() if p in comps[cls]["sig"]] if k != "env" else []
            ref = [f"{k}:{s}" for s in sub] or [f"{k}:"]
            pin_net(net, ports, p, "w" in hs, ref)
    tsig = {}
    for t in tests:
        for p, h in t["sig"].items():
            tsig.setdefault(p, set()).add(h)
    for p, hs in tsig.items():
        pin_net(net, ports, p, "w" in hs, ["test:"])
    # what the tests use
    for t in tests:
        for s in t["seqs"]:
            if s["on"]:
                a, pa = comp_of(s["on"])
                if a in kids:
                    net(s["cls"], "seq", ["test:start()"], [f"{a}:{pa}"], "sequence", tests=[])["tests"].append(t["name"])
        for u in t["uses"]:
            a, pa = comp_of(u)
            if a in kids and pa:
                kind = "call" if u.endswith("()") else "peek"
                net(f"test → {u}", kind, ["test:"], [f"{a}:{pa}"], tests=[])["tests"].append(t["name"])
    out = list(nets.values())
    for n in out:
        if "tests" in n:
            n["tests"] = sorted(set(n["tests"]))
    return {"title": "env", "cls": "CxpEnv", "file": REL(env_m.path), "nodes": nodes, "nets": out}


def pin_net(net, ports, p, writes, refs):
    info = ports[p]
    clk = bool(CLK_RE.search(p))
    if info["dir"] == "in":
        n = net(p, "pin", refs if writes else [], ["dut:" + p] + ([] if writes else refs), info["w"] or "1")
    else:
        n = net(p, "pin", ["dut:" + p], refs, info["w"] or "1")
    if clk:
        n["clk"] = 1


def agent_scope(w: World, comps, name, cls, env_sc) -> dict:
    m, cd = w.cls[cls]
    bi = w.build_info(cls)
    nodes = {"dut": {"bnd": 1}, "env": {"glue": 1, "kind": "env", "cls": "CxpEnv"}}
    for k, c in bi["children"].items():
        nodes[k] = {"cls": c, "kind": w.kind(c)}
    nets = {}

    def net(nm, kind, d, l, wdt="", **kw):
        n = nets.setdefault((kind, nm), {"n": nm, "k": kind, "w": wdt, "d": [], "l": [], **kw})
        n["d"] += [x for x in d if x not in n["d"]]
        n["l"] += [x for x in l if x not in n["l"]]
        return n
    cp = next((n for n in cd.body if isinstance(n, ast.FunctionDef) and n.name == "connect_phase"), None)
    for st in (cp.body if cp else []):
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call) and getattr(st.value.func, "attr", "") == "connect":
            a, pa = dotted(st.value.func.value)[5:].split(".", 1)
            b, pb = dotted(st.value.args[0])[5:].split(".", 1)
            net(f"{a}.{pa}", "seq", [f"{b}:{pb}"], [f"{a}:{pa}"], "sequence items")
        elif isinstance(st, ast.Assign):
            t, v = dotted(st.targets[0])[5:], dotted(st.value)[5:]
            a, pa = t.split(".", 1)
            net(t, "call", [f"{a}:{pa}"], [f"{v}:"], comps[bi["children"][v]]["kind"])
    for k, c in bi["children"].items():
        for p, h in comps[c]["sig"].items():
            pin_net(net, w.ports, p, h == "w", [f"{k}:"])
    if comps[cls]["sig"]:
        nodes["self"] = {"glue": 1, "kind": "agent", "cls": cls}
        for p, h in comps[cls]["sig"].items():
            pin_net(net, w.ports, p, h == "w", ["self:"])
    # the env side: who listens to the ports, who holds handles into the agent
    for n in env_sc["nets"]:
        for d in n["d"]:
            if d.startswith(name + ":") and n["k"] in ("tlm", "cb"):
                sub = d.split(":", 1)[1]
                tgt = sub.split(".")[0] if sub.split(".")[0] in nodes else "self"
                if tgt == "self":
                    nodes.setdefault("self", {"glue": 1, "kind": "agent", "cls": cls})
                net(n["n"], n["k"], [f"{tgt}:{sub.split('.', 1)[1] if tgt != 'self' and '.' in sub else sub}"],
                    ["env:" + x.replace(":", ".") for x in n["l"]], n["w"])
        for l in n["l"]:
            if l.startswith(name + ":") and n["k"] in ("call", "seq", "peek", "cb"):
                sub = l.split(":", 1)[1]
                tgt = sub.split(".")[0] if sub.split(".")[0] in nodes else "self"
                if tgt == "self":
                    nodes.setdefault("self", {"glue": 1, "kind": "agent", "cls": cls})
                rest = sub.split(".", 1)[1] if tgt != "self" and "." in sub else ("" if tgt != "self" else sub)
                nn = net(n["n"], n["k"], ["env:" + x.replace(":", ".").rstrip(".") for x in n["d"]], [f"{tgt}:{rest}"], n["w"])
                if "tests" in n:
                    nn["tests"] = n["tests"]
    return {"title": name, "cls": cls, "file": REL(m.path), "nodes": nodes, "nets": list(nets.values())}


HINTS = {
    "env": [["test", "vseqr"],
            ["clkrst_ag", "cfg_ag", "video_ag", "io_ag", "apb_ag", "uplink_ag", "txwire_ag", "sideband_ag", "reg_ag"],
            ["env", "host", "trig_resp"],
            ["sb_test", "sb_control", "sb_reg", "sb_linkreset", "sb_linktest", "sb_linkstate"],
            ["sb_stream", "sb_linkpro", "sb_linkerr", "sb_rxtrig", "sb_ioack", "sb_txtrig"],
            ["cov", "pkt_log", "stream_dump"]],
}
NW, NH, GX, GY, BW = 176, 62, 64, 30, 96


def layout(scopes: dict) -> None:
    """DUT bar on the left, then columns: HINTS where given, else by signal flow
    (longest path from the DUT over the non-pin nets)."""
    for sid, sc in scopes.items():
        ids = [k for k in sc["nodes"] if k != "dut"]
        if sid in HINTS:
            cols = [[u for u in c if u in sc["nodes"]] for c in HINTS[sid]]
            placed = {u for c in cols for u in c}
            assert placed == set(ids), f"{sid}: HINTS {sorted(placed ^ set(ids))}"
        else:
            order = {"sequencer": 0, "driver": 1, "agent": 1, "monitor": 2, "env": 3}
            cols = defaultdict(list)
            for u in ids:
                cols[order.get(sc["nodes"][u].get("kind"), 1) if u not in ("self", "env") else (1 if u == "self" else 3)].append(u)
            cols = [cols[k] for k in sorted(cols)]
        nrow = max(len(c) for c in cols)
        width = BW + 30 + len(cols) * NW + (len(cols) + 1) * GX + 20
        height = max(360, 80 + nrow * (NH + GY))
        for c, us in enumerate(cols):
            x = BW + 30 + GX + c * (NW + GX)
            top = (height - len(us) * (NH + GY) + GY) / 2
            for i, u in enumerate(us):
                sc["nodes"][u]["x"] = round(x)
                sc["nodes"][u]["y"] = round(top + i * (NH + GY))
        sc["W"], sc["H"] = round(width), round(height)


# ---------------------------------------------------------------------------
def txn_of(w: World, mod: Mod, port: str) -> str:
    """Class of what is written to analysis port `port` of a class in `mod`
    (the driver may write it through a handle: self.mon.ap.write)."""
    for n in ast.walk(mod.tree):
        if not (isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "write" and n.args):
            continue
        p = dotted(n.func.value) or ""
        if not (p == "self." + port or p.endswith("." + port)) or not p.startswith("self"):
            continue
        a = n.args[0]
        if isinstance(a, ast.Call) and isinstance(a.func, ast.Name):
            return a.func.id
        if isinstance(a, ast.Tuple):
            return "tuple"
        if isinstance(a, ast.Name):
            fn = n
            while fn is not None and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = getattr(fn, "_parent", None)
            if fn is None:
                continue
            for x in fn.args.args:
                if x.arg == a.id and x.annotation is not None:
                    return dotted(x.annotation) or ast.unparse(x.annotation)
            for x in ast.walk(fn):
                if isinstance(x, ast.Assign) and any(isinstance(t, ast.Name) and t.id == a.id for t in x.targets) \
                        and isinstance(x.value, ast.Call) and isinstance(x.value.func, ast.Name):
                    return x.value.func.id
                if isinstance(x, ast.AnnAssign) and getattr(x.target, "id", "") == a.id:
                    return ast.unparse(x.annotation)
    return ""


def test_info(w: World, name: str, lists, knobs, plan_tests) -> dict:
    m, cd = w.cls[name]
    chain = [c for c in w.chain(name) if c != "CxpTopTest"]
    attrs = {}
    for c in reversed(w.chain(name)):
        cm, ccd = w.cls[c]
        for n in ccd.body:
            if isinstance(n, (ast.Assign, ast.AnnAssign)):
                t = n.targets[0] if isinstance(n, ast.Assign) else n.target
                if isinstance(t, ast.Name) and t.id.isupper() and n.value is not None:
                    seg = ast.get_source_segment(cm.src, n.value)
                    try:
                        val = ast.literal_eval(n.value)
                        val = json.loads(json.dumps(val, default=str)) if not isinstance(val, dict) else {
                            (",".join(k) if isinstance(k, tuple) else str(k)): v for k, v in val.items()}
                    except Exception:
                        val = seg if not isinstance(n.value, ast.Name) else _resolve_name(m, n.value.id, cm)
                    attrs[t.id] = {"v": val, "from": c}
    base_defaults = {k: v["v"] for k, v in attrs.items() if v["from"] == "CxpTopTest"}
    over = {k: v["v"] for k, v in attrs.items() if v["from"] != "CxpTopTest" and k not in ("PLAN", "PLAN_PARTIAL", "EXPECT_FAIL")}
    seqs, uses, checks = [], set(), set()
    for c in chain:
        cm, ccd = w.cls[c]
        for fn in ast.walk(ccd):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local = {}
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Call) \
                        and isinstance(n.value.func, ast.Name) and n.value.func.id in w.cls:
                    local[n.targets[0].id] = n.value.func.id
                if isinstance(n, ast.Assign):
                    vals = n.value.elts if isinstance(n.value, ast.Tuple) else [n.value]
                    tgts = n.targets[0].elts if isinstance(n.targets[0], ast.Tuple) else n.targets
                    for t, v in zip(tgts, vals):
                        if isinstance(t, ast.Name) and dotted(v) and dotted(v).startswith("self.env"):
                            local[t.id] = dotted(v)
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "start" and n.args:
                    v = n.func.value
                    sc = v.func.id if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) else local.get(getattr(v, "id", ""))
                    if sc and w.kind(sc) == "sequence":
                        on = dotted(n.args[0]) or ""
                        head = on.split(".")[0]
                        on = strip_self(local.get(head, head) + on[len(head):]) if head in local and isinstance(local.get(head), str) and local[head].startswith("self") else strip_self(on)
                        if not any(s["cls"] == sc and s["on"] == on for s in seqs):
                            seqs.append({"cls": sc, "on": on})
                if isinstance(n, ast.Attribute):
                    p = dotted(n)
                    if p:
                        head = p.split(".")[0]
                        if head in local and isinstance(local[head], str) and local[head].startswith("self.env"):
                            p = local[head] + p[len(head):]
                        if p.startswith("self.env.") or p.startswith("env."):
                            par = getattr(n, "_parent", None)
                            if isinstance(par, ast.Attribute):
                                continue
                            if isinstance(par, (ast.Assign, ast.Tuple)) and not (
                                    isinstance(par, ast.Assign) and n in par.targets):
                                continue        # an alias: env, drv = self.env, self.env.uplink_ag.drv
                            if isinstance(par, ast.Call) and n in par.args and getattr(par.func, "attr", "") == "start":
                                continue        # the sequencer a sequence starts on
                            parts = strip_self(p).split(".")
                            if len(parts) >= 2 and not parts[-1].endswith("seqr"):
                                depth = 3 if parts[1] in ("drv", "mon", "seqr", "trig_seqr", "tpg_mon") and len(parts) >= 3 else 2
                                called = isinstance(par, ast.Call) and par.func is n
                                keep = depth + 1 if called and len(parts) > depth else depth
                                uses.add(".".join(parts[:keep]) + ("()" if called else ""))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "check" \
                        and (dotted(n.func.value) or "").endswith("sb_test") and len(n.args) >= 2 \
                        and isinstance(n.args[1], ast.Constant):
                    checks.add(n.args[1].value)
    # helpers the test calls that make their own checks
    for c in chain:
        cm, ccd = w.cls[c]
        for n in ast.walk(ccd):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in cm.funcs:
                for x in ast.walk(cm.funcs[n.func.id]):
                    if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute) and x.func.attr == "check" \
                            and len(x.args) >= 2 and isinstance(x.args[1], ast.Constant):
                        checks.add(x.args[1].value)
    sig = {}
    for c in chain:
        for p, h in w.class_signals(c).items() if c == name else []:
            sig[p] = h
    sig = {p: h for p, h in w.class_signals(name).items()
           if p not in w.class_signals("CxpTopTest") or name == "CxpTopTest"}
    tiers = [t.lower() for t, v in lists.items() if name in v]
    dec = sorted({n.id for c in chain for n in ast.walk(w.cls[c][1]) if isinstance(n, ast.Name) and n.id in DEC_NAMES})
    pe = plan_tests.get(name, {})
    ef = attrs.get("EXPECT_FAIL", {}).get("v", {}) or {}
    return {
        "name": name, "file": REL(m.path), "line": cd.lineno, "lines": cd.end_lineno - cd.lineno + 1,
        "base": w.bases(name)[0], "doc": doc_all(cd), "tiers": tiers,
        "knobs": knobs.get(name, {}), "requires": attrs.get("REQUIRES", {}).get("v", {}) or {},
        "over": {k: v for k, v in over.items() if k != "REQUIRES"},
        "plan_rows": list(attrs.get("PLAN", {}).get("v", []) or []),
        "partial": attrs.get("PLAN_PARTIAL", {}).get("v", {}) or {},
        "expect_fail": ef, "dec": dec, "seqs": seqs, "uses": sorted(uses), "checks": sorted(checks), "sig": sig,
        "vp": pe.get("vp", ""), "area": pe.get("area", ""), "area_no": pe.get("area_no", 99), "title": pe.get("title", "") or doc_first(cd).split(". ")[0],
        "cat": pe.get("cat", ""), "spec": pe.get("spec", ""), "desc": pe.get("desc", ""), "how": pe.get("how", ""),
        "pass": pe.get("pass", []), "fail": pe.get("fail", []), "plan_tiers": pe.get("tiers", []),
        "_base_defaults": base_defaults,
    }


def _resolve_name(m: Mod, name: str, cm: Mod, mods=None):
    for mm in (cm, m):
        if name in mm.imports and mm.imports[name][0] in MODS:
            src = MODS[mm.imports[name][0]]
            if name in src.consts:
                try:
                    return ast.literal_eval(src.consts[name])
                except Exception:
                    pass
        if name in mm.consts:
            try:
                v = ast.literal_eval(mm.consts[name])
                if isinstance(v, dict):
                    return {(",".join(k) if isinstance(k, tuple) else str(k)): x for k, x in v.items()}
                return list(v) if isinstance(v, tuple) else v
            except Exception:
                pass
            if isinstance(mm.consts[name], ast.BinOp):
                parts = []
                for side in (mm.consts[name].left, mm.consts[name].right):
                    try:
                        parts += list(ast.literal_eval(side))
                    except Exception:
                        pass
                if parts:
                    return parts
    return name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="fail if the page's UVM block is stale")
    a = ap.parse_args()
    mods = load_mods()
    MODS.update(mods)
    ports, params = tb_ports()
    w = World(mods, ports)
    msgs = []

    global DEC_NAMES
    DEC_NAMES = {k for k in mods["uvm.common.decisions"].consts if k.isupper()}
    # components
    comps = {}
    for name, (m, cd) in sorted(w.cls.items()):
        k = w.kind(name)
        if not k or k == "test" and name != "CxpTopTest":
            continue
        bi = w.build_info(name)
        c = {"kind": k, "file": REL(m.path), "line": cd.lineno, "lines": cd.end_lineno - cd.lineno + 1,
             "base": w.bases(name)[0] if w.bases(name) else "", "doc": doc_all(cd) or doc_first(m.tree),
             "children": bi["children"], "ports": bi["ports"], "fifos": bi["fifos"], "exports": bi["exports"],
             "items": bi["item_ports"], "sig": w.class_signals(name), "cfg": w.configdb(name)}
        if k == "scoreboard":
            c["kinds"] = w.err_kinds(name)
        if bi["ports"]:
            c["txn"] = {pt: txn_of(w, m, pt) for pt in bi["ports"]}
        dec = sorted({n.id for n in ast.walk(cd) if isinstance(n, ast.Name) and n.id in DEC_NAMES})
        if dec:
            c["dec"] = dec
        comps[name] = c

    lists, knobs, mk_defaults, targets = makefile()
    plan_tests, plan_kinds, plan_stamp = plan()
    test_names = [n for n in w.cls if w.kind(n) == "test" and n.startswith("test_")]
    order = {n: (w.cls[n][0].name, w.cls[n][1].lineno) for n in test_names}
    test_names.sort(key=lambda n: order[n])
    tests = [test_info(w, n, {k: v for k, v in lists.items() if k in ("SMOKE", "CI", "FEATURE", "XIFC", "NIGHTLY", "WEEKLY")},
                       knobs, plan_tests) for n in test_names]
    base_defaults = tests[0].pop("_base_defaults") if tests else {}
    for t in tests:
        t.pop("_base_defaults", None)

    env = env_scope(w, comps, tests)
    scopes = {"env": env}
    for k, n in env["nodes"].items():
        if n.get("child"):
            scopes[k] = agent_scope(w, comps, k, n["cls"], env)
    for k, n in env["nodes"].items():
        kd = n.get("kind")
        n["role"] = ("test" if k == "test" else "env" if n.get("glue") else kd if kd in ("agent", "scoreboard", "sequencer")
                     else "host" if any(nn["k"] == "call" and any(d.startswith(k + ":") for d in nn["d"]) for nn in env["nets"])
                     else "sink" if kd else "")
    for sid, sc in scopes.items():
        if sid != "env":
            for k, n in sc["nodes"].items():
                n["role"] = "env" if n.get("glue") else n.get("kind", "")
    layout(scopes)

    # -- checks against the code ----------------------------------------------
    defined = set(test_names)
    for tier, names in lists.items():
        for n in names:
            if n not in defined:
                msgs.append(f"Makefile {tier}_TESTS names {n}, no such test class")
    for n in sorted(defined - set(lists.get("WEEKLY", []))):
        msgs.append(f"{n}: in no tier (WEEKLY_TESTS)")
    for n in sorted(defined - set(plan_tests)):
        msgs.append(f"{n}: missing from the verification plan")
    for n in sorted(set(plan_tests) - defined):
        msgs.append(f"plan lists {n}, no such test class")
    for t in tests:
        if t["vp"] and sorted(t["plan_tiers"]) != sorted(t["tiers"]):
            msgs.append(f"{t['name']}: plan tiers {', '.join(t['plan_tiers'])}; Makefile {', '.join(t['tiers'])}")
        req = {k: str(v) for k, v in t["requires"].items()}
        if req != t["knobs"]:
            msgs.append(f"{t['name']}: the Makefile builds it with "
                        f"{' '.join(f'{k}={v}' for k, v in t['knobs'].items()) or 'the defaults'}, but the class "
                        + (f"REQUIRES {' '.join(f'{k}={v}' for k, v in req.items())}" if req else
                           "has no REQUIRES, so a run on another build is not refused"))
        for key in t["expect_fail"]:
            sb, kind = key.split(",")
            cls = env["nodes"].get(sb, {}).get("cls")
            if cls and kind not in comps[cls].get("kinds", {}) and sb != "sb_test":
                msgs.append(f"{t['name']}: EXPECT_FAIL {sb}/{kind}: {cls} raises no such kind")
    for cls, ks in plan_kinds.items():
        if cls in comps and "kinds" in comps[cls]:
            code = comps[cls]["kinds"]
            for k in sorted(set(ks) - set(code)):
                if not any("{" in c and re.fullmatch(re.sub(r"\\\{.*?\\\}", ".+", re.escape(c)), k) for c in code):
                    msgs.append(f"the plan's §3.3 lists error kind {k} for {cls}, which the code never raises")
            for k in sorted(set(code) - set(ks)):
                if "{" not in k:
                    msgs.append(f"{cls} raises error kind {k}, which the plan's §3.3 does not describe")
            comps[cls]["kdesc"] = {k: ks[k] for k in ks if k in code}
    used = defaultdict(lambda: {"w": [], "r": []})
    for name, c in comps.items():
        for p, h in c["sig"].items():
            used[p][h].append(name)
    for t in tests:
        for p, h in t["sig"].items():
            used[p][h].append(t["name"])
    for p, h in w.class_signals("CxpTopTest").items():
        used[p][h].append("CxpTopTest")
    for p, info in ports.items():
        info["drv"] = sorted(set(used[p]["w"]))
        info["smp"] = sorted(set(used[p]["r"]))
        if not info["drv"] and not info["smp"]:
            msgs.append(f"tb_cxp_top.{p} ({info['dir']}): no Python class reads or drives it")
        elif info["dir"] == "in" and not info["drv"]:
            msgs.append(f"tb_cxp_top.{p} (in): read but never driven by Python")
        if info.get("unused"):
            msgs.append(f"tb_cxp_top.{p} (in): driven by {', '.join(info['drv']) or 'nobody'}, but nothing in the shell reads it - it reaches neither the DUT nor any shell logic")
    for name, c in comps.items():
        for pt in c["ports"]:
            if c["kind"] in ("monitor", "agent") and not any(
                    n["k"] == "tlm" and any(d.endswith(":" + pt) or d.endswith("." + pt) for d in n["d"]) for n in env["nets"]):
                holders = [k for k, n in env["nodes"].items() if n.get("cls") == name or name in w.build_info(n.get("cls", "CxpEnv")).get("children", {}).values()]
                if holders:
                    msgs.append(f"{name}.{pt}: analysis port connected to nothing")

    read = {d for c in comps.values() for d in c.get("dec", [])} | {d for t in tests for d in t["dec"]}
    for d in sorted(DEC_NAMES - read, key=lambda k: (len(k.split("_")[0]), k)):
        msgs.append(f"decisions.py {d}: no class or test reads it")
    nk = sum(len(c.get("kinds", {})) for c in comps.values())
    checked = [
        f"{len(test_names)} test classes against the Makefile tier lists ({len(lists.get('WEEKLY', []))} in WEEKLY_TESTS) and the plan's {len(plan_tests)} entries, tiers included",
        f"{sum(1 for t in tests if t['knobs'] or t['requires'])} build-knob tests: Makefile KNOBS against the class REQUIRES",
        f"{len(ports)} harness ports: where each goes in the shell and which Python class drives or samples it",
        f"{nk} scoreboard error kinds against the plan's §3.3, and every EXPECT_FAIL tag against the kinds its scoreboard raises",
        f"{sum(len(c['ports']) for c in comps.values())} analysis ports: each connected to at least one export",
        f"{len(DEC_NAMES)} decision constants: which classes and tests read each one",
    ]
    tiers = {}
    for t, info in targets.items():
        names = lists.get(info["list"], [])
        tiers[t] = {**info, "tests": names}
    data = {
        "meta": {"plan": plan_stamp, "make": mk_defaults, "params": params, "base": base_defaults},
        "scopes": scopes, "comps": comps, "tests": tests, "tiers": tiers,
        "lists": {k: v for k, v in lists.items() if k in ("SPEC", "CONC")},
        "dut": ports, "cov": coverage(mods), "decisions": decisions(mods), "msgs": msgs, "checked": checked,
    }
    for msg in msgs:
        print("map:", msg)
    with open(PAGE, encoding="utf-8") as f:
        html = f.read()
    if BEGIN not in html:
        sys.exit(f"{PAGE}: no {BEGIN} marker")
    block = f"{BEGIN}\nconst UVM = {json.dumps(data, separators=(',', ':'), ensure_ascii=False)};\n{END}"
    new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda _: block, html, flags=re.S)
    if a.check:
        return int(new != html)
    with open(PAGE, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    print(f"{len(comps)} classes, {len(tests)} tests, {len(scopes)} scopes, "
          f"{sum(len(s['nets']) for s in scopes.values())} nets -> {REL(PAGE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
