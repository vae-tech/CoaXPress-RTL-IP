#!/usr/bin/env python3
"""Match the parameters each check reads with the ones its case declares.

    python3 check_params.py        # exit 1 on a mismatch

Every check reads its stimulus counts and lists from the catalogue
(emulator.params, see gen_validation_cases.py) through Context::iparam(),
sparam(), ilist(), slist(), spsmList() or rows().  This script finds, for
each CXP_CHECK in src/cxp/validation/cases/<area>/<ID>.cpp, the keys its
function and every helper it calls read (a static walk: helpers in the same
file and the area's _helpers.cpp, then in _common.cpp), and compares them with the case's declared keys (a group's
members count as "group.member").  It fails when a check reads a key its case
does not declare (the check would end in ERROR at run time) or when a case
declares a key its check never reads (the catalogue prose would describe a
number that does not drive the check).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES = HERE.parent / "src" / "cxp" / "validation" / "cases"
CATALOGUE = HERE / "cxp_validation_cases.json"

# The Context accessors; every string literal in their argument list is a key
# (c.iparam(cond ? "a" : "b") reads both).
READ = re.compile(r"\b(?:iparam|sparam|ilist|slist|spsmList|rows|param)\(")
REGISTER = re.compile(r"CXP_CHECK\(\s*\"([^\"]+)\"\s*,\s*(\w+)\s*\)")
CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


def strip_comments(src: str) -> str:
    """Remove // and /* */ comments, keeping string literals intact."""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == '"' or c == "'":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1])
            i = j + 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def functions(src: str) -> dict[str, str]:
    """Bodies of the functions defined at namespace scope, by name."""
    src = re.sub(r"(?m)^\s*#.*$", "", strip_comments(src))  # preprocessor lines
    funcs: dict[str, str] = {}
    depth_kinds: list[str] = []  # "ns" or "other" per open brace
    last_stmt = 0                # start of the text before the current '{'
    parens = 0                   # a '{' inside ( ) is a default argument, not a block
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"' or c == "'":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            i = j + 1
            continue
        if c == "(":
            parens += 1
        elif c == ")":
            parens -= 1
        elif parens and c in "{}":
            pass
        elif c in ";}":
            if c == "}" and depth_kinds:
                depth_kinds.pop()
            last_stmt = i + 1
        elif c == "{":
            head = src[last_stmt:i].strip()
            at_ns = all(k == "ns" for k in depth_kinds)
            if re.match(r"^(inline\s+)?namespace\b", head):
                depth_kinds.append("ns")
                last_stmt = i + 1
            elif at_ns and head.endswith(")") or at_ns and re.search(r"\)\s*const$", head):
                m = re.search(r"([A-Za-z_]\w*)\s*\(", head)
                # Match the closing brace of the body.
                d, j = 1, i + 1
                while j < n and d:
                    if src[j] in "\"'":
                        q = src[j]
                        j += 1
                        while j < n and src[j] != q:
                            j += 2 if src[j] == "\\" else 1
                    elif src[j] == "{":
                        d += 1
                    elif src[j] == "}":
                        d -= 1
                    j += 1
                if m:
                    funcs[m.group(1)] = src[i:j]
                i = j
                last_stmt = j
                continue
            else:
                depth_kinds.append("other")
                last_stmt = i + 1
        i += 1
    return funcs


def reads_of(body: str) -> set[str]:
    keys = set()
    for m in READ.finditer(body):
        depth, j = 1, m.end()
        while j < len(body) and depth:
            depth += {"(": 1, ")": -1}.get(body[j], 0)
            j += 1
        keys |= set(re.findall(r"\"([A-Za-z0-9_.]+)\"", body[m.end():j - 1]))
    return keys


def main() -> int:
    common = functions((CASES / "_common.cpp").read_text(encoding="utf-8"))
    cat = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    declared: dict[str, set[str]] = {}
    for c in cat["cases"]:
        keys = set()
        for k, v in c["emulator"].get("params", {}).items():
            keys |= {f"{k}.{m}" for m in v} if isinstance(v, dict) else {k}
        declared[c["id"]] = keys

    errors, seen = [], set()
    for path in sorted(p for p in CASES.glob("*/*.cpp") if not p.name.startswith("_")):
        src = path.read_text(encoding="utf-8")
        helpers = path.parent / "_helpers.cpp"
        local = functions(helpers.read_text(encoding="utf-8")) if helpers.exists() else {}
        local.update(functions(src))
        for case_id, fn in REGISTER.findall(strip_comments(src)):
            seen.add(case_id)
            if fn not in local:
                errors.append(f"{path.name}: {case_id}: check function {fn} not found")
                continue
            reads, todo, done = set(), [fn], set()
            while todo:
                f = todo.pop()
                if f in done:
                    continue
                done.add(f)
                body = local.get(f, common.get(f))
                if body is None:
                    continue
                reads |= reads_of(body)
                todo += [g for g in CALL.findall(body) if (g in local or g in common) and g not in done]
            want = declared.get(case_id)
            if want is None:
                errors.append(f"{path.name}: {case_id}: not in the catalogue")
                continue
            for k in sorted(reads - want):
                errors.append(f"{path.name}: {case_id} reads parameter '{k}', which its case does not declare")
            for k in sorted(want - reads):
                errors.append(f"{path.name}: {case_id} declares parameter '{k}', which its check never reads")
    for case_id, keys in declared.items():
        if keys and case_id not in seen:
            errors.append(f"{case_id} declares parameters but has no check")
    for e in errors:
        print(e, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
