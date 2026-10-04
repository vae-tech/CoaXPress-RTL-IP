"""Cocotb TB for `cxp_app_pixel_ingress`.

Sensor-side single-pixel adapter (modules doc §2.1): one pixel per cycle
in, one out through a 1-entry output register, with a derived SOL flag,
SOF-based frame gating (stray pre-SOF beats dropped, a stray EOF pulses
`spurious_eof`) and a per-frame latch of the six geometry fields that feed
the Table 38 image header. The TB runs a 10 ns `app_clk` through the thin
`tb_cxp_app_pixel_ingress_top` wrapper (PIX_W = 16, ports without `_i`/`_o`).

`drive_frame` presents one beat per cycle honouring `s_pix_ready` (with
optional random valid gaps) and drives the frame's geometry on every
beat; `capture` samples every accepted `m_pix_*` beat (optionally with
random `m_pix_ready`); `check_frame` compares the captured beats with the
`(x + y) & 0xFF` frame model, flags included. Metadata is watched by small
per-test coroutines sampling `m_meta_*` on `m_meta_valid`. Tests follow
the verification plan of `docs/design/cxp_camera_ip_modules.md` §2.1 ("plan
item N"), with its 1024×768 frame scaled to 8×4. No FSM is registered.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Valid 8×4 frame ingest — bit-exact order, flags, metadata latched.
  2  Random back-pressure on both sides — no pixel loss, order kept.
  3  EOL/EOF without SOF — strays dropped, `spurious_eof` pulsed, next
     frame clean.
  4  Two back-to-back frames — metadata captured once per SOF.
  5  Reset mid-frame — outputs cleared, next frame clean.
  6  SOF inside an open frame (a sensor cut off mid-image) — reported on
     `sof_restart`, the new frame starts with SOF and SOL.
  7  The cut frame ends: its last pixel leaves with EOL and EOF; a pixel
     waits for its successor, an EOF pixel does not; a one-pixel cut
     frame keeps its own metadata.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from cxp_testcase import cxp_test


CLK_PERIOD_NS = 10

# Default frame geometry (the plan's 1024×768 scaled to 8×4).
GEOM = dict(xoffs=0x10, yoffs=0x20, xsize=8, ysize=4, pixfmt=0x0101, tapg=0x0000,
            streamid=0x5A, sourcetag=0xBEEF, flags=0x81)


@dataclass(frozen=True)
class OutBeat:
    """One captured output beat (data + boundary flags)."""
    data: int
    sol:  int
    eol:  int
    sof:  int
    eof:  int


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def bringup(dut, period_ns: int = CLK_PERIOD_NS):
    """Start the clock and apply the initial reset."""
    cocotb.start_soon(Clock(dut.app_clk, period_ns, unit="ns").start(start_high=False))
    await do_reset(dut)


async def do_reset(dut, cycles: int = 4):
    """Drive every input to 0, hold `app_rst_n` low `cycles` edges, release."""
    dut.app_rst_n.value    = 0
    dut.s_pix_data.value   = 0
    dut.s_pix_valid.value  = 0
    dut.s_pix_sof.value    = 0
    dut.s_pix_eol.value    = 0
    dut.s_pix_eof.value    = 0
    dut.s_pix_xoffs.value  = 0
    dut.s_pix_yoffs.value  = 0
    dut.s_pix_xsize.value  = 0
    dut.s_pix_ysize.value  = 0
    dut.s_pix_pixfmt.value = 0
    dut.s_pix_tapg.value   = 0
    dut.s_pix_streamid.value  = 0
    dut.s_pix_sourcetag.value = 0
    dut.s_pix_flags.value     = 0
    dut.m_pix_ready.value  = 0
    for _ in range(cycles):
        await RisingEdge(dut.app_clk)
    dut.app_rst_n.value = 1
    await RisingEdge(dut.app_clk)


# -----------------------------------------------------------------------------
# Stimulus / capture / checking
# -----------------------------------------------------------------------------
def make_frame(xsize: int, ysize: int):
    """Pixel value model: pix(x, y) = (x + y) & 0xFF."""
    return [[(x + y) & 0xFF for x in range(xsize)] for y in range(ysize)]


async def drive_frame(dut, frame, *, geom, ready_gap=0):
    """Drive one frame pixel-by-pixel honouring s_pix_ready back-pressure.

    `geom` = dict(xoffs, yoffs, xsize, ysize, pixfmt, tapg) presented on
    every beat of the frame.  `ready_gap` randomly de-asserts s_pix_valid.
    """
    ysize = len(frame)
    xsize = len(frame[0])
    for y in range(ysize):
        for x in range(xsize):
            sof = (x == 0 and y == 0)
            eol = (x == xsize - 1)
            eof = (eol and y == ysize - 1)

            if ready_gap and random.random() < 0.3:
                dut.s_pix_valid.value = 0
                for _ in range(random.randint(1, 3)):
                    await RisingEdge(dut.app_clk)

            dut.s_pix_data.value   = frame[y][x]
            dut.s_pix_valid.value  = 1
            dut.s_pix_sof.value    = int(sof)
            dut.s_pix_eol.value    = int(eol)
            dut.s_pix_eof.value    = int(eof)
            dut.s_pix_xoffs.value  = geom["xoffs"]
            dut.s_pix_yoffs.value  = geom["yoffs"]
            dut.s_pix_xsize.value  = geom["xsize"]
            dut.s_pix_ysize.value  = geom["ysize"]
            dut.s_pix_pixfmt.value = geom["pixfmt"]
            dut.s_pix_tapg.value   = geom["tapg"]
            dut.s_pix_streamid.value  = geom.get("streamid", 0)
            dut.s_pix_sourcetag.value = geom.get("sourcetag", 0)
            dut.s_pix_flags.value     = geom.get("flags", 0)

            # Wait for the handshake (skid has room).
            await RisingEdge(dut.app_clk)
            while dut.s_pix_ready.value == 0:
                await RisingEdge(dut.app_clk)
    dut.s_pix_valid.value = 0
    dut.s_pix_sof.value   = 0
    dut.s_pix_eol.value   = 0
    dut.s_pix_eof.value   = 0


async def capture(dut, out, *, ready_random=False, stop_evt=None):
    """Sample m_pix_* on every accepted output beat until stop_evt is set."""
    while True:
        if ready_random:
            dut.m_pix_ready.value = random.randint(0, 1)
        else:
            dut.m_pix_ready.value = 1
        await RisingEdge(dut.app_clk)
        if dut.m_pix_valid.value == 1 and dut.m_pix_ready.value == 1:
            out.append(OutBeat(
                data=int(dut.m_pix_data.value),
                sol=int(dut.m_pix_sol.value),
                eol=int(dut.m_pix_eol.value),
                sof=int(dut.m_pix_sof.value),
                eof=int(dut.m_pix_eof.value),
            ))
        if stop_evt is not None and stop_evt.is_set() and dut.m_pix_valid.value == 0:
            return


def check_frame(out, frame):
    """Verify the captured beats reconstruct the frame in order with the
    expected SOL/EOL/SOF/EOF boundary flags."""
    ysize = len(frame)
    xsize = len(frame[0])
    expect = []
    for y in range(ysize):
        for x in range(xsize):
            expect.append(OutBeat(
                data=frame[y][x],
                sol=int(x == 0),
                eol=int(x == xsize - 1),
                sof=int(x == 0 and y == 0),
                eof=int(x == xsize - 1 and y == ysize - 1),
            ))
    assert len(out) == len(expect), f"beat count {len(out)} != {len(expect)}"
    for i, (g, e) in enumerate(zip(out, expect)):
        assert g == e, f"beat {i}: got {g}, expected {e}"


# -----------------------------------------------------------------------------
# TC 1 — Valid Frame Ingest And Metadata Latch
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_valid_frame_ingest(dut):
    """A clean frame passes bit-exact and its geometry is latched at SOF.

    The basic accept path: frame gate opened by SOF, SOL derived on the
    SOF pixel and after every EOL, EOF closing the frame, and the six
    `m_meta_*` fields captured on the accepted SOF beat (plan item 1).

    Stimulus: GEOM (xoffs 0x10, yoffs 0x20, 8×4, pixfmt 0x0101, tapg 0) on
              every beat of an 8×4 frame, one beat per cycle; `m_pix_ready`
              = 1 throughout; a watcher records all six `m_meta_*` fields
              whenever `m_meta_valid` = 1; capture stops 8 cycles after the
              last beat.
    Checks:   `check_frame` — 32 beats, data/sol/eol/sof/eof each equal to
              the model; the recorded metadata equals GEOM.
    Note:     the watcher keeps only the last `m_meta_valid` sample, and the
              `m_meta_valid` timing against `m_pix_sof` is not checked.
    """
    dut.TESTCASE.value = 1
    await bringup(dut)
    frame = make_frame(GEOM["xsize"], GEOM["ysize"])

    out = []
    stop = cocotb.triggers.Event()
    cap = cocotb.start_soon(capture(dut, out, stop_evt=stop))

    # Watch the metadata latch.
    meta_seen = {}

    async def meta_watch():
        while not stop.is_set():
            await RisingEdge(dut.app_clk)
            if dut.m_meta_valid.value == 1:
                meta_seen.update(
                    xoffs=int(dut.m_meta_xoffs.value),
                    yoffs=int(dut.m_meta_yoffs.value),
                    xsize=int(dut.m_meta_xsize.value),
                    ysize=int(dut.m_meta_ysize.value),
                    pixfmt=int(dut.m_meta_pixfmt.value),
                    tapg=int(dut.m_meta_tapg.value),
                    streamid=int(dut.m_meta_streamid.value),
                    sourcetag=int(dut.m_meta_sourcetag.value),
                    flags=int(dut.m_meta_flags.value),
                )
    mw = cocotb.start_soon(meta_watch())

    await drive_frame(dut, frame, geom=GEOM)
    for _ in range(8):
        await RisingEdge(dut.app_clk)
    stop.set()
    await cap
    await mw

    check_frame(out, frame)
    assert meta_seen == dict(
        xoffs=GEOM["xoffs"], yoffs=GEOM["yoffs"],
        xsize=GEOM["xsize"], ysize=GEOM["ysize"],
        pixfmt=GEOM["pixfmt"], tapg=GEOM["tapg"],
        streamid=GEOM["streamid"], sourcetag=GEOM["sourcetag"], flags=GEOM["flags"],
    ), f"metadata latch mismatch: {meta_seen}"


# -----------------------------------------------------------------------------
# TC 2 — Back-Pressure Without Pixel Loss
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_backpressure_no_loss(dut):
    """Random gaps on both sides lose, duplicate or reorder no pixel.

    Exercises the 1-entry output register under stall (`s_pix_ready` =
    `~buf_valid_q | m_pix_ready`), including same-cycle dequeue + enqueue
    (plan item 2).

    Stimulus: `random.seed(2)`; 8×4 frame with GEOM; before each beat, with
              probability 0.3, `s_pix_valid` drops for 1–3 cycles; the
              capture task re-randomises `m_pix_ready` (0/1) every cycle,
              also during the 20 drain cycles after the driver finishes.
    Checks:   `check_frame` — 32 beats in order with exact data and flags.
    Note:     `m_pix_ready` is a fair coin per cycle, so stalls are short;
              an indefinite `m_pix_ready` = 0 hold is not exercised.
    """
    dut.TESTCASE.value = 2
    random.seed(2)
    await bringup(dut)
    frame = make_frame(8, 4)

    out = []
    stop = cocotb.triggers.Event()
    cap = cocotb.start_soon(capture(dut, out, ready_random=True, stop_evt=stop))
    await drive_frame(dut, frame, geom=GEOM, ready_gap=1)
    dut.m_pix_ready.value = 1
    for _ in range(20):
        await RisingEdge(dut.app_clk)
    stop.set()
    await cap
    check_frame(out, frame)


# -----------------------------------------------------------------------------
# TC 3 — EOL/EOF Without SOF Dropped And Flagged
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_spurious_eof_dropped(dut):
    """Beats before any SOF are dropped and a stray EOF pulses `spurious_eof`.

    Frame gating (`pix_is_real`) must discard pixels of a frame whose SOF
    was missed, flag the stray EOF, and leave the next real frame clean
    (plan item 3).

    Stimulus: `m_pix_ready` = 1; three consecutive valid beats with SOF =
              0, data 0xAB and EOF = 0, 1, 0; a watcher samples
              `spurious_eof` for 12 cycles from the first stray; then an
              8×2 frame (GEOM geometry) is driven and captured until 8
              cycles after its last beat.
    Checks:   `spurious_eof` seen at least once in the window; `m_pix_valid`
              = 0 at the end of the window; `check_frame` for the 8×2
              frame (16 beats) and its first beat carries SOF.
    Note:     the `m_pix_valid` check is one sample ~9 cycles after the
              last stray with ready = 1, so a one-cycle leak would already
              have drained; `spurious_eof` staying 0 on the non-EOF strays
              and idle `m_meta_*` during the strays are not checked.
    """
    dut.TESTCASE.value = 3
    await bringup(dut)
    dut.m_pix_ready.value = 1

    saw_spurious = False

    async def flag_watch():
        nonlocal saw_spurious
        for _ in range(12):
            await RisingEdge(dut.app_clk)
            if dut.spurious_eof.value == 1:
                saw_spurious = True
    fw = cocotb.start_soon(flag_watch())

    # Stray pixels with no SOF — including a spurious EOF.
    for eof in (0, 1, 0):
        dut.s_pix_data.value  = 0xAB
        dut.s_pix_valid.value = 1
        dut.s_pix_sof.value   = 0
        dut.s_pix_eol.value   = 0
        dut.s_pix_eof.value   = eof
        await RisingEdge(dut.app_clk)
    dut.s_pix_valid.value = 0
    await fw
    assert saw_spurious, "spurious_eof never asserted for pre-SOF EOF"

    # No output may have been produced for the stray pixels.
    assert dut.m_pix_valid.value == 0, "stray pixel leaked to output"

    # A proper frame afterwards must still be clean.
    out = []
    stop = cocotb.triggers.Event()
    cap = cocotb.start_soon(capture(dut, out, stop_evt=stop))
    frame = make_frame(8, 2)
    await drive_frame(dut, frame, geom=GEOM)
    for _ in range(8):
        await RisingEdge(dut.app_clk)
    stop.set()
    await cap
    check_frame(out, frame)
    assert out[0].sof == 1, "first beat after stray pixels not SOF"


# -----------------------------------------------------------------------------
# TC 4 — Metadata Captured Once Per SOF
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_meta_change_midframe(dut):
    """Metadata is captured once per accepted SOF, from that SOF's beat.

    Two back-to-back frames with different geometry must produce exactly
    two `m_meta_valid` captures, each with its own frame's values; the EOF
    beat followed directly by a SOF beat must re-open the gate (plan
    item 4).

    Stimulus: `m_pix_ready` = 1; g1 = (xoffs 1, yoffs 2, 4×2, pixfmt
              0x0101, tapg 0), g2 = (xoffs 9, yoffs 8, 4×2, pixfmt 0x0103,
              tapg 1); frame f1 driven with g1, then f2 with g2 with no
              idle beat between them; a watcher records `m_meta_xoffs` at
              every `m_meta_valid` pulse for 80 cycles.
    Checks:   the recorded list is exactly [1, 9].
    Note:     `drive_frame` holds each frame's geometry on every beat, so
              the inputs switch g1 → g2 exactly on f2's SOF beat and never
              change mid-frame; a non-holding (transparent) latch also
              passes. Only xoffs is compared.
    """
    dut.TESTCASE.value = 4
    await bringup(dut)
    dut.m_pix_ready.value = 1

    g1 = dict(xoffs=1, yoffs=2, xsize=4, ysize=2, pixfmt=0x0101, tapg=0)
    g2 = dict(xoffs=9, yoffs=8, xsize=4, ysize=2, pixfmt=0x0103, tapg=1)

    latched = []

    async def meta_watch():
        for _ in range(80):
            await RisingEdge(dut.app_clk)
            if dut.m_meta_valid.value == 1:
                latched.append(int(dut.m_meta_xoffs.value))
    mw = cocotb.start_soon(meta_watch())

    f1 = make_frame(4, 2)
    f2 = make_frame(4, 2)
    # Drive frame 1 with g1, then frame 2 with g2 back to back.  The
    # geometry inputs switch to g2 on frame 2's SOF beat; the latch must
    # capture g1 on frame 1's SOF and g2 on frame 2's SOF.
    await drive_frame(dut, f1, geom=g1)
    await drive_frame(dut, f2, geom=g2)
    for _ in range(8):
        await RisingEdge(dut.app_clk)
    await mw

    assert latched == [g1["xoffs"], g2["xoffs"]], (
        f"metadata latched on wrong SOFs: {latched}")


# -----------------------------------------------------------------------------
# TC 5 — Reset Mid-Frame Returns To Safe State
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_reset_midframe(dut):
    """Reset mid-frame clears the outputs and the next frame arrives clean.

    An asynchronous reset in the middle of a frame must drop the buffered
    pixel and the metadata strobe, and the first frame after reset must
    start with a clean SOF (plan item 5).

    Stimulus: `m_pix_ready` = 1; an 8×4 frame driver is started and killed
              after 10 clock edges (about 10 beats accepted: line 0 and the
              start of line 1); `do_reset` drives all inputs and
              `m_pix_ready` to 0, holds `app_rst_n` low 4 cycles and
              releases it; then `m_pix_ready` = 1 and the same 8×4 frame is
              driven from a fresh SOF and captured.
    Checks:   `m_pix_valid` = 0 and `m_meta_valid` = 0 right after reset;
              `check_frame` — 32 beats exact; the first beat has SOF and
              SOL.
    Note:     the reset of `in_frame_q` / `pending_sol_q` is not
              observable here — a SOF beat is accepted and gets SOL either
              way. `task.kill()` warns as deprecated under cocotb 2.0.
    """
    dut.TESTCASE.value = 5
    await bringup(dut)
    dut.m_pix_ready.value = 1

    frame = make_frame(8, 4)
    # Drive ~10 beats (line 0 and the start of line 1), then yank reset.
    drv = cocotb.start_soon(drive_frame(dut, frame, geom=GEOM))
    for _ in range(10):
        await RisingEdge(dut.app_clk)
    drv.kill()
    await do_reset(dut)

    assert dut.m_pix_valid.value == 0, "m_pix_valid not cleared by reset"
    assert dut.m_meta_valid.value == 0, "m_meta_valid not cleared by reset"

    # A clean frame after reset must be intact.
    dut.m_pix_ready.value = 1
    out = []
    stop = cocotb.triggers.Event()
    cap = cocotb.start_soon(capture(dut, out, stop_evt=stop))
    await drive_frame(dut, frame, geom=GEOM)
    for _ in range(8):
        await RisingEdge(dut.app_clk)
    stop.set()
    await cap
    check_frame(out, frame)
    assert out[0].sof == 1 and out[0].sol == 1, "post-reset frame SOF unclean"


# -----------------------------------------------------------------------------
# TC 6 — SOF Inside An Open Frame
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_sof_restart(dut):
    """A frame cut off mid-image is reported, and the next one starts clean.

    §9.4.2: every image starts with its header and SOF; a sensor that cuts
    an image short breaks that image, and the device has to say so.

    Stimulus: the first 5 pixels of an 8 x 4 frame (no EOL, no EOF), then a
              whole 8 x 4 frame; `m_pix_ready` = 1.
    Checks:   `sof_restart` pulses once, on the second SOF; the second
              frame's 32 pixels come out in order, its first with SOF and
              SOL, its last with EOF.
    """
    dut.TESTCASE.value = 6
    await bringup(dut)
    dut.m_pix_ready.value = 1
    out, restarts = [], []
    stop = cocotb.triggers.Event()
    cap = cocotb.start_soon(capture(dut, out, stop_evt=stop))

    async def count():
        while True:
            await cocotb.triggers.ReadOnly()
            restarts.append(int(dut.sof_restart.value))
            await RisingEdge(dut.app_clk)

    cnt = cocotb.start_soon(count())
    full = make_frame(8, 4)
    for x in range(5):                     # first 5 pixels, no EOL / EOF
        dut.s_pix_data.value = 0xE0 + x
        dut.s_pix_valid.value = 1
        dut.s_pix_sof.value = int(x == 0)
        dut.s_pix_eol.value = 0
        dut.s_pix_eof.value = 0
        await RisingEdge(dut.app_clk)
        while dut.s_pix_ready.value == 0:
            await RisingEdge(dut.app_clk)
    dut.s_pix_valid.value = 0
    dut.s_pix_sof.value = 0
    await drive_frame(dut, full, geom=GEOM)
    for _ in range(10):
        await RisingEdge(dut.app_clk)
    stop.set()
    cnt.cancel()
    second = out[5:]
    assert sum(restarts) == 1, f"sof_restart pulsed {sum(restarts)} times"
    assert [b.data for b in second] == [v for row in full for v in row]
    assert second[0].sof == 1 and second[0].sol == 1 and second[-1].eof == 1


# -----------------------------------------------------------------------------
# TC 7 — The Cut Frame Ends With EOF
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_cut_frame_ends(dut):
    """A frame cut short by the next SOF ends with EOL + EOF on its last pixel.

    §8.5.2 / §9.4.6.1: the last stream packet of an image may be short, but
    it must end.  Without an EOF the gate, the packer and the stream keep
    the cut image open and its last packet never gets an EOP.

    Stimulus: `m_pix_ready` = 1; 2.5 lines of a 4 x 4 frame (10 pixels, no
              EOF), 6 idle cycles, a whole 4 x 2 frame (geometry xoffs 7),
              6 idle cycles; then a one-pixel frame (xoffs 3, no EOF)
              followed at once by a 4 x 1 frame (xoffs 5).
    Checks:   during the first idle gap 9 pixels are out (the 10th waits
              for its successor); the 10th leaves with EOL + EOF and data
              intact; every pixel of the 4 x 2 frame follows, its last with
              EOF and nothing waiting after it; `sof_restart` pulses twice;
              `m_meta_valid` pulses with each SOF pixel's `m_pix_sof`, and
              the one-pixel frame's SOF pixel carries xoffs 3, not 5.
    """
    dut.TESTCASE.value = 7
    await bringup(dut)
    dut.m_pix_ready.value = 1
    out = []
    metas = []          # (xoffs, sof of the beat on the same edge)
    restarts = 0
    stop = cocotb.triggers.Event()
    cap = cocotb.start_soon(capture(dut, out, stop_evt=stop))

    async def watch():
        nonlocal restarts
        while True:
            await cocotb.triggers.ReadOnly()
            restarts += int(dut.sof_restart.value)
            if int(dut.m_meta_valid.value):
                metas.append((int(dut.m_meta_xoffs.value), int(dut.m_pix_sof.value),
                              int(dut.m_pix_valid.value)))
            await RisingEdge(dut.app_clk)

    w = cocotb.start_soon(watch())

    async def beats(pixels, xoffs):
        for data, sof, eol, eof in pixels:
            dut.s_pix_data.value = data
            dut.s_pix_valid.value = 1
            dut.s_pix_sof.value = sof
            dut.s_pix_eol.value = eol
            dut.s_pix_eof.value = eof
            dut.s_pix_xoffs.value = xoffs
            await RisingEdge(dut.app_clk)
            while dut.s_pix_ready.value == 0:
                await RisingEdge(dut.app_clk)
        dut.s_pix_valid.value = 0
        dut.s_pix_sof.value = dut.s_pix_eol.value = dut.s_pix_eof.value = 0

    cut = [(0x40 + i, int(i == 0), int(i % 4 == 3), 0) for i in range(10)]
    await beats(cut, 1)
    for _ in range(6):
        await RisingEdge(dut.app_clk)
    assert len(out) == 9, f"{len(out)} pixels out before the next SOF (9 expected)"
    full = [(0x80 + i, int(i == 0), int(i % 4 == 3), int(i == 7)) for i in range(8)]
    await beats(full, 7)
    for _ in range(6):
        await RisingEdge(dut.app_clk)
    assert len(out) == 18, f"{len(out)} pixels out after the whole frame (18 expected)"
    last_cut = out[9]
    assert (last_cut.data, last_cut.eol, last_cut.eof) == (0x49, 1, 1), f"cut frame's last pixel {last_cut}"
    assert [b.data for b in out[10:]] == [0x80 + i for i in range(8)]
    assert out[10].sof == 1 and out[17].eof == 1 and out[17].eol == 1
    await beats([(0xC0, 1, 0, 0)], 3)
    await beats([(0xD0 + i, int(i == 0), int(i == 3), int(i == 3)) for i in range(4)], 5)
    for _ in range(8):
        await RisingEdge(dut.app_clk)
    stop.set()
    w.cancel()
    assert restarts == 2, f"sof_restart pulsed {restarts} times"
    assert out[18].data == 0xC0 and out[18].sof == out[18].eof == out[18].eol == 1
    assert [x for x, _, _ in metas] == [1, 7, 3, 5], f"metadata per SOF {metas}"
    assert all(sof == 1 and v == 1 for _, sof, v in metas), f"m_meta_valid off the SOF beat {metas}"
