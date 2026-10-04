"""stream_scoreboard — frame reconstruction & type-0x01 packet checking.

Implements proposal §7 row 1 plus the activation matrix entries for the
stream tests.  Two analysis exports:

* `video_in`  — GoldenFrame objects from cxp_video_agent's monitor.
* `wire_pkts` — reassembled packets from cxp_tx_wire_agent.

For every STREAM (0x01) packet we
  1. cross-check the DsizeP header bytes against the observed non-IDLE
     data-word count and the configured cfg_dsizeP (from ConfigDB);
  2. verify CRC32 over the framed packet (header bytes 2..5 + non-idle
     data words) per table 18 §6.5.1;
  3. push the (idle-stripped) payload into a per-token queue feeding a
     coroutine-based frame reassembler.

The reassembler hunts for K28.3 image-header / line-marker words, decodes
their 4×replicated header bytes (3-of-4 majority vote), and accumulates
one 32-bit pixel word per beat between line markers.  When a frame is
complete we pop the next GoldenFrame from video_fifo and compare both
the decoded header fields and the pixel words.  Each mismatch is
reported under its own error kind (``header_field``, ``pixel_data``,
``lost_frame``, ...) so a test's tag can name one without tolerating
the rest.

Golden frames come from whichever pixel source the test selected: the
s_pix_* VideoMonitor for cfg_use_tpg = 0, the TpgMonitor on the internal
generator's output for cfg_use_tpg = 1.  The pop is non-blocking — a
frame arriving with no golden behind it is counted and reported, never
waited for — and a golden frame that never comes back off the wire is an
error, not a warning.

PacketTag continuity (§8.5.3) is checked across every stream packet:
+1 mod 256, back to 0 after the §10.3.28 reset window opens.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import cocotb
from cocotb.triggers import Edge, RisingEdge
from cocotb.utils import get_sim_time

from cxp_protocol import stream as gs
from cxp_protocol.quirks import DEVICE
from cocotb.queue import Queue
from pyuvm import ConfigDB, uvm_tlm_analysis_fifo

from uvm.common.handles import get_dut
from uvm.scoreboards.sb_base import CxpScoreboard

from uvm.common.cxp_pkg import (
    K28_3, PacketType, RECT_HDR_WORDS, ARB_HDR_WORDS, WireBeatKind,
    CFG_DSIZE_P_KEY, DEFAULT_PKT_DSIZE_P, STREAM_PKT_OVERHEAD_WORDS,
    classify_wire_beat, crc32_words, majority_byte,
)


# Rect line marker is 2 words on the wire (K28.3 + 4×0x02) per
# cxp_app_line_marker.sv (intentionally diverges from the
# cxp_pkg.RECT_LINE_WORDS=9 spec-MD constant — see CLAUDE.md).
_RECT_LINE_MARKER_WORDS = 2
_ARB_LINE_MARKER_WORDS  = 11


def _u16(msb: int, lsb: int) -> int:
    return ((msb & 0xFF) << 8) | (lsb & 0xFF)


def _u24(msb: int, mid: int, lsb: int) -> int:
    return ((msb & 0xFF) << 16) | ((mid & 0xFF) << 8) | (lsb & 0xFF)


def _decode_rect_image_header(b: List[int]) -> Dict[str, int]:
    # Layout: cxp_app_image_header.sv:151-176 (spec table 37).
    return dict(
        arbitrary = False,
        streamid  = b[2],
        sourcetag = _u16(b[3], b[4]),
        xsize     = _u24(b[5], b[6], b[7]),
        xoffs     = _u24(b[8], b[9], b[10]),
        ysize     = _u24(b[11], b[12], b[13]),
        yoffs     = _u24(b[14], b[15], b[16]),
        dsizeL    = _u24(b[17], b[18], b[19]),  # 32-bit words per line
        pixfmt    = _u16(b[20], b[21]),
        tapg      = _u16(b[22], b[23]),
        flags     = b[24],
    )


def _decode_arb_image_header(b: List[int]) -> Dict[str, int]:
    # Layout: cxp_app_image_header.sv:180-197 (spec table 39).
    # xsize / xoffs / dsizeL live in each line marker, not here.
    return dict(
        arbitrary = True,
        streamid  = b[2],
        sourcetag = _u16(b[3], b[4]),
        ysize     = _u24(b[5], b[6], b[7]),
        yoffs     = _u24(b[8], b[9], b[10]),
        pixfmt    = _u16(b[11], b[12]),
        tapg      = _u16(b[13], b[14]),
        flags     = b[15],
        xsize     = 0,
        xoffs     = 0,
        dsizeL    = 0,
    )


def _decode_arb_line_marker(b: List[int]) -> Tuple[int, int, int]:
    # cxp_app_line_marker.sv:82-92 (spec table 40).  b[0]=K28.3, b[1]=0x04.
    xsize  = _u24(b[2], b[3], b[4])
    xoffs  = _u24(b[5], b[6], b[7])
    dsizeL = _u24(b[8], b[9], b[10])   # 32-bit words per line
    return xsize, xoffs, dsizeL


class _Cut(Exception):
    """The image being decoded stopped short on the wire."""


class StreamScoreboard(CxpScoreboard):
    """Frame reconstruction + CRC checker for downlink stream packets."""

    def build_phase(self):
        super().build_phase()
        self.video_fifo = uvm_tlm_analysis_fifo("video_fifo", self)
        self.wire_fifo  = uvm_tlm_analysis_fifo("wire_fifo",  self)
        self.sb_fifo    = uvm_tlm_analysis_fifo("sb_fifo",    self)
        self.video_xp   = self.video_fifo.analysis_export
        self.wire_xp    = self.wire_fifo.analysis_export
        self.sideband_xp = self.sb_fifo.analysis_export

        self.crc_checked = 0
        self.frames_seen = 0
        self.frames_unmatched = 0
        self.frames_flushed = 0
        # Every stream flush the device performs, [start, end] in ns (end
        # None while it lasts): a ConnectionReset window or TestMode.  An
        # image begun before a flush ends may never reach the wire — the
        # device drops it whole, and keeps dropping until an image starts
        # after the flush.
        self._flushes: list[list] = []

        # §8.5.3 PacketTag: one counter per stream, +1 mod 256 per packet,
        # back to 0 after a ConnectionReset / ConnectionConfig write.
        # `None` = no packet seen yet, so the next tag is unconstrained.
        self._tag_expect: int | None = None
        # Every PacketTag seen, in order (tests look at where they restart).
        self.tags: list = []
        self.frame_cov: list = []       # (pixfmt, xsize, arbitrary, tpg)
        self.pkt_cov: list = []         # (payload words, DsizeP in force)
        self.tag_events: list = []      # "wrap", "reset"
        # Start times of reset windows not yet applied to a packet: the
        # first packet whose SOP is later must carry tag 0; a packet that
        # was already on the wire keeps its running tag.
        self._tag_resets: list[float] = []

        # Configured stream-packet payload size in 32-bit words (cfg_dsizeP).
        # The DUT's chopper packetises the merged stream into exactly this
        # many words per packet, except the trailing packet of a frame which
        # may close short on EOP.  Read once at build time — the test sets
        # this via ConfigDB before env build_phase.
        self.cfg_dsizeP = int(ConfigDB().get(
            self, "", CFG_DSIZE_P_KEY, default=DEFAULT_PKT_DSIZE_P
        ))

        # Token queue between the wire-packet drain (run_phase) and the
        # frame reassembler coroutine.  Holds (data, kmask, StreamID of
        # the packet) in arrival order; the reassembler consumes them
        # one-by-one.
        self._tokens: Queue = Queue()
        self._tok_sid = 0
        self._tok_t = 0.0
        self._img_t = 0.0
        self._pushback = None
        self._resume_type = None
        self.frames_torn = 0
        self.conn_cfg_writes = 0

        # Histories of what gates a stream packet, [(t_ns, value)]:
        # StreamPacketSizeMax, TestMode and the ConnectionReset bit.
        self._spsm: list = [(0.0, 0)]
        self._stream_payload_out: list = []
        self._stream_reset_ns = 0.0
        self._tmode: list = [(0.0, 0)]
        self._crst: list = [(0.0, 0)]
        self.max_pkt_bytes = 0

    def start_of_simulation_phase(self):
        self.logger.warning(
            f"StreamScoreboard configured: cfg_dsizeP={self.cfg_dsizeP} "
            f"(default={DEFAULT_PKT_DSIZE_P}) "
            f"rect_hdr_words={RECT_HDR_WORDS} arb_hdr_words={ARB_HDR_WORDS} "
            f"rect_line_marker_words={_RECT_LINE_MARKER_WORDS} "
            f"arb_line_marker_words={_ARB_LINE_MARKER_WORDS}"
        )

    async def _reset_loop(self):
        """Record the start of every §10.3.28 window.

        A ConnectionReset (or a ConnectionConfig write) restarts stream
        control, and `sb_link_reset_active` is the window it opens.  The
        first stream packet whose SOP follows the window start must carry
        PacketTag 0 (§8.5.3); `_check_tag` applies it.
        """
        prev = 0
        while True:
            evt = await self.sb_fifo.get()
            active = int(getattr(evt, "sb_link_reset_active", 0))
            if active != prev:
                self._crst.append((get_sim_time("ns"), active))
            if active and not prev:
                self._tag_resets.append(get_sim_time("ns"))
                self._flushes.append([get_sim_time("ns"), None])
            if prev and not active and self._flushes:
                self._flushes[-1][1] = get_sim_time("ns")
            prev = active

    async def _spsm_loop(self):
        """StreamPacketSizeMax as the register holds it (§10.3.32)."""
        dut = get_dut()
        while True:
            await Edge(dut.bs_stream_pkt_dsize)
            self._spsm.append((get_sim_time("ns"), int(dut.bs_stream_pkt_dsize.value)))

    async def _conn_cfg_loop(self):
        """A ConnectionConfig write restarts the PacketTag (§8.5.3) and
        flushes the stream path like a ConnectionReset."""
        dut = get_dut()
        while True:
            await RisingEdge(dut.bs_conn_cfg_wr)
            self.conn_cfg_writes += 1
            t = get_sim_time("ns")
            self._tag_resets.append(t)
            self._flushes.append([t, t])

    async def _test_mode_loop(self):
        """TestMode switched on flushes the stream path too (§8.7.4)."""
        dut = get_dut()
        prev = 0
        while True:
            await Edge(dut.bs_test_mode)
            tm = int(dut.bs_test_mode.value)
            self._tmode.append((get_sim_time("ns"), tm))
            if tm and not prev:
                self._flushes.append([get_sim_time("ns"), None])
            if prev and not tm and self._flushes:
                self._flushes[-1][1] = get_sim_time("ns")
            prev = tm

    def _check_tag(self, tag_word: int, sop_ns: float):
        tag, ok = majority_byte(tag_word)
        if not ok:
            self.err(
                "tag_vote",
                f"stream PacketTag replica majority failed: 0x{tag_word:08x}"
            )
            return
        self.tags.append(tag)
        if self._tag_resets and sop_ns > self._tag_resets[0]:
            while self._tag_resets and sop_ns > self._tag_resets[0]:
                self._tag_resets.pop(0)
            self._tag_expect = 0
            self.tag_events.append("reset")
        elif self._tag_expect == 0 and tag == 0:
            self.tag_events.append("wrap")
        if self._tag_expect is not None and tag != self._tag_expect:
            self.err(
                "tag_continuity",
                f"stream PacketTag {tag} breaks continuity, expected "
                f"{self._tag_expect} (§8.5.3)"
            )
        self._tag_expect = (tag + 1) & 0xFF

    async def run_phase(self):
        cocotb.start_soon(self._frame_loop())
        cocotb.start_soon(self._reset_loop())
        cocotb.start_soon(self._test_mode_loop())
        cocotb.start_soon(self._spsm_loop())
        cocotb.start_soon(self._conn_cfg_loop())

        # Per cxp_tx_stream_pkt.sv §6.5.1 / table 18 the on-wire packet
        # layout is:
        #   words[ 0]    SOP K27.7
        #   words[ 1]    4×PacketType(0x01)         (NOT in CRC)
        #   words[ 2]    4×StreamID                ┐
        #   words[ 3]    4×PacketTag               │  CRC32 coverage:
        #   words[ 4]    4×DsizeP[15:8]            │   words[2..5] + the
        #   words[ 5]    4×DsizeP[7:0]             │   non-IDLE data words
        #   words[ 6..]  payload (data words +     │   (image-header K28.3
        #                K28.3 image-header        │    markers ARE in the
        #                markers + line markers    │    CRC; arbiter-injected
        #                + possibly arbiter-       │    mid-packet IDLE
        #                injected IDLE stretchers) │    words are NOT).
        #   words[-2]    CRC32 (single value,      ┘
        #                BIG-ENDIAN, NOT 4×)
        #   words[-1]    EOP K29.7
        while True:
            pkt = await self.wire_fifo.get()
            if pkt.type_byte != PacketType.STREAM:
                continue
            words = pkt.words
            if len(words) < STREAM_PKT_OVERHEAD_WORDS:
                self.err(
                    "pkt_short",f"stream packet too short: {len(words)} words")
                continue

            # Adaptive packet-size check against ConfigDB cfg_dsizeP.
            # The tx_arbiter (§6.2.5) may inject IDLE words mid-packet for
            # the 100-word cadence rule.  Those sit inside SOP..EOP but are
            # NOT in the framed packet (not CRC'd, not counted against
            # DsizeP), so strip them before comparing.
            #
            # Invariants per CXP table 18 + cxp_tx_stream_pkt.sv:
            #   - DsizeP header (bytes 4,5) must equal the non-idle data
            #     word count (header is self-consistent with what was framed).
            #   - data word count must be <= cfg_dsizeP (chopper never
            #     exceeds it; trailing packet of a frame may close short
            #     on EOP).
            payload_words = [
                (d, k) for (d, k) in words[6:-2]
                if classify_wire_beat(d, k) != WireBeatKind.IDLE
            ]
            data_words_count = len(payload_words)
            dsize_msb, ok_msb = majority_byte(words[4][0])
            dsize_lsb, ok_lsb = majority_byte(words[5][0])
            if not (ok_msb and ok_lsb):
                self.err(
                    "dsizep_vote",
                    f"stream DsizeP replica majority failed: "
                    f"hi=0x{words[4][0]:08x} lo=0x{words[5][0]:08x}"
                )
            # Table 19: type, StreamID, PacketTag and DsizeP are each one
            # byte sent four times; on a clean link all four are equal.
            for i, what in ((1, "type"), (2, "StreamID"), (3, "PacketTag"),
                            (4, "DsizeP[15:8]"), (5, "DsizeP[7:0]")):
                w, k = words[i]
                if k != 0 or len({(w >> (8 * j)) & 0xFF for j in range(4)}) != 1:
                    self.err("replica",
                             f"stream packet {what} word 0x{w:08x} (kmask {k:x}) "
                             "is not one byte four times")
            sid, _ = majority_byte(words[2][0])
            self._check_tag(words[3][0], pkt.sop_ns)
            self._check_gates(pkt, len(words))
            self._stream_payload_out.append((pkt.eop_ns, data_words_count))
            payload_wire = (dsize_msb << 8) | dsize_lsb
            spsm_now = self._value_at(self._spsm, pkt.sop_ns)
            self.pkt_cov.append((data_words_count, max(1, spsm_now // 4 - 8)))
            if payload_wire != data_words_count:
                self.err(
                    "dsizep_mismatch",
                    f"stream DsizeP header={payload_wire} disagrees with "
                    f"observed data words={data_words_count} "
                    f"(wire len={len(words)})"
                )

            # Table 19: the CRC covers the non-IDLE stream data words only
            # (not StreamID / PacketTag / DsizeP); the arbiter's
            # mid-packet IDLE stretchers are not part of the framed packet.
            covered = [d for (d, _k) in payload_words]
            crc_word = words[-2][0]
            crc_calc = crc32_words(covered)
            # The wire word is the CRC register itself (§8.2.2.2).
            crc_observed = crc_word & 0xFFFF_FFFF
            if crc_observed != crc_calc:
                self.err(
                    "crc",
                    f"stream CRC mismatch: got 0x{crc_observed:08x} "
                    f"want 0x{crc_calc:08x}"
                )
            self.crc_checked += 1

            # Push the payload (idle-stripped) onto the reassembler queue.
            for d, k in payload_words:
                self._tokens.put_nowait((d, k, sid, pkt.sop_ns))

    # ------------------------------------------------------------------
    # Frame reassembler
    # ------------------------------------------------------------------
    async def _frame_loop(self):
        while True:
            try:
                await self._decode_one_frame()
            except _Cut as c:
                self._torn(str(c))

    async def _next(self) -> Tuple[int, int]:
        if self._pushback is not None:
            d, k = self._pushback
            self._pushback = None
            return d, k
        d, k, self._tok_sid, self._tok_t = await self._tokens.get()
        return d, k

    def _torn(self, what: str) -> None:
        """An image stopped short on the wire.  Inside a flush (TestMode,
        ConnectionReset, ConnectionConfig, a device reset) that is how the
        device ends an image it has begun; anywhere else it is an error."""
        t0 = self._img_t - self._GATE_MARGIN_NS
        t1 = self._tok_t + self._GATE_MARGIN_NS
        if any(start <= t1 and (end is None or end >= t0)
               for start, end in self._flushes):
            self.frames_torn += 1
            self.logger.info(f"image cut by a flush: {what}")
        else:
            self.err("framing", what)

    # A stream packet may not start this long after the register or bit
    # that forbids it changed: the rx -> tx crossing and a word or two.
    _GATE_MARGIN_NS = 400.0

    @staticmethod
    def _value_at(hist: list, t: float):
        v = hist[0][1]
        for ht, hv in hist:
            if ht > t:
                break
            v = hv
        return v

    def _held(self, hist: list, t: float, pred) -> bool:
        """`pred` held on the history for the whole margin before `t`."""
        t0 = t - self._GATE_MARGIN_NS
        if not pred(self._value_at(hist, t0)):
            return False
        return all(pred(v) for ht, v in hist if t0 < ht <= t)

    def _check_gates(self, pkt, n_words: int) -> None:
        """Table 44 / §10.3.32 / §8.7.4 / §10.3.28 at a packet's SOP."""
        t = pkt.sop_ns
        from uvm.common.decisions import D4_SPSM_MIN_BYTES
        if self._held(self._spsm, t, lambda v: v < D4_SPSM_MIN_BYTES):
            self.err("sop_spsm_off",
                     f"stream packet at {t:.0f} ns while StreamPacketSizeMax "
                     f"= {self._value_at(self._spsm, t)} (Table 44: none below "
                     f"{D4_SPSM_MIN_BYTES})")
        if self._held(self._tmode, t, lambda v: v == 1):
            self.err("sop_test_mode",
                     f"stream packet started at {t:.0f} ns with TestMode = 1 "
                     "(§8.7.4)")
        if self._held(self._crst, t, lambda v: v == 1):
            self.err("sop_reset_window",
                     f"stream packet started at {t:.0f} ns inside a "
                     "ConnectionReset (§10.3.28)")
        # §8.5.2 / §10.3.32: the whole packet, K27.7 to K29.7, in bytes.
        nbytes = 4 * n_words
        self.max_pkt_bytes = max(self.max_pkt_bytes, nbytes)
        # Images already chopped before a smaller value reached the app
        # domain can remain in the FIFO.  Count payload words actually
        # transmitted since each change; a wall-clock drain estimate is
        # wrong when control traffic or short packets stall the stream.
        from uvm.common import build
        hist = [(ht, v) for ht, v in self._spsm
                if self._stream_reset_ns <= ht <= t]
        limit = hist[-1][1]
        for i in range(len(hist) - 1):
            changed_at = hist[i + 1][0] - self._GATE_MARGIN_NS
            drained = sum(n for end, n in self._stream_payload_out
                          if changed_at <= end < t)
            # A packet can already be in the transmit pipeline when the
            # register changes, in addition to the words stored in FIFO.
            in_flight = max(32, (hist[i][1] + 3) // 4)
            if drained < build.FIFO_DEPTH + in_flight:
                limit = max(limit, hist[i][1])
        if nbytes > limit:
            hist = ", ".join(f"{v}@{ht:.0f}" for ht, v in self._spsm[-3:])
            self.err("pkt_over_spsm",
                     f"stream packet of {nbytes} bytes at {t:.0f} ns, "
                     f"StreamPacketSizeMax = {limit} (last writes {hist})")

    async def _next_majority(self) -> int:
        word, km = await self._next()
        if km:
            # A marker where a header byte is due: the image was cut.
            self._pushback = (word, km)
            raise _Cut(f"marker 0x{word:08x} inside an image header or line marker")
        byte, ok = majority_byte(word)
        if not ok:
            self.err(
                "vote",
                f"replica majority vote failed on word 0x{word:08x}"
            )
        elif km != 0 or len({(word >> (8 * j)) & 0xFF for j in range(4)}) != 1:
            self.err("replica",
                     f"marker byte word 0x{word:08x} (kmask {km:x}) is not "
                     "one byte four times")
        return byte

    def _is_kmark(self, word: int, kmask: int) -> bool:
        """§9.4.4: a marker is K28.3 in all four lanes."""
        lanes_k3 = [((word >> (8 * i)) & 0xFF) == K28_3 and (kmask >> i) & 1
                    for i in range(4)]
        if any(lanes_k3) and not all(lanes_k3):
            self.err("kmark_partial",
                     f"K28.3 in some lanes only: 0x{word:08x} kmask {kmask:x}")
        return all(lanes_k3)

    async def _decode_one_frame(self):
        # 1. Hunt for the next image-header K28.3 marker (or resume on one
        # a cut image's line ran into).
        resume, self._resume_type = self._resume_type, None
        while resume is None:
            word, km = await self._next()
            if self._is_kmark(word, km):
                break
            self._torn(f"unexpected non-marker word before image header: "
                       f"data=0x{word:08x} km=0x{km:x}")

        pkt_sid = self._tok_sid
        self._img_t = self._tok_t
        # 2. Header type byte (idx 1) selects rect / arb.
        type_byte = resume if resume is not None else await self._next_majority()
        if type_byte == 0x01:
            hdr_total = RECT_HDR_WORDS
            decoder   = _decode_rect_image_header
        elif type_byte == 0x03:
            hdr_total = ARB_HDR_WORDS
            decoder   = _decode_arb_image_header
        else:
            self.err(
                "framing",
                f"image header type byte 0x{type_byte:02x} not in {{0x01,0x03}}"
            )
            return

        # 3. Read the remaining header bytes (one per word).  Index 0
        # was the K28.3 marker itself — keep a placeholder so the table
        # lookups below match the spec's word indices 1:1.
        header_bytes: List[int] = [0, type_byte]
        for _ in range(hdr_total - 2):
            header_bytes.append(await self._next_majority())
        fields = decoder(header_bytes)
        if fields["streamid"] != pkt_sid:
            self.err("streamid",
                     f"image header StreamID {fields['streamid']}, the stream "
                     f"packet carrying it says {pkt_sid} (Table 19 / 37)")
        bits_h = gs.device_bits(fields.get("pixfmt", 0), DEVICE) or 8
        if not fields["arbitrary"] and fields["dsizeL"] != gs.dsizel(
                fields["xsize"], bits_h, DEVICE):
            self.err("dsizel",
                     f"image header DsizeL {fields['dsizeL']}, a line of "
                     f"{fields['xsize']} pixels at {bits_h} bits is "
                     f"{gs.dsizel(fields['xsize'], bits_h, DEVICE)} words (§9.4.6)")

        # 4. For each line: K28.3 line marker + the line's pixel words.
        pixels: List[int] = []
        for row in range(fields["ysize"]):
            word, km = await self._next()
            if not self._is_kmark(word, km):
                self._torn(f"frame#{self.frames_seen + 1} row {row}: expected "
                           f"line-marker K28.3, got data=0x{word:08x} km=0x{km:x}")
                return

            line_type = await self._next_majority()
            if line_type == 0x02:
                # Rect line marker: K28.3 + one 0x02 type byte = 2 words.
                line_xsize = fields["xsize"]
            elif line_type == 0x04:
                # Arb line marker: K28.3 + type + xsize(3) + xoffs(3) +
                # dsizeL(3) = 11 words.  Read the remaining 9 byte slots.
                arb_bytes: List[int] = [0, line_type]
                for _ in range(_ARB_LINE_MARKER_WORDS - 2):
                    arb_bytes.append(await self._next_majority())
                line_xsize, line_xoffs, line_dsizeL = _decode_arb_line_marker(arb_bytes)
                bits_l = gs.device_bits(fields.get("pixfmt", 0), DEVICE) or 8
                if line_dsizeL != gs.dsizel(line_xsize, bits_l, DEVICE):
                    self.err("dsizel",
                             f"line marker DsizeL {line_dsizeL}, a line of "
                             f"{line_xsize} pixels at {bits_l} bits is "
                             f"{gs.dsizel(line_xsize, bits_l, DEVICE)} words")
                if row == 0:
                    # First line marker is the authoritative xsize/xoffs/
                    # dsizeL for arbitrary images — image header omits them.
                    fields["xsize"]  = line_xsize
                    fields["xoffs"]  = line_xoffs
                    fields["dsizeL"] = line_dsizeL
                elif (line_xsize  != fields["xsize"]
                      or line_xoffs  != fields["xoffs"]
                      or line_dsizeL != fields["dsizeL"]):
                    self.err(
                        "line_marker",
                        f"frame#{self.frames_seen + 1} row {row}: arb "
                        f"line-marker mismatch vs row 0 (xsize/xoffs/dsizeL = "
                        f"{line_xsize}/{line_xoffs}/{line_dsizeL} vs "
                        f"{fields['xsize']}/{fields['xoffs']}/{fields['dsizeL']})"
                    )
            elif line_type in (0x01, 0x03):
                # An image header where a line marker is due: the image
                # was cut and the next one begins here.
                self._resume_type = line_type
                self._torn(f"frame#{self.frames_seen + 1} row {row}: a new "
                           f"image header in place of a line marker")
                return
            else:
                self.err(
                    "framing",
                    f"unexpected line-marker type byte 0x{line_type:02x}"
                )
                return

            # §9.4.2: a line is packed at the width of the image's
            # PixelFormat and padded to a whole word.
            bits = gs.device_bits(fields.get("pixfmt", 0), DEVICE) or 8
            line_words = max(1, gs.line_words(line_xsize, bits))
            for _ in range(line_words):
                w, km = await self._next()
                if km:
                    # A marker where pixel data is due: this image was cut;
                    # the marker belongs to what follows.
                    self._pushback = (w, km)
                    self._torn(f"frame#{self.frames_seen + 1} row {row}: marker "
                               f"0x{w:08x} inside the line data")
                    return
                pixels.append(w)

        self.frames_seen += 1
        self.frame_cov.append((fields.get("pixfmt", 0), fields.get("xsize", 0),
                               bool(fields.get("arbitrary")),
                               int(get_dut().cfg_use_tpg.value)))
        await self._compare_with_golden(fields, pixels)

    # ------------------------------------------------------------------
    # Golden compare
    # ------------------------------------------------------------------
    async def _compare_with_golden(self, fields: Dict[str, int],
                                   pixels: List[int]):
        # Never block: a frame on the wire with no golden behind it used
        # to wedge this coroutine for the rest of the test, so every
        # later frame went unchecked.  Take what is there, count what is
        # not, and keep decoding.
        ok, golden = self.video_fifo.try_get()
        # A golden image begun before a flush, or while the stream was held
        # (StreamPacketSizeMax below one packet), that is not the one on the
        # wire was dropped whole by the device: skip it, and only it.
        while (ok and not self._matches(golden, fields, pixels)
               and self._flushed(golden.t_sof)):
            self.frames_flushed += 1
            ok, golden = self.video_fifo.try_get()
        if not ok:
            self.frames_unmatched += 1
            self.logger.warning(
                f"frame#{self.frames_seen} reassembled off the wire with no "
                "golden frame queued — not compared"
            )
            return
        frame_id = self.frames_seen

        for fld in ("xsize", "ysize", "xoffs", "yoffs",
                    "pixfmt", "tapg", "streamid", "sourcetag", "flags"):
            actual   = fields.get(fld, 0)
            expected = getattr(golden, fld)
            if actual != expected:
                self.err(
                    "header_field",
                    f"frame#{frame_id} {fld}: wire=0x{actual:x} "
                    f"golden=0x{expected:x}"
                )

        if len(pixels) != len(golden.pixels):
            self.err(
                "pixel_count",
                f"frame#{frame_id} pixel-word count: wire={len(pixels)} "
                f"golden={len(golden.pixels)}"
            )

        for i, (got, want) in enumerate(zip(pixels, golden.pixels)):
            if got != want:
                self.err(
                    "pixel_data",
                    f"frame#{frame_id} pixel[{i}]: wire=0x{got:08x} "
                    f"golden=0x{want:08x}"
                )
                break  # first mismatch is enough — don't spam the log

    # The flush reaches the pixel clock a few cycles after it is released.
    _FLUSH_MARGIN_NS = 2000.0

    def _flushed(self, t_sof: float) -> bool:
        """An image that began at `t_sof` may have been dropped: it started
        before a flush ended (or one is still on), or while
        StreamPacketSizeMax held the stream (the acquisition gate takes the
        pixels and drops the image, decision D4)."""
        from uvm.common.decisions import D4_SPSM_MIN_BYTES
        if any(t_sof < (end if end is not None else float("inf")) + self._FLUSH_MARGIN_NS
               for _start, end in self._flushes):
            return True
        return (self._value_at(self._spsm, t_sof) < D4_SPSM_MIN_BYTES or
                self._value_at(self._spsm, t_sof - self._FLUSH_MARGIN_NS)
                < D4_SPSM_MIN_BYTES)

    @staticmethod
    def _matches(golden, fields, pixels) -> bool:
        return (golden.sourcetag == fields.get("sourcetag", 0)
                and golden.xsize == fields.get("xsize", 0)
                and golden.ysize == fields.get("ysize", 0)
                and list(golden.pixels[:4]) == list(pixels[:4]))

    def flush_window(self, t0: float, t1: float | None = None) -> None:
        """A stretch in which the test changes the image source or format
        under a running image: an image begun before it ends may be dropped
        whole (the one on the wire says which)."""
        self._flushes.append([t0, t1])

    def close_flush_window(self) -> None:
        for w in self._flushes:
            if w[1] is None:
                w[1] = get_sim_time("ns")

    def device_reset(self) -> None:
        """The device was reset: the images in flight are gone, the tags
        start again at 0 (and StreamPacketSizeMax at 0)."""
        t = get_sim_time("ns")
        self._tag_resets.append(t)
        self._flushes.append([t, t])
        self._spsm.append((t, 0))
        self._stream_reset_ns = t
        self._stream_payload_out.clear()

    def pending_count(self) -> int:
        """Golden frames the wire still owes."""
        return self.video_fifo.used()

    # ------------------------------------------------------------------
    def _final_check(self):
        # A frame the env presented to the DUT and never got back off the
        # wire is a lost image, whether or not other packets made it —
        # "the wire was busy" is not an excuse for dropping one.
        left = []
        while True:
            ok, frame = self.video_fifo.try_get()
            if not ok:
                break
            # An image begun before a flush ended may have been dropped
            # whole by it (and nothing followed to show it on the wire).
            if self._flushed(frame.t_sof):
                self.frames_flushed += 1
                continue
            left.append(frame)
        if left:
            self.err(
                "lost_frame",
                f"{len(left)} frame(s) sent to the DUT never came back off "
                f"the wire ({self.crc_checked} stream packets observed, "
                f"{self.frames_seen} frames reassembled); first: SourceTag "
                f"0x{left[0].sourcetag:04x}, {left[0].xsize}x{left[0].ysize}, "
                f"started at {left[0].t_sof:.0f} ns"
            )
        if self.frames_unmatched:
            self.err(
                "unmatched_frame",
                f"{self.frames_unmatched} frame(s) reassembled off the wire "
                "with no golden frame to compare against"
            )

    def report_phase(self):
        self.logger.info(
            f"stream_scoreboard: {self.frames_seen} frames, "
            f"{self.crc_checked} packets (largest {self.max_pkt_bytes} bytes), "
            f"{self.frames_torn} cut by a flush, "
            f"{self.frames_unmatched} uncompared, {self.frames_flushed} dropped by a "
            f"flush, {self.err_summary()}"
        )
