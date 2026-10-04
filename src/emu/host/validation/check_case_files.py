#!/usr/bin/env python3
"""Check the shape of the case files under src/cxp/validation/cases/.

    python3 check_case_files.py        # exit 1 on a violation

Every test case lives in cases/<area>/<ID>.cpp and registers exactly that ID
with CXP_CHECK.  This script fails when a case file's base name is not the ID
it registers, when a file registers two IDs or none, when a case sits in the
wrong area directory, when two files register the same ID, or when a helper
file (a name starting with "_") registers a check.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES = HERE.parent / "src" / "cxp" / "validation" / "cases"

REGISTER = re.compile(r"CXP_CHECK\(\s*\"([^\"]+)\"")


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def area_of(case_id: str) -> str | None:
    """cases/ directory of an ID: CXP-CAM-INIT-001 and CXP-EMU-INIT-101 -> init, UVM-test_x -> uvm."""
    if case_id.startswith("UVM-"):
        return "uvm"
    m = re.match(r"CXP-(?:CAM|EMU)-([A-Z]+)-", case_id)
    return m.group(1).lower() if m else None


def main() -> int:
    errors: list[str] = []
    owner: dict[str, Path] = {}
    files = sorted(CASES.rglob("*.cpp"))
    for path in files:
        rel = path.relative_to(CASES)
        ids = REGISTER.findall(strip_comments(path.read_text(encoding="utf-8")))
        if path.name.startswith("_"):
            for case_id in ids:
                errors.append(f"{rel}: helper file registers {case_id}")
            continue
        if not ids:
            errors.append(f"{rel}: registers no case")
            continue
        if len(ids) > 1:
            errors.append(f"{rel}: registers {len(ids)} cases ({', '.join(ids)}); one file per case")
        for case_id in ids:
            if case_id in owner:
                errors.append(f"{rel}: {case_id} is also registered by {owner[case_id].relative_to(CASES)}")
            owner.setdefault(case_id, path)
        case_id = ids[0]
        if path.stem != case_id:
            errors.append(f"{rel}: registers {case_id}; the file must be named {case_id}.cpp")
        area = area_of(case_id)
        if area is None:
            errors.append(f"{rel}: {case_id} is neither a CXP-CAM-, a CXP-EMU- nor a UVM- ID")
        elif path.parent.name != area:
            errors.append(f"{rel}: {case_id} belongs in cases/{area}/")
    for e in errors:
        print(e, file=sys.stderr)
    print(f"{len(owner)} cases in {len(files)} files under {CASES.relative_to(HERE.parent)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
