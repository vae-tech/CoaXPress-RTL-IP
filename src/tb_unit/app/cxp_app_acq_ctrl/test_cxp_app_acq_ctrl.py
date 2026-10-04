"""Cocotb TB for `cxp_app_acq_ctrl`.

Acquisition control (GenICam SFNC AcquisitionStart / AcquisitionStop /
AcquisitionMode / AcquisitionFrameCount): `active` says whether the image
source may begin a new image, and every pixel passes its gate a whole image
at a time.  The TB pulses start / stop, ends an image with a one-pixel
image (SOF and EOF together) and samples `active` in ReadOnly.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Reset → inactive; AcquisitionStart → active (Continuous).
  2  Continuous: frames do not end it; AcquisitionStop does.
  3  SingleFrame: active drops in the cycle the first image ends.
  4  MultiFrame: exactly FrameCount images (0 counts as 1).
  5  stream_en low (StreamPacketSizeMax not set) holds active low.
  6  A new AcquisitionStart restarts the image count.
  7  The gate: an image entered completes after AcquisitionStop; images
     starting while stopped, and pixels outside any image, are taken and
     dropped.
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import NextTimeStep, ReadOnly, RisingEdge

from cxp_testcase import cxp_test

CONT, SINGLE, MULTI = 0, 1, 2


async def reset(dut, mode=CONT, count=0, stream_en=1):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.acq_start.value = 0
    dut.acq_stop.value = 0
    dut.run.value = 0
    dut.pix_data.value = 0
    dut.pix_valid.value = 0
    dut.pix_sof.value = 0
    dut.pix_eof.value = 0
    dut.out_ready.value = 1
    dut.acq_mode.value = mode
    dut.frame_count.value = count
    dut.stream_en.value = stream_en
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def pulse(dut, name):
    getattr(dut, name).value = 1
    await RisingEdge(dut.clk)
    getattr(dut, name).value = 0


async def active(dut) -> int:
    await ReadOnly()
    v = int(dut.active.value)
    await NextTimeStep()
    return v


async def end_frame(dut) -> int:
    """Pass a one-pixel image (SOF and EOF) through the gate; returns
    `active` in that same cycle."""
    dut.pix_valid.value = 1
    dut.pix_sof.value = 1
    dut.pix_eof.value = 1
    await ReadOnly()
    a = int(dut.active.value)
    await RisingEdge(dut.clk)
    dut.pix_valid.value = 0
    dut.pix_sof.value = 0
    dut.pix_eof.value = 0
    return a


# -----------------------------------------------------------------------------
# TC 1 — Start
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_start(dut):
    """Inactive after reset; AcquisitionStart arms it.

    Stimulus: reset; pulse acq_start.
    Checks:   active 0 before, 1 after.
    """
    dut.TESTCASE.value = 1
    await reset(dut)
    assert await active(dut) == 0
    await pulse(dut, "acq_start")
    assert await active(dut) == 1


# -----------------------------------------------------------------------------
# TC 2 — Continuous
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_continuous(dut):
    """Continuous mode runs until AcquisitionStop.

    Stimulus: start; 5 frame ends; stop.
    Checks:   active 1 through the frames, 0 after stop.
    """
    dut.TESTCASE.value = 2
    await reset(dut, CONT)
    await pulse(dut, "acq_start")
    for _ in range(5):
        assert await end_frame(dut) == 1
    await pulse(dut, "acq_stop")
    assert await active(dut) == 0


# -----------------------------------------------------------------------------
# TC 3 — SingleFrame
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_single_frame(dut):
    """SingleFrame: active is low already in the cycle the image ends.

    A source deciding at its last pixel whether to roll into another image
    must see the drop in that cycle.
    Stimulus: mode 1; start; one frame end.
    Checks:   active 1 before, 0 in the frame-end cycle and after.
    """
    dut.TESTCASE.value = 3
    await reset(dut, SINGLE)
    await pulse(dut, "acq_start")
    assert await active(dut) == 1
    assert await end_frame(dut) == 0
    assert await active(dut) == 0


# -----------------------------------------------------------------------------
# TC 4 — MultiFrame
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_multi_frame(dut):
    """MultiFrame: FrameCount images, 0 counting as 1.

    Stimulus: mode 2, FrameCount 3; start; frame ends.  Then FrameCount 0.
    Checks:   active through ends 1 and 2, dropping at end 3; with
              FrameCount 0 it drops at the first end.
    """
    dut.TESTCASE.value = 4
    await reset(dut, MULTI, 3)
    await pulse(dut, "acq_start")
    assert [await end_frame(dut) for _ in range(3)] == [1, 1, 0]
    assert await active(dut) == 0
    dut.frame_count.value = 0
    await pulse(dut, "acq_start")
    assert await end_frame(dut) == 0


# -----------------------------------------------------------------------------
# TC 5 — Stream Not Enabled
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_stream_disabled(dut):
    """No image may start while stream packets are not allowed.

    §10.3.32: StreamPacketSizeMax 0 means no data packets.
    Stimulus: stream_en 0; start; then stream_en 1.
    Checks:   active 0 while stream_en is 0, 1 once it rises.
    """
    dut.TESTCASE.value = 5
    await reset(dut, CONT, stream_en=0)
    await pulse(dut, "acq_start")
    assert await active(dut) == 0
    dut.stream_en.value = 1
    assert await active(dut) == 1


# -----------------------------------------------------------------------------
# TC 6 — Restart
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_restart(dut):
    """A new AcquisitionStart restarts the MultiFrame count.

    Stimulus: mode 2, count 2; start; one end; start again; ends.
    Checks:   after the restart two more ends are allowed.
    """
    dut.TESTCASE.value = 6
    await reset(dut, MULTI, 2)
    await pulse(dut, "acq_start")
    assert await end_frame(dut) == 1
    await pulse(dut, "acq_start")
    assert [await end_frame(dut) for _ in range(2)] == [1, 0]


# -----------------------------------------------------------------------------
# TC 7 — Pixel Gate
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_pixel_gate(dut):
    """Images enter whole or not at all.

    §11.2.1.5: AcquisitionStop ends the acquisition after the image in
    progress.

    Stimulus: Continuous; 3 pixels with no SOF (a sensor running before
              any image); a 6-pixel image while stopped;
              AcquisitionStart; a 6-pixel image with AcquisitionStop
              pulsed at its 3rd pixel; another 6-pixel image.
    Checks:   every pixel is taken (`pix_ready` = 1 with `out_ready` = 1);
              only the image that started after AcquisitionStart comes
              out, all 6 pixels of it.
    """
    dut.TESTCASE.value = 7
    await reset(dut, mode=CONT)
    out = []

    async def watch():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if int(dut.out_valid.value) and int(dut.out_ready.value):
                out.append(int(dut.out_data.value))

    w = cocotb.start_soon(watch())

    async def pixels(values, sof_first=True, stop_at=None):
        for i, v in enumerate(values):
            dut.pix_data.value = v
            dut.pix_valid.value = 1
            dut.pix_sof.value = int(sof_first and i == 0)
            dut.pix_eof.value = int(sof_first and i == len(values) - 1)
            if stop_at is not None and i == stop_at:
                dut.acq_stop.value = 1
            await ReadOnly()
            assert int(dut.pix_ready.value) == 1, "gate stalled a pixel"
            await RisingEdge(dut.clk)
            dut.acq_stop.value = 0
        dut.pix_valid.value = 0
        dut.pix_sof.value = 0
        dut.pix_eof.value = 0

    await pixels([0x10, 0x11, 0x12], sof_first=False)
    await pixels([0x20 + i for i in range(6)])
    await pulse(dut, "acq_start")
    await pixels([0x30 + i for i in range(6)], stop_at=2)
    await pixels([0x40 + i for i in range(6)])
    for _ in range(4):
        await RisingEdge(dut.clk)
    w.cancel()
    assert out == [0x30 + i for i in range(6)], [hex(v) for v in out]
