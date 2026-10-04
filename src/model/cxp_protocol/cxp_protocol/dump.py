"""Raw downlink dump: every device -> host word, as the wire carried it.

One file per simulation, gzip-compressed.  The simulation's tx monitor
writes it; the offline audit, the emulator and ``python -m
cxp_protocol.dump`` read it, all through this module, so the format has
one definition.

Layout (all little-endian)::

    header, 16 bytes   magic b"CXDL" | version u16 | flags u16 |
                       tx period in ps u32 | reserved u32
    record, 16 bytes   tx_cycle u64 | word u32 | kmask u8 | 3 pad bytes

`word` holds P0 in bits [7:0] and `kmask` bit i flags lane Pi as a
K-character (the convention of :mod:`cxp_protocol.kcodes`).  `tx_cycle`
counts rising edges of the transmit clock from the start of the monitor;
the header's period is the one in force when the dump started (a test
that retunes the clock changes the time a cycle stands for, not the
count).

With ``FLAG_IDLE_RUNS`` set in the header's flags, an IDLE record stands
for itself and for every cycle up to the next record's: a writer may
record only the first word of an IDLE run.  `idle_words` in the summary
counts the run.

A writer that hits its size cap stops and appends one record with
``kmask = KMASK_TRUNCATED``, a value no 4-lane mask can take, so a reader
knows the dump is incomplete rather than guessing from its length.
"""

from __future__ import annotations

import gzip
import struct
import sys
from collections import Counter
from dataclasses import dataclass
from typing import BinaryIO, Iterator, List, Optional, Tuple

from .kcodes import (KMASK_ALL, K27_7, K28_2, K28_3, K28_4, K28_6, K29_7, is_idle,
                     lanes)
from .packets import Deframer

MAGIC = b"CXDL"
VERSION = 1
HEADER = struct.Struct("<4sHHII")
RECORD = struct.Struct("<QIB3x")
KMASK_TRUNCATED = 0xFF
FLAG_IDLE_RUNS = 0x0001

Record = Tuple[int, int, int]          # (tx_cycle, word, kmask)


@dataclass
class Header:
    version: int
    flags: int
    tx_period_ps: int


class DumpWriter:
    """Append (tx_cycle, word, kmask) records to a gzip file.

    `max_bytes` caps the *compressed* size on disk (0 = no cap).  The
    check runs when the record buffer is flushed, so the file may
    overshoot the cap by at most one flush.
    """

    FLUSH_RECORDS = 4096

    def __init__(self, path: str, tx_period_ps: int, max_bytes: int = 0,
                 flags: int = 0):
        self.path = path
        self.max_bytes = int(max_bytes)
        self._raw = open(path, "wb")
        self._gz = gzip.GzipFile(fileobj=self._raw, mode="wb", compresslevel=6)
        self._gz.write(HEADER.pack(MAGIC, VERSION, int(flags), int(tx_period_ps), 0))
        self._buf: List[bytes] = []
        self.records = 0
        self.truncated = False
        self.closed = False

    def write(self, tx_cycle: int, word: int, kmask: int) -> None:
        if self.truncated or self.closed:
            return
        self._buf.append(RECORD.pack(tx_cycle, word & 0xFFFF_FFFF, kmask & 0xF))
        self.records += 1
        if len(self._buf) >= self.FLUSH_RECORDS:
            self._flush()

    def _flush(self) -> None:
        if not self._buf:
            return
        self._gz.write(b"".join(self._buf))
        self._buf.clear()
        if self.max_bytes and self._raw.tell() >= self.max_bytes:
            self._gz.write(RECORD.pack(0, 0, KMASK_TRUNCATED))
            self.truncated = True

    def close(self) -> None:
        if self.closed:
            return
        if not self.truncated:
            self._flush()
        self._gz.close()
        self._raw.close()
        self.closed = True


def _open(src) -> BinaryIO:
    if isinstance(src, (str, bytes)) or hasattr(src, "__fspath__"):
        return gzip.open(src, "rb")
    return gzip.GzipFile(fileobj=src, mode="rb")


def read_header(fh: BinaryIO) -> Header:
    raw = fh.read(HEADER.size)
    if len(raw) != HEADER.size:
        raise ValueError("downlink dump: short header")
    magic, version, flags, period, _ = HEADER.unpack(raw)
    if magic != MAGIC:
        raise ValueError(f"downlink dump: bad magic {magic!r}")
    if version != VERSION:
        raise ValueError(f"downlink dump: version {version}, reader knows {VERSION}")
    return Header(version, flags, period)


class Dump:
    """A dump read back: header, records, and whether it was cut short."""

    def __init__(self, header: Header, records: List[Record], truncated: bool):
        self.header = header
        self.records = records
        self.truncated = truncated

    def beats(self) -> Iterator[Tuple[int, int]]:
        """(word, kmask) per record, the form :class:`Deframer` takes."""
        for _, w, k in self.records:
            yield w, k


def iter_records(src) -> Tuple[Header, Iterator[Record]]:
    """Header plus a lazy record iterator (stops at a truncation mark)."""
    fh = _open(src)
    hdr = read_header(fh)

    def gen():
        with fh:
            while True:
                raw = fh.read(RECORD.size)
                if len(raw) < RECORD.size:
                    return
                rec = RECORD.unpack(raw)
                if rec[2] == KMASK_TRUNCATED:
                    return
                yield rec
    return hdr, gen()


def read(src) -> Dump:
    fh = _open(src)
    with fh:
        hdr = read_header(fh)
        body = fh.read()
    n = len(body) // RECORD.size
    if len(body) % RECORD.size:
        raise ValueError(f"downlink dump: {len(body) % RECORD.size} trailing bytes")
    records: List[Record] = []
    truncated = False
    for i in range(n):
        rec = RECORD.unpack_from(body, i * RECORD.size)
        if rec[2] == KMASK_TRUNCATED:
            truncated = True
            break
        records.append(rec)
    return Dump(hdr, records, truncated)


# ---------------------------------------------------------------------------
# Summary (python -m cxp_protocol.dump FILE...)
# ---------------------------------------------------------------------------
def _k_kind(word: int, kmask: int) -> Optional[str]:
    if kmask != KMASK_ALL:
        return None
    ls = lanes(word)
    if len(set(ls)) != 1:
        return None
    return {K27_7: "SOP", K29_7: "EOP", K28_3: "K28.3", K28_4: "TRIG_RISE",
            K28_2: "TRIG_FALL", K28_6: "IOACK"}.get(ls[0])


def summarise(d: Dump) -> dict:
    """Counts a reader can check at a glance; structure, not verdicts."""
    kinds: Counter = Counter()
    idle = 0
    runs = bool(d.header.flags & FLAG_IDLE_RUNS)
    for i, (c, w, k) in enumerate(d.records):
        if is_idle(w, k):
            if runs and i + 1 < len(d.records):
                idle += d.records[i + 1][0] - c
            else:
                idle += 1
        else:
            kinds[_k_kind(w, k) or ("data" if k == 0 else "other-K")] += 1
    df = Deframer()
    df.feed(d.beats())
    types: Counter = Counter()
    for f in df.frames:
        types[f"0x{f.type:02X}" if f.kind == "long" and f.type is not None else f.kind] += 1
    first = d.records[0][0] if d.records else 0
    last = d.records[-1][0] if d.records else 0
    return {
        "tx_period_ps": d.header.tx_period_ps,
        "words": len(d.records),
        "first_cycle": first,
        "last_cycle": last,
        "idle_words": idle,
        "beats": dict(kinds),
        "packets": dict(types),
        "deframer_errors": len(df.errors),
        "max_non_idle_run": df.max_run,
        "truncated": d.truncated,
    }


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m cxp_protocol.dump DUMP.bin.gz [...]", file=sys.stderr)
        return 2
    rc = 0
    for path in argv:
        try:
            s = summarise(read(path))
        except (OSError, ValueError) as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            rc = 1
            continue
        span = s["last_cycle"] - s["first_cycle"] + 1 if s["words"] else 0
        print(f"{path}")
        print(f"  words {s['words']}  cycles {s['first_cycle']}..{s['last_cycle']} "
              f"({span * s['tx_period_ps'] / 1e6:.3f} us at {s['tx_period_ps']} ps)"
              + ("  TRUNCATED" if s["truncated"] else ""))
        print(f"  IDLE {s['idle_words']}  "
              + "  ".join(f"{k} {v}" for k, v in sorted(s["beats"].items())))
        print("  packets " + ("  ".join(f"{k} {v}" for k, v in sorted(s["packets"].items()))
                              or "none"))
        print(f"  longest non-IDLE run {s['max_non_idle_run']}  "
              f"deframer errors {s['deframer_errors']}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
