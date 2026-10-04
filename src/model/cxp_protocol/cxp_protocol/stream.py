"""Image stream format (CXP-001-2015 §9.4): pixel formats, packing,
image headers and line markers, and the stream-side reassembly model.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .kcodes import K28_3, KMASK_ALL, KMASK_NONE, rep4, vote
from .packets import Beat, StreamPkt, parse_stream_packet
from .quirks import SPEC, Quirks

# Marker sub-types (Tables 38-41).
HDR_RECT = 0x01
LINE_RECT = 0x02
HDR_ARB = 0x03
LINE_ARB = 0x04

RECT_HDR_WORDS = 25
RECT_LINE_WORDS = 2
ARB_HDR_WORDS = 16
ARB_LINE_WORDS = 11

MARKER: Beat = (rep4(K28_3), KMASK_ALL)

# Table 25 codes used by the device.
PIXFMT_RAW = 0x0000
PIXFMT_MONO8 = 0x0101
PIXFMT_MONO10 = 0x0102
PIXFMT_MONO12 = 0x0103
PIXFMT_MONO14 = 0x0104
PIXFMT_MONO16 = 0x0105

# §11.2.1.6: the PixelFormat feature holds the GenICam PFNC value; the
# device maps it to the Table 25 PixelF code it sends.
PFNC_TO_PIXELF = {
    0x0108_0001: PIXFMT_MONO8,
    0x0110_0003: 0x0102,          # Mono10
    0x0110_0005: 0x0103,          # Mono12
    0x0110_0025: 0x0104,          # Mono14
    0x0110_0007: PIXFMT_MONO16,
}
PIXELF_TO_PFNC = {v: k for k, v in PFNC_TO_PIXELF.items()}


def pfnc_to_pixelf(pfnc: int) -> int:
    """PFNC value -> PixelF code; 0 for a value the device does not take."""
    return PFNC_TO_PIXELF.get(pfnc, 0)


def msb_align(sample: int, width: int, bits: int) -> int:
    """§9.4.2 / Figure 32: a `width`-bit sensor sample in a `bits`-bit
    format, MSB-aligned (zero LSBs when the format is wider, the sample's
    MSBs when it is narrower) -- what cxp_app_pixel_packer sends."""
    sample &= (1 << width) - 1
    return sample << (bits - width) if bits >= width else sample >> (width - bits)

# Table 26: width code -> bits.
_WIDTH = {1: 8, 2: 10, 3: 12, 4: 14, 5: 16}

# Components per pixel by data type (Tables 27-34); YUV/YCbCr by sub-type.
_COMPONENTS = {1: Fraction(1), 2: Fraction(1), 3: Fraction(1), 4: Fraction(3), 5: Fraction(4)}
_CHROMA = {1: Fraction(3, 2), 2: Fraction(2), 3: Fraction(3)}   # 411 / 422 / 444


@dataclass(frozen=True)
class PixFmt:
    code: int
    supported: bool
    bits: int                  # bits per component (packing width)
    components: Fraction       # components per pixel (average)

    @property
    def bits_per_pixel(self) -> Fraction:
        return self.bits * self.components


def pixfmt_desc(code: int, q: Quirks = SPEC) -> PixFmt:
    """Decode a Table 25 PixelF code."""
    dtype, sub, width = code >> 8, (code >> 4) & 0xF, code & 0xF
    bits = _WIDTH.get(width)
    if bits is None:
        return PixFmt(code, False, 0, Fraction(0))
    if dtype in (1,) and sub == 0:
        comps = _COMPONENTS[1]
    elif dtype == 2 and sub >= 1:
        comps = _COMPONENTS[2]
    elif dtype == 3 and 1 <= sub <= 4:
        comps = _COMPONENTS[3]
    elif dtype in (4, 5) and sub == 0:
        comps = _COMPONENTS[dtype]
    elif dtype in (6, 7, 8) and sub in _CHROMA:
        comps = _CHROMA[sub]
    else:
        return PixFmt(code, False, 0, Fraction(0))
    return PixFmt(code, True, bits, comps)


def device_bits(code: int, q: Quirks = SPEC) -> int:
    """Packing width the device uses for a single-component format
    (mono / planar / Bayer), or 0 if the device does not support it."""
    d = pixfmt_desc(code, q)
    if not d.supported or d.components != 1:
        return 0
    return d.bits


def pack_line(pixels: Sequence[int], bits: int) -> List[int]:
    """§9.4.2: pack one line, MSB of D(0) in P0 bit 7, zero-padded to a word."""
    acc = nbits = 0
    for p in pixels:
        acc = (acc << bits) | (p & ((1 << bits) - 1))
        nbits += bits
    pad = (-nbits) % 32
    acc <<= pad
    nbits += pad
    raw = acc.to_bytes(nbits // 8, "big") if nbits else b""
    return [int.from_bytes(raw[i:i + 4], "little") for i in range(0, len(raw), 4)]


def unpack_line(words: Sequence[int], bits: int, n: int) -> List[int]:
    raw = b"".join((w & 0xFFFF_FFFF).to_bytes(4, "little") for w in words)
    acc = int.from_bytes(raw, "big") if raw else 0
    total = 8 * len(raw)
    out = []
    for i in range(n):
        shift = total - (i + 1) * bits
        out.append((acc >> shift) & ((1 << bits) - 1) if shift >= 0 else 0)
    return out


def line_words(xsize: int, bits: int) -> int:
    return (xsize * bits + 31) // 32


def dsizel(xsize: int, bits: int, q: Quirks = SPEC) -> int:
    """DsizeL field value: 32-bit data words per line (Tables 38/41)."""
    return line_words(xsize, bits)


@dataclass
class ImageMeta:
    streamid: int = 0
    sourcetag: int = 0
    xsize: int = 0
    xoffs: int = 0
    ysize: int = 0
    yoffs: int = 0
    pixfmt: int = PIXFMT_MONO8
    tapg: int = 0
    flags: int = 0
    arbitrary: bool = False


def _b3(v: int) -> List[int]:
    return [(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF]


def _b2(v: int) -> List[int]:
    return [(v >> 8) & 0xFF, v & 0xFF]


def _marker(sub: int, fields: Sequence[int]) -> List[Beat]:
    return [MARKER, (rep4(sub), KMASK_NONE)] + [(rep4(b), KMASK_NONE) for b in fields]


def image_header(m: ImageMeta, dsl: int = 0) -> List[Beat]:
    """Table 38 (rectangular) or Table 40 (arbitrary)."""
    if m.arbitrary:
        f = ([m.streamid & 0xFF] + _b2(m.sourcetag) + _b3(m.ysize) + _b3(m.yoffs)
             + _b2(m.pixfmt) + _b2(m.tapg) + [m.flags & 0xFF])
        return _marker(HDR_ARB, f)
    f = ([m.streamid & 0xFF] + _b2(m.sourcetag) + _b3(m.xsize) + _b3(m.xoffs) + _b3(m.ysize)
         + _b3(m.yoffs) + _b3(dsl) + _b2(m.pixfmt) + _b2(m.tapg) + [m.flags & 0xFF])
    return _marker(HDR_RECT, f)


def line_marker(m: ImageMeta, dsl: int = 0) -> List[Beat]:
    """Table 39 (rectangular) or Table 41 (arbitrary)."""
    if m.arbitrary:
        return _marker(LINE_ARB, _b3(m.xsize) + _b3(m.xoffs) + _b3(dsl))
    return _marker(LINE_RECT, [])


def image_stream(m: ImageMeta, lines: Sequence[Sequence[int]], q: Quirks = SPEC,
                 pixfmt_on_wire: Optional[int] = None) -> List[Beat]:
    """Header + (line marker + packed line) per line, as the device emits."""
    bits = device_bits(m.pixfmt, q) or 8
    dsl = dsizel(m.xsize, bits, q)
    hm = m if pixfmt_on_wire is None else replace(m, pixfmt=pixfmt_on_wire)
    out = image_header(hm, dsl)
    for ln in lines:
        out += line_marker(m, dsl)
        out += [(w, KMASK_NONE) for w in pack_line(ln, bits)]
    return out


def chop(payload: Sequence[Beat], n: int) -> List[List[Beat]]:
    """Split a stream into packets of at most n words (§8.5)."""
    return [list(payload[i:i + n]) for i in range(0, len(payload), n)]


# ---------------------------------------------------------------------------
# Stream reassembly (host side)
# ---------------------------------------------------------------------------
@dataclass
class Image:
    meta: ImageMeta
    dsizel: int
    lines: List[List[int]] = field(default_factory=list)   # packed words per line

    def pixels(self, bits: int, xsize: Optional[int] = None) -> List[List[int]]:
        n = self.meta.xsize if xsize is None else xsize
        return [unpack_line(ln, bits, n) for ln in self.lines]


class StreamReassembler:
    """Rebuild images from stream packets (tag checking included).

    Markers may straddle packet boundaries; a marker is recognised by the
    K flags of its first word, so pixel data equal to 0x7C7C7C7C is never
    taken for a marker.
    """

    def __init__(self, q: Quirks = SPEC) -> None:
        self.q = q
        self.images: List[Image] = []
        self.errors: List[str] = []
        self.tags: Dict[int, int] = {}
        self.packets = 0
        self.crc_errors = 0
        self._cur: Optional[Image] = None
        self._pend: List[Beat] = []
        self._need = 0

    def push_packet(self, body: Sequence[Beat]) -> StreamPkt:
        p = parse_stream_packet(body, self.q)
        self.packets += 1
        if not p.crc_ok:
            self.crc_errors += 1
            self.errors.append(f"CRC error in packet {self.packets}")
        exp = self.tags.get(p.streamid)
        if exp is not None and p.tag != exp:
            self.errors.append(f"stream {p.streamid}: tag {p.tag}, expected {exp}")
        self.tags[p.streamid] = (p.tag + 1) & 0xFF
        if p.dsizep != len(p.payload):
            self.errors.append(f"DsizeP {p.dsizep} != payload {len(p.payload)}")
        for b in p.payload:
            self._push_beat(b)
        return p

    def _push_beat(self, b: Beat) -> None:
        if self._pend:
            self._pend.append(b)
            if len(self._pend) == 2:
                sub = vote(self._pend[1][0])[0]
                self._need = {HDR_RECT: RECT_HDR_WORDS, HDR_ARB: ARB_HDR_WORDS,
                              LINE_RECT: RECT_LINE_WORDS, LINE_ARB: ARB_LINE_WORDS}.get(sub, 0)
                if not self._need:
                    self.errors.append(f"unknown marker sub-type 0x{sub:02x}")
                    self._pend = []
                    return
            if self._need and len(self._pend) == self._need:
                self._marker(self._pend)
                self._pend = []
            return
        if b[1] == KMASK_ALL and vote(b[0])[0] == K28_3:
            self._pend = [b]
            return
        if self._cur is None or not self._cur.lines:
            self.errors.append("pixel data outside a line")
            return
        self._cur.lines[-1].append(b[0])

    def _marker(self, beats: List[Beat]) -> None:
        f = [vote(w)[0] for w, _ in beats[2:]]
        sub = vote(beats[1][0])[0]
        u3 = lambda i: (f[i] << 16) | (f[i + 1] << 8) | f[i + 2]
        if sub == HDR_RECT:
            m = ImageMeta(streamid=f[0], sourcetag=(f[1] << 8) | f[2], xsize=u3(3), xoffs=u3(6),
                          ysize=u3(9), yoffs=u3(12), pixfmt=(f[18] << 8) | f[19],
                          tapg=(f[20] << 8) | f[21], flags=f[22])
            self._cur = Image(m, u3(15))
            self.images.append(self._cur)
        elif sub == HDR_ARB:
            m = ImageMeta(streamid=f[0], sourcetag=(f[1] << 8) | f[2], ysize=u3(3), yoffs=u3(6),
                          pixfmt=(f[9] << 8) | f[10], tapg=(f[11] << 8) | f[12], flags=f[13],
                          arbitrary=True)
            self._cur = Image(m, 0)
            self.images.append(self._cur)
        elif self._cur is None:
            self.errors.append("line marker before any image header")
        else:
            if sub == LINE_ARB:
                self._cur.meta.xsize, self._cur.meta.xoffs, self._cur.dsizel = u3(0), u3(3), u3(6)
            self._cur.lines.append([])
