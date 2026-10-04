#!/usr/bin/env python3
"""Module connectivity for docs/design/cxp_rtl_map.html, from the elaborated RTL.

    python3 tools/gen_rtl_conn.py            # rewrite the CONN block in the map
    python3 tools/gen_rtl_conn.py --check    # exit 1 if the block is stale

Elaborates cxp_device_top with Verilator (--json-only, every generate unrolled)
with p_ASYNC_CLOCKS = 1 and a non-zero user window, so every CDC primitive and
the APB bridge exist.  For every module that instantiates something (a
"scope") it lists:

    nodes  the instances, plus `in` / `out` (the scope's ports) and `glue`
           (the scope's own assign / always logic)
    nets   every signal that joins two of them: drivers, loads, width/type;
           a packed-struct member select is named var.member

The page draws one graph per scope from this and drills into child scopes.
It also cross-checks the hand-written module entries in the page (MODS:
children, ports) against the netlist and prints every disagreement.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP = os.path.join(ROOT, "docs", "design", "cxp_rtl_map.html")
FLIST = os.path.join(ROOT, "src", "rtl", "cxp_ip.f")
TOP = "cxp_device_top"
GPARAMS = {"p_ASYNC_CLOCKS": "1", "p_USER_SIZE": "4096"}
BEGIN, END = "/* CONN:BEGIN (tools/gen_rtl_conn.py) */", "/* CONN:END */"
CLK_RE = re.compile(r"(^|_)(clk|rst_n|rst)(_[io])?$|^(rx|tx|app)_rst_s_n$")


def elaborate(tmp: str) -> dict:
    cmd = ["verilator", "--json-only", "-F", FLIST, "--top-module", TOP,
           "-Wno-fatal", "-Wno-lint", "-Wno-style", "--Mdir", tmp]
    cmd += [f"-G{k}={v}" for k, v in GPARAMS.items()]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"verilator failed:\n{r.stderr}")
    with open(os.path.join(tmp, f"V{TOP}.tree.json"), encoding="utf-8") as f:
        return json.load(f)


class Types:
    """Width and display name of a dtype address from the type table."""

    def __init__(self, netlist: dict):
        self.t = {}
        for misc in netlist["miscsp"]:
            if misc["type"] == "TYPETABLE":
                for d in misc["typesp"]:
                    self.t[d["addr"]] = d

    @staticmethod
    def _const(node) -> int:
        v = node[0]["name"]                       # 32'sh1f, 32'ha
        return int(v.split("h")[-1], 16) if "h" in v else int(v.split("'")[-1].lstrip("sd") or 0)

    def width(self, a: str) -> int:
        d = self.t.get(a)
        if d is None:
            return 1
        k = d["type"]
        if k == "BASICDTYPE":
            if "range" in d:
                hi, lo = (int(x) for x in d["range"].split(":"))
                return abs(hi - lo) + 1
            return 32 if d.get("keyword") in ("int", "integer") else 1
        if k in ("REFDTYPE", "ENUMDTYPE", "MEMBERDTYPE"):
            return self.width(d["refDTypep"])
        if k == "PACKARRAYDTYPE":
            r = d["rangep"][0]
            return (abs(self._const(r["leftp"]) - self._const(r["rightp"])) + 1) * self.width(d["refDTypep"])
        if k == "STRUCTDTYPE":
            return sum(self.width(m["refDTypep"]) for m in d.get("membersp", []))
        return 1

    def struct(self, a: str):
        d = self.t.get(a)
        while d is not None and d["type"] == "REFDTYPE":
            d = self.t.get(d["refDTypep"])
        return d if d is not None and d["type"] == "STRUCTDTYPE" else None

    def member_at(self, a: str, lsb: int, w: int):
        """Member path covering bits [lsb +: w] of a packed struct, or None."""
        s = self.struct(a)
        if s is None:
            return None
        pos = self.width(s["addr"])
        for m in s.get("membersp", []):
            mw = self.width(m["refDTypep"])
            pos -= mw                                  # first member is the MSBs
            if pos <= lsb and lsb + w <= pos + mw:
                if lsb == pos and w == mw:
                    return m["name"]
                sub = self.member_at(m["refDTypep"], lsb - pos, w)
                return m["name"] + ("." + sub if sub else f"[{lsb - pos + w - 1}:{lsb - pos}]")
        return None

    def name(self, a: str) -> str:
        d = self.t.get(a)
        if d is None:
            return "?"
        k = d["type"]
        if k == "STRUCTDTYPE" or (k == "ENUMDTYPE" and d.get("name")):
            return d["name"].split("::")[-1].split(".")[-1]
        if k == "REFDTYPE":
            return d["name"]
        if k == "UNPACKARRAYDTYPE":
            r = d["rangep"][0]
            n = abs(self._const(r["leftp"]) - self._const(r["rightp"])) + 1
            return f"{self.name(d['refDTypep'])} ×{n}"
        w = self.width(a)
        return "1" if w == 1 else f"[{w - 1}:0]"


def walk(n, fn):
    if isinstance(n, dict):
        fn(n)
        for v in n.values():
            if isinstance(v, (dict, list)):
                walk(v, fn)
    elif isinstance(n, list):
        for v in n:
            walk(v, fn)


def refs(expr, ty: Types, vars_: dict):
    """(var, member-or-None) for every variable an expression touches."""
    out = []

    def go(e):
        k = e["type"]
        if k == "VARREF":
            if e["varp"] in vars_:
                out.append((vars_[e["varp"]], None))
        elif k == "SEL" and e["fromp"][0]["type"] == "VARREF" and e["lsbp"][0]["type"] == "CONST" \
                and e["fromp"][0]["varp"] in vars_:
            v = vars_[e["fromp"][0]["varp"]]
            w = e.get("widthConst", 1)
            mem = ty.member_at(v["dtypep"], Types._const(e["lsbp"]), w)
            out.append((v, (mem, w) if mem else None))
        else:
            for val in e.values():
                if isinstance(val, list):
                    for c in val:
                        if isinstance(c, dict) and "type" in c:
                            go(c)
    for e in expr:
        go(e)
    return out


def items(stmts, prefix=""):
    """(generate prefix, statement) for a module body, generate blocks flattened."""
    for s in stmts:
        if s["type"] == "GENBLOCK":
            name = s.get("name", "")
            yield from items(s.get("itemsp", []), prefix + name + "." if "[" in name or not s.get("implied") else prefix)
        else:
            yield prefix, s


def build(netlist: dict) -> dict:
    ty = Types(netlist)
    mods = [m for m in netlist["modulesp"] if m["type"] == "MODULE"]
    by_addr = {m["addr"]: m for m in mods}
    portvar = {}                                       # child port VAR addr -> VAR
    for m in mods:
        for _, s in items(m["stmtsp"]):
            if s["type"] == "VAR":
                portvar[s["addr"]] = s

    # One scope per module; a parameter specialisation whose structure differs
    # (e.g. a generate block left out) gets its own key, module#2, ...
    scopes, spec_key = {}, {}
    for m in mods:
        mod = m["origName"]
        body = list(items(m["stmtsp"]))
        cells = [(p, s) for p, s in body if s["type"] == "CELL"]
        if not cells:
            continue
        vars_ = {s["addr"]: s for _, s in body if s["type"] == "VAR"
                 and s.get("varType") not in ("GPARAM", "LPARAM", "GENVAR")}
        uses = defaultdict(lambda: {"d": [], "l": [], "mem": set(), "whole": False, "mw": {}})

        def use(v, mem, node, port, drives):
            u = uses[v["name"]]
            u["v"] = v
            tgt = u["d"] if drives else u["l"]
            tgt.append((node, port, mem[0] if mem else None))
            if mem:
                u["mem"].add(mem[0])
                u["mw"][mem[0]] = "1" if mem[1] == 1 else f"[{mem[1] - 1}:0]"

            else:
                u["whole"] = True

        nodes = {"in": {"bnd": 1}, "out": {"bnd": 1}}
        ties = defaultdict(list)
        for v in vars_.values():
            if v["direction"] == "INPUT":
                use(v, None, "in", v["name"], True)
            elif v["direction"] == "OUTPUT":
                use(v, None, "out", v["name"], False)
        for pre, c in cells:
            child = by_addr[c["modp"]]["origName"]
            nid = pre + c["name"]
            nodes[nid] = {"inst": nid, "mod": child, "spec": c["modp"]}
            for pin in c.get("pinsp", []):
                pv = portvar.get(pin.get("modVarp"))
                drives = pv is not None and pv["direction"] == "OUTPUT"
                ex = pin.get("exprp", [])
                if not ex:
                    ties[nid].append([pin["name"], "open"])
                    continue
                rs = refs(ex, ty, vars_)
                if not rs and ex[0]["type"] == "CONST":
                    ties[nid].append([pin["name"], ex[0]["name"]])
                for v, mem in rs:
                    use(v, mem, nid, pin["name"], drives)
        glue = False
        for pre, s in body:
            if s["type"] in ("CELL", "VAR"):
                continue
            def fn(e):
                nonlocal glue
                if e.get("type") == "VARREF" and e.get("varp") in vars_:
                    glue = True
                    use(vars_[e["varp"]], None, "glue", "logic", e["access"] in ("WR", "RW"))
            walk(s, fn)
        if glue:
            nodes["glue"] = {"glue": 1}

        nets = []
        for name, u in uses.items():
            v = u["v"]
            groups = [(name, u["d"], u["l"])]
            if u["mem"] and not u["whole"]:            # only member selects: one net per member
                groups = [(f"{name}.{mm}", [x for x in u["d"] if x[2] == mm], [x for x in u["l"] if x[2] == mm])
                          for mm in sorted(u["mem"])]
            for nm, d, l in groups:
                dn = {x[0] for x in d}
                ln = {x[0] for x in l}
                if not d or not l or (dn | ln) <= {"glue"} or len(dn | ln) < 2:
                    continue
                fmt = lambda x: f"{x[0]}.{x[1]}" + (f"@{x[2]}" if x[2] and "." not in nm else "")
                w = ty.name(v["dtypep"]) if nm == name else u["mw"].get(nm.split(".", 1)[1], "")
                net = {"n": nm, "w": w, "d": sorted({fmt(x) for x in d}), "l": sorted({fmt(x) for x in l})}
                if CLK_RE.search(name):
                    net["clk"] = 1
                nets.append(net)
        nets.sort(key=lambda n: (n.get("clk", 0), n["n"]))
        sc = {"mod": mod, "nodes": nodes, "nets": nets, "ties": dict(ties)}
        shape = lambda o: json.dumps([o["nodes"], [{k: v for k, v in n.items() if k != "w"} for n in o["nets"]],
                                      o["ties"]], sort_keys=True, default=str)
        keys = [k for k in scopes if scopes[k]["mod"] == mod]
        key = next((k for k in keys if shape(scopes[k]) == shape(sc)), None)
        if key is None:
            key = mod if not keys else f"{mod}#{len(keys) + 1}"
            scopes[key] = sc
        else:                                          # same structure, other widths: list both
            for old, new in zip(scopes[key]["nets"], nets):
                ws = old["w"].split(" / ")
                if new["w"] not in ws:
                    old["w"] = " / ".join(ws + [new["w"]])
        spec_key[m["addr"]] = key

    for sc in scopes.values():
        for nid, n in sc["nodes"].items():
            spec = n.pop("spec", None)
            if spec in spec_key:
                n["child"] = spec_key[spec]
    layout(scopes)
    return scopes


# Column layout overrides for scopes where signal flow alone gives a poor
# picture (feedback loops, independent siblings).  Node ids as in the netlist.
HINTS = {
    "cxp_device_top": [["cxp_cdc_reset_i", "cxp_interface_top_i"], ["glue", "cxp_ctrl_bootstrap_regs_i"]],
    "cxp_interface_top": [["cxp_rx_domain_i", "cxp_app_domain_i"], ["cxp_cdc_layer_i"], ["cxp_tx_domain_i"]],
    "cxp_rx_domain": [["cxp_rx_link_i"], ["glue"], ["cxp_ctrl_plane_i"]],
    "cxp_tx_domain": [["cxp_tx_ctrl_ack_i", "cxp_tx_linktest_i", "cxp_tx_stream_pkt_i"],
                      ["glue", "cxp_tx_arbiter_i", "cxp_tx_trigger_hs_i", "cxp_tx_io_ack_i"],
                      ["cxp_tx_inserter_i"]],
    "cxp_rx_link": [["cxp_rx_lspd_sampler_i"],
                    [f"g_lane[{i}].cxp_rx_8b10b_decoder_i" for i in range(4)],
                    ["glue"], ["cxp_rx_link_mon_i", "cxp_rx_trigger_lspd_i"],
                    ["cxp_rx_packet_parser_i"], ["cxp_rx_linktest_i"]],
    "cxp_app_domain": [["cxp_app_pixel_ingress_i", "cxp_app_tpg_i"], ["glue"], ["cxp_app_acq_ctrl_i"],
                       ["cxp_app_pixel_packer_i"], ["cxp_app_stream_i"]],
    "cxp_cdc_layer": [["g_cdc.cxp_cdc_bus_cfg_app_i", "g_cdc.cxp_cdc_bus_cfg_tx_i", "g_cdc.cxp_cdc_pulse_acq_start_i",
                       "g_cdc.cxp_cdc_pulse_acq_stop_i", "g_cdc.cxp_cdc_pulse_conn_cfg_wr_i", "g_cdc.cxp_cdc_pulse_clr_lt_i"],
                      ["g_cdc.cxp_cdc_sync_crst_i", "g_cdc.cxp_cdc_sync_link_i", "g_cdc.cxp_cdc_pulse_trig_rcvd_i",
                       "g_cdc.cxp_cdc_pulse_ioack_rcvd_i", "g_cdc.cxp_cdc_req_rsp_i", "cxp_cdc_stream_fifo_i"],
                      ["g_cdc.cxp_cdc_sync_crst_done_i", "g_cdc.cxp_cdc_bus_lt_pkt_tx_i", "glue"]],
}
NW, NH, GX, GY, BW, MAXROWS = 176, 62, 74, 30, 96, 6


def layout(scopes: dict) -> None:
    """Left-to-right columns: in | instances in signal-flow order | out.

    Order: greedy feedback-arc-set (Eades) on net counts, nodes fed from the
    scope inputs pulled left and nodes feeding its outputs pushed right; then
    longest path over the forward edges, barycentre sweeps inside a column,
    and columns of more than MAXROWS nodes wrapped."""
    for mod, sc in scopes.items():
        ids = [k for k in sc["nodes"] if k not in ("in", "out")]
        w = defaultdict(int)
        bias = defaultdict(int)
        for n in sc["nets"]:
            if n.get("clk"):
                continue
            ds = {node_of(x, sc) for x in n["d"]}
            ls = {node_of(x, sc) for x in n["l"]}
            for a in ds:
                for b in ls:
                    if a == b:
                        continue
                    if a == "in" and b in ids:
                        bias[b] += 1
                    elif b == "out" and a in ids:
                        bias[a] -= 1
                    elif a in ids and b in ids:
                        w[a, b] += 1
        if mod in HINTS:
            cols = {i: list(c) for i, c in enumerate(HINTS[mod])}
            placed = {u for c in cols.values() for u in c}
            assert placed == set(ids), f"{mod}: HINTS {sorted(placed ^ set(ids))}"
        else:
            left, right, rest = [], [], set(ids)
            outw = lambda u: sum(w[u, v] for v in rest if v != u)
            inw = lambda u: sum(w[v, u] for v in rest if v != u)
            while rest:
                sinks = [u for u in rest if outw(u) == 0 and inw(u) > 0]
                srcs = [u for u in rest if inw(u) == 0]
                if srcs:
                    u = max(srcs, key=lambda u: (bias[u], outw(u), -ids.index(u)))
                    left.append(u)
                elif sinks:
                    u = min(sinks, key=lambda u: (bias[u], -ids.index(u)))
                    right.insert(0, u)
                else:
                    u = max(rest, key=lambda u: (outw(u) - inw(u) + bias[u], -ids.index(u)))
                    left.append(u)
                rest.discard(u)
            order = left + right
            rank = {u: i for i, u in enumerate(order)}
            layer = {u: 0 for u in order}
            for u in order:
                for v in order:
                    if rank[v] > rank[u] and w[u, v]:
                        layer[v] = max(layer[v], layer[u] + 1)
            # nodes with no peers at all sit in the first column
            cols = defaultdict(list)
            for u in order:
                cols[layer[u]].append(u)
            und = defaultdict(int)
            for (a, b), k in w.items():
                und[a, b] += k
                und[b, a] += k
            for _ in range(8):
                pos = {u: i for cc in cols.values() for i, u in enumerate(cc)}
                for c in sorted(cols):
                    def bary(u):
                        tot = sum(und[u, v] for v in ids)
                        return sum(pos[v] * und[u, v] for v in ids) / tot if tot else pos[u]
                    cols[c].sort(key=bary)
            wrapped, k = {}, 0
            for c in sorted(cols):
                us = cols[c]
                for i in range(0, len(us), MAXROWS):
                    wrapped[k] = us[i:i + MAXROWS]
                    k += 1
            cols = wrapped
        ncol = max(cols) + 1 if cols else 1
        nrow = max((len(v) for v in cols.values()), default=1)
        width = 2 * BW + 2 * 30 + ncol * NW + (ncol + 1) * GX
        height = max(400, 80 + nrow * (NH + GY))
        for c, us in cols.items():
            x = BW + 30 + GX + c * (NW + GX)
            top = (height - len(us) * (NH + GY) + GY) / 2
            for i, u in enumerate(us):
                sc["nodes"][u]["x"] = round(x)
                sc["nodes"][u]["y"] = round(top + i * (NH + GY))
        sc["W"], sc["H"] = round(width), round(height)


def node_of(ref: str, sc: dict) -> str:
    """Node id of a 'node.port' reference (instance ids may contain dots)."""
    head = ref.split("@")[0]
    node = head.rsplit(".", 1)[0]
    return node if node in sc["nodes"] else head.split(".")[0]


def cross_check(scopes: dict, html: str) -> list[str]:
    m = re.search(r"^const MODS = (\{.*\});$", html, re.M)
    if not m:
        return ["MODS block not found in the page"]
    mods = json.loads(m.group(1))
    out = []
    for key, sc in scopes.items():
        name = sc["mod"]
        page = {c["inst"].split(" ")[0] for c in mods.get(name, {}).get("children", [])}
        real = {n["inst"] for k, n in sc["nodes"].items() if "inst" in n}
        flat = {re.sub(r"\[\d+\]", "", r) for r in real}
        pagef = {re.sub(r"\[\d+(\.\.\d+)?\]", "", p) for p in page}
        for p in sorted(pagef - flat):
            out.append(f"{name}: page lists child {p}, not in the netlist")
        for r in sorted(flat - pagef):
            out.append(f"{name}: netlist child {r} missing from the page")
    return out


def ports_check(netlist: dict, html: str) -> list[str]:
    m = re.search(r"^const MODS = (\{.*\});$", html, re.M)
    mods = json.loads(m.group(1))
    out, seen = [], set()
    for mod in netlist["modulesp"]:
        name = mod.get("origName")
        if mod["type"] != "MODULE" or name in seen or name not in mods:
            continue
        seen.add(name)
        real = {s["name"]: s["direction"].lower().replace("input", "in").replace("output", "out")
                for _, s in items(mod["stmtsp"]) if s["type"] == "VAR" and s["direction"] != "NONE"}
        page = {p["name"]: p["dir"] for p in mods[name].get("ports", [])}
        gp = {s["name"] for _, s in items(mod["stmtsp"]) if s["type"] == "VAR" and s.get("varType") == "GPARAM"}
        pp = {p["name"] for p in mods[name].get("params", [])}
        for p in sorted(pp - gp):
            out.append(f"{name}: page parameter {p} does not exist")
        for p in sorted(gp - pp):
            out.append(f"{name}: parameter {p} missing from the page")
        for p in sorted(set(page) - set(real)):
            out.append(f"{name}: page port {p} does not exist")
        for p in sorted(set(real) - set(page)):
            out.append(f"{name}: port {p} ({real[p]}) missing from the page")
        for p in sorted(set(real) & set(page)):
            if page[p] != real[p]:
                out.append(f"{name}: port {p} is {real[p]}, page says {page[p]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="fail if the page's CONN block is stale")
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        netlist = elaborate(tmp)
    scopes = build(netlist)
    with open(MAP, encoding="utf-8") as f:
        html = f.read()
    for msg in cross_check(scopes, html) + ports_check(netlist, html):
        print("map:", msg)
    meta = {"top": TOP, "params": GPARAMS}
    block = f"{BEGIN}\nconst CONN = {json.dumps({'meta': meta, 'scopes': scopes}, separators=(',', ':'))};\n{END}"
    if BEGIN not in html:
        sys.exit(f"{MAP}: no {BEGIN} marker")
    new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda _: block, html, flags=re.S)
    if a.check:
        return int(new != html)
    with open(MAP, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    n = sum(len(s["nets"]) for s in scopes.values())
    print(f"{len(scopes)} scopes, {n} nets -> {os.path.relpath(MAP, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
