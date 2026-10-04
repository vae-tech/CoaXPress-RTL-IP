"""Packet codecs for CXP-001-2015 §8.3-§8.7 (Tables 15-23).

A *beat* is ``(word, kmask)``; a packet encodes to a list of beats from
the SOP (or short-packet indication) word to the EOP word inclusive.
Uplink (host -> device) traffic can also be expressed as a character
stream ``[(byte, is_k), ...]`` because the Table 15 low-speed trigger is
inserted at a character boundary (§8.2.4).

Every encoder/decoder takes ``q`` (:class:`~cxp_protocol.quirks.Quirks`),
defaulting to the specification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

from .crc import crc32, crc_wire
from .kcodes import (IDLE_KMASK, IDLE_WORD, K27_7, K28_2, K28_4, K28_5, K28_6,
                     K29_7, KMASK_ALL, KMASK_NONE, MASK32, all_k, be_word,
                     from_be_word, is_idle, lanes, rep4, vote)
from .quirks import SPEC, Quirks

Beat = Tuple[int, int]
Char = Tuple[int, bool]

# Table 18 packet types.
TYPE_STREAM = 0x01
TYPE_CTRL_CMD = 0x02
TYPE_CTRL_ACK = 0x03
TYPE_LINKTEST = 0x04

# Table 21 opcodes.
OP_READ = 0x00
OP_WRITE = 0x01
OP_RESET = 0xFF

# Table 22 acknowledgment codes.
ACK_OK_DATA = 0x00
ACK_OK_WRITE = 0x01
ACK_OK_RESET = 0x03
ACK_WAIT = 0x04
ACK_BAD_ADDR = 0x40
ACK_BAD_DATA = 0x41
ACK_BAD_OP = 0x42
ACK_RO_WRITE = 0x43
ACK_WO_READ = 0x44
ACK_OVERSIZE = 0x45
ACK_SIZE_MISMATCH = 0x46
ACK_MALFORMED = 0x47
ACK_CRC = 0x80
ACK_LONG_FORM = (ACK_OK_DATA, ACK_WAIT)

# Table 17.
IOACK_OK = 0x01

# Table 15 / §8.3.2.1.
TRIG_DELAY_MAX = 239     # Table 15: 239 minus the units of 1/24 bit

# Table 23.
LT_DATA_WORDS = 1024

SOP: Beat = (rep4(K27_7), KMASK_ALL)
EOP: Beat = (rep4(K29_7), KMASK_ALL)
IDLE: Beat = (IDLE_WORD, IDLE_KMASK)


def _crc_word(words: Sequence[int], q: Quirks) -> int:
    return crc_wire(crc32(words))


def _crc_ok(words: Sequence[int], crc_w: int, q: Quirks) -> bool:
    return (crc_w & MASK32) == _crc_word(words, q)


def data_beats(words: Iterable[int]) -> List[Beat]:
    return [(w & MASK32, KMASK_NONE) for w in words]


def nwords_of(size_bytes: int) -> int:
    return (size_bytes + 3) // 4


def data_wire(data: Sequence[int], size: int) -> List[int]:
    """Register values as big-endian wire words (§8.2.1), with the
    4N - B pad bytes after the last data byte set to 0 (Tables 21/22)."""
    words = [be_word(d) for d in data]
    if words and size % 4 and len(words) == nwords_of(size):
        words[-1] &= (1 << (8 * (size % 4))) - 1
    return words


# ---------------------------------------------------------------------------
# Control command (Table 21)
# ---------------------------------------------------------------------------
@dataclass
class CtrlCmd:
    op: int
    addr: int = 0
    size: int = 0                      # B, bytes
    data: List[int] = field(default_factory=list)  # register values (native)
    crc_ok: bool = True

    @property
    def nwords(self) -> int:
        return nwords_of(self.size)


def ctrl_cmd(op: int, addr: int = 0, size: Optional[int] = None,
             data: Sequence[int] = (), q: Quirks = SPEC,
             corrupt_crc: bool = False) -> List[Beat]:
    """Encode a control command.  ``data`` holds register values; they go
    on the wire big-endian.  ``size`` defaults to 4*len(data) for a write,
    4 for a read and 0 for a reset."""
    if size is None:
        size = 4 * len(data) if op == OP_WRITE else (0 if op == OP_RESET else 4)
    body = [(op & 0xFF) | (((size >> 16) & 0xFF) << 8) | (((size >> 8) & 0xFF) << 16)
            | ((size & 0xFF) << 24),
            be_word(addr)]
    if op == OP_WRITE:
        body += data_wire(data, size)
    crc_w = _crc_word(body, q) ^ (MASK32 if corrupt_crc else 0)
    return [SOP, (rep4(TYPE_CTRL_CMD), KMASK_NONE)] + data_beats(body) + [(crc_w, KMASK_NONE), EOP]


def parse_ctrl_cmd(body: Sequence[int], q: Quirks = SPEC) -> CtrlCmd:
    """Decode the words between SOP and EOP (TYPE word first)."""
    t, _ = vote(body[0])
    if t != TYPE_CTRL_CMD:
        raise ValueError("not a control command")
    words = list(body[1:])
    p = lanes(words[0])
    op, size = p[0], (p[1] << 16) | (p[2] << 8) | p[3]
    addr = from_be_word(words[1])
    payload = words[2:-1]
    return CtrlCmd(op=op, addr=addr, size=size,
                   data=[from_be_word(w) for w in payload] if op == OP_WRITE else [],
                   crc_ok=_crc_ok(words[:-1], words[-1], q))


# ---------------------------------------------------------------------------
# Acknowledgment (Table 22)
# ---------------------------------------------------------------------------
@dataclass
class CtrlAck:
    code: int
    size: int = 0                      # B, bytes (long form only)
    data: List[int] = field(default_factory=list)
    crc_ok: bool = True

    @property
    def long_form(self) -> bool:
        return self.code in ACK_LONG_FORM


def ctrl_ack(code: int, data: Sequence[int] = (), size: Optional[int] = None,
             q: Quirks = SPEC, corrupt_crc: bool = False) -> List[Beat]:
    head = [SOP, (rep4(TYPE_CTRL_ACK), KMASK_NONE), (rep4(code), KMASK_NONE)]
    if code not in ACK_LONG_FORM:
        return head + [EOP]
    if size is None:
        size = 4 * len(data)
    size_words = [be_word(size)]
    dw = data_wire(data, size)
    body = [rep4(code)] + size_words + dw
    crc_w = _crc_word(body, q) ^ (MASK32 if corrupt_crc else 0)
    return head + data_beats(size_words + dw) + [(crc_w, KMASK_NONE), EOP]


def parse_ctrl_ack(body: Sequence[int], q: Quirks = SPEC) -> CtrlAck:
    """Decode the words between SOP and EOP (TYPE word first)."""
    t, _ = vote(body[0])
    if t != TYPE_CTRL_ACK:
        raise ValueError("not an acknowledgment")
    code, _ = vote(body[1])
    if len(body) == 2:
        return CtrlAck(code=code)
    words = list(body[1:])
    size = from_be_word(words[1])
    data = [from_be_word(w) for w in words[2:-1]]
    return CtrlAck(code=code, size=size, data=data, crc_ok=_crc_ok(words[:-1], words[-1], q))


# ---------------------------------------------------------------------------
# Trigger and I/O acknowledgment (Tables 15, 16, 17)
# ---------------------------------------------------------------------------
def trigger_ls_chars(rising: bool, delay: int) -> List[Char]:
    """Table 15: six characters, inserted at any character boundary."""
    a, b = (K28_2, K28_4) if rising else (K28_4, K28_2)
    d = delay & 0xFF
    return [(a, True), (b, True), (b, True), (d, False), (d, False), (d, False)]


def trigger_hs(rising: bool, delay: int = 0) -> List[Beat]:
    """Table 16: two words, inserted at any word boundary."""
    return [(rep4(K28_4 if rising else K28_2), KMASK_ALL), (rep4(delay), KMASK_NONE)]


def io_ack(code: int = IOACK_OK) -> List[Beat]:
    """Table 17."""
    return [(rep4(K28_6), KMASK_ALL), (rep4(code), KMASK_NONE)]


def trigger_uplink(rising: bool, delay: int, q: Quirks = SPEC) -> List[Char]:
    """Host -> device trigger: the Table 15 characters."""
    return trigger_ls_chars(rising, delay)


# ---------------------------------------------------------------------------
# Stream data packet (Table 19)
# ---------------------------------------------------------------------------
@dataclass
class StreamPkt:
    streamid: int
    tag: int
    dsizep: int
    payload: List[Beat]
    crc_ok: bool = True


def stream_packet(streamid: int, tag: int, payload: Sequence[Beat],
                  q: Quirks = SPEC, corrupt_crc: bool = False) -> List[Beat]:
    n = len(payload)
    hdr = [rep4(streamid), rep4(tag), rep4(n >> 8), rep4(n)]
    crc_w = _crc_word([w for w, _ in payload], q) ^ (MASK32 if corrupt_crc else 0)
    return ([SOP, (rep4(TYPE_STREAM), KMASK_NONE)] + data_beats(hdr) + list(payload)
            + [(crc_w, KMASK_NONE), EOP])


def parse_stream_packet(body: Sequence[Beat], q: Quirks = SPEC) -> StreamPkt:
    """Decode the beats between SOP and EOP (TYPE beat first)."""
    t, _ = vote(body[0][0])
    if t != TYPE_STREAM:
        raise ValueError("not a stream packet")
    hdr = [w for w, _ in body[1:5]]
    sid, tag = vote(hdr[0])[0], vote(hdr[1])[0]
    n = (vote(hdr[2])[0] << 8) | vote(hdr[3])[0]
    payload = list(body[5:-1])
    return StreamPkt(sid, tag, n, payload, _crc_ok([w for w, _ in payload], body[-1][0], q))


# ---------------------------------------------------------------------------
# Connection test packet (Table 23)
# ---------------------------------------------------------------------------
def linktest_word(i: int) -> int:
    s = (4 * i) & 0xFF
    return s | (((s + 1) & 0xFF) << 8) | (((s + 2) & 0xFF) << 16) | (((s + 3) & 0xFF) << 24)


def linktest_payload(n: int = LT_DATA_WORDS) -> List[int]:
    return [linktest_word(i) for i in range(n)]


def linktest_packet(n: int = LT_DATA_WORDS) -> List[Beat]:
    return [SOP, (rep4(TYPE_LINKTEST), KMASK_NONE)] + data_beats(linktest_payload(n)) + [EOP]


def linktest_errors(payload: Sequence[int], q: Quirks = SPEC) -> int:
    """Word errors a receiver counts for a received test payload (§8.7.1):
    each differing word, plus each missing or surplus word."""
    n = len(payload)
    diff = sum(1 for i in range(min(n, LT_DATA_WORDS)) if payload[i] != linktest_word(i))
    return diff + abs(LT_DATA_WORDS - n)


# ---------------------------------------------------------------------------
# Beat / character stream helpers
# ---------------------------------------------------------------------------
def beats_to_chars(beats: Iterable[Beat]) -> List[Char]:
    out: List[Char] = []
    for w, k in beats:
        for i, b in enumerate(lanes(w)):
            out.append((b, bool((k >> i) & 1)))
    return out


def chars_to_beats(chars: Sequence[Char]) -> List[Beat]:
    if len(chars) % 4:
        raise ValueError("character count is not a multiple of 4")
    out = []
    for i in range(0, len(chars), 4):
        w = k = 0
        for j in range(4):
            b, kf = chars[i + j]
            w |= (b & 0xFF) << (8 * j)
            k |= int(kf) << j
        out.append((w, k))
    return out


def insert_chars(stream: Sequence[Char], at: int, ins: Sequence[Char]) -> List[Char]:
    """Insert characters (e.g. a Table 15 trigger) at a character index."""
    return list(stream[:at]) + list(ins) + list(stream[at:])


# ---------------------------------------------------------------------------
# Downlink deframer (device -> host words)
# ---------------------------------------------------------------------------
@dataclass
class Frame:
    kind: str                  # "long", "trigger", "ioack"
    beats: List[Beat]          # full packet incl. indication / SOP..EOP
    start: int = 0             # beat index of the first word

    @property
    def body(self) -> List[Beat]:
        return self.beats[1:-1] if self.kind == "long" else self.beats[1:]

    @property
    def type(self) -> Optional[int]:
        return vote(self.beats[1][0])[0] if self.kind == "long" and len(self.beats) > 2 else None


class Deframer:
    """Split a downlink beat stream into packets.

    Handles IDLE words inside a long packet (§8.2.5.2 stretching) and
    trigger / I/O-ack packets inserted inside a long packet (§8.2.4).
    Also records how many consecutive non-IDLE words were seen
    (``max_run``) for the §8.2.5.1 IDLE check.
    """

    def __init__(self) -> None:
        self.frames: List[Frame] = []
        self.errors: List[str] = []
        self._long: Optional[Frame] = None
        self._short: Optional[Frame] = None
        self._idx = 0
        self.run = 0
        self.max_run = 0

    def push(self, word: int, kmask: int) -> List[Frame]:
        done: List[Frame] = []
        i = self._idx
        self._idx += 1
        if is_idle(word, kmask):
            self.run = 0
            return done
        self.run += 1
        self.max_run = max(self.max_run, self.run)
        if self._short is not None:
            self._short.beats.append((word, kmask))
            done.append(self._short)
            self._short = None
        elif kmask == KMASK_ALL and (all_k(word, kmask, K28_2) or all_k(word, kmask, K28_4)):
            self._short = Frame("trigger", [(word, kmask)], i)
        elif all_k(word, kmask, K28_6):
            self._short = Frame("ioack", [(word, kmask)], i)
        elif all_k(word, kmask, K27_7):
            if self._long is not None:
                self.errors.append(f"SOP inside packet at beat {i}")
            self._long = Frame("long", [(word, kmask)], i)
        elif all_k(word, kmask, K29_7):
            if self._long is None:
                self.errors.append(f"EOP outside packet at beat {i}")
            else:
                self._long.beats.append((word, kmask))
                done.append(self._long)
                self._long = None
        elif self._long is not None:
            self._long.beats.append((word, kmask))
        else:
            self.errors.append(f"stray word {word:08x}/{kmask:x} at beat {i}")
        self.frames.extend(done)
        return done

    def feed(self, beats: Iterable[Beat]) -> List[Frame]:
        out: List[Frame] = []
        for w, k in beats:
            out += self.push(w, k)
        return out


# ---------------------------------------------------------------------------
# Uplink receiver model (host -> device characters), the golden model of a
# character-granular device front end: extract Table 15 triggers at any
# character boundary, then pack the remaining characters into words framed
# on K28.5 (IDLE P0) and K27.7 (SOP).
# ---------------------------------------------------------------------------
@dataclass
class UplinkEvent:
    kind: str                 # "trigger", "long", "ioack"
    rising: bool = False
    delay: int = 0
    beats: List[Beat] = field(default_factory=list)


class UplinkReceiver:
    """Host -> device character stream to events.

    Table 15 triggers are taken out at any character boundary: two of the
    three leader characters in place are enough (§8.2.2), and the three
    characters after the leader go with it.  Two Delay characters alike
    (data, at most 239) give a "trigger" event; otherwise the packet is a
    "trigger_glitch"."""

    _RISE = (K28_2, K28_4, K28_4)
    _FALL = (K28_4, K28_2, K28_2)

    def __init__(self, q: Quirks = SPEC) -> None:
        self.q = q
        self.events: List[UplinkEvent] = []
        self._hold: List[Char] = []
        self._lane: List[Char] = []
        self._deframer = Deframer()

    def _emit_word(self, chars: List[Char]) -> None:
        (w, k), = chars_to_beats(chars)
        for f in self._deframer.push(w, k):
            if f.kind == "long":
                self.events.append(UplinkEvent("long", beats=f.beats))
            elif f.kind == "ioack":
                self.events.append(UplinkEvent("ioack", beats=f.beats))

    def _pack(self, c: Char) -> None:
        if c[1] and c[0] in (K28_5, K27_7) and self._lane and not (
                c[0] == K27_7 and all(x == (K27_7, True) for x in self._lane)):
            self._lane = []              # re-frame: a comma / SOP starts a word
        self._lane.append(c)
        if len(self._lane) == 4:
            self._emit_word(self._lane)
            self._lane = []

    @classmethod
    def _leader(cls, h: Sequence[Char]) -> Optional[bool]:
        """True / False for a rising / falling leader (2 of 3), else None."""
        for rising, pat in ((True, cls._RISE), (False, cls._FALL)):
            if sum(k and b == e for (b, k), e in zip(h, pat)) >= 2:
                return rising
        return None

    def push(self, c: Char) -> None:
        self._hold.append(c)
        while len(self._hold) >= 3:
            h = self._hold
            rising = self._leader(h[:3])
            if rising is None:
                self._pack(h.pop(0))
                continue
            if len(h) < 6:
                return
            ds = [b for b, k in h[3:6] if not k]
            d = max(set(ds), key=ds.count) if ds else -1
            ok = ds.count(d) >= 2 and 0 <= d <= TRIG_DELAY_MAX
            self.events.append(UplinkEvent("trigger" if ok else "trigger_glitch",
                                           rising, d if ok else 0))
            del h[:6]

    def feed(self, chars: Iterable[Char]) -> List[UplinkEvent]:
        n = len(self.events)
        for c in chars:
            self.push(c)
        return self.events[n:]
