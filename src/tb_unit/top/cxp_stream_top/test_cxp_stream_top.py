"""Cocotb integration TB for `cxp_stream_top`.

Exercises the integrated CoaXPress device-side stream pipeline:

    pixel source → cxp_app_image_header, cxp_app_line_marker
                 → priority merger (+ skid register) + DsizeP chopper
                 → cxp_cdc_stream_fifo (CDC)
                 → cxp_tx_stream_pkt
                 → CXP type-0x01 stream packet on the wire

The wrapper offers two pixel sources: `pix_sel = 0` routes a local 8×4 Mono8
`cxp_app_tpg` → `cxp_app_pixel_packer` (ready-gated same-cycle
`sof`/`sol` pulses, a copy of the `cxp_interface_top` glue, free-running
with `cfg_run = 1`); `pix_sel = 1` routes the Python-driven `ext_pix_*` /
`ext_meta_*` ports, used to drive the same-cycle-pulse contract of
`cxp_app_pixel_ingress`, the pulse-ahead contract and corner cases the TPG
cannot produce. `app_clk` runs at 10 ns and `tx_clk` at 8 ns to exercise the
CDC FIFO; `cfg_dsizeP` = 11 so packets straddle header / marker / line
boundaries; `cfg_arbitrary` = 0 (rectangular) in every test but 16. `capture()`
records every accepted wire beat and the checkers verify:

  - CXP framing of every packet: K27.7 SOP, the 6-word header (type 0x01,
    StreamID, PacketTag, DsizeP), CRC32 against the golden §8.2.2.2 CRC over
    the words the DUT covers (register, LSByte in P0), K29.7 trailer;
  - PacketTag walking 0, 1, 2, … on the single StreamID (mod 256);
  - the concatenated payload against `expected_frame_words()`: one 25-word
    rectangular image header per frame followed by `Y_SIZE` × (2-word line
    marker + `X_SIZE/4` pixel words), data and kmask, K28.3 with kmask 0xF.

The golden header carries DsizeL = ceil(X_SIZE × 8 / 32) words (Mono8,
Table 38). The TPG path marks each frame's last pixel word
(`pix_word_eof_i`), so each frame goes out as packets of 11, 11, 11 and 8
words (`tpg_dsizeP`, §8.5.2). The ext path leaves `ext_pix_word_eof` at 0
unless a test drives it; such drives pad the stream to a multiple of DsizeP
(`flush_chopper`) so the last packet closes. The frame is small (8×4, 41
merged words) so a run takes a few thousand cycles.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  TPG: framing, CRC and PacketTag of the first packets after start-up.
  2  TPG: payload of the first two frames = golden, framing of ~400 packets.
  3  TPG: data-slot kmask only 0 or 0xF; K28.3 marker words present.
  4  Ext, same-cycle pulses: K28.3 is payload word 0 of packet 0.
  5  Ext, same-cycle pulses: one frame = golden, word for word.
  6  Ext, same-cycle pulses: each line marker precedes its line's pixels.
  7  Ext, wire stalled for 200 cycles: K28.3 still payload word 0.
  8  Same-cycle and pulse-ahead conventions give identical golden frames.
  9  Bursty producer (2 idle cycles per word): frame = golden.
 10  Two back-to-back frames: 82 words = golden × 2, 10 K28.3.
 11  Idle producer: no packet on the wire.
 12  No end-of-frame mark: the chopper carries the residual into the next frame.
 13  End-of-frame mark: a short last packet, the next header starts a packet.
 14  The tail packet keeps its image's StreamID when the metadata moves on.
 15  DsizeP lowered below an open packet's count closes it; no wedge.
"""

from __future__ import annotations

from dataclasses import dataclass

from cxp_protocol import crc as gcrc

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from cxp_testcase import cxp_test


# Frame geometry and metadata — must match the wrapper's TPG parameters.
X_SIZE    = 8
Y_SIZE    = 4
X_OFFS    = 0
Y_OFFS    = 0
PIXFMT    = 0x0101           # GenICam Mono8
TAPG      = 0x0000           # Geometry_1X_1Y
STREAMID  = 0x0001
SOURCETAG = 0x0000
FLAGS     = 0x00

# Ext-path geometry (same as the TPG so expected_frame_words() applies).
EXT_X_SIZE = X_SIZE          # 8
EXT_Y_SIZE = Y_SIZE          # 4

# Stream-packet payload size, in 32-bit words.  Picked so packets do
# not align to frame boundaries (exercises packets that straddle the
# image-header / line-marker boundaries).
DSIZE_P = 11

# 8b/10b control character byte values and marker type bytes.
K27_7          = 0xFB
K28_3          = 0x7C
K29_7          = 0xFD
HDR_TYPE_REC   = 0x01
LINE_TYPE_RECT = 0x02
LINE_TYPE_ARB  = 0x04

# Clock periods.  Different rates -> exercises the CDC FIFO.
APP_PERIOD_NS = 10
TX_PERIOD_NS  = 8

# Total simulation budget in tx_clk cycles.
SIM_CYCLES = 8000


# -----------------------------------------------------------------------------
# Captured wire beat
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class WireBeat:
    """One accepted beat on the `m_*` wire port."""
    data:  int
    kmask: int
    sop:   int
    eop:   int


def frame_packet_sizes(words_per_frame: int, dsizeP: int = DSIZE_P) -> list[int]:
    """DsizeP of each packet of one frame when the chopper closes on the
    frame's last word: full packets, then the remainder."""
    sizes = [dsizeP] * (words_per_frame // dsizeP)
    if words_per_frame % dsizeP:
        sizes.append(words_per_frame % dsizeP)
    return sizes


def tpg_dsizeP(index: int) -> int:
    """DsizeP of the index-th packet from the free-running TPG."""
    sizes = frame_packet_sizes(25 + Y_SIZE * (2 + X_SIZE // 4))
    return sizes[index % len(sizes)]


def rep4(b: int) -> int:
    """4× byte replication for a 32-bit lane word."""
    b &= 0xFF
    return (b << 24) | (b << 16) | (b << 8) | b


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def bringup(dut, *, pix_sel: int = 0):
    """Start both clocks, park all inputs, reset 8 `tx_clk`, then wait 8."""
    cocotb.start_soon(Clock(dut.app_clk, APP_PERIOD_NS, unit="ns").start(start_high=False))
    cocotb.start_soon(Clock(dut.tx_clk,  TX_PERIOD_NS,  unit="ns").start(start_high=False))

    dut.app_rst_n.value     = 0
    dut.tx_rst_n.value      = 0
    dut.cfg_run.value       = 0
    dut.cfg_arbitrary.value = 0
    dut.cfg_dsizeP.value    = DSIZE_P
    dut.m_ready.value       = 0

    # External pix-path defaults (parked unless pix_sel=1 + driven by test).
    dut.pix_sel.value             = pix_sel
    dut.ext_pix_word_data.value   = 0
    dut.ext_pix_word_valid.value  = 0
    dut.ext_pix_frame_start.value = 0
    dut.ext_pix_line_start.value  = 0
    dut.ext_pix_word_eof.value    = 0
    dut.ext_meta_xsize.value      = 0
    dut.ext_meta_ysize.value      = 0
    dut.ext_meta_xoffs.value      = 0
    dut.ext_meta_yoffs.value      = 0
    dut.ext_meta_pixfmt.value     = 0
    dut.ext_meta_tapg.value       = 0
    dut.ext_meta_streamid.value   = 0
    dut.ext_meta_sourcetag.value  = 0
    dut.ext_meta_flags.value      = 0

    for _ in range(8):
        await RisingEdge(dut.tx_clk)
    dut.app_rst_n.value = 1
    dut.tx_rst_n.value  = 1
    for _ in range(8):
        await RisingEdge(dut.tx_clk)


# -----------------------------------------------------------------------------
# Wire monitor
# -----------------------------------------------------------------------------
async def capture(dut, cycles: int, m_ready_pattern=None) -> list[WireBeat]:
    """Run `cycles` `tx_clk` edges and return every accepted WireBeat.

    `m_ready` is 1 every cycle, or `m_ready_pattern(cycle) -> 0|1`.
    """
    beats: list[WireBeat] = []
    for c in range(cycles):
        dut.m_ready.value = 1 if m_ready_pattern is None else m_ready_pattern(c)
        await RisingEdge(dut.tx_clk)
        if int(dut.m_valid.value) and int(dut.m_ready.value):
            beats.append(WireBeat(
                data  = int(dut.m_data.value),
                kmask = int(dut.m_kmask.value),
                sop   = int(dut.m_sop.value),
                eop   = int(dut.m_eop.value),
            ))
    return beats


def split_packets(beats: list[WireBeat]) -> list[list[WireBeat]]:
    """Split a captured beat stream into complete SOP..EOP packets.

    Asserts no SOP inside an open packet and no EOP outside one; beats
    before the first SOP are dropped and an unterminated tail is discarded.
    """
    pkts: list[list[WireBeat]] = []
    cur:  list[WireBeat] = []
    in_pkt = False
    for b in beats:
        if b.sop:
            assert not in_pkt, f"SOP inside an open packet: {b}"
            cur = [b]
            in_pkt = True
        else:
            if not in_pkt:
                # Drop leading bubble that might appear pre-bringup.
                continue
            cur.append(b)
        if b.eop:
            assert in_pkt, f"EOP outside a packet: {b}"
            pkts.append(cur)
            cur = []
            in_pkt = False
    return pkts


# -----------------------------------------------------------------------------
# CRC32 reference
# -----------------------------------------------------------------------------
def crc32_bytes(stream_id: int, tag: int, dsizeP: int,
                data_words: list[int]) -> bytes:
    """The bytes the DUT folds into its CRC: the data words only (Table 19,
    "stream data 4 to (N+3)"), P0 first."""
    raw = bytearray()
    for w in data_words:
        raw += w.to_bytes(4, "little")
    return bytes(raw)


def expected_crc_word(stream_id: int, tag: int, dsizeP: int,
                      data_words: list[int]) -> int:
    """The CRC word on m_data: the §8.2.2.2 register over the covered words,
    LSByte in P0 (golden `cxp_protocol.crc`)."""
    raw = crc32_bytes(stream_id, tag, dsizeP, data_words)
    words = [int.from_bytes(raw[i:i + 4], "little") for i in range(0, len(raw), 4)]
    return gcrc.crc_word(words)


# -----------------------------------------------------------------------------
# Packet checker
# -----------------------------------------------------------------------------
def check_packet_framing(pkt: list[WireBeat], stream_id: int, tag: int,
                         dsizeP: int) -> tuple[list[int], list[int]]:
    """Verify the CXP envelope; return (data_words, data_kmasks).

    Checks length 6 + dsizeP + 2, the 6 header words (K27.7 with kmask 0xF
    and sop, then type 0x01, StreamID, PacketTag, DsizeP hi/lo), sop/eop
    low on data, the CRC word, and the K29.7 trailer with kmask 0xF + eop.
    `tag` is a hint for the expected per-stream PacketTag value.  It
    must match what's on the wire; the caller is responsible for
    computing the right tag (per-stream counter starting at 0)."""

    expected_len = 6 + dsizeP + 1 + 1   # hdr + data + CRC + EOP
    assert len(pkt) == expected_len, (
        f"packet length {len(pkt)} != expected {expected_len}"
    )

    # Header (table 18).
    exp_hdr = [
        WireBeat(rep4(K27_7),               0xF, 1, 0),
        WireBeat(rep4(HDR_TYPE_REC),        0x0, 0, 0),
        WireBeat(rep4(stream_id & 0xFF),    0x0, 0, 0),
        WireBeat(rep4(tag & 0xFF),          0x0, 0, 0),
        WireBeat(rep4((dsizeP >> 8) & 0xFF),0x0, 0, 0),
        WireBeat(rep4(dsizeP & 0xFF),       0x0, 0, 0),
    ]
    for i, (got, exp) in enumerate(zip(pkt[:6], exp_hdr)):
        assert got == exp, f"header word {i}: got {got}, expected {exp}"

    # Data slice.
    data_beats = pkt[6:6 + dsizeP]
    data_words = [b.data  for b in data_beats]
    data_kmask = [b.kmask for b in data_beats]
    for i, b in enumerate(data_beats):
        assert b.sop == 0,  f"data word {i}: SOP set"
        assert b.eop == 0,  f"data word {i}: EOP set"

    # CRC word.
    crc_beat = pkt[6 + dsizeP]
    exp_crc  = expected_crc_word(stream_id, tag, dsizeP, data_words)
    assert crc_beat.data  == exp_crc, (
        f"CRC: got {crc_beat.data:#010x}, expected {exp_crc:#010x}"
    )
    assert crc_beat.kmask == 0
    assert crc_beat.sop   == 0
    assert crc_beat.eop   == 0

    # Trailer.
    eop_beat = pkt[-1]
    assert eop_beat.data  == rep4(K29_7)
    assert eop_beat.kmask == 0xF
    assert eop_beat.eop   == 1
    assert eop_beat.sop   == 0

    return data_words, data_kmask


# -----------------------------------------------------------------------------
# Reference model — merged-stream payload of one frame
#
# Stream of 32-bit words emitted by the merger (= packet payload before
# DsizeP chopping) for one frame:
#
#   [25 image-header words]
#   for y in 0..Y_SIZE-1:
#       [2 line-marker words]
#       [X_SIZE/4 pixel words at line y]
#
# The same pattern repeats every frame (cfg_run=1 → continuous).
# -----------------------------------------------------------------------------
def expected_image_header_rect(sourcetag: int = SOURCETAG) -> list[tuple[int, int]]:
    """Return the 25 (data, kmask) tuples of the rectangular image header
    for the testbench's TPG configuration.  The TPG counts SourceTag per
    image from 0 (Table 38); the external path carries `ext_meta_sourcetag`."""

    def _w(b: int, kmask: int = 0) -> tuple[int, int]:
        return (rep4(b), kmask)

    sid_lo = STREAMID & 0xFF
    src_hi = (sourcetag >> 8) & 0xFF
    src_lo = sourcetag       & 0xFF
    xsize  = X_SIZE
    ysize  = Y_SIZE
    xoffs  = X_OFFS
    yoffs  = Y_OFFS
    dsizeL = (X_SIZE * 8 + 31) // 32   # Mono8, 32-bit words per line
    pix    = PIXFMT
    tapg   = TAPG
    flags  = FLAGS

    return [
        _w(K28_3, 0xF),                                    #  0
        _w(HDR_TYPE_REC),                                  #  1
        _w(sid_lo),                                        #  2
        _w(src_hi), _w(src_lo),                            #  3,4
        _w((xsize >> 16) & 0xFF), _w((xsize >> 8) & 0xFF), _w(xsize & 0xFF),  # 5..7
        _w((xoffs >> 16) & 0xFF), _w((xoffs >> 8) & 0xFF), _w(xoffs & 0xFF),  # 8..10
        _w((ysize >> 16) & 0xFF), _w((ysize >> 8) & 0xFF), _w(ysize & 0xFF),  # 11..13
        _w((yoffs >> 16) & 0xFF), _w((yoffs >> 8) & 0xFF), _w(yoffs & 0xFF),  # 14..16
        _w((dsizeL >> 16) & 0xFF), _w((dsizeL >> 8) & 0xFF), _w(dsizeL & 0xFF),  # 17..19
        _w((pix  >> 8) & 0xFF), _w(pix  & 0xFF),                              # 20..21
        _w((tapg >> 8) & 0xFF), _w(tapg & 0xFF),                              # 22..23
        _w(flags),                                                            # 24
    ]


def expected_line_marker_rect() -> list[tuple[int, int]]:
    """The 2 (data, kmask) tuples of the rectangular line marker."""
    return [(rep4(K28_3), 0xF), (rep4(LINE_TYPE_RECT), 0x0)]


def expected_pixel_word(y: int, x_idx: int) -> int:
    """Pixel-pattern word at line y, word position x_idx in line.

    Mono8 P0..P3 packing: P0 in m_data[7:0], P3 in m_data[31:24].
    Pattern: pixel(x,y) = (x + y) & 0xFF for x = 4*x_idx..4*x_idx+3."""
    base = 4 * x_idx
    p0 = (base + 0 + y) & 0xFF
    p1 = (base + 1 + y) & 0xFF
    p2 = (base + 2 + y) & 0xFF
    p3 = (base + 3 + y) & 0xFF
    return (p3 << 24) | (p2 << 16) | (p1 << 8) | p0


def expected_frame_words(sourcetag: int = SOURCETAG) -> list[tuple[int, int]]:
    """All (data, kmask) tuples in one frame's merged-stream payload."""
    out  = list(expected_image_header_rect(sourcetag))
    line_marker = expected_line_marker_rect()
    x_words = X_SIZE // 4
    for y in range(Y_SIZE):
        out += line_marker
        for xi in range(x_words):
            out.append((expected_pixel_word(y, xi), 0))
    return out


# -----------------------------------------------------------------------------
# External-drive helpers
#
# Tests 4..12 drive the pixel path directly through the ext_pix_* /
# ext_meta_* ports (pix_sel=1).  They exercise the in-merger skid register
# in cxp_stream_top against the same-cycle-pulse contract that
# cxp_app_pixel_ingress uses (and against other corner cases the TPG can't
# produce — non-aligned pulse timings, mid-frame stalls, etc.).
#
# The "golden frame" is identical to the rectangular header / line-marker /
# pixel-data sequence the TPG would have produced for the same X_SIZE /
# Y_SIZE / metadata, so expected_frame_words() matches it byte-for-byte.
# -----------------------------------------------------------------------------
def ext_meta_set(dut, *,
                 xsize=EXT_X_SIZE, ysize=EXT_Y_SIZE,
                 xoffs=X_OFFS, yoffs=Y_OFFS,
                 pixfmt=PIXFMT, tapg=TAPG,
                 streamid=STREAMID, sourcetag=SOURCETAG, flags=FLAGS):
    """Drive the ext_meta_* level signals.  These mirror the metadata an
    external pixel ingress would forward from cxp_ctrl_bootstrap_regs."""
    dut.ext_meta_xsize.value     = xsize
    dut.ext_meta_ysize.value     = ysize
    dut.ext_meta_xoffs.value     = xoffs
    dut.ext_meta_yoffs.value     = yoffs
    dut.ext_meta_pixfmt.value    = pixfmt
    dut.ext_meta_tapg.value      = tapg
    dut.ext_meta_streamid.value  = streamid
    dut.ext_meta_sourcetag.value = sourcetag
    dut.ext_meta_flags.value     = flags


def ext_pix_idle(dut):
    """Drop valid, data, both pulses and the end-of-frame mark."""
    dut.ext_pix_word_valid.value  = 0
    dut.ext_pix_word_data.value   = 0
    dut.ext_pix_frame_start.value = 0
    dut.ext_pix_line_start.value  = 0
    dut.ext_pix_word_eof.value    = 0


def frame_pixel_words(*, xsize=EXT_X_SIZE, ysize=EXT_Y_SIZE):
    """Return the list of (data, fs, ls) tuples that a same-cycle-pulse
    source would assert for one frame at the given geometry.  fs fires on
    the very first pixel of the frame; ls fires on the first pixel of
    every line (including line 0)."""
    words = []
    x_words = xsize // 4
    for y in range(ysize):
        for xi in range(x_words):
            data = expected_pixel_word(y, xi)
            fs = 1 if (y == 0 and xi == 0) else 0
            ls = 1 if xi == 0 else 0
            words.append((data, fs, ls))
    return words


async def _push_one_word(dut, data: int, fs: int, ls: int, eof: int = 0):
    """Push one (data, fs, ls) tuple onto the external pix bus, retrying
    each cycle until pix_word_ready=1 marks it accepted; `eof` marks the
    frame's last pixel word.  Fails after 2000 cycles without ready (a
    wedged pipeline)."""
    for _ in range(2000):
        dut.ext_pix_word_data.value   = data
        dut.ext_pix_word_valid.value  = 1
        dut.ext_pix_frame_start.value = fs
        dut.ext_pix_line_start.value  = ls
        dut.ext_pix_word_eof.value    = eof
        await RisingEdge(dut.app_clk)
        if int(dut.ext_pix_word_ready.value):
            return
    raise AssertionError("pixel bus not ready for 2000 cycles: the stream pipeline is wedged")


async def flush_chopper(dut, *, dsizeP: int = DSIZE_P, words_emitted: int):
    """Pad the pix stream with enough no-pulse data words to flush whatever
    partial packet is sitting in the DsizeP chopper, so the final EOP is
    actually produced on the wire.  Padding words land in the payload after
    the frame; callers verify only the first words_emitted words."""
    rem = words_emitted % dsizeP
    pad = (dsizeP - rem) % dsizeP
    for _ in range(pad):
        await _push_one_word(dut, 0xDEAD_BEEF, fs=0, ls=0)
    ext_pix_idle(dut)


async def drive_ext_frame_same_cycle_pulse(dut, *,
                                           xsize=EXT_X_SIZE, ysize=EXT_Y_SIZE,
                                           inter_word_idle=0,
                                           flush=True, dsizeP=DSIZE_P,
                                           mark_eof=False):
    """Drive one frame onto the external pix path using the pulse-coincident-
    with-data contract (the convention cxp_app_pixel_ingress emits).

    inter_word_idle: number of idle cycles inserted between pixel words
    (simulates bursty / gappy producers; verifies the skid register doesn't
    spuriously emit data while pix_word_valid=0).
    flush: if True, append enough zero-pulse pixel words to round the
    merged stream up to a multiple of dsizeP so the final packet EOPs.
    mark_eof: mark the last pixel word as the frame's last (pix_word_eof_i)."""
    words = frame_pixel_words(xsize=xsize, ysize=ysize)
    for i, (data, fs, ls) in enumerate(words):
        await _push_one_word(dut, data, fs, ls,
                             eof=int(mark_eof and i == len(words) - 1))
        for _ in range(inter_word_idle):
            ext_pix_idle(dut)
            await RisingEdge(dut.app_clk)
    ext_pix_idle(dut)
    if flush:
        # One frame's merged-stream length = 25 hdr + ysize * (2 + xsize/4).
        emitted = 25 + ysize * (2 + xsize // 4)
        await flush_chopper(dut, dsizeP=dsizeP, words_emitted=emitted)


async def drive_ext_frame_pulse_ahead(dut, *,
                                      xsize=EXT_X_SIZE, ysize=EXT_Y_SIZE,
                                      flush=True, dsizeP=DSIZE_P):
    """Drive one frame onto the external pix path using the pulse-leads-
    data-by-one-cycle contract.  The fs/ls pulses are asserted (ungated) on
    a cycle where pix_word_valid=0; the data word follows the next cycle."""
    words = frame_pixel_words(xsize=xsize, ysize=ysize)
    for data, fs, ls in words:
        if fs or ls:
            # Lead cycle: pulse only, no data.
            ext_pix_idle(dut)
            dut.ext_pix_frame_start.value = fs
            dut.ext_pix_line_start.value  = ls
            await RisingEdge(dut.app_clk)
        await _push_one_word(dut, data, fs=0, ls=0)
    ext_pix_idle(dut)
    if flush:
        emitted = 25 + ysize * (2 + xsize // 4)
        await flush_chopper(dut, dsizeP=dsizeP, words_emitted=emitted)


def _split_and_extract_payload(beats, *, stream_id, dsizeP, n_pkts):
    """Helper: split, check framing of n_pkts, return flat payload list."""
    pkts = split_packets(beats)
    assert len(pkts) >= n_pkts, f"need ≥ {n_pkts} pkts, got {len(pkts)}"
    payload = []
    for tag, pkt in enumerate(pkts[:n_pkts]):
        words, kmasks = check_packet_framing(
            pkt, stream_id=stream_id, tag=tag & 0xFF, dsizeP=dsizeP,
        )
        payload.extend(zip(words, kmasks))
    return payload


# -----------------------------------------------------------------------------
# TC 1 — Packet Framing
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_packet_framing(dut):
    """Stream packets after start-up carry a correct CXP envelope and tags.

    Guards the Table 18/19 stream-packet envelope `cxp_tx_stream_pkt` puts
    around each DsizeP chopper block, and the per-StreamID PacketTag
    increment from 0.

    Stimulus: TPG source (`pix_sel` 0), `cfg_run` = 1 after bring-up,
              `m_ready` = 1, 2000 `tx_clk` cycles captured.
    Checks:   `split_packets` rules (no SOP inside / EOP outside a packet);
              ≥ 4 complete packets; `check_packet_framing` on every packet
              with tag = packet index and DsizeP `tpg_dsizeP` (11, 11, 11, 8
              per frame; length N + 8, header words, sop/eop,
              CRC vs `expected_crc_word`, K29.7 trailer).
    """
    dut.TESTCASE.value = 1
    await bringup(dut)
    dut.cfg_run.value = 1

    beats = await capture(dut, cycles=2000)
    pkts  = split_packets(beats)
    assert len(pkts) >= 4, (
        f"expected at least 4 captured packets, got {len(pkts)} "
        f"({len(beats)} beats)"
    )

    sid = STREAMID & 0xFF
    for tag, pkt in enumerate(pkts):
        check_packet_framing(pkt, stream_id=sid, tag=tag, dsizeP=tpg_dsizeP(tag))


# -----------------------------------------------------------------------------
# TC 2 — Full-Frame Decode (TPG)
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_full_frame_decode(dut):
    """The TPG payload decodes to header + line markers + ramp pixels.

    Proves merger order and chopper continuity across packet and frame
    boundaries with the ready-gated TPG source, including the loop-back of
    `cfg_run = 1` (next frame's header follows without a gap).

    Stimulus: TPG source, `cfg_run` = 1, `m_ready` = 1, `SIM_CYCLES` (8000)
              `tx_clk` cycles captured — ~400 packets, so PacketTag wraps
              past 0xFF.
    Checks:   ≥ 10 packets (2 frames of 4 packets + 2); framing of every
              packet with tag = index mod 256 and DsizeP `tpg_dsizeP` (each
              frame ends in its own short packet); the concatenated payload
              equals `expected_frame_words()` for SourceTag 0 then 1 (the
              TPG counts images) word by word (data and kmask) over
              min(captured, 82) ≥ 41 words.
    Note:     only the first two frames' payload is compared; later
              packets are covered by the envelope / CRC checks only.
    """
    dut.TESTCASE.value = 2
    await bringup(dut)
    dut.cfg_run.value = 1

    # Frame size in merged-stream words: 25 hdr + Y_SIZE × (2 + X_SIZE/4).
    words_per_frame = 25 + Y_SIZE * (2 + X_SIZE // 4)
    pkts_per_frame  = (words_per_frame + DSIZE_P - 1) // DSIZE_P
    # Aim for ≥ 2 frames worth of data so we also exercise the loop-
    # back of cfg_run=1 (next frame's header arrives without a gap).
    target_pkts     = pkts_per_frame * 2 + 2

    beats = await capture(dut, cycles=SIM_CYCLES)
    pkts  = split_packets(beats)
    assert len(pkts) >= target_pkts, (
        f"expected ≥ {target_pkts} packets, got {len(pkts)}"
    )

    sid = STREAMID & 0xFF
    payload: list[tuple[int, int]] = []
    for tag, pkt in enumerate(pkts):
        words, kmasks = check_packet_framing(
            pkt, stream_id=sid, tag=tag & 0xFF, dsizeP=tpg_dsizeP(tag),
        )
        payload.extend(zip(words, kmasks))

    # Reference: two frames' worth of merged-stream words; the TPG's
    # SourceTag is 0 on the first image after reset and 1 on the next.
    ref = expected_frame_words(0) + expected_frame_words(1)

    # Compare element-wise up to whichever ends first.
    n = min(len(payload), len(ref))
    assert n >= words_per_frame, (
        f"captured payload ({len(payload)}) shorter than one frame "
        f"({words_per_frame}) — extend SIM_CYCLES"
    )

    for i in range(n):
        got = payload[i]
        exp = ref[i]
        assert got == exp, (
            f"merged-stream word {i} (frame {i // words_per_frame}, "
            f"in-frame idx {i % words_per_frame}): "
            f"got data={got[0]:#010x} kmask={got[1]:#x}, "
            f"expected data={exp[0]:#010x} kmask={exp[1]:#x}"
        )


# -----------------------------------------------------------------------------
# TC 3 — Kmask Passthrough
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_kmask_passthrough(dut):
    """K28.3 marker words keep kmask 0xF on the wire; other data has 0.

    Regression guard for the `cxp_tx_stream_pkt` kmask-passthrough fix:
    every K28.3 marker (image header / line markers) must carry kmask 0xF
    through merger, FIFO and framer; everything else kmask 0.

    Stimulus: TPG source, `cfg_run` = 1, `m_ready` = 1, 4000 `tx_clk`
              cycles captured.
    Checks:   framing of every packet (tag = index mod 256); every data-slot
              kmask is 0 or 0xF; ≥ `Y_SIZE` + 1 (5) slots with kmask 0xF;
              ≥ 1 slot with kmask 0.
    Note:     loose — a 0xF slot is not checked to carry 0x7C, and with
              several frames captured the image-header markers alone meet
              the count, so losing every line marker would still pass.
    """
    dut.TESTCASE.value = 3
    await bringup(dut)
    dut.cfg_run.value = 1

    beats = await capture(dut, cycles=4000)
    pkts  = split_packets(beats)

    # We expect exactly one K28.3 marker at the start of every image
    # header (Y_SIZE+1 of them counting line markers per frame).
    sid = STREAMID & 0xFF
    k28_3_seen = 0
    data_kmask_zero = 0
    for tag, pkt in enumerate(pkts):
        _, kmasks = check_packet_framing(
            pkt, stream_id=sid, tag=tag & 0xFF, dsizeP=tpg_dsizeP(tag),
        )
        for km in kmasks:
            if km == 0xF:
                k28_3_seen += 1
            elif km == 0:
                data_kmask_zero += 1
            else:
                raise AssertionError(
                    f"unexpected kmask in data slot: {km:#x}"
                )

    # In a single frame: 1 image-header K28.3 + Y_SIZE line-marker K28.3
    # = Y_SIZE + 1 marker words.  We typically capture > 1 frame.
    assert k28_3_seen >= (Y_SIZE + 1), (
        f"expected ≥ {Y_SIZE + 1} K28.3 marker words, got {k28_3_seen}"
    )
    assert data_kmask_zero > 0, "no plain-data words observed"


# -----------------------------------------------------------------------------
# TC 4 — Skid: Same-Cycle Pulse, K28.3 Leads the First Pixel
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_skid_same_cycle_pulse_first_marker(dut):
    """With same-cycle pulses the image-header K28.3 precedes all pixel data.

    `cxp_app_pixel_ingress` asserts `pix_frame_start` / `pix_line_start` on the
    SAME cycle as the first pixel's `pix_word_valid`; the skid register must
    park that pixel while the header and line marker pass. Reproduces
    test_stream_video's failure at unit level: under the original (no-skid)
    merger the first pixel slipped ahead of K28.3 and appeared as a leading
    0x00000000 in packets[0].payload.

    Stimulus: `pix_sel` 1, golden `ext_meta`; one 8×4 frame via
              `drive_ext_frame_same_cycle_pulse` (fs + ls on word 0, ls on
              each line's first word, each word held until accepted), 3
              0xDEADBEEF pad words, 200 `app_clk` drain cycles; 8000-cycle
              capture with `m_ready` = 1 running in the background.
    Checks:   ≥ 1 packet; payload word 0 of packet 0 = 4×K28.3 with
              kmask 0xF.
    """
    dut.TESTCASE.value = 4
    await bringup(dut, pix_sel=1)
    ext_meta_set(dut)

    capture_task = cocotb.start_soon(capture(dut, cycles=SIM_CYCLES))
    await drive_ext_frame_same_cycle_pulse(dut)
    # Let the FIFO drain.
    for _ in range(200):
        await RisingEdge(dut.app_clk)
    beats = await capture_task

    pkts = split_packets(beats)
    assert len(pkts) >= 1, "no packet captured"

    # First payload slot of the very first packet must be K28.3.
    pkt0_payload = pkts[0][6:6 + DSIZE_P]
    first = pkt0_payload[0]
    assert first.data == rep4(K28_3) and first.kmask == 0xF, (
        f"first payload slot of packet 0 is not K28.3: data=0x{first.data:08x} "
        f"kmask=0x{first.kmask:x} — same-cycle-pulse + no skid would leave "
        f"a leading zero data word here"
    )


# -----------------------------------------------------------------------------
# TC 5 — Skid: Same-Cycle Pulse, Full-Frame Decode
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_skid_same_cycle_full_frame_decode(dut):
    """A same-cycle-pulse frame decodes to the golden reference word for word.

    Whole-frame order (header > line marker > skid) for the accept-cycle
    pulse convention used by `cxp_interface_top`.

    Stimulus: as TC 4 — one same-cycle frame, 3 pad words, 200 drain
              cycles, `m_ready` = 1.
    Checks:   framing of the first 4 packets (tag = index); their first 41
              payload words equal `expected_frame_words()` (data + kmask).
    """
    dut.TESTCASE.value = 5
    await bringup(dut, pix_sel=1)
    ext_meta_set(dut)

    capture_task = cocotb.start_soon(capture(dut, cycles=SIM_CYCLES))
    await drive_ext_frame_same_cycle_pulse(dut)
    for _ in range(200):
        await RisingEdge(dut.app_clk)
    beats = await capture_task

    words_per_frame = 25 + EXT_Y_SIZE * (2 + EXT_X_SIZE // 4)
    pkts_per_frame  = (words_per_frame + DSIZE_P - 1) // DSIZE_P
    payload = _split_and_extract_payload(
        beats, stream_id=STREAMID & 0xFF, dsizeP=DSIZE_P,
        n_pkts=pkts_per_frame,
    )

    ref = expected_frame_words()
    n = min(len(payload), words_per_frame)
    for i in range(n):
        assert payload[i] == ref[i], (
            f"merged-stream word {i}: got data={payload[i][0]:#010x} "
            f"kmask={payload[i][1]:#x}, expected data={ref[i][0]:#010x} "
            f"kmask={ref[i][1]:#x}"
        )


# -----------------------------------------------------------------------------
# TC 6 — Skid: Line Marker Leads Each Line
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_skid_line_marker_lead(dut):
    """Every line marker precedes its own line's pixels, not just line 0.

    Line-marker > skid priority on each line even when `ls` fires on the
    same cycle as that line's first pixel.

    Stimulus: as TC 4 — one same-cycle frame, 3 pad words, 200 drain
              cycles, `m_ready` = 1.
    Checks:   framing of the first 4 packets; exactly `Y_SIZE` + 1 (5)
              K28.3 words (kmask 0xF, byte 0x7C) in the first 41 payload
              words; after each of the 4 line markers the next word is
              (4×0x02, kmask 0) and the word after that has the data of the
              line's first pixel word.
    """
    dut.TESTCASE.value = 6
    await bringup(dut, pix_sel=1)
    ext_meta_set(dut)

    capture_task = cocotb.start_soon(capture(dut, cycles=SIM_CYCLES))
    await drive_ext_frame_same_cycle_pulse(dut)
    for _ in range(200):
        await RisingEdge(dut.app_clk)
    beats = await capture_task

    words_per_frame = 25 + EXT_Y_SIZE * (2 + EXT_X_SIZE // 4)
    pkts_per_frame  = (words_per_frame + DSIZE_P - 1) // DSIZE_P
    payload = _split_and_extract_payload(
        beats, stream_id=STREAMID & 0xFF, dsizeP=DSIZE_P,
        n_pkts=pkts_per_frame,
    )

    # Walk the payload and find every K28.3 marker.  In one frame we expect
    # exactly Y_SIZE + 1 K28.3 markers (1 image header + Y_SIZE line markers).
    # For each line marker, the next non-marker payload word must be the
    # 0x02 rect-line-type byte, then the line's first pixel word.
    marker_positions = [
        i for i, (d, k) in enumerate(payload[:words_per_frame])
        if k == 0xF and (d & 0xFF) == K28_3
    ]
    assert len(marker_positions) == EXT_Y_SIZE + 1, (
        f"expected {EXT_Y_SIZE + 1} K28.3 markers in one frame, got "
        f"{len(marker_positions)}"
    )

    # Image header marker first; the rest are line markers.
    for li, m_pos in enumerate(marker_positions[1:]):
        # m_pos points at K28.3.  Next word = LINE_TYPE_RECT (0x02);
        # word after that = first pixel of line li.
        assert payload[m_pos + 1] == (rep4(LINE_TYPE_RECT), 0), (
            f"line {li}: word after K28.3 not 0x02 line-type marker"
        )
        first_pix = payload[m_pos + 2][0]
        expected  = expected_pixel_word(li, 0)
        assert first_pix == expected, (
            f"line {li}: first pixel after line marker is 0x{first_pix:08x}, "
            f"expected 0x{expected:08x} — pixel slipped past line marker?"
        )


# -----------------------------------------------------------------------------
# TC 7 — Skid: Order Holds Under Wire Back-Pressure
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_skid_holds_under_backpressure(dut):
    """K28.3 still leads the first pixel after an initial wire stall.

    Intended to show that the skid register and the FIFO together keep the
    header-before-pixel order when the wire (`m_ready`) stalls at start-up.

    Stimulus: as TC 4 (one same-cycle frame, 3 pad words) but `m_ready` = 0
              for the first 200 `tx_clk` capture cycles, then 1; 400
              `app_clk` drain cycles.
    Checks:   ≥ 1 packet; payload word 0 of packet 0 = 4×K28.3 with
              kmask 0xF.
    Note:     the FIFO cannot fill here — 44 words never reach the
              almost-full threshold (252 of 256), so `merge_ready` stays 1
              and the merger itself sees no back-pressure; only the wire
              stall is exercised.
    """
    dut.TESTCASE.value = 7
    await bringup(dut, pix_sel=1)
    ext_meta_set(dut)

    # Start capture with m_ready held off for the first 200 cycles, then
    # full-throttle afterwards (the FIFO absorbs the whole frame, so the
    # merger itself is not back-pressured).
    def ready_pattern(c):
        return 0 if c < 200 else 1

    capture_task = cocotb.start_soon(capture(dut, cycles=SIM_CYCLES,
                                             m_ready_pattern=ready_pattern))
    await drive_ext_frame_same_cycle_pulse(dut)
    for _ in range(400):
        await RisingEdge(dut.app_clk)
    beats = await capture_task

    pkts = split_packets(beats)
    assert len(pkts) >= 1, "no packet captured under back-pressure"
    pkt0_payload = pkts[0][6:6 + DSIZE_P]
    assert pkt0_payload[0].data  == rep4(K28_3), \
        "back-pressure regression: first payload slot is not K28.3"
    assert pkt0_payload[0].kmask == 0xF


# -----------------------------------------------------------------------------
# TC 8 — Both Pulse Conventions Equivalent
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_skid_both_pulse_conventions_equivalent(dut):
    """Same-cycle and pulse-ahead `fs`/`ls` give the same merged stream.

    The `cxp_stream_top` header comment promises both source conventions
    are tolerated: pulses coincident with the accepted pixel
    (`cxp_app_pixel_ingress`) and pulses one cycle ahead of the data.

    Stimulus: bring-up + one same-cycle frame (3 pad words, 200 drain
              cycles); then a second bring-up + one pulse-ahead frame where
              `fs`/`ls` are asserted alone on a `valid` = 0 cycle and the
              word follows on the next cycle. `m_ready` = 1 throughout.
    Checks:   framing of the first 4 packets of each run; each run's first
              41 payload words equal `expected_frame_words()`, and the two
              runs are equal.
    Note:     the pulse-ahead lead cycle is only safe because the skid
              fires in it (`merge_ready` = 1 here); the `merge_ready` = 0
              case is not covered (src/doc/cxp_stream_top.md, Medium 1).
    """
    dut.TESTCASE.value = 8

    words_per_frame = 25 + EXT_Y_SIZE * (2 + EXT_X_SIZE // 4)
    pkts_per_frame  = (words_per_frame + DSIZE_P - 1) // DSIZE_P
    ref = expected_frame_words()

    async def run_one(drive_fn):
        await bringup(dut, pix_sel=1)
        ext_meta_set(dut)
        cap = cocotb.start_soon(capture(dut, cycles=SIM_CYCLES))
        await drive_fn(dut)
        for _ in range(200):
            await RisingEdge(dut.app_clk)
        b = await cap
        payload = _split_and_extract_payload(
            b, stream_id=STREAMID & 0xFF, dsizeP=DSIZE_P,
            n_pkts=pkts_per_frame,
        )
        return payload[:words_per_frame]

    same_cycle  = await run_one(drive_ext_frame_same_cycle_pulse)
    pulse_ahead = await run_one(drive_ext_frame_pulse_ahead)

    assert same_cycle  == ref, "same-cycle-pulse frame diverged from golden"
    assert pulse_ahead == ref, "pulse-leads frame diverged from golden"
    assert same_cycle  == pulse_ahead, (
        "the two pulse conventions produced different merged streams"
    )


# -----------------------------------------------------------------------------
# TC 9 — Bursty Producer
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_skid_bursty_producer(dut):
    """Idle gaps between pixel words neither leak nor lose data.

    The skid register must not emit stale data while `pix_word_valid` = 0
    and must not drop a pixel across the gaps.

    Stimulus: one same-cycle frame with 2 idle `app_clk` cycles (`valid`
              and pulses 0) after every accepted pixel word, 3 pad words,
              400 drain cycles; `m_ready` = 1.
    Checks:   framing of the first 4 packets; their first 41 payload words
              equal `expected_frame_words()`.
    """
    dut.TESTCASE.value = 9
    await bringup(dut, pix_sel=1)
    ext_meta_set(dut)

    capture_task = cocotb.start_soon(capture(dut, cycles=SIM_CYCLES))
    await drive_ext_frame_same_cycle_pulse(dut, inter_word_idle=2)
    for _ in range(400):
        await RisingEdge(dut.app_clk)
    beats = await capture_task

    words_per_frame = 25 + EXT_Y_SIZE * (2 + EXT_X_SIZE // 4)
    pkts_per_frame  = (words_per_frame + DSIZE_P - 1) // DSIZE_P
    payload = _split_and_extract_payload(
        beats, stream_id=STREAMID & 0xFF, dsizeP=DSIZE_P,
        n_pkts=pkts_per_frame,
    )
    ref = expected_frame_words()
    for i in range(words_per_frame):
        assert payload[i] == ref[i], (
            f"bursty: word {i} got data={payload[i][0]:#010x} "
            f"kmask={payload[i][1]:#x}, expected data={ref[i][0]:#010x} "
            f"kmask={ref[i][1]:#x}"
        )


# -----------------------------------------------------------------------------
# TC 10 — Back-to-Back Frames
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_skid_back_to_back_frames(dut):
    """Two frames with no gap yield two complete, ordered frames.

    A frame pulse right after the previous frame's last pixel must produce
    exactly one new image header, and every group must start with K28.3.

    Stimulus: two same-cycle frames back to back with no padding between
              them, then one `flush_chopper` over 82 words (6 pad words),
              400 drain cycles; `m_ready` = 1.
    Checks:   framing of the first 8 packets; the first 82 payload words
              equal `expected_frame_words()` × 2; exactly 2 × (`Y_SIZE`+1)
              = 10 K28.3 marker words among them.
    """
    dut.TESTCASE.value = 10
    await bringup(dut, pix_sel=1)
    ext_meta_set(dut)

    capture_task = cocotb.start_soon(capture(dut, cycles=SIM_CYCLES))
    # Don't pad between frames — the back-to-back test depends on frame
    # boundaries being immediate.  Flush manually after both frames using
    # the combined word count so the chopper rounds to a packet boundary.
    await drive_ext_frame_same_cycle_pulse(dut, flush=False)
    await drive_ext_frame_same_cycle_pulse(dut, flush=False)
    words_per_frame = 25 + EXT_Y_SIZE * (2 + EXT_X_SIZE // 4)
    await flush_chopper(dut, dsizeP=DSIZE_P,
                        words_emitted=2 * words_per_frame)
    for _ in range(400):
        await RisingEdge(dut.app_clk)
    beats = await capture_task

    words_per_frame = 25 + EXT_Y_SIZE * (2 + EXT_X_SIZE // 4)
    pkts_per_frame  = (words_per_frame + DSIZE_P - 1) // DSIZE_P
    payload = _split_and_extract_payload(
        beats, stream_id=STREAMID & 0xFF, dsizeP=DSIZE_P,
        n_pkts=pkts_per_frame * 2,
    )
    ref = expected_frame_words() * 2
    for i in range(words_per_frame * 2):
        assert payload[i] == ref[i], (
            f"two-frame: word {i} (frame {i // words_per_frame}, in-frame "
            f"{i % words_per_frame}) got data={payload[i][0]:#010x} "
            f"kmask={payload[i][1]:#x}, expected data={ref[i][0]:#010x} "
            f"kmask={ref[i][1]:#x}"
        )

    # Marker count: 2 × (1 image header + Y_SIZE line markers).
    markers = sum(
        1 for d, k in payload[:words_per_frame * 2]
        if k == 0xF and (d & 0xFF) == K28_3
    )
    assert markers == 2 * (EXT_Y_SIZE + 1), (
        f"expected {2 * (EXT_Y_SIZE + 1)} K28.3 markers across 2 frames, "
        f"got {markers}"
    )


# -----------------------------------------------------------------------------
# TC 11 — Idle Producer, No Packets
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_skid_idle_no_packets(dut):
    """An idle producer never spawns a stream packet.

    The skid register and generators must not synthesise words without
    `pix_word_valid` or a frame / line pulse.

    Stimulus: `pix_sel` 1, golden `ext_meta` driven, `ext_pix_word_valid`
              and both pulses held 0; 1500 `tx_clk` cycles captured with
              `m_ready` = 1.
    Checks:   `split_packets` finds 0 complete packets.
    Note:     only SOP..EOP-complete packets are counted; a SOP whose EOP
              falls outside the window would be discarded, not flagged.
    """
    dut.TESTCASE.value = 11
    await bringup(dut, pix_sel=1)
    ext_meta_set(dut)
    beats = await capture(dut, cycles=1500)
    pkts  = split_packets(beats)
    assert len(pkts) == 0, (
        f"idle producer produced {len(pkts)} packets — skid leaked data?"
    )


# -----------------------------------------------------------------------------
# TC 12 — DsizeP Chopper Residual Across a Frame Boundary
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_chopper_residual_across_frame_boundary(dut):
    """Without an end-of-frame mark the chopper carries a frame's residual.

    A source that never marks its last word (`ext_pix_word_eof` = 0): the
    chopper counts merged-stream words 0..cfg_dsizeP-1 and tags SOP/EOP
    only on those boundaries. When a frame (25 + Y*(2+X/4) words)
    is not a multiple of cfg_dsizeP, the residual stays mid-packet until
    the next frame's first words finish it, so frame N+1's image-header
    K28.3 lands inside that packet. Production example, 640×480 at
    DsizeP 256: 77785 words/frame = 303 × 256 R 217, so the next header
    sits at packet offset 217. Here: 41 words/frame, 41 mod 11 = 8.

    Stimulus: two same-cycle frames back to back with no padding between
              them, then one `flush_chopper` over 82 words, 400 drain
              cycles; `m_ready` = 1.
    Checks:   residual = 41 mod 11 ≠ 0; ≥ 8 packets; framing of the first
              8; ≥ 10 K28.3 words in their payload; the 6th K28.3 (frame 2
              header) is at concatenated payload index 41; its in-packet
              offset 41 mod 11 equals the residual (8) and is ≠ 0 — the
              discriminator against an end-of-frame short-packet chopper,
              which would give the same packet count but offset 0.
    Note:     pins what an unmarked source gets; the TPG and the pixel
              ingress mark their last word, and test 13 covers that close.
    """
    dut.TESTCASE.value = 12
    await bringup(dut, pix_sel=1)
    ext_meta_set(dut)

    words_per_frame = 25 + EXT_Y_SIZE * (2 + EXT_X_SIZE // 4)
    residual = words_per_frame % DSIZE_P
    assert residual != 0, (
        f"frame ({words_per_frame} words) divides cfg_dsizeP ({DSIZE_P}) "
        f"evenly — pick non-aligned tb constants so the residual case "
        f"actually exercises"
    )

    capture_task = cocotb.start_soon(capture(dut, cycles=SIM_CYCLES))
    await drive_ext_frame_same_cycle_pulse(dut, flush=False)
    await drive_ext_frame_same_cycle_pulse(dut, flush=False)
    # Round to a packet boundary so the last packet actually EOPs;
    # tests only assert about packets fully within the two-frame span.
    await flush_chopper(dut, dsizeP=DSIZE_P,
                        words_emitted=2 * words_per_frame)
    for _ in range(400):
        await RisingEdge(dut.app_clk)
    beats = await capture_task

    pkts = split_packets(beats)
    expected_pkts = (2 * words_per_frame + DSIZE_P - 1) // DSIZE_P
    assert len(pkts) >= expected_pkts, (
        f"expected ≥ {expected_pkts} packets for 2 frames at dsizeP={DSIZE_P}, "
        f"got {len(pkts)}"
    )

    # Walk the concatenated payload of the first ⌈2W/dsizeP⌉ packets.
    sid = STREAMID & 0xFF
    payload: list[tuple[int, int]] = []
    for tag, pkt in enumerate(pkts[:expected_pkts]):
        words, kmasks = check_packet_framing(
            pkt, stream_id=sid, tag=tag & 0xFF, dsizeP=DSIZE_P,
        )
        payload.extend(zip(words, kmasks))

    # K28.3 marker positions in the concatenated payload.  K28.3 carries
    # kmask=0xF + byte 0x7C — distinguishes it from any pixel data.
    marker_idxs = [i for i, (d, k) in enumerate(payload)
                   if k == 0xF and (d & 0xFF) == K28_3]
    # Per frame: 1 image-header K28.3 + Y_SIZE line-marker K28.3 words.
    expected_markers = 2 * (EXT_Y_SIZE + 1)
    assert len(marker_idxs) >= expected_markers, (
        f"expected ≥ {expected_markers} K28.3 markers across 2 frames, "
        f"got {len(marker_idxs)}"
    )

    # Frame 2's image header is the (Y_SIZE+1)th K28.3 in the concatenated
    # payload (frame 1 = 1 hdr + Y_SIZE LMs = Y_SIZE+1 markers, then frame
    # 2's image header).  It must sit at merged-stream-word index =
    # words_per_frame (immediately after frame 1's last pixel).
    frame2_hdr_idx = marker_idxs[EXT_Y_SIZE + 1]
    assert frame2_hdr_idx == words_per_frame, (
        f"frame-2 image header at concatenated idx {frame2_hdr_idx}, "
        f"expected {words_per_frame} (= frame-1 size)"
    )

    # The actual discriminator: that header MUST NOT sit at packet word
    # 0.  Packet boundaries are at multiples of dsizeP in the concatenated
    # payload.  Holdover puts the header at offset = residual; a short-
    # final-packet chopper would put it at offset 0.
    in_pkt_offset = frame2_hdr_idx % DSIZE_P
    assert in_pkt_offset == residual, (
        f"frame-2 image header lands at packet offset {in_pkt_offset}, "
        f"expected {residual} (= frame-1 residual carried into pkt N+1) "
        f"-- chopper closed the packet at end-of-frame instead of holding "
        f"the residual?"
    )
    assert in_pkt_offset != 0, (
        "frame-2 image header is packet-aligned -- holdover NOT exercised "
        "by current tb constants"
    )


# -----------------------------------------------------------------------------
# TC 13 — End-of-frame close
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_eof_closes_short_packet(dut):
    """The image's last pixel word closes a short packet (§8.5.2).

    Guards the end-of-frame close: an image's tail goes out at once in a
    packet whose DsizeP is its own length, and the next image's header
    starts a packet.  Before the close, the 8-word tail of each frame waited
    for the next frame's words and the last one never left.

    Stimulus: two same-cycle frames back to back, the last pixel word of
              each marked with `ext_pix_word_eof`, no padding; 400 drain
              cycles; `m_ready` = 1.
    Checks:   exactly 8 packets (11, 11, 11, 8 per frame) with correct
              framing, tags 0..7 and DsizeP = payload; the payload equals
              `expected_frame_words()` × 2; each frame's image-header K28.3
              is payload word 0 of packets 0 and 4.
    """
    dut.TESTCASE.value = 13
    await bringup(dut, pix_sel=1)
    ext_meta_set(dut)

    words_per_frame = 25 + EXT_Y_SIZE * (2 + EXT_X_SIZE // 4)
    sizes = frame_packet_sizes(words_per_frame)
    assert sizes[-1] != DSIZE_P, "pick tb constants that leave a short tail"

    capture_task = cocotb.start_soon(capture(dut, cycles=SIM_CYCLES // 2))
    await drive_ext_frame_same_cycle_pulse(dut, flush=False, mark_eof=True)
    await drive_ext_frame_same_cycle_pulse(dut, flush=False, mark_eof=True)
    for _ in range(400):
        await RisingEdge(dut.app_clk)
    beats = await capture_task

    pkts = split_packets(beats)
    assert len(pkts) == 2 * len(sizes), (
        f"expected {2 * len(sizes)} packets ({sizes} per frame), got {len(pkts)}"
    )
    sid = STREAMID & 0xFF
    payload: list[tuple[int, int]] = []
    starts = []
    for tag, pkt in enumerate(pkts):
        starts.append(len(payload))
        words, kmasks = check_packet_framing(
            pkt, stream_id=sid, tag=tag, dsizeP=sizes[tag % len(sizes)],
        )
        payload.extend(zip(words, kmasks))
    assert payload == expected_frame_words() * 2, "payload differs from 2 golden frames"
    for p in (0, len(sizes)):
        d, k = payload[starts[p]]
        assert k == 0xF and (d & 0xFF) == K28_3, (
            f"packet {p} does not start with the image header: {d:#010x}/{k:#x}"
        )


# -----------------------------------------------------------------------------
# TC 14 — StreamID of the tail packet
# -----------------------------------------------------------------------------
@cxp_test()
async def test_14_tail_packet_keeps_streamid(dut):
    """An image's last packet carries the image's StreamID (§9.3).

    With store and forward the tail packet leaves after the image's last
    word; a source whose metadata moves on at once (the next image, or none)
    must not change the StreamID of what is still queued.

    Stimulus: ext frame with StreamID 0x05 and `ext_pix_word_eof` on its
              last word; `ext_meta_streamid` set to 0x00 right after that
              word; 400 drain cycles.
    Checks:   4 packets (11, 11, 11, 8) framed with StreamID 0x05.
    """
    dut.TESTCASE.value = 14
    await bringup(dut, pix_sel=1)
    ext_meta_set(dut, streamid=0x05)
    capture_task = cocotb.start_soon(capture(dut, cycles=SIM_CYCLES // 4))
    await drive_ext_frame_same_cycle_pulse(dut, flush=False, mark_eof=True)
    ext_meta_set(dut, streamid=0x00)
    for _ in range(400):
        await RisingEdge(dut.app_clk)
    beats = await capture_task

    sizes = frame_packet_sizes(25 + EXT_Y_SIZE * (2 + EXT_X_SIZE // 4))
    pkts = split_packets(beats)
    assert len(pkts) == len(sizes), f"expected {len(sizes)} packets, got {len(pkts)}"
    for tag, pkt in enumerate(pkts):
        check_packet_framing(pkt, stream_id=0x05, tag=tag, dsizeP=sizes[tag])


# -----------------------------------------------------------------------------
# TC 15 — DsizeP lowered mid-packet
# -----------------------------------------------------------------------------
@cxp_test()
async def test_15_dsizeP_lowered_mid_packet(dut):
    """A DsizeP lower than the open packet's count closes it at once.

    The host may write StreamPacketSizeMax while streaming.  A packet can
    only leave once all of it is in the FIFO, so a chopper that waited for
    its count to come round again would fill the FIFO and stop the stream.

    Stimulus: `cfg_dsizeP` = 11; 3 lines of an ext frame (37 merged words,
              packet 3 open at 4 words); `cfg_dsizeP` = 3; the rest of the
              frame and 7 more frames with no end-of-frame mark (more than
              the 256-word FIFO holds), then one marked frame; 600 drain
              cycles.
    Checks:   every packet is framed with DsizeP = its payload (≤ 11); the
              concatenated payload equals `expected_frame_words()` × 9.
    """
    dut.TESTCASE.value = 15
    await bringup(dut, pix_sel=1)
    ext_meta_set(dut)
    capture_task = cocotb.start_soon(capture(dut, cycles=SIM_CYCLES // 2))

    words = frame_pixel_words()
    # 25 header + (2 marker + 2 pixel) x 2 lines = 33 merged words: packet
    # 3 open at word 0; 1 more line -> 37 words, packet 3 at 4 of 11.
    for data, fs, ls in words[:6]:
        await _push_one_word(dut, data, fs, ls)
    ext_pix_idle(dut)
    for _ in range(40):
        await RisingEdge(dut.app_clk)
    dut.cfg_dsizeP.value = 3
    for data, fs, ls in words[6:]:
        await _push_one_word(dut, data, fs, ls)
    for _ in range(7):
        await drive_ext_frame_same_cycle_pulse(dut, flush=False)
    await drive_ext_frame_same_cycle_pulse(dut, flush=False, mark_eof=True)
    for _ in range(600):
        await RisingEdge(dut.app_clk)
    beats = await capture_task

    sid = STREAMID & 0xFF
    payload: list[tuple[int, int]] = []
    for tag, pkt in enumerate(split_packets(beats)):
        n = len(pkt) - 8
        assert 1 <= n <= DSIZE_P, f"packet {tag}: {n} payload words"
        words_, kmasks = check_packet_framing(pkt, stream_id=sid, tag=tag, dsizeP=n)
        payload.extend(zip(words_, kmasks))
    assert payload == expected_frame_words() * 9, (
        f"payload of {len(payload)} words differs from 9 golden frames"
    )


# -----------------------------------------------------------------------------
# TC 16 — Line markers keep the frame's metadata
# -----------------------------------------------------------------------------
@cxp_test()
async def test_16_line_markers_keep_frame_meta(dut):
    """Every arbitrary line marker carries the image's Xsize / Xoffs (§9.4).

    A pixel source upstream of this module (ingress, packer) still holds
    the last pixels when its metadata moves on to the next image, or to
    none; the line markers of the image in flight must not change with it.

    Stimulus: `cfg_arbitrary` = 1; ext frame 8x4 with Xoffs 4; line 0
              pushed, then `ext_meta_xsize` / `ext_meta_xoffs` set to 0;
              the rest of the frame with `ext_pix_word_eof` on its last
              word; 400 drain cycles.
    Checks:   4 arbitrary line markers, each Xsize 8, Xoffs 4, DsizeL 2.
    """
    dut.TESTCASE.value = 16
    await bringup(dut, pix_sel=1)
    dut.cfg_arbitrary.value = 1
    ext_meta_set(dut, xoffs=4)
    capture_task = cocotb.start_soon(capture(dut, cycles=SIM_CYCLES // 4))

    words = frame_pixel_words()
    per_line = EXT_X_SIZE // 4
    for data, fs, ls in words[:per_line]:
        await _push_one_word(dut, data, fs, ls)
    ext_meta_set(dut, xsize=0, xoffs=0)
    for i, (data, fs, ls) in enumerate(words[per_line:], start=per_line):
        await _push_one_word(dut, data, fs, ls, eof=int(i == len(words) - 1))
    ext_pix_idle(dut)
    for _ in range(400):
        await RisingEdge(dut.app_clk)
    beats = await capture_task

    sid = STREAMID & 0xFF
    payload: list[tuple[int, int]] = []
    for tag, pkt in enumerate(split_packets(beats)):
        words_, kmasks = check_packet_framing(pkt, stream_id=sid, tag=tag, dsizeP=len(pkt) - 8)
        payload.extend(zip(words_, kmasks))

    def byte(i: int) -> int:
        return payload[i][0] & 0xFF

    markers = []
    for i, (d, k) in enumerate(payload):
        if k == 0xF and (d & 0xFF) == K28_3 and i + 10 < len(payload) and byte(i + 1) == LINE_TYPE_ARB:
            markers.append((byte(i + 2) << 16 | byte(i + 3) << 8 | byte(i + 4),
                            byte(i + 5) << 16 | byte(i + 6) << 8 | byte(i + 7),
                            byte(i + 8) << 16 | byte(i + 9) << 8 | byte(i + 10)))
    assert markers == [(EXT_X_SIZE, 4, EXT_X_SIZE // 4)] * EXT_Y_SIZE, (
        f"line markers (Xsize, Xoffs, DsizeL): {markers}"
    )
