"""cxp_video_agent — drives the external single-pixel ingress port.

Implements proposal §6.1.  Each video_xact represents one frame
(rect or arbitrary).  cxp_app_pixel_ingress now takes one raw pixel per
cycle (spec §2.1) — cxp_app_pixel_packer does the 32-bit packing inside the
DUT — so the driver clocks s_pix_data + s_pix_valid/sof/eol/eof into the
DUT on app_clk, honouring s_pix_ready back-pressure.

For Mono8 the packer's byte-reversed output is bit-identical to the
legacy 4-px/word layout.  The driver therefore expands the golden
32-bit words into single pixels (first pixel in byte[7:0]) and the
monitor reconstructs the very same 32-bit words from accepted pixels —
so the stream_scoreboard's golden contract (a list of 32-bit words) is
unchanged.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from cxp_protocol import stream as gs
from cxp_protocol.quirks import DEVICE
from typing import List

import cocotb
from cocotb.utils import get_sim_time
from cocotb.triggers import NextTimeStep, ReadOnly, RisingEdge

from pyuvm import (
    uvm_agent, uvm_analysis_port, uvm_driver, uvm_monitor, uvm_sequence,
    uvm_sequence_item, uvm_sequencer
)
from uvm.common.handles import get_dut
from uvm.common.seed import rng as seed_rng

from uvm.common.cxp_pkg import RECT_HDR_WORDS  # noqa: F401  (re-exported for tests)


@dataclass
class VideoXact:
    """One frame description (§6.1 random space)."""
    xsize: int = 8
    ysize: int = 4
    xoffs: int = 0
    yoffs: int = 0
    pixfmt: int = 0x0101  # Mono8
    tapg: int = 0
    streamid: int = 1              # Table 37 StreamID (8 bit)
    sourcetag: int = 0             # Table 37 SourceTag (16 bit), per frame
    flags: int = 0
    arbitrary: bool = False
    dval_density: float = 1.0      # per-pixel s_pix_valid density (10..100%)
    pixels: List[int] = field(default_factory=list)  # 32-bit golden words
    # One value per pixel, LSB-justified, for any pixel format; when set it
    # is what the driver sends (`pixels` is then ignored).
    raw_pixels: List[int] = field(default_factory=list)

    def fill_ramp_raw(self, bits: int, seed: int = 0):
        """A ramp of `bits`-bit pixels, different on every line."""
        m = (1 << bits) - 1
        self.raw_pixels = [((seed + 37 * y + 5 * x + (x * y) % 7) * 0x9E3779B1 >> 7) & m
                           for y in range(self.ysize) for x in range(self.xsize)]

    def fill_ramp(self):
        """Default pattern: 8-bit ramp packed 4-px per word."""
        self.pixels = []
        v = 0
        for _ in range(self.xsize * self.ysize // 4 or 1):
            self.pixels.append(v & 0xFFFF_FFFF)
            v = (v + 0x0403_0201) & 0xFFFF_FFFF


class VideoItem(uvm_sequence_item):
    def __init__(self, name="video_item"):
        super().__init__(name)
        self.xact = VideoXact()


@dataclass
class GoldenFrame:
    """Snapshot of one frame as the env presented it to the DUT.

    `pixels` is the list of 32-bit words reconstructed from the single
    pixels accepted on the s_pix_* bus (4 Mono8 px / word, first pixel in
    byte[7:0]) — i.e. the same set of words that should reappear
    (post-header-strip, post-line-marker-strip) in the downlink stream
    packets after cxp_app_pixel_packer.  The metadata fields mirror the
    `ext_meta_*` signals driven by VideoDriver and are decoded out of
    the image header by the stream scoreboard for comparison.
    """
    xsize:     int
    ysize:     int
    xoffs:     int
    yoffs:     int
    pixfmt:    int
    tapg:      int
    streamid:  int
    sourcetag: int
    flags:     int
    pixels:    List[int]
    t_sof:     float = 0.0          # sim time (ns) of the frame's first pixel


class VideoSequencer(uvm_sequencer):
    pass


def _words_to_pixels(words: List[int], n_px: int) -> List[int]:
    """Unpack 32-bit golden words into a flat Mono8 single-pixel stream.

    Layout matches the byte-reversed cxp_app_pixel_packer output: word bits
    [7:0] = pixel 0, [15:8] = pixel 1, ... so the first-transmitted pixel
    is the LSB byte (legacy 4-px/word convention)."""
    px: List[int] = []
    for w in words:
        px += [w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF, (w >> 24) & 0xFF]
    if len(px) < n_px:
        px += [0] * (n_px - len(px))
    return px[:n_px]


class VideoDriver(uvm_driver):
    """Single-pixel stream driver.  Randomly de-asserts s_pix_valid so
    the ingress skid + packer see realistic gaps, and honours
    s_pix_ready back-pressure (no pixel is dropped)."""

    def build_phase(self):
        super().build_phase()
        self.stalls = self.max_stall = 0
        self.stalls_at_eol = self.stalls_at_eof = self.stalls_at_sof = 0

    async def run_phase(self):
        dut = get_dut()
        rng = seed_rng("video_drv")
        while True:
            item = await self.seq_item_port.get_next_item()
            x = item.xact
            # Drive metadata level signals as the env saw them.
            dut.ext_meta_xsize.value     = x.xsize
            dut.ext_meta_ysize.value     = x.ysize
            dut.ext_meta_xoffs.value     = x.xoffs
            dut.ext_meta_yoffs.value     = x.yoffs
            dut.ext_meta_pixfmt.value    = x.pixfmt
            dut.ext_meta_tapg.value      = x.tapg
            dut.ext_meta_streamid.value  = x.streamid
            dut.ext_meta_sourcetag.value = x.sourcetag
            dut.ext_meta_flags.value     = x.flags

            n_px = x.xsize * x.ysize
            if x.raw_pixels:
                flat = list(x.raw_pixels[:n_px]) + [0] * max(0, n_px - len(x.raw_pixels))
            else:
                flat = _words_to_pixels(x.pixels, n_px) if x.pixels else [0] * n_px

            bits = gs.device_bits(x.pixfmt, DEVICE) or 8
            for row in range(x.ysize):
                for col in range(x.xsize):
                    idx = row * x.xsize + col
                    sof = (idx == 0)
                    eol = (col == x.xsize - 1)
                    eof = (eol and row == x.ysize - 1)

                    # Random idle gap before this pixel (s_pix_valid low).
                    while rng.random() > x.dval_density:
                        dut.s_pix_valid.value = 0
                        await RisingEdge(dut.app_clk_in)

                    # A sensor of the frame's format width, MSB-aligned on
                    # the 16-bit port (p_PIX_W = 16): the device sends the
                    # value unchanged when PixelFormat is that format.
                    dut.s_pix_data.value  = flat[idx] << (16 - bits) & 0xFFFF
                    dut.s_pix_valid.value = 1
                    dut.s_pix_sof.value   = int(sof)
                    dut.s_pix_eol.value   = int(eol)
                    dut.s_pix_eof.value   = int(eof)

                    # Hold the beat until cxp_app_pixel_ingress accepts it:
                    # the edge takes it if s_pix_ready is high in the cycle
                    # before (read settled, not after the edge, when it
                    # already shows the next cycle's back-pressure).
                    waited = 0
                    while True:
                        await ReadOnly()
                        taken = int(dut.s_pix_ready.value)
                        await RisingEdge(dut.app_clk_in)
                        if taken:
                            break
                        waited += 1
                    if waited:
                        # Back-pressure (s_pix_ready low), for the tests
                        # that must see it.
                        self.stalls += 1
                        self.max_stall = max(self.max_stall, waited)
                        if eol:
                            self.stalls_at_eol += 1
                        if eof:
                            self.stalls_at_eof += 1
                        if sof:
                            self.stalls_at_sof += 1

            dut.s_pix_valid.value = 0
            dut.s_pix_sof.value   = 0
            dut.s_pix_eol.value   = 0
            dut.s_pix_eof.value   = 0
            await RisingEdge(dut.app_clk_in)
            self.seq_item_port.item_done()


class VideoMonitor(uvm_monitor):
    """Packs the pixels accepted on the s_pix_* bus line by line with the
    golden §9.4.2 packer and publishes one GoldenFrame per frame (frame end
    = accepted beat with s_pix_eof)."""

    def build_phase(self):
        self.ap = uvm_analysis_port("ap", self)

    async def run_phase(self):
        dut = get_dut()
        words: List[int] = []
        line: List[int] = []
        t_sof = 0.0
        while True:
            # The port's settled state in a cycle is what the next edge
            # takes; sleep while nothing is offered.
            await ReadOnly()
            if not int(dut.s_pix_valid.value):
                await RisingEdge(dut.s_pix_valid)
                continue
            hs = int(dut.s_pix_ready.value)
            sof, eol, eof = (int(dut.s_pix_sof.value), int(dut.s_pix_eol.value),
                             int(dut.s_pix_eof.value))
            px = int(dut.s_pix_data.value)
            # The PixelFormat register (a PFNC value, §11.2.1.6) is the
            # format the device packs and announces; the sensor's metadata
            # only while it maps to no PixelF code.
            pixfmt = gs.pfnc_to_pixelf(int(dut.bs_pixel_format.value)) or int(dut.ext_meta_pixfmt.value)
            meta = dict(
                xsize     = int(dut.ext_meta_xsize.value),
                ysize     = int(dut.ext_meta_ysize.value),
                xoffs     = int(dut.ext_meta_xoffs.value),
                yoffs     = int(dut.ext_meta_yoffs.value),
                pixfmt    = pixfmt,
                tapg      = int(dut.ext_meta_tapg.value),
                streamid  = int(dut.ext_meta_streamid.value) & 0xFF,
                sourcetag = int(dut.ext_meta_sourcetag.value),
                flags     = int(dut.ext_meta_flags.value),
            )
            await RisingEdge(dut.app_clk_in)
            if not hs:
                continue
            if sof:
                words, line, t_sof = [], [], get_sim_time("ns")
            bits = gs.device_bits(pixfmt, DEVICE) or 8
            # The port carries 16-bit samples (p_PIX_W = 16); the device
            # MSB-aligns them into the format (§9.4.2, Figure 32).
            line.append(gs.msb_align(px, 16, bits))
            if eol or eof:
                words += gs.pack_line(line, bits)
                line = []
            if eof:
                self.ap.write(GoldenFrame(pixels=list(words), t_sof=t_sof, **meta))
                words = []


class TpgMonitor(uvm_monitor):
    """Publish one GoldenFrame per frame the internal TPG emits.

    With `cfg_use_tpg` = 1 the pixel source is inside the DUT, so the
    s_pix_* bus VideoMonitor watches is idle and the stream scoreboard
    gets no golden at all — it decoded the first frame off the wire and
    then blocked for ever on an empty golden FIFO.  This monitor takes
    the TPG's own output handshake as the golden instead, which makes
    every frame downstream of it (packer, chopper, framer, arbiter,
    wire) comparable, and leaves the pattern itself to the
    cxp_app_tpg unit bench.

    Each line is packed with the golden §9.4.2 packer
    (`cxp_protocol.stream.pack_line`) at the width of the frame's
    PixelFormat; a format the device does not support is counted in
    `skipped_frames` and reported, never guessed.
    """

    # The TPG's metadata is what it latches at frame start (geometry,
    # format, offsets, the SourceTag incremented per image, Table 38) and
    # what it takes live from the registers: StreamID, TapG, Flags.

    def build_phase(self):
        self.ap = uvm_analysis_port("ap", self)
        self.skipped_frames = 0

    async def run_phase(self):
        dut = get_dut()
        tpg = dut.cxp_device_top_i.cxp_interface_top_i.cxp_app_domain_i.cxp_app_tpg_i
        words: List[int] = []
        line: List[int] = []
        saw_sof = False
        xsize = ysize = pixfmt = xoffs = yoffs = srctag = bits = sid = 0
        tapg = flags = 0
        t_sof = 0.0
        while True:
            await ReadOnly()
            if not int(tpg.m_pix_valid_o.value):
                await RisingEdge(tpg.m_pix_valid_o)
                continue
            hs = int(tpg.m_pix_ready_i.value)
            sof, eol, eof = (int(tpg.m_pix_sof_o.value), int(tpg.m_pix_eol_o.value),
                             int(tpg.m_pix_eof_o.value))
            px = int(tpg.m_pix_data_o.value)
            if sof:
                # Geometry and format are latched in the DUT at frame
                # start and hold for the whole frame.
                hdr = (int(tpg.xsize_q.value), int(tpg.ysize_q.value),
                       int(tpg.pixfmt_q.value), int(tpg.xoffs_q.value),
                       int(tpg.yoffs_q.value), int(tpg.srctag_q.value),
                       # TPG images carry Image1StreamID, TapGeometry and
                       # StreamFlags (cxp_device_top).
                       int(dut.bs_stream_id.value) & 0xFF,
                       int(tpg.cfg_tapg_i.value), int(tpg.cfg_flags_i.value))
            await RisingEdge(dut.app_clk_in)
            if not hs:
                continue
            if sof:
                xsize, ysize, pixfmt, xoffs, yoffs, srctag, sid, tapg, flags = hdr
                bits   = gs.device_bits(pixfmt, DEVICE)
                words, line, saw_sof = [], [], True
                t_sof  = get_sim_time("ns")
            if not saw_sof:
                continue                      # frame already in flight at reset
            line.append(px)
            if eol or eof:
                words += gs.pack_line(line, bits or 8)
                line = []
            if eof:
                if bits:
                    self.ap.write(GoldenFrame(
                        xsize     = xsize,
                        ysize     = ysize,
                        xoffs     = xoffs,
                        yoffs     = yoffs,
                        pixfmt    = pixfmt,
                        tapg      = tapg,
                        streamid  = sid,
                        sourcetag = srctag,
                        flags     = flags,
                        pixels    = list(words),
                        t_sof     = t_sof,
                    ))
                else:
                    self.skipped_frames += 1
                words = []
                saw_sof = False

    def report_phase(self):
        if self.skipped_frames:
            self.logger.warning(
                f"TpgMonitor: {self.skipped_frames} TPG frame(s) in a pixel "
                "format the device does not support: not published as golden"
            )


class VideoAgent(uvm_agent):
    def build_phase(self):
        self.seqr    = VideoSequencer("seqr", self)
        self.drv     = VideoDriver("drv", self)
        self.mon     = VideoMonitor("mon", self)
        self.tpg_mon = TpgMonitor("tpg_mon", self)

    def connect_phase(self):
        self.drv.seq_item_port.connect(self.seqr.seq_item_export)


class VideoRandomSeq(uvm_sequence):
    """Random-frame base sequence (§9.1 video_random_seq).

    Image-header metadata knobs (sensor path, `ext_meta_*`):

    * `sourcetag_start` — SourceTag of the first frame.  With
      `sourcetag_auto` each later frame takes the previous one + 1, wrapping
      0xFFFF -> 0x0000 (§9.4.6.1); without it every frame carries the start
      value.
    * `streamid` — the StreamID every frame carries (default 1, fixed).
      With `streamid_random` each frame draws one from 0..255 instead, from
      an RNG of its own so the geometry stream below is unchanged.
    """

    def __init__(self, name="video_random_seq", n_frames=1, rng_seed=0xCAFE,
                 sourcetag_start=0, sourcetag_auto=False,
                 streamid=1, streamid_random=False):
        super().__init__(name)
        self.n_frames = n_frames
        self.rng = seed_rng("video_random", rng_seed)
        self.sourcetag_start = sourcetag_start & 0xFFFF
        self.sourcetag_auto = sourcetag_auto
        self.streamid = streamid & 0xFF
        self.streamid_rng = (seed_rng("video_streamid", rng_seed)
                             if streamid_random else None)

    def _meta(self, x: VideoXact, i: int) -> None:
        x.sourcetag = ((self.sourcetag_start + i) if self.sourcetag_auto
                       else self.sourcetag_start) & 0xFFFF
        x.streamid = (self.streamid_rng.randrange(256) if self.streamid_rng
                      else self.streamid)

    async def body(self):
        for i in range(self.n_frames):
            item = VideoItem("v")
            await self.start_item(item)
            x = item.xact
            x.xsize = self.rng.choice([4, 8, 12, 16, 32, 64, 128])
            x.ysize = self.rng.choice([2, 4, 8, 16, 32])
            # Mono8 only.  With cxp_app_pixel_packer now in the datapath the
            # pixfmt is honoured for real (2 B/px for Mono16, etc.), but
            # the env's golden model — fill_ramp + the Mono8 4-px/word
            # stream-scoreboard reassembler — only validates Mono8.  Wider
            # formats are exercised by the unit-level cxp_app_pixel_packer TB.
            # Still draw (and discard) the pixfmt choice so the RNG stream
            # — and hence every test's deterministic geometry — is
            # unchanged from before the packer integration.
            self.rng.choice([0x0101, 0x0102, 0x0105, 0x0106, 0x0108])
            x.pixfmt = 0x0101
            x.arbitrary = self.rng.random() < 0.25
            x.dval_density = self.rng.uniform(0.5, 1.0)
            x.fill_ramp()
            self._meta(x, i)
            await self.finish_item(item)
