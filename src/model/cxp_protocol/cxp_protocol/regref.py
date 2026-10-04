"""Reference register file (CXP-001-2015 §10.3, Table 45).

What a conforming device should answer for any control read or write:
the register values *and* the Table 22 acknowledgment code.  The map it
executes is `regmodel.py`, generated from `src/regmap/cxp_regmap.yaml` — the
same file the RTL register file is generated from — so a testbench that
checks a device against this model is checking it against the map and
not against a second, hand-copied list of addresses.

    ref = RegRef()
    code, words = ref.read(regmap.STANDARD, 4)        # (0x00, [0xC0A79AE5])
    code = ref.write(regmap.TEST_MODE, [1])           # 0x01
    ref.connection_reset()                            # §10.3.28

Live values the map cannot know — the connection-test counters, and the
XML ROM — are supplied by the caller (`set_counter`, `set_xml`).  Read
them before they are set and the model says so rather than inventing a
value: `read` raises `Unknown`.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from . import regmodel as _m
from .packets import (ACK_BAD_ADDR, ACK_BAD_DATA, ACK_OK_DATA, ACK_OK_WRITE,
                      ACK_RO_WRITE, ACK_WO_READ, nwords_of)


class Unknown(Exception):
    """A live value the model has not been told (counter, XML ROM)."""


# Row tuple indices of regmodel.BOOTSTRAP.
_NAME, _ADDR, _KIND, _NBYTES, _VALUE, _RESET, _CRST, _MAX, _ALLOWED, _BITS, _MIN, _MULT = range(12)


def _refused(v: int, lo, hi, allowed, multiple=None) -> bool:
    """A value the register does not take (0x41)."""
    return ((lo is not None and v < lo) or (hi is not None and v > hi)
            or (allowed is not None and v not in allowed)
            or (bool(multiple) and v % multiple != 0))


def _str_words(text: str, nbytes: int) -> List[int]:
    """A STR row as register values: big-endian, NUL-padded (§10.3.13)."""
    raw = text.encode("ascii")[:nbytes].ljust(nbytes, b"\0")
    return [int.from_bytes(raw[i:i + 4], "big") for i in range(0, nbytes, 4)]


class RegRef:
    """The register file a host should see."""

    def __init__(self) -> None:
        self.counters: Dict[str, int] = {}
        self.xml: Optional[bytes] = None
        self.reset()

    # -- state -------------------------------------------------------------
    def reset(self) -> None:
        """Power-on state."""
        self.store: Dict[int, int] = {}
        for r in _m.BOOTSTRAP:
            if r[_KIND] in ("RW", "CRST"):
                self.store[r[_ADDR]] = r[_RESET]
            elif r[_KIND] == "NVSTR":
                for w in range(r[_NBYTES] // 4):
                    self.store[r[_ADDR] + 4 * w] = 0
        for d in _m.DEVICE:
            self.store[d[2]] = d[3]
        for w in _m.MANUFACTURER:
            self.store[w[1]] = w[2]

    def connection_reset(self) -> None:
        """§10.3.28: load the ConnectionReset value of every row that has
        one; leave the use-case features and the manufacturer words."""
        for r in _m.BOOTSTRAP:
            if r[_KIND] == "RW" and r[_CRST] is not None:
                self.store[r[_ADDR]] = r[_CRST]
            elif r[_KIND] == "CRST":
                self.store[r[_ADDR]] = 0
        for name in list(self.counters):
            self.counters[name] = 0

    def set_counter(self, name: str, value: int) -> None:
        """A live §10.3.37-39 counter's current value."""
        self.counters[name] = int(value)

    def set_xml(self, blob: bytes) -> None:
        self.xml = bytes(blob)

    # -- decode ------------------------------------------------------------
    def _row(self, addr: int):
        for r in _m.BOOTSTRAP:
            if r[_ADDR] <= addr < r[_ADDR] + max(4, r[_NBYTES]):
                return r
        return None

    def _device(self, addr: int):
        for d in _m.DEVICE:
            if addr in (d[1], d[2]):
                return d
        return None

    def _in_mfr(self, addr: int) -> bool:
        return _m.MFR_BASE <= addr < _m.MFR_BASE + 4 * _m.MFR_WORDS

    def _in_xml(self, addr: int) -> bool:
        return _m.XML_BLOB_ADDR <= addr < _m.XML_BLOB_ADDR + _m.XML_BLOB_BYTES

    # -- access ------------------------------------------------------------
    def word(self, addr: int) -> int:
        """One register value, or raise Unknown / KeyError."""
        row = self._row(addr)
        if row is not None:
            k, off = row[_KIND], addr - row[_ADDR]
            if k == "RO":
                return int(row[_VALUE])
            if k in ("RW", "CRST"):
                return self.store[row[_ADDR]] & ((1 << row[_BITS]) - 1)
            if k == "STR":
                return _str_words(str(row[_VALUE]), row[_NBYTES])[off // 4]
            if k == "NVSTR":
                return self.store[addr]
            if k == "ZERO":
                return 0
            if k == "CNT":
                if row[_NAME] not in self.counters:
                    raise Unknown(f"{row[_NAME]} is a live counter")
                v = self.counters[row[_NAME]]
                if row[_NBYTES] == 8:          # high word first (§10.3.38)
                    return (v >> 32) & 0xFFFF_FFFF if off == 0 else v & 0xFFFF_FFFF
                return v & 0xFFFF_FFFF
        dev = self._device(addr)
        if dev is not None and addr == dev[1]:
            return dev[2]                      # the slot reads the address
        if dev is not None and dev[7] == "WO":
            raise PermissionError(addr)
        if self._in_mfr(addr):
            return self.store.get(addr, 0)
        if self._in_xml(addr):
            if self.xml is None:
                raise Unknown("the XML ROM has not been supplied")
            off = addr - _m.XML_BLOB_ADDR
            return int.from_bytes(self.xml[off:off + 4].ljust(4, b"\0"), "big")
        raise KeyError(addr)

    def read(self, addr: int, nbytes: int = 4) -> Tuple[int, List[int]]:
        """(Table 22 code, register values).  Unaligned or undecoded → 0x40."""
        if addr & 3 or nbytes <= 0:
            return ACK_BAD_ADDR, []
        out: List[int] = []
        for i in range(nwords_of(nbytes)):
            try:
                out.append(self.word(addr + 4 * i))
            except KeyError:
                return ACK_BAD_ADDR, []
            except PermissionError:
                return ACK_WO_READ, []
        return ACK_OK_DATA, out

    def write(self, addr: int, values: Sequence[int]) -> int:
        """Table 22 code.  A refused write changes nothing."""
        if addr & 3:
            return ACK_BAD_ADDR
        # Decide the whole access before changing anything.
        plan: List[Tuple[int, int]] = []
        for i, v in enumerate(values):
            a, v = addr + 4 * i, int(v) & 0xFFFF_FFFF
            row = self._row(a)
            if row is not None:
                k = row[_KIND]
                if k in ("RO", "STR", "ZERO"):
                    return ACK_RO_WRITE
                if k == "CNT":
                    if v != 0:
                        return ACK_BAD_DATA    # only a write of 0 clears it
                    plan.append((-1, 0))       # the clear is a side effect
                    continue
                if k in ("RW", "CRST"):
                    if _refused(v, row[_MIN], row[_MAX], row[_ALLOWED], row[_MULT]):
                        return ACK_BAD_DATA
                    plan.append((row[_ADDR], v))
                    continue
                if k == "NVSTR":
                    plan.append((a, v))
                    continue
            dev = self._device(a)
            if dev is not None and a == dev[1]:
                return ACK_RO_WRITE            # the slot is read-only
            if dev is not None and a == dev[2]:
                if _refused(v, dev[5], dev[6], dev[4]):
                    return ACK_BAD_DATA
                plan.append((a, v))
                continue
            if self._in_mfr(a):
                w = next((w for w in _m.MANUFACTURER if w[1] == a), None)
                if w is not None and _refused(v, w[3], w[4], w[5]):
                    return ACK_BAD_DATA
                plan.append((a, v))
                continue
            if self._in_xml(a):
                return ACK_RO_WRITE
            return ACK_BAD_ADDR
        for a, v in plan:
            if a < 0:
                continue
            self.store[a] = v
        # A write of 1 to ConnectionReset applies §10.3.28.
        crst = [r for r in _m.BOOTSTRAP if r[_KIND] == "CRST"]
        if crst and crst[0][_ADDR] == addr and values and int(values[0]) & 1:
            self.connection_reset()
        return ACK_OK_WRITE

    # -- convenience --------------------------------------------------------
    def addr_of(self, name: str) -> int:
        for r in _m.BOOTSTRAP:
            if r[_NAME] == name:
                return r[_ADDR]
        for d in _m.DEVICE:
            if d[0] == name:
                return d[2]
        for w in _m.MANUFACTURER:
            if w[0] == name:
                return w[1]
        raise KeyError(name)

    def readonly_addrs(self) -> List[int]:
        """Every address a write must refuse with 0x43."""
        out: List[int] = []
        for r in _m.BOOTSTRAP:
            if r[_KIND] in ("RO", "STR", "ZERO"):
                out += list(range(r[_ADDR], r[_ADDR] + max(4, r[_NBYTES]), 4))
        out += [d[1] for d in _m.DEVICE]
        return sorted(set(out))

    def writable_addrs(self) -> List[int]:
        out = [r[_ADDR] for r in _m.BOOTSTRAP if r[_KIND] in ("RW", "CRST")]
        out += [d[2] for d in _m.DEVICE]
        out += [w[1] for w in _m.MANUFACTURER]
        return sorted(set(out))
