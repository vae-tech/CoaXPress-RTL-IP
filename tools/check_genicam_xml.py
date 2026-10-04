#!/usr/bin/env python3
"""GenApi validity gate for the device's GenICam XML.

    python3 tools/check_genicam_xml.py [xml]     # default: src/regmap/genicam/cxp_camera.xml

Loads the XML with the EMVA GenICam reference implementation (the `genicam`
package, GenApi's own schema check included), connects its Port to the
reference register model (`cxp_protocol.regref`, generated from the same
YAML as the RTL register file), then walks every feature:

  * every readable feature reads through the port with a Table 22 OK;
  * a write-only feature (Command) is refused on read (0x44) and its
    CommandValue is taken on write;
  * a read-only feature is refused on write (0x43);
  * Integer Min / Max / Inc and Enumeration entries are exactly the values
    the register file takes: each bound, Min + Inc and each entry is taken,
    the value one past a bound, between Min and Min + Inc, or between
    entries is refused (0x41);
  * VendorName / ModelName equal the served DeviceVendorName /
    DeviceModelName, and the XmlVersion feature equals the file's
    Major/Minor/SubMinorVersion.

A missing `genicam` package is a failure, not a skip.  No XSD ships with the
package and none is vendored here, so the schema check is GenApi's own.
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_XML = os.path.join(ROOT, "src", "regmap", "genicam", "cxp_camera.xml")
sys.path.insert(0, os.path.join(ROOT, "src", "model", "cxp_protocol"))

try:
    from genicam import genapi
except ImportError:
    sys.exit("check_genicam_xml: the EMVA GenICam reference implementation is missing.\n"
             "  install it with:  python3 -m pip install genicam")

from cxp_protocol import regmap, regmodel                      # noqa: E402
from cxp_protocol.packets import (ACK_BAD_DATA, ACK_OK_DATA,   # noqa: E402
                                  ACK_OK_WRITE, ACK_RO_WRITE, ACK_WO_READ)
from cxp_protocol.regref import RegRef                          # noqa: E402

T = genapi.EInterfaceType


class ModelPort(genapi.AbstractPort):
    """The device as the reference model answers; records the last access."""

    def __init__(self, xml: bytes) -> None:
        super().__init__()
        self.ref = RegRef()
        self.ref.set_xml(xml)
        for r in regmodel.BOOTSTRAP:
            if r[2] == "CNT":
                self.ref.set_counter(r[0], 0)
        self.last = None

    def get_access_mode(self):
        return genapi.EAccessMode.RW

    def read(self, addr, length):
        self.last = (addr, length)
        code, words = self.ref.read(addr, length)
        if code != ACK_OK_DATA:
            raise IOError(f"read 0x{addr:X}/{length}: ack 0x{code:02X}")
        return b"".join(w.to_bytes(4, "big") for w in words)[:length]

    def write(self, addr, data):
        data = bytes(data)
        self.last = (addr, len(data))
        words = [int.from_bytes(data[i:i + 4].ljust(4, b"\0"), "big") for i in range(0, len(data), 4)]
        code = self.ref.write(addr, words)
        if code != ACK_OK_WRITE:
            raise IOError(f"write 0x{addr:X}: ack 0x{code:02X}")


def takes(addr: int, nbytes: int, v: int) -> int:
    """Table 22 code of writing v to a fresh register file."""
    words = [(v >> (32 * i)) & 0xFFFF_FFFF for i in reversed(range(max(1, nbytes // 4)))]
    return RegRef().write(addr, words)


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_XML
    xml = open(path, "rb").read()
    errs = []
    nm = genapi.NodeMap()
    try:
        nm.load_xml_from_string(xml.decode("utf-8"))
    except Exception as e:                                     # noqa: BLE001
        print(f"FAIL {path}: GenApi refuses it:\n  {e}")
        return 1
    port = ModelPort(xml)
    nm.connect(port, "Device")

    head = re.search(rb"<RegisterDescription\b(.*?)>", xml, re.S).group(1).decode()
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', head))
    # The features: every leaf under Root, walked through the categories.
    feats, todo, cats = [], [nm.get_node("Root")], 0
    while todo:
        c = todo.pop(0)
        cats += 1
        for n in c.features:
            if T(n.node.principal_interface_type) == T.intfICategory:
                todo.append(nm.get_node(n.node.name))
            else:
                feats.append(nm.get_node(n.node.name))
    counts = {}
    for n in feats:
        name, kind = n.node.name, T(n.node.principal_interface_type)
        counts[kind.name] = counts.get(kind.name, 0) + 1
        try:
            if kind == T.intfICommand:
                n.execute()                                    # CommandValue taken
                addr, ln = port.last
                if port.ref.read(addr, ln)[0] != ACK_WO_READ:
                    errs.append(f"{name}: write-only register 0x{addr:X} is readable")
                continue
            val = n.value                                      # every other feature reads
            addr, ln = port.last
            ro = RegRef().write(addr, [0] * max(1, ln // 4)) == ACK_RO_WRITE
            acc = n.get_access_mode()
            if ro != (acc == genapi.EAccessMode.RO):
                errs.append(f"{name}: XML access {acc} but the register "
                            f"{'refuses' if ro else 'takes'} writes")
            if kind == T.intfIInteger and acc == genapi.EAccessMode.RW:
                lo, hi, inc, top = n.min, n.max, n.inc, (1 << (8 * ln)) - 1
                if lo == 0 and hi >= min(top, (1 << 63) - 1) and inc == 1:
                    continue                                   # the XML states no limit
                for v in {lo, hi}:
                    if takes(addr, ln, v) != ACK_OK_WRITE:
                        errs.append(f"{name}: XML bound {v} is refused by the register")
                if hi < top and takes(addr, ln, hi + 1) != ACK_BAD_DATA:
                    errs.append(f"{name}: {hi + 1} > XML Max is taken by the register")
                if lo > 0 and takes(addr, ln, lo - 1) != ACK_BAD_DATA:
                    errs.append(f"{name}: {lo - 1} < XML Min is taken by the register")
                # The step: Min + Inc is taken, everything in between is not.
                if lo + inc <= hi:
                    if takes(addr, ln, lo + inc) != ACK_OK_WRITE:
                        errs.append(f"{name}: {lo + inc} (Min + Inc) is refused by the register")
                    for v in range(lo + 1, lo + inc):
                        if takes(addr, ln, v) != ACK_BAD_DATA:
                            errs.append(f"{name}: {v} off the XML Inc {inc} is taken by the register")
                            break
            if kind == T.intfIEnumeration:
                vals = sorted(e.value for e in n.entries)
                for v in vals:
                    if takes(addr, ln, v) != ACK_OK_WRITE:
                        errs.append(f"{name}: entry value 0x{v:X} is refused by the register")
                gaps = [v for v in range(vals[0], vals[-1] + 2) if v not in vals]
                if vals[0] > 0:
                    gaps.append(vals[0] - 1)
                for v in gaps:
                    if takes(addr, ln, v) != ACK_BAD_DATA:
                        errs.append(f"{name}: 0x{v:X} (no entry) is taken by the register")
            if name == "DeviceVendorName" and val != attrs["VendorName"]:
                errs.append(f"VendorName {attrs['VendorName']!r} != DeviceVendorName {val!r}")
            if name == "DeviceModelName" and val != attrs["ModelName"]:
                errs.append(f"ModelName {attrs['ModelName']!r} != DeviceModelName {val!r}")
            if name == "XmlVersion":
                want = (int(attrs["MajorVersion"]) << 16 | int(attrs["MinorVersion"]) << 8
                        | int(attrs.get("SubMinorVersion", 0)))
                if val != want:
                    errs.append(f"XmlVersion reads 0x{val:X}, the file says 0x{want:X}")
        except Exception as e:                                 # noqa: BLE001
            errs.append(f"{name}: {e}")

    if len(xml) != regmap.XML_BLOB_BYTES:
        errs.append(f"{path} is {len(xml)} bytes, the map says XML_BLOB_BYTES={regmap.XML_BLOB_BYTES}")
    for e in errs:
        print(f"FAIL {e}")
    summary = ", ".join(f"{v} {k[5:]}" for k, v in sorted(counts.items()))
    print(f"{'FAIL' if errs else 'OK'}   {os.path.relpath(path, ROOT)}: GenApi "
          f"{genapi.__name__} loaded {len(nm.nodes)} nodes; {cats} categories, {len(feats)} features ({summary}); "
          f"{len(errs)} error(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
