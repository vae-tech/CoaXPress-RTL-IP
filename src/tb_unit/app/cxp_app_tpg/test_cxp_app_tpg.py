"""Cocotb TB for `cxp_app_tpg`.

Single-pixel test-pattern source: one pixel per `app_clk` on a valid/ready
bus with SOF/SOL/EOL/EOF flags, plus the image-header metadata (`meta_*`)
of the frame in flight. The TB drives the run-time `cfg_*` inputs on a
10 ns clock through the thin `tb_cxp_app_tpg_top` wrapper (port
rename without `_i`/`_o`, defaults X_SIZE=64, Y_SIZE=32, PIXFMT=0x0101).
`capture_frame` records every handshake from the first SOF to the next EOF,
optionally calling a per-cycle callback in the active region to poke
`cfg_*` / `m_pix_ready` mid-frame; `check_frame` compares every pixel with
a Python mirror of the RTL pattern mux.

Invariant under test: `cfg_xsize` / `cfg_ysize` / `cfg_pixfmt` /
`cfg_testpat` are latched at frame start, so a host write mid-frame must
not corrupt the frame in flight. From SOF through EOF of one frame:
exactly `xsize_q * ysize_q` pixels with the flags at the latched
positions; every pixel equal to the latched pattern (gradient
`(x + y) & 0xFF`, eight resolution-scaled bars, a per-frame flat level,
or the grey bands); `meta_xsize` / `meta_ysize` / `meta_pixfmt` constant
at the latched values (DsizeL is derived downstream from xsize and
pixfmt). The race tests then run
frame N+1 to prove the new values take effect.

Notes: `PIXFMT_MONO16` = 0x0105 (Table 25). The race tests drop `cfg_run`
only after the next frame has started; TC 12 is the one that drops it
mid-frame, and so the one that covers the FSM's stop arc `ST_EMIT →
ST_IDLE`.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  All `cfg_*` = 0 → compile-time defaults: 64×32 Mono8 gradient.
  2  Mid-frame `cfg_xsize` write ignored by frame N, applied on N+1.
  3  Mid-frame `cfg_ysize` write ignored by frame N, applied on N+1.
  4  Mid-frame `cfg_pixfmt` write ignored by frame N, applied on N+1.
  5  Mid-frame xsize + ysize + pixfmt write ignored, all applied on N+1.
  6  `cfg_*` write during an `m_pix_ready` stall ignored by frame N.
  7  `cfg_pixfmt` = 0 falls back to the `PIXFMT` parameter.
  8  Bars pattern: eight bars whose width tracks xsize (32 → 48 px).
  9  Flat pattern: level steps by 16 per completed frame.
 10  Grey-bars pattern: eight 8-pixel bands of the grey ramp.
 11  Mid-frame `cfg_testpat` write ignored by frame N, applied on N+1.
 12  Mid-frame `cfg_run` drop: the frame finishes, then the TPG idles
     until `cfg_run` returns.
 13  SourceTag counts images from 0 and wraps; a `cfg_srctag` change
     presets the next image; offsets latch at frame start.
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly

from cxp_testcase import cxp_test
from fsm_coverage import register_fsm

register_fsm(
    name="test_pattern_gen",
    states=["ST_IDLE", "ST_EMIT"],
    state_path="cxp_app_tpg_i.state_q",
    clk_path="app_clk",
    arcs=[("ST_IDLE", "ST_EMIT"), ("ST_EMIT", "ST_IDLE")],
)


CLK_PERIOD_NS = 10

# PFNC pixel-format codes that the TPG and packer agree on (see
# cxp_app_pixel_packer.sv and src/regmap/genicam/cxp_camera.xml).
PIXFMT_MONO8  = 0x0101
PIXFMT_MONO10 = 0x0102
PIXFMT_MONO12 = 0x0103
PIXFMT_MONO14 = 0x0104
PIXFMT_MONO16 = 0x0105

# TestPattern register / cfg_testpat selector codes — mirror the RTL
# localparams TPG_GRADIENT / TPG_BARS / TPG_FLAT / TPG_GREYBARS.
TPG_GRADIENT = 0
TPG_BARS     = 1
TPG_FLAT     = 2
TPG_GREYBARS = 3

# GREYBARS graduated ramp — the 7 lower band levels; band 7 is 0xFF.
_GREYBAR_LEVELS = (0x10, 0x30, 0x50, 0x70, 0x90, 0xC0, 0xE0)


# -----------------------------------------------------------------------------
# Reference model
# -----------------------------------------------------------------------------
def band_index(x: int, xsize: int) -> int:
    """Column band 0..7 — the line split into exactly 8 equal-width bands
    (band width = xsize // 8).  Mirrors the RTL `band_idx` cascade; a
    line narrower than 8 px collapses every band so the whole line reads
    band 7."""
    bw = xsize // 8
    if bw == 0:
        return 7
    return min(x // bw, 7)


def expected_pixel(x: int, y: int, xsize: int,
                   pattern: int, frame_idx: int) -> int:
    """Mirror the RTL cxp_app_tpg `pix_val` mux."""
    if pattern == TPG_BARS:
        return 0xFF if (band_index(x, xsize) & 1) else 0x00
    if pattern == TPG_FLAT:
        return (frame_idx * 16) & 0xFF
    if pattern == TPG_GREYBARS:
        band = band_index(x, xsize)
        return _GREYBAR_LEVELS[band] if band < 7 else 0xFF
    return (x + y) & 0xFF                        # TPG_GRADIENT


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def reset(dut):
    """Start the clock; hold reset 4 cycles with `cfg_*` = 0, ready = 1."""
    cocotb.start_soon(Clock(dut.app_clk, CLK_PERIOD_NS, unit="ns").start())
    dut.app_rst_n.value   = 0
    dut.cfg_run.value     = 0
    dut.cfg_xsize.value   = 0
    dut.cfg_ysize.value   = 0
    dut.cfg_pixfmt.value  = 0
    dut.cfg_testpat.value = TPG_GRADIENT
    dut.cfg_xoffs.value   = 0
    dut.cfg_yoffs.value   = 0
    dut.cfg_srctag.value  = 0
    dut.m_pix_ready.value = 1
    for _ in range(4):
        await RisingEdge(dut.app_clk)
    dut.app_rst_n.value = 1
    for _ in range(2):
        await RisingEdge(dut.app_clk)


# -----------------------------------------------------------------------------
# Frame capture / checking
# -----------------------------------------------------------------------------
async def capture_frame(dut, *, mid_frame_cb=None,
                        max_cycles: int = 200_000) -> list[dict]:
    """Return the next complete frame (first SOF → EOF) as pixel records.

    Records every `m_pix_valid & m_pix_ready` handshake from the first SOF
    on; pixels of a frame already in flight are skipped.  After each clock
    edge the optional `mid_frame_cb(pixel_count, last_rec_or_None)` runs
    in the active region (between RisingEdge and the next ReadOnly — the
    legal cocotb write window), so it may drive `cfg_*` / `m_pix_ready`.

    Loop structure (the one and only legal pattern in this TB):

      await RisingEdge(clk)   # advance to start of next cycle
      <drive signals>         # writes take effect this cycle
      await ReadOnly()        # sample
      <read signals>          # current-cycle values

    Never nest ReadOnly()s and never write a signal after ReadOnly().
    Raises TimeoutError if no EOF arrives within `max_cycles`.
    """
    pixels: list[dict] = []
    saw_sof = False
    for _ in range(max_cycles):
        await ReadOnly()
        valid = int(dut.m_pix_valid.value)
        ready = int(dut.m_pix_ready.value)
        sof   = int(dut.m_pix_sof.value)
        sol   = int(dut.m_pix_sol.value)
        eol   = int(dut.m_pix_eol.value)
        eof   = int(dut.m_pix_eof.value)
        if valid and ready:
            if not saw_sof and sof:
                saw_sof = True
            if saw_sof:
                rec = {
                    "data":        int(dut.m_pix_data.value),
                    "sof": sof, "sol": sol, "eol": eol, "eof": eof,
                    "meta_xsize":  int(dut.meta_xsize.value),
                    "meta_ysize":  int(dut.meta_ysize.value),
                    "meta_pixfmt": int(dut.meta_pixfmt.value),
                    "meta_xoffs":  int(dut.meta_xoffs.value),
                    "meta_yoffs":  int(dut.meta_yoffs.value),
                    "meta_srctag": int(dut.meta_sourcetag.value),
                }
                pixels.append(rec)
                if eof:
                    await RisingEdge(dut.app_clk)
                    return pixels
        await RisingEdge(dut.app_clk)
        # Active region (between RisingEdge and the next ReadOnly) —
        # legal to drive signals here.  Pass the most recently captured
        # record (or None) so the callback can decide when to fire.
        if mid_frame_cb is not None:
            mid_frame_cb(len(pixels), pixels[-1] if pixels else None)
    raise TimeoutError("capture_frame: no EOF within max_cycles")


def check_frame(pixels, xsize, ysize, pixfmt, *,
                pattern=TPG_GRADIENT, frame_idx=0, label=""):
    """Assert pixel count, flags, data and latched `meta_*` for one frame."""
    tag = f"[{label}] " if label else ""
    assert len(pixels) == xsize * ysize, (
        f"{tag}pixel count {len(pixels)} != xsize*ysize "
        f"({xsize}*{ysize} = {xsize*ysize})")

    for idx, rec in enumerate(pixels):
        y = idx // xsize
        x = idx %  xsize
        exp_sof = 1 if (x == 0 and y == 0)                 else 0
        exp_sol = 1 if (x == 0)                            else 0
        exp_eol = 1 if (x == xsize - 1)                    else 0
        exp_eof = 1 if (x == xsize - 1 and y == ysize - 1) else 0
        assert rec["sof"] == exp_sof, f"{tag}sof[{x},{y}]={rec['sof']} exp {exp_sof}"
        assert rec["sol"] == exp_sol, f"{tag}sol[{x},{y}]={rec['sol']} exp {exp_sol}"
        assert rec["eol"] == exp_eol, f"{tag}eol[{x},{y}]={rec['eol']} exp {exp_eol}"
        assert rec["eof"] == exp_eof, f"{tag}eof[{x},{y}]={rec['eof']} exp {exp_eof}"

        exp_pix = expected_pixel(x, y, xsize, pattern, frame_idx)
        assert rec["data"] == exp_pix, (
            f"{tag}pix[{x},{y}]=0x{rec['data']:02x} exp 0x{exp_pix:02x}")

        assert rec["meta_xsize"]  == xsize,     f"{tag}meta_xsize drift at [{x},{y}]"
        assert rec["meta_ysize"]  == ysize,     f"{tag}meta_ysize drift at [{x},{y}]"
        assert rec["meta_pixfmt"] == pixfmt,    f"{tag}meta_pixfmt drift at [{x},{y}]"


# -----------------------------------------------------------------------------
# TC 1 — Baseline Frame With Default Config
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_baseline_frame(dut):
    """With every `cfg_*` at 0 the TPG streams a full-size default frame.

    Zero size and zero format select the compile-time maxima and
    `p_PIXFMT` (64×32, Mono8 0x0101); also a sanity run of the capture
    harness and a full-size wrap of both pixel counters.

    Stimulus: after `reset()`, `cfg_run` = 1 with `cfg_xsize`, `cfg_ysize`,
              `cfg_pixfmt` = 0, gradient, `m_pix_ready` = 1; one frame is
              captured from its first SOF, then `cfg_run` = 0.
    Checks:   `check_frame` for 64×32 Mono8 gradient — 2048 pixels, SOF/
              SOL/EOL/EOF positions, data `(x + y) & 0xFF`, and on every
              pixel `meta_xsize` = 64, `meta_ysize` = 32, `meta_pixfmt` =
              0x0101.
    """
    dut.TESTCASE.value = 1
    await reset(dut)
    dut.cfg_run.value = 1
    frame = await capture_frame(dut)
    dut.cfg_run.value = 0
    check_frame(frame, xsize=64, ysize=32, pixfmt=PIXFMT_MONO8,
                label="baseline")


# -----------------------------------------------------------------------------
# TC 2 — Mid-Frame cfg_xsize Change Ignored
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_cfg_xsize_change_mid_frame_ignored(dut):
    """A mid-frame `cfg_xsize` write leaves frame N intact and applies on N+1.

    Guards the frame-start latch of `xsize_q`: a host Width write while
    the TPG streams must not change the line length of the frame in
    flight; the free-run restart at the EOF handshake picks it up.

    Stimulus: 8×4 Mono8 gradient, free-run; the callback writes
              `cfg_xsize` = 24 once 12 pixels of frame N have been
              captured; frames N and N+1 are captured back to back.
    Checks:   the poke fired; `check_frame` for frame N at 8×4 (incl.
              `meta_xsize` = 8 on every pixel) and frame N+1 at 24×4
              (`meta_xsize` = 24 on every pixel).
    """
    dut.TESTCASE.value = 2
    await reset(dut)
    dut.cfg_xsize.value  = 8
    dut.cfg_ysize.value  = 4
    dut.cfg_pixfmt.value = PIXFMT_MONO8
    dut.cfg_run.value    = 1

    fired = [False]
    def poke(idx, _rec):
        # After 12 pixels (~mid of a 32-pixel frame) inject the change.
        if not fired[0] and idx >= 12:
            dut.cfg_xsize.value = 24
            fired[0] = True

    frame_n = await capture_frame(dut, mid_frame_cb=poke)
    assert fired[0], "harness bug: cfg_xsize poke never fired"
    check_frame(frame_n, xsize=8, ysize=4, pixfmt=PIXFMT_MONO8,
                label="frame-N cfg_xsize race")

    # Frame N+1 should use the latched-at-next-SOF value.
    frame_next = await capture_frame(dut)
    dut.cfg_run.value = 0
    check_frame(frame_next, xsize=24, ysize=4, pixfmt=PIXFMT_MONO8,
                label="frame-N+1 cfg_xsize=24")


# -----------------------------------------------------------------------------
# TC 3 — Mid-Frame cfg_ysize Change Ignored
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_cfg_ysize_change_mid_frame_ignored(dut):
    """A mid-frame `cfg_ysize` write leaves frame N intact and applies on N+1.

    Guards the `ysize_q` latch: EOF (`last_pix_in_frame`) must use the
    latched height, so frame N still ends after its original line count.

    Stimulus: 8×4 Mono8 gradient, free-run; the callback writes
              `cfg_ysize` = 8 once 16 pixels (two lines) of frame N have
              been captured; frames N and N+1 are captured back to back.
    Checks:   the poke fired; `check_frame` for frame N at 8×4 (exactly 4
              lines, `meta_ysize` = 4) and frame N+1 at 8×8
              (`meta_ysize` = 8).
    """
    dut.TESTCASE.value = 3
    await reset(dut)
    dut.cfg_xsize.value  = 8
    dut.cfg_ysize.value  = 4
    dut.cfg_pixfmt.value = PIXFMT_MONO8
    dut.cfg_run.value    = 1

    fired = [False]
    def poke(idx, _rec):
        if not fired[0] and idx >= 16:
            dut.cfg_ysize.value = 8
            fired[0] = True

    frame_n = await capture_frame(dut, mid_frame_cb=poke)
    assert fired[0], "harness bug: cfg_ysize poke never fired"
    check_frame(frame_n, xsize=8, ysize=4, pixfmt=PIXFMT_MONO8,
                label="frame-N cfg_ysize race")

    frame_next = await capture_frame(dut)
    dut.cfg_run.value = 0
    check_frame(frame_next, xsize=8, ysize=8, pixfmt=PIXFMT_MONO8,
                label="frame-N+1 cfg_ysize=8")


# -----------------------------------------------------------------------------
# TC 4 — Mid-Frame cfg_pixfmt Change Ignored
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_cfg_pixfmt_change_mid_frame_ignored(dut):
    """A mid-line `cfg_pixfmt` write leaves frame N intact and applies on N+1.

    Guards the `pixfmt_q` latch: the header's PixelF and DsizeL of the
    frame in flight must not change; also exercises the 16-bit arm of
    the `cbits_q` decode.

    Stimulus: 8×4 Mono8 gradient, free-run; the callback writes
              `cfg_pixfmt` = `PIXFMT_MONO16` (0x0105) once 6 pixels of
              frame N have been captured; frames N and N+1 captured.
    Checks:   the poke fired; frame N has `meta_pixfmt` = 0x0101 on every
              pixel; frame N+1 has 0x0105; geometry, flags and gradient
              data per
              `check_frame` in both.
    Note:     pixel data stays 8-bit in both frames.
    """
    dut.TESTCASE.value = 4
    await reset(dut)
    dut.cfg_xsize.value  = 8
    dut.cfg_ysize.value  = 4
    dut.cfg_pixfmt.value = PIXFMT_MONO8
    dut.cfg_run.value    = 1

    fired = [False]
    def poke(idx, _rec):
        if not fired[0] and idx >= 6:
            dut.cfg_pixfmt.value = PIXFMT_MONO16
            fired[0] = True

    frame_n = await capture_frame(dut, mid_frame_cb=poke)
    assert fired[0], "harness bug: cfg_pixfmt poke never fired"
    check_frame(frame_n, xsize=8, ysize=4, pixfmt=PIXFMT_MONO8,
                label="frame-N cfg_pixfmt race")

    frame_next = await capture_frame(dut)
    dut.cfg_run.value = 0
    check_frame(frame_next, xsize=8, ysize=4, pixfmt=PIXFMT_MONO16,
                label="frame-N+1 cfg_pixfmt=Mono16")


# -----------------------------------------------------------------------------
# TC 5 — Mid-Frame Change Of All Geometry Inputs Ignored
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_all_cfg_change_mid_frame_ignored(dut):
    """A simultaneous size + format write mid-frame applies atomically on N+1.

    All latched fields must update on the same `load_size` edge, so
    frame N+1 never mixes old and new values; also exercises the 12-bit
    arm of `cbits_q`.

    Stimulus: 8×4 Mono8 gradient, free-run; once 10 pixels of frame N
              have been captured, one callback writes `cfg_xsize` = 16,
              `cfg_ysize` = 2 and `cfg_pixfmt` = Mono12 (0x0103)
              together; frames N and N+1 are captured back to back.
    Checks:   the poke fired; frame N at 8×4 Mono8;
              frame N+1 at 16×2 Mono12, each via
              `check_frame`.
    Note:     "all" means the three geometry/format inputs; `cfg_testpat`
              stays gradient here (covered by TC 11).
    """
    dut.TESTCASE.value = 5
    await reset(dut)
    dut.cfg_xsize.value  = 8
    dut.cfg_ysize.value  = 4
    dut.cfg_pixfmt.value = PIXFMT_MONO8
    dut.cfg_run.value    = 1

    fired = [False]
    def poke(idx, _rec):
        if not fired[0] and idx >= 10:
            dut.cfg_xsize.value  = 16
            dut.cfg_ysize.value  = 2
            dut.cfg_pixfmt.value = PIXFMT_MONO12
            fired[0] = True

    frame_n = await capture_frame(dut, mid_frame_cb=poke)
    assert fired[0], "harness bug: triple-cfg poke never fired"
    check_frame(frame_n, xsize=8, ysize=4, pixfmt=PIXFMT_MONO8,
                label="frame-N triple-race")

    frame_next = await capture_frame(dut)
    dut.cfg_run.value = 0
    check_frame(frame_next, xsize=16, ysize=2, pixfmt=PIXFMT_MONO12,
                label="frame-N+1 triple-change")


# -----------------------------------------------------------------------------
# TC 6 — cfg Change Under Back-Pressure Ignored
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_cfg_change_under_backpressure_ignored(dut):
    """A `cfg_*` write during an `m_pix_ready` stall leaves frame N intact.

    Combines the frame-start latch with consumer back-pressure: `x_q` /
    `y_q` must hold while ready is low, and the stalled frame must resume
    at its latched geometry with no lost or repeated pixel.

    Stimulus: 8×4 Mono8 gradient, free-run; a callback stage machine
              drops `m_pix_ready` once 10 pixels are captured, two cycles
              later writes `cfg_xsize` = 32, `cfg_ysize` = 1, `cfg_pixfmt`
              = 0x0105, and re-asserts ready two cycles after that (about
              4 stalled cycles); frames N and N+1 captured back to back.
    Checks:   the stage machine completed; `check_frame` for frame N at
              8×4 Mono8 (every pixel index-exact) and for frame N+1 at
              32×1 0x0105.
    Note:     only handshakes are recorded, so data/flag stability while
              stalled is not checked; the write lands mid-frame like TC
              2–5, so the stall adds no extra latch evidence.
    """
    dut.TESTCASE.value = 6
    await reset(dut)
    dut.cfg_xsize.value  = 8
    dut.cfg_ysize.value  = 4
    dut.cfg_pixfmt.value = PIXFMT_MONO8
    dut.cfg_run.value    = 1

    # State machine: count handshakes, drop ready around idx=10, write
    # cfg_* while stalled, then re-assert ready.  The callback is the
    # only place we touch signals — capture_frame runs the legal
    # RisingEdge → ReadOnly cadence around us.
    state = {"stage": 0, "stall_started": 0}

    def poke(idx, _rec):
        s = state
        if s["stage"] == 0 and idx >= 10:
            dut.m_pix_ready.value = 0
            s["stage"]         = 1
            s["stall_started"] = idx
        elif s["stage"] == 1:
            # Second stalled cycle; cfg_* is written on the next one.
            s["stage"] = 2
        elif s["stage"] == 2:
            dut.cfg_xsize.value  = 32
            dut.cfg_ysize.value  = 1
            dut.cfg_pixfmt.value = PIXFMT_MONO16
            s["stage"] = 3
        elif s["stage"] == 3:
            # One more stalled cycle (~4 in total) before ready returns.
            s["stage"] = 4
        elif s["stage"] == 4:
            dut.m_pix_ready.value = 1
            s["stage"] = 5

    frame_n = await capture_frame(dut, mid_frame_cb=poke)
    assert state["stage"] >= 5, (
        f"harness bug: backpressure sequence did not complete (stage={state['stage']})")
    check_frame(frame_n, xsize=8, ysize=4, pixfmt=PIXFMT_MONO8,
                label="frame-N backpressured cfg-race")

    frame_next = await capture_frame(dut)
    dut.cfg_run.value = 0
    check_frame(frame_next, xsize=32, ysize=1, pixfmt=PIXFMT_MONO16,
                label="frame-N+1 after backpressured write")


# -----------------------------------------------------------------------------
# TC 7 — cfg_pixfmt = 0 Falls Back To Parameter
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_cfg_pixfmt_zero_falls_back_to_parameter(dut):
    """`cfg_pixfmt` = 0 selects the `PIXFMT` parameter, not a PixelF of 0.

    Guards the `pixfmt_eff` fallback with a non-default geometry, so the
    header never announces pixel format 0.

    Stimulus: `cfg_xsize` = 8, `cfg_ysize` = 2, `cfg_pixfmt` = 0, gradient,
              run; one frame captured, then `cfg_run` = 0.
    Checks:   `check_frame` for 8×2 with `meta_pixfmt` = 0x0101 (Mono8)
              on every pixel.
    Note:     the same fallback is already exercised by TC 1.
    """
    dut.TESTCASE.value = 7
    await reset(dut)
    dut.cfg_xsize.value  = 8
    dut.cfg_ysize.value  = 2
    dut.cfg_pixfmt.value = 0
    dut.cfg_run.value    = 1
    frame = await capture_frame(dut)
    dut.cfg_run.value = 0
    check_frame(frame, xsize=8, ysize=2, pixfmt=PIXFMT_MONO8,
                label="cfg_pixfmt=0 fallback")


# -----------------------------------------------------------------------------
# TC 8 — Bars Pattern Scales With Resolution
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_bars_pattern(dut):
    """The Bars pattern always shows 8 bars whose width is xsize / 8.

    `cfg_testpat` = Bars alternates 0x00 / 0xFF across eight equal bands;
    checked at two resolutions to prove the bar count stays 8 and the
    width tracks the latched xsize (also a mid-frame xsize latch check).

    Stimulus: Bars at 32×4 Mono8, free-run; the callback writes
              `cfg_xsize` = 48 once 8 pixels of frame A have been
              captured; frames A and B are captured back to back.
    Checks:   `check_frame` with the Bars mirror — frame A at 32 px
              (eight 4-pixel bars), frame B at 48 px (eight 6-pixel
              bars), plus flags and `meta_*`.
    Note:     both widths are multiples of 8, so the remainder-widened
              band 7 and the xsize < 8 collapse are not exercised.
    """
    dut.TESTCASE.value = 8
    await reset(dut)
    dut.cfg_xsize.value   = 32
    dut.cfg_ysize.value   = 4
    dut.cfg_pixfmt.value  = PIXFMT_MONO8
    dut.cfg_testpat.value = TPG_BARS
    dut.cfg_run.value     = 1

    # Frame N at 32 px ⇒ eight 4-pixel bars; mid-frame bump xsize to 48.
    fired = [False]
    def poke(idx, _rec):
        if not fired[0] and idx >= 8:
            dut.cfg_xsize.value = 48
            fired[0] = True

    frame_a = await capture_frame(dut, mid_frame_cb=poke)
    assert fired[0], "harness bug: xsize poke never fired"
    check_frame(frame_a, xsize=32, ysize=4, pixfmt=PIXFMT_MONO8,
                pattern=TPG_BARS, label="bars xsize=32")

    # Frame N+1 at 48 px ⇒ eight 6-pixel bars — the count stays 8.
    frame_b = await capture_frame(dut)
    dut.cfg_run.value = 0
    check_frame(frame_b, xsize=48, ysize=4, pixfmt=PIXFMT_MONO8,
                pattern=TPG_BARS, label="bars xsize=48")


# -----------------------------------------------------------------------------
# TC 9 — Flat Pattern Steps Per Frame
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_flat_pattern(dut):
    """The Flat pattern is a uniform field whose level steps 16 per frame.

    `frame_cnt_q` must advance once per EOF handshake and the flat arm
    must present `frame_cnt_q[3:0] * 16` on every pixel.

    Stimulus: Flat at 8×4 Mono8, free-run from reset; three back-to-back
              frames captured, then `cfg_run` = 0.
    Checks:   `check_frame` per frame with every pixel equal to 0x00,
              0x10 and 0x20 for frames 0, 1, 2, plus flags and `meta_*`.
    Note:     the 16-frame wrap of the level is not reached.
    """
    dut.TESTCASE.value = 9
    await reset(dut)
    dut.cfg_xsize.value   = 8
    dut.cfg_ysize.value   = 4
    dut.cfg_pixfmt.value  = PIXFMT_MONO8
    dut.cfg_testpat.value = TPG_FLAT
    dut.cfg_run.value     = 1
    for k in range(3):
        frame = await capture_frame(dut)
        check_frame(frame, xsize=8, ysize=4, pixfmt=PIXFMT_MONO8,
                    pattern=TPG_FLAT, frame_idx=k, label=f"flat frame {k}")
    dut.cfg_run.value = 0


# -----------------------------------------------------------------------------
# TC 10 — Grey-Bars Pattern
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_greybars_pattern(dut):
    """The GreyBars pattern shows eight equal bands of the grey ramp.

    Checks the `greybar_val` table and its band mapping: band width =
    xsize / 8, levels 0x10, 0x30, 0x50, 0x70, 0x90, 0xC0, 0xE0, 0xFF.

    Stimulus: GreyBars at 64×4 Mono8; one frame captured, then
              `cfg_run` = 0.
    Checks:   `check_frame` with the GreyBars mirror — eight 8-pixel bands
              at the eight levels on every line, plus flags and `meta_*`.
    """
    dut.TESTCASE.value = 10
    await reset(dut)
    dut.cfg_xsize.value   = 64
    dut.cfg_ysize.value   = 4
    dut.cfg_pixfmt.value  = PIXFMT_MONO8
    dut.cfg_testpat.value = TPG_GREYBARS
    dut.cfg_run.value     = 1
    frame = await capture_frame(dut)
    dut.cfg_run.value = 0
    check_frame(frame, xsize=64, ysize=4, pixfmt=PIXFMT_MONO8,
                pattern=TPG_GREYBARS, label="greybars")


# -----------------------------------------------------------------------------
# TC 11 — Mid-Frame cfg_testpat Change Ignored
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_cfg_testpat_change_mid_frame_ignored(dut):
    """A mid-frame TestPattern write leaves frame N intact and applies on N+1.

    Guards the `testpat_q` latch: switching the pattern mid-frame must not
    splice two images into one frame.

    Stimulus: Gradient at 16×4 Mono8, free-run; the callback writes
              `cfg_testpat` = Bars once 8 pixels of frame N have been
              captured; frames N and N+1 are captured back to back.
    Checks:   the poke fired; `check_frame` for frame N as a gradient and
              frame N+1 as Bars (eight 2-pixel bars), both 16×4.
    """
    dut.TESTCASE.value = 11
    await reset(dut)
    dut.cfg_xsize.value   = 16
    dut.cfg_ysize.value   = 4
    dut.cfg_pixfmt.value  = PIXFMT_MONO8
    dut.cfg_testpat.value = TPG_GRADIENT
    dut.cfg_run.value     = 1

    fired = [False]
    def poke(idx, _rec):
        if not fired[0] and idx >= 8:
            dut.cfg_testpat.value = TPG_BARS
            fired[0] = True

    frame_n = await capture_frame(dut, mid_frame_cb=poke)
    assert fired[0], "harness bug: cfg_testpat poke never fired"
    check_frame(frame_n, xsize=16, ysize=4, pixfmt=PIXFMT_MONO8,
                pattern=TPG_GRADIENT, label="frame-N testpat race")

    frame_next = await capture_frame(dut)
    dut.cfg_run.value = 0
    check_frame(frame_next, xsize=16, ysize=4, pixfmt=PIXFMT_MONO8,
                pattern=TPG_BARS, label="frame-N+1 testpat=Bars")


# -----------------------------------------------------------------------------
# TC 12 — Stop Finishes The Frame In Flight
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_stop_at_frame_end(dut):
    """Clearing `cfg_run` mid-frame ends the frame, then stops.

    An AcquisitionStop must not tear a frame in half: the downstream
    packetiser has already sent the image header for it, so the TPG runs
    the frame in flight to its EOF and only then leaves ST_EMIT.  This is
    the FSM's stop arc; every other test keeps `cfg_run` up across the
    frame boundary.

    Stimulus: gradient at 16×4 Mono8, free-run; the callback clears
              `cfg_run` once 8 of the 64 pixels of the frame are
              captured.
    Checks:   the poke fired; the frame still completes whole (64 pixels,
              flags and data intact through EOF); no further pixel is
              offered for 200 cycles after it; raising `cfg_run` again
              starts a fresh frame.
    """
    dut.TESTCASE.value = 12
    await reset(dut)
    dut.cfg_xsize.value   = 16
    dut.cfg_ysize.value   = 4
    dut.cfg_pixfmt.value  = PIXFMT_MONO8
    dut.cfg_testpat.value = TPG_GRADIENT
    dut.cfg_run.value     = 1

    fired = [False]
    def poke(idx, _rec):
        if not fired[0] and idx >= 8:
            dut.cfg_run.value = 0
            fired[0] = True

    frame = await capture_frame(dut, mid_frame_cb=poke)
    assert fired[0], "harness bug: cfg_run drop never fired"
    check_frame(frame, xsize=16, ysize=4, pixfmt=PIXFMT_MONO8,
                pattern=TPG_GRADIENT, label="frame stopped mid-flight")

    for _ in range(200):
        await ReadOnly()
        assert int(dut.m_pix_valid.value) == 0, "pixel offered after the stop"
        await RisingEdge(dut.app_clk)

    dut.cfg_run.value = 1
    restart = await capture_frame(dut)
    dut.cfg_run.value = 0
    check_frame(restart, xsize=16, ysize=4, pixfmt=PIXFMT_MONO8,
                pattern=TPG_GRADIENT, label="frame after restart")


# -----------------------------------------------------------------------------
# TC 13 — SourceTag Per Image, Offsets Per Frame
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_sourcetag_and_offsets(dut):
    """SourceTag is incremented for each image (Table 38); offsets latch.

    The first image after reset carries SourceTag 0 and each following
    image one more.  A host write that changes the SourceTag register
    (`cfg_srctag`) presets the tag of the next image, from where the count
    goes on and wraps 0xFFFF -> 0x0000.  OffsetX / OffsetY reach the
    header like the size: latched at frame start.

    Stimulus: 4x2 Mono8 free-run.  Three frames at rest; then, mid-frame,
              `cfg_srctag` = 0xFFFE with offsets 12 / 6; three more
              frames.
    Checks:   tags 0, 1, 2; the frame in flight at the write keeps its tag
              and offsets 0 / 0; the next three frames carry 0xFFFE,
              0xFFFF, 0x0000 and offsets 12 / 6; every pixel of a frame
              carries the same tag and offsets.
    """
    dut.TESTCASE.value = 13
    await reset(dut)
    dut.cfg_xsize.value  = 4
    dut.cfg_ysize.value  = 2
    dut.cfg_pixfmt.value = PIXFMT_MONO8
    dut.cfg_run.value    = 1

    def meta_of(frame, label):
        keys = ("meta_srctag", "meta_xoffs", "meta_yoffs")
        first = tuple(frame[0][k] for k in keys)
        for rec in frame:
            assert tuple(rec[k] for k in keys) == first, f"{label}: metadata drifts inside the frame"
        return first

    got = [meta_of(await capture_frame(dut), f"frame {i}") for i in range(3)]
    assert got == [(0, 0, 0), (1, 0, 0), (2, 0, 0)], f"tags at rest: {got}"

    fired = [False]
    def poke(idx, _rec):
        if not fired[0] and idx >= 3:
            dut.cfg_srctag.value = 0xFFFE
            dut.cfg_xoffs.value  = 12
            dut.cfg_yoffs.value  = 6
            fired[0] = True

    in_flight = meta_of(await capture_frame(dut, mid_frame_cb=poke), "frame at the write")
    assert fired[0], "harness bug: preset never fired"
    assert in_flight == (3, 0, 0), f"frame in flight changed: {in_flight}"
    got = [meta_of(await capture_frame(dut), f"frame {i} after preset") for i in range(3)]
    dut.cfg_run.value = 0
    assert got == [(0xFFFE, 12, 6), (0xFFFF, 12, 6), (0x0000, 12, 6)], f"after preset: {got}"
