"""stream_dump_subscriber — text dump of non-IDLE CXP downlink traffic.

Passive subscriber on the TxWire monitor's packet analysis port.  For
every SOP..EOP packet it emits:

  * a short decoded summary (STREAM streamid/tag/DsizeP + CRC OK/BAD,
    CTRL_ACK rsp_code/host_id/packet_tag, etc.)
  * one summary line per image header / line marker found in the payload
  * an exhaustive wire-word dump where every word is labelled on the
    right-hand side with its role / decoded field, e.g.:

        [0027] data=0x00000000 kmask=0b0000 <-- PIXEL line_word[0]

K28.3 image headers (rect/arb, per table 37 / table 39) and line markers
(rect/arb, per table 38 / table 40) are walked field-by-field.
Mid-packet arbiter-injected IDLE stretchers (CXP §6.2.5 100-word cadence)
are dumped too — they're flagged ``IDLE (mid-pkt stretcher)`` so the
reader sees the cadence, but they're stripped from CRC coverage.

The file lands at ``./cxp_stream_dump.txt`` by default; ``CXP_STREAM_DUMP``
env var overrides.  ``src/verif/Makefile`` copies it into
``00_test_results/<UVM_TESTNAME>/`` next to results.xml.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

from cocotb.utils import get_sim_time
from pyuvm import uvm_component, uvm_tlm_analysis_fifo

from uvm.common.cxp_pkg import (
    ARB_HDR_WORDS, K28_3, PacketType, RECT_HDR_WORDS, WireBeatKind,
    byteswap32, classify_wire_beat, crc32_words, majority_byte,
)


_RECT_LINE_MARKER_WORDS = 2
_ARB_LINE_MARKER_WORDS  = 11


def _u16(msb: int, lsb: int) -> int:
    return ((msb & 0xFF) << 8) | (lsb & 0xFF)


def _u24(msb: int, mid: int, lsb: int) -> int:
    return ((msb & 0xFF) << 16) | ((mid & 0xFF) << 8) | (lsb & 0xFF)


class StreamDumpSubscriber(uvm_component):
    """Decodes every non-IDLE CXP downlink packet to a text file."""

    DEFAULT_PATH = "cxp_stream_dump.txt"

    # Per-word labels for the rectangular image header (table 37, 25 words).
    _RECT_HDR_LABELS = [
        "K28.3 IMG_HEADER_MARKER (RECT)",
        "HDR_TYPE=0x01 (RECT)",
        "HDR_STREAM_ID",
        "HDR_SOURCE_TAG[15:8]",
        "HDR_SOURCE_TAG[7:0]",
        "HDR_XSIZE[23:16]",
        "HDR_XSIZE[15:8]",
        "HDR_XSIZE[7:0]",
        "HDR_XOFFS[23:16]",
        "HDR_XOFFS[15:8]",
        "HDR_XOFFS[7:0]",
        "HDR_YSIZE[23:16]",
        "HDR_YSIZE[15:8]",
        "HDR_YSIZE[7:0]",
        "HDR_YOFFS[23:16]",
        "HDR_YOFFS[15:8]",
        "HDR_YOFFS[7:0]",
        "HDR_RESERVED",
        "HDR_DSIZE_L[15:8]",
        "HDR_DSIZE_L[7:0]",
        "HDR_PIXFMT[15:8]",
        "HDR_PIXFMT[7:0]",
        "HDR_TAPG[15:8]",
        "HDR_TAPG[7:0]",
        "HDR_FLAGS",
    ]

    # Per-word labels for the arbitrary image header (table 39, 16 words).
    _ARB_HDR_LABELS = [
        "K28.3 IMG_HEADER_MARKER (ARB)",
        "HDR_TYPE=0x03 (ARB)",
        "HDR_STREAM_ID",
        "HDR_SOURCE_TAG[15:8]",
        "HDR_SOURCE_TAG[7:0]",
        "HDR_YSIZE[23:16]",
        "HDR_YSIZE[15:8]",
        "HDR_YSIZE[7:0]",
        "HDR_YOFFS[23:16]",
        "HDR_YOFFS[15:8]",
        "HDR_YOFFS[7:0]",
        "HDR_PIXFMT[15:8]",
        "HDR_PIXFMT[7:0]",
        "HDR_TAPG[15:8]",
        "HDR_TAPG[7:0]",
        "HDR_FLAGS",
    ]

    _RECT_LINE_LABELS = [
        "K28.3 LINE_MARKER (RECT)",
        "LINE_TYPE=0x02 (RECT)",
    ]

    _ARB_LINE_LABELS = [
        "K28.3 LINE_MARKER (ARB)",
        "LINE_TYPE=0x04 (ARB)",
        "LINE_XSIZE[23:16]",
        "LINE_XSIZE[15:8]",
        "LINE_XSIZE[7:0]",
        "LINE_XOFFS[23:16]",
        "LINE_XOFFS[15:8]",
        "LINE_XOFFS[7:0]",
        "LINE_DSIZE_L[23:16]",
        "LINE_DSIZE_L[15:8]",
        "LINE_DSIZE_L[7:0]",
    ]

    def build_phase(self):
        self.pkt_fifo = uvm_tlm_analysis_fifo("pkt_fifo", self)
        self.pkt_xp   = self.pkt_fifo.analysis_export

        self.path = os.environ.get("CXP_STREAM_DUMP", self.DEFAULT_PATH)
        self._fh = None
        self.pkts_dumped   = 0
        self.stream_pkts   = 0
        self.ctrl_ack_pkts = 0
        self.crc_ok        = 0
        self.crc_bad       = 0

        # Frame-level state — persists across stream packets so line
        # numbering is continuous even when a frame spans multiple
        # packets (chopper splits at cfg_dsizeP word boundaries).  Reset
        # only when a new image header is seen.
        self._fr_line_idx    = 0      # count of LINE markers in frame
        self._fr_pxl_line    = 0      # line# emitted on pxl labels
        self._fr_pxl_idx     = 0      # word# within current line
        self._fr_pixels_left = 0      # countdown when xsize known
        self._fr_in_pixels   = False  # currently emitting line pixels?
        self._fr_rect_xsize  = None   # cached rect-header xsize

    def start_of_simulation_phase(self):
        test_name = os.environ.get("UVM_TESTNAME", "unknown")
        try:
            self._fh = open(self.path, "w", buffering=1)
        except OSError as exc:
            self.logger.warning(f"stream dump: cannot open {self.path}: {exc}")
            self._fh = None
            return
        self._fh.write(
            f"# cxp_stream_dump test={test_name}\n"
            f"# layout: per-packet decoded summary + full wire-word dump,\n"
            f"#         every word annotated on the RHS with its role/field.\n"
            f"# kmask bits: lane3 lane2 lane1 lane0  (1 = K-code byte)\n"
            f"# -----------------------------------------------------------\n"
        )
        self.logger.warning(f"stream_dump: writing to {self.path}")

    async def run_phase(self):
        while True:
            pkt = await self.pkt_fifo.get()
            self._dump_packet(pkt)

    # ------------------------------------------------------------------
    # Packet dispatcher
    # ------------------------------------------------------------------
    def _dump_packet(self, pkt) -> None:
        if self._fh is None:
            return
        self.pkts_dumped += 1
        t_ns = get_sim_time(unit="ns")
        words = pkt.words
        self._fh.write(
            f"\n[{t_ns:>10.0f} ns] PACKET #{self.pkts_dumped}  "
            f"type=0x{pkt.type_byte:02x}  wire_words={len(words)}\n"
        )
        if pkt.type_byte == PacketType.STREAM:
            self.stream_pkts += 1
            annots = self._meta_stream(words)
        elif pkt.type_byte == PacketType.CTRL_ACK:
            self.ctrl_ack_pkts += 1
            annots = self._meta_ctrl_ack(words)
        else:
            annots = self._meta_unknown(words)
        self._fh.write("  --- wire words ---\n")
        for j, pair in enumerate(words):
            self._dump_one(j, pair, annots[j], prefix="  ")
        self._fh.flush()

    # ------------------------------------------------------------------
    # STREAM (0x01) — table 18
    # ------------------------------------------------------------------
    def _meta_stream(self, words: List[Tuple[int, int]]) -> List[str]:
        N = len(words)
        annots = [""] * N
        if N < 8:
            self._fh.write(f"  STREAM truncated ({N} words)\n")
            return self._fill_blanks(words, annots)

        streamid, _   = majority_byte(words[2][0])
        pkt_tag,  _   = majority_byte(words[3][0])
        dsizeP_hi, _  = majority_byte(words[4][0])
        dsizeP_lo, _  = majority_byte(words[5][0])
        dsizeP = (dsizeP_hi << 8) | dsizeP_lo
        self._fh.write(
            f"  STREAM  streamid=0x{streamid:02x}  packet_tag=0x{pkt_tag:02x}  "
            f"DsizeP={dsizeP}\n"
        )

        payload_pairs = [
            (d, k) for (d, k) in words[6:-2]
            if classify_wire_beat(d, k) != WireBeatKind.IDLE
        ]
        covered = [d for (d, _k) in payload_pairs]   # Table 19: data only
        crc_calc = crc32_words(covered)
        crc_wire = words[-2][0]
        crc_obs = crc_wire & 0xFFFF_FFFF
        crc_tag = "OK" if crc_obs == crc_calc else "BAD"
        if crc_obs == crc_calc:
            self.crc_ok += 1
        else:
            self.crc_bad += 1
        self._fh.write(
            f"  CRC32   wire=0x{crc_obs:08x}  calc=0x{crc_calc:08x}  [{crc_tag}]\n"
        )
        self._fh.write(
            f"  payload non-IDLE words={len(payload_pairs)} "
            f"(stripped {len(words) - 8 - len(payload_pairs)} mid-pkt IDLE)\n"
        )

        # Framing slots.
        annots[0]   = "SOP K27.7"
        annots[1]   = "TYPE=0x01 (STREAM)"
        annots[2]   = f"STREAM_ID=0x{streamid:02x}"
        annots[3]   = f"PACKET_TAG=0x{pkt_tag:02x}"
        annots[4]   = f"DSIZE_P[15:8]=0x{dsizeP_hi:02x}"
        annots[5]   = f"DSIZE_P[7:0]=0x{dsizeP_lo:02x}"
        annots[N-2] = f"CRC32 (wire BE) wire=0x{crc_obs:08x} [{crc_tag}]"
        annots[N-1] = "EOP K29.7"

        self._walk_stream_payload(words, annots, 6, N - 2)
        return self._fill_blanks(words, annots)

    def _walk_stream_payload(
        self,
        words: List[Tuple[int, int]],
        annots: List[str],
        start: int,
        end: int,
    ) -> None:
        i = start
        while i < end:
            d, k = words[i]

            if classify_wire_beat(d, k) == WireBeatKind.IDLE:
                annots[i] = "IDLE (mid-pkt stretcher)"
                i += 1
                continue

            # Check K28.3 marker BEFORE the pixel-state branch so a
            # marker always cleanly ends the current line — needed when
            # xsize is unknown (header was in a prior packet) and we run
            # the pixel state open-loop until the next marker.
            if self._is_kmark(d, k):
                if i + 1 >= end:
                    annots[i] = "K28.3 marker (truncated)"
                    self._fh.write(f"    [{i:04d}] K28.3 marker (truncated)\n")
                    i += 1
                    continue
                tb, _ = majority_byte(words[i + 1][0])
                if tb == 0x01:
                    consumed = self._handle_rect_hdr(words, annots, i, end)
                    # New frame — reset frame-level state.
                    self._fr_line_idx    = 0
                    self._fr_pxl_idx     = 0
                    self._fr_pixels_left = 0
                    self._fr_in_pixels   = False
                    if i + 8 <= end:
                        b5, _ = majority_byte(words[i + 5][0])
                        b6, _ = majority_byte(words[i + 6][0])
                        b7, _ = majority_byte(words[i + 7][0])
                        self._fr_rect_xsize = _u24(b5, b6, b7)
                    i += consumed
                elif tb == 0x03:
                    consumed = self._handle_arb_hdr(words, annots, i, end)
                    self._fr_line_idx    = 0
                    self._fr_pxl_idx     = 0
                    self._fr_pixels_left = 0
                    self._fr_in_pixels   = False
                    self._fr_rect_xsize  = None
                    i += consumed
                elif tb == 0x02:
                    consumed = self._handle_rect_line(
                        words, annots, i, end, self._fr_line_idx
                    )
                    self._fr_pxl_line  = self._fr_line_idx
                    self._fr_line_idx += 1
                    self._fr_pxl_idx   = 0
                    self._fr_in_pixels = True
                    self._fr_pixels_left = (
                        max(1, (self._fr_rect_xsize + 3) // 4)
                        if self._fr_rect_xsize is not None else 0
                    )
                    i += consumed
                elif tb == 0x04:
                    consumed, xs = self._handle_arb_line(
                        words, annots, i, end, self._fr_line_idx
                    )
                    self._fr_pxl_line  = self._fr_line_idx
                    self._fr_line_idx += 1
                    self._fr_pxl_idx   = 0
                    self._fr_in_pixels = True
                    self._fr_pixels_left = (
                        max(1, (xs + 3) // 4) if xs is not None else 0
                    )
                    i += consumed
                else:
                    annots[i]     = f"K28.3 marker UNKNOWN type=0x{tb:02x}"
                    annots[i + 1] = f"MARKER_TYPE=0x{tb:02x} (UNKNOWN)"
                    self._fh.write(
                        f"    [{i:04d}] K28.3 marker UNKNOWN type=0x{tb:02x}\n"
                    )
                    i += 2
                continue

            # Non-marker payload word.
            if self._fr_in_pixels:
                annots[i] = f"pxl {self._fr_pxl_line} {self._fr_pxl_idx}"
                self._fr_pxl_idx += 1
                if self._fr_pixels_left > 0:
                    self._fr_pixels_left -= 1
                    if self._fr_pixels_left == 0:
                        self._fr_in_pixels = False
            else:
                annots[i] = "PAYLOAD"
            i += 1

    def _handle_rect_hdr(
        self,
        words: List[Tuple[int, int]],
        annots: List[str],
        base: int,
        end: int,
    ) -> int:
        consumed = min(RECT_HDR_WORDS, end - base)
        b = [0]
        annots[base] = self._RECT_HDR_LABELS[0]
        for off in range(1, consumed):
            byte, _ = majority_byte(words[base + off][0])
            b.append(byte)
            label = self._RECT_HDR_LABELS[off]
            annots[base + off] = (
                label if off == 1 else f"{label} = 0x{byte:02x}"
            )

        if consumed >= RECT_HDR_WORDS:
            self._fh.write(
                f"    [{base:04d}] IMAGE HEADER RECT  "
                f"streamid=0x{b[2]:x}  sourcetag=0x{_u16(b[3], b[4]):x}  "
                f"xsize=0x{_u24(b[5], b[6], b[7]):x}  xoffs=0x{_u24(b[8], b[9], b[10]):x}  "
                f"ysize=0x{_u24(b[11], b[12], b[13]):x}  yoffs=0x{_u24(b[14], b[15], b[16]):x}  "
                f"dsizeL=0x{_u24(b[17], b[18], b[19]):x}  pixfmt=0x{_u16(b[20], b[21]):x}  "
                f"tapg=0x{_u16(b[22], b[23]):x}  flags=0x{b[24]:x}\n"
            )
        else:
            self._fh.write(
                f"    [{base:04d}] IMAGE HEADER RECT (truncated: have {consumed}"
                f" of {RECT_HDR_WORDS})\n"
            )
        return consumed

    def _handle_arb_hdr(
        self,
        words: List[Tuple[int, int]],
        annots: List[str],
        base: int,
        end: int,
    ) -> int:
        consumed = min(ARB_HDR_WORDS, end - base)
        b = [0]
        annots[base] = self._ARB_HDR_LABELS[0]
        for off in range(1, consumed):
            byte, _ = majority_byte(words[base + off][0])
            b.append(byte)
            label = self._ARB_HDR_LABELS[off]
            annots[base + off] = (
                label if off == 1 else f"{label} = 0x{byte:02x}"
            )

        if consumed >= ARB_HDR_WORDS:
            self._fh.write(
                f"    [{base:04d}] IMAGE HEADER ARB  "
                f"streamid=0x{b[2]:x}  sourcetag=0x{_u16(b[3], b[4]):x}  "
                f"ysize=0x{_u24(b[5], b[6], b[7]):x}  yoffs=0x{_u24(b[8], b[9], b[10]):x}  "
                f"pixfmt=0x{_u16(b[11], b[12]):x}  tapg=0x{_u16(b[13], b[14]):x}  "
                f"flags=0x{b[15]:x}\n"
            )
        else:
            self._fh.write(
                f"    [{base:04d}] IMAGE HEADER ARB (truncated: have {consumed}"
                f" of {ARB_HDR_WORDS})\n"
            )
        return consumed

    def _handle_rect_line(
        self,
        words: List[Tuple[int, int]],
        annots: List[str],
        base: int,
        end: int,
        line_idx: int,
    ) -> int:
        consumed = min(_RECT_LINE_MARKER_WORDS, end - base)
        annots[base] = self._RECT_LINE_LABELS[0]
        if base + 1 < end:
            annots[base + 1] = self._RECT_LINE_LABELS[1]
        self._fh.write(
            f"    [{base:04d}] LINE MARKER RECT  line={line_idx}\n"
        )
        return consumed

    def _handle_arb_line(
        self,
        words: List[Tuple[int, int]],
        annots: List[str],
        base: int,
        end: int,
        line_idx: int,
    ) -> Tuple[int, Optional[int]]:
        consumed = min(_ARB_LINE_MARKER_WORDS, end - base)
        annots[base] = self._ARB_LINE_LABELS[0]
        b: List[int] = [0]
        for off in range(1, consumed):
            byte, _ = majority_byte(words[base + off][0])
            b.append(byte)
            label = self._ARB_LINE_LABELS[off]
            annots[base + off] = (
                label if off == 1 else f"{label} = 0x{byte:02x}"
            )

        xs: Optional[int] = None
        if consumed >= _ARB_LINE_MARKER_WORDS:
            xs     = _u24(b[2], b[3], b[4])
            xoffs  = _u24(b[5], b[6], b[7])
            dsizeL = _u24(b[8], b[9], b[10])
            self._fh.write(
                f"    [{base:04d}] LINE MARKER ARB  line={line_idx}  "
                f"xsize=0x{xs:x}  xoffs=0x{xoffs:x}  dsizeL=0x{dsizeL:x}\n"
            )
        else:
            self._fh.write(
                f"    [{base:04d}] LINE MARKER ARB (truncated: have {consumed}"
                f" of {_ARB_LINE_MARKER_WORDS})\n"
            )
        return consumed, xs

    # ------------------------------------------------------------------
    # CTRL_ACK (0x03) — Table 22 layout
    # ------------------------------------------------------------------
    def _meta_ctrl_ack(self, words: List[Tuple[int, int]]) -> List[str]:
        # §8.6.3 two ack shapes: immediate (SOP|4×TYPE|4×CODE|EOP, N=4)
        # and data ack (SOP|4×TYPE|4×CODE|Size|data...|CRC|EOP, N>=6).
        N = len(words)
        annots = [""] * N
        if N != 4 and N < 6:
            self._fh.write(f"  CTRL_ACK truncated/malformed ({N} words)\n")
            return self._fill_blanks(words, annots)

        rsp_code, _ = majority_byte(words[2][0])
        annots[0]   = "SOP K27.7"
        annots[1]   = "TYPE=0x03 (CTRL_ACK)"
        annots[2]   = f"RSP_CODE=0x{rsp_code:02x}"
        annots[N-1] = "EOP K29.7"

        if N == 4:
            # Immediate ack — write-OK / reset / error: no Size, no CRC.
            self._fh.write(
                f"  CTRL_ACK  rsp_code=0x{rsp_code:02x}  (immediate, no data)\n"
            )
            return annots

        # Data ack — read-OK (0x00) / Wait (0x04): one Size word holding
        # B (big-endian), data, CRC.
        size_b = byteswap32(words[3][0])
        self._fh.write(
            f"  CTRL_ACK  rsp_code=0x{rsp_code:02x}  size={size_b}B  "
            f"CRC32 wire=0x{words[-2][0]:08x}\n"
        )
        annots[3]   = f"SIZE={size_b} (big-endian)"
        annots[N-2] = f"CRC32 wire=0x{words[-2][0]:08x}"

        for j in range(4, N - 2):
            d, k = words[j]
            if classify_wire_beat(d, k) == WireBeatKind.IDLE:
                annots[j] = "IDLE (mid-pkt stretcher)"
            else:
                annots[j] = f"DATA[{j - 4}] (big-endian)"
        return annots

    # ------------------------------------------------------------------
    # Unknown packet type — just classify each word.
    # ------------------------------------------------------------------
    def _meta_unknown(self, words: List[Tuple[int, int]]) -> List[str]:
        self._fh.write(f"  UNKNOWN type — raw {len(words)} words follow\n")
        return self._fill_blanks(words, [""] * len(words))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _fill_blanks(
        self,
        words: List[Tuple[int, int]],
        annots: List[str],
    ) -> List[str]:
        for j, (d, k) in enumerate(words):
            if annots[j]:
                continue
            kind = classify_wire_beat(d, k)
            if kind == WireBeatKind.IDLE:
                annots[j] = "IDLE"
            else:
                annots[j] = kind.name
        return annots

    def _dump_one(
        self,
        idx: int,
        pair: Tuple[int, int],
        annot: str,
        prefix: str = "  ",
    ) -> None:
        d, k = pair
        # Blank-out kmask field when all-zero so K-coded words stand out.
        kfield = f"kmask=0b{k:04b}" if k else " " * len("kmask=0b0000")
        self._fh.write(
            f"{prefix}[{idx:04d}] data=0x{d:08x} {kfield} <-- {annot}\n"
        )

    @staticmethod
    def _is_kmark(word: int, kmask: int) -> bool:
        return kmask == 0xF and (word & 0xFF) == K28_3

    # ------------------------------------------------------------------
    def report_phase(self):
        if self._fh is not None:
            self._fh.write(
                f"\n# summary  total={self.pkts_dumped}  "
                f"stream={self.stream_pkts}  ctrl_ack={self.ctrl_ack_pkts}  "
                f"crc_ok={self.crc_ok}  crc_bad={self.crc_bad}\n"
            )
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        self.logger.info(
            f"stream_dump: pkts={self.pkts_dumped} "
            f"stream={self.stream_pkts} ctrl_ack={self.ctrl_ack_pkts} "
            f"crc_ok={self.crc_ok} crc_bad={self.crc_bad} -> {self.path}"
        )
