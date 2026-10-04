"""Cocotb TB for `cxp_app_line_marker`.

Line-marker generator for the stream data path: on a one-cycle `line_start`
it latches the line metadata and emits either the 2-word rectangular marker
(4×K28.3, 4×0x02) or the 11-word arbitrary marker (4×K28.3, 4×0x04, Xsize,
Xoffs, DsizeL bytes MSB first), each byte replicated on all four lanes, over
a valid/ready word port. The TB runs a 10 ns `app_clk`, pulses `line_start`
via `kick_off`, collects accepted words with `collect_words` (optionally
under a repeating `m_word_ready` pattern) and compares them byte-exact
against `expected_rect_words` / `expected_arb_words`; `check_kmask` requires
kmask 0xF on word 0 only. The wrapper only renames ports; no FSM coverage.

Covers every case of the `cxp_camera_ip_modules.md` §2.4 verification plan
plus cases the plan implies (exact kmask pattern, byte replication, dropped
`line_start` during emission, idle behaviour). The golden follows the v1.0
citations the TB was written against (§7.4.6.3 / §7.4.7.3, v1.0 Table 40;
the plan says "fig 27"), which docs/design/modules/app/cxp_app_line_marker.md maps to CXP
1.1.1 §9.4.6.3 / Table 39 and §9.4.7.3 / Table 41. DsizeL is the 24-bit
count of 32-bit words per line, which the DUT derives from Xsize and
PixelF and the model takes from the golden `cxp_protocol.stream.dsizel`.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Rectangular marker = 4×K28.3, 4×0x02, kmask F/0.
  2  Arbitrary marker, 11 words byte-exact, for 3 metadata sets.
  3  Byte replication — a 1-bit flip in one lane is majority-recoverable.
  4  No `line_start` pulse → no emission.
  5  `line_start` during emission is dropped, not buffered.
  6  Back-pressure (ready 1,0,0,0) → sequence intact.
  7  Three consecutive arbitrary markers, each with its own metadata.
"""

from __future__ import annotations

from dataclasses import dataclass

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import NextTimeStep, ReadOnly, RisingEdge

from cxp_protocol import DEVICE
from cxp_protocol import stream as gs
from cxp_testcase import cxp_test


CLK_PERIOD_NS    = 10
K28_3            = 0x7C
LINE_TYPE_RECT   = 0x02
LINE_TYPE_ARB    = 0x04

RECT_WORDS = 2
ARB_WORDS  = 11


# -----------------------------------------------------------------------------
# Reference model
# -----------------------------------------------------------------------------
@dataclass
class LineCfg:
    """Per-line metadata driven on the `line_*` ports."""
    xsize:  int = 0x012345
    xoffs:  int = 0x000010
    pixfmt: int = 0x0101

    @property
    def dsizeL(self) -> int:
        """Table 41 DsizeL: 32-bit words per line of Xsize pixels."""
        return gs.dsizel(self.xsize, gs.device_bits(self.pixfmt, DEVICE) or 8)


def expected_rect_words() -> list[int]:
    """Byte sequence of the rectangular marker (one byte per word)."""
    return [K28_3, LINE_TYPE_RECT]


def expected_arb_words(c: LineCfg) -> list[int]:
    """Byte sequence of the arbitrary marker for `c` (one byte per word)."""
    return [
        K28_3,
        LINE_TYPE_ARB,
        (c.xsize  >> 16) & 0xFF,
        (c.xsize  >>  8) & 0xFF,
        (c.xsize  >>  0) & 0xFF,
        (c.xoffs  >> 16) & 0xFF,
        (c.xoffs  >>  8) & 0xFF,
        (c.xoffs  >>  0) & 0xFF,
        (c.dsizeL >> 16) & 0xFF,
        (c.dsizeL >>  8) & 0xFF,
        (c.dsizeL >>  0) & 0xFF,
    ]


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def reset(dut):
    """Start the clock, zero the inputs (ready = 1), reset 4 edges + 2."""
    cocotb.start_soon(Clock(dut.app_clk, CLK_PERIOD_NS, unit="ns").start())
    dut.app_rst_n.value     = 0
    dut.cfg_arbitrary.value = 0
    dut.line_xsize.value    = 0
    dut.line_xoffs.value    = 0
    dut.line_pixfmt.value   = 0
    dut.line_start.value    = 0
    dut.m_word_ready.value  = 1
    for _ in range(4):
        await RisingEdge(dut.app_clk)
    dut.app_rst_n.value = 1
    for _ in range(2):
        await RisingEdge(dut.app_clk)


# -----------------------------------------------------------------------------
# Drivers / monitor
# -----------------------------------------------------------------------------
def drive_cfg(dut, c: LineCfg) -> None:
    """Drive the line metadata ports from `c`."""
    dut.line_xsize.value  = c.xsize
    dut.line_xoffs.value  = c.xoffs
    dut.line_pixfmt.value = c.pixfmt


async def kick_off(dut, c: LineCfg, arbitrary: bool) -> None:
    """Set form + metadata and hold `line_start` high for one edge."""
    dut.cfg_arbitrary.value = 1 if arbitrary else 0
    drive_cfg(dut, c)
    dut.line_start.value = 1
    await RisingEdge(dut.app_clk)
    dut.line_start.value = 0


async def collect_words(dut, n: int, ready_pattern: list[int] | None = None,
                        max_idle_cycles: int = 4096):
    """Capture `n` accepted words + kmasks under a repeating ready pattern.

    `m_word_ready` is driven from `ready_pattern` (default all-1) each cycle;
    a word is recorded on every edge where valid & ready. Raises
    TimeoutError after `max_idle_cycles` cycles without progress.
    """
    words: list[int] = []
    kmasks: list[int] = []
    rp = ready_pattern or [1]
    rpi = 0
    idle = 0
    while len(words) < n:
        dut.m_word_ready.value = rp[rpi % len(rp)]
        await RisingEdge(dut.app_clk)
        if int(dut.m_word_valid.value) and int(dut.m_word_ready.value):
            words.append(int(dut.m_word_data.value))
            kmasks.append(int(dut.m_word_kmask.value))
            idle = 0
        else:
            idle += 1
            if idle > max_idle_cycles:
                raise TimeoutError(
                    f"collect_words: no progress for {idle} cycles "
                    f"(captured {len(words)}/{n})"
                )
        rpi += 1
    dut.m_word_ready.value = 1
    return words, kmasks


# -----------------------------------------------------------------------------
# Checkers
# -----------------------------------------------------------------------------
def check_byte_replication(words: list[int], byte_seq: list[int]) -> None:
    """Each word must equal its expected byte replicated on all 4 lanes."""
    assert len(words) == len(byte_seq), (
        f"length mismatch: got {len(words)} words, expected {len(byte_seq)}"
    )
    for i, (w, b) in enumerate(zip(words, byte_seq)):
        expected = (b << 24) | (b << 16) | (b << 8) | b
        assert w == expected, (
            f"word {i}: byte 0x{b:02x} -> expected 0x{expected:08x}, "
            f"got 0x{w:08x}"
        )


def check_kmask(kmasks: list[int]) -> None:
    """Only word 0 is the K28.3 marker; kmask = 0xF there only, 0 elsewhere."""
    assert kmasks[0] == 0b1111, f"kmask[0] = {kmasks[0]:04b}, expected 1111"
    for i, km in enumerate(kmasks[1:], start=1):
        assert km == 0, f"kmask[{i}] = {km:04b}, expected 0000"


# -----------------------------------------------------------------------------
# TC 1 — Rectangular Marker Content
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_rect_marker_content(dut):
    """The rectangular marker is 4×K28.3 followed by 4×0x02.

    Verification plan §2.4 #1; this is the only unit test of rectangular
    mode (the metadata ports are ignored in this form).

    Stimulus: `cfg_arbitrary = 0`, default LineCfg (Xsize 0x012345, Xoffs
              0x10, PixelF Mono8), one `line_start` pulse, ready held 1;
              2 words captured.
    Checks:   words == [4×0x7C, 4×0x02] (`check_byte_replication`);
              kmask 0xF on word 0, 0 on word 1 (`check_kmask`).
    Note:     capture stops after word 2 and nothing checks that valid
              drops afterwards, so a wrong rectangular length would pass
              here (the `cxp_stream_top` golden bounds it).
    """
    dut.TESTCASE.value = 1
    await reset(dut)
    await kick_off(dut, LineCfg(), arbitrary=False)
    words, kmasks = await collect_words(dut, RECT_WORDS)
    check_byte_replication(words, expected_rect_words())
    check_kmask(kmasks)


# -----------------------------------------------------------------------------
# TC 2 — Arbitrary Marker Content
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_arb_marker_content(dut):
    """The 11-word arbitrary marker is byte-exact for several metadata sets.

    Verification plan §2.4 #2: word order K28.3, 0x04, Xsize[23:0],
    Xoffs[23:0], DsizeL (MSB first), and a fresh metadata latch per marker.

    Stimulus: `cfg_arbitrary = 1`, ready held 1, three markers:
              (Xsize 0xABCDEF, Xoffs 0x010203, PixelF Mono8),
              (0x000001, 0x000000, Mono12), (0xFFFFFF, 0xFFFFFF, Mono16);
              11 words captured per marker, one idle edge before the next
              `line_start`.
    Checks:   per marker, the 11 words equal `expected_arb_words` replicated
              4× (DsizeL in 32-bit words, all 24 bits — 0x800000 for the
              last), and kmask is 0xF on word 0 only.
    Note:     valid dropping after the last marker is not checked here.
    """
    dut.TESTCASE.value = 2
    await reset(dut)
    cases = [
        LineCfg(xsize=0xABCDEF, xoffs=0x010203, pixfmt=0x0101),
        LineCfg(xsize=0x000001, xoffs=0x000000, pixfmt=0x0103),
        LineCfg(xsize=0xFFFFFF, xoffs=0xFFFFFF, pixfmt=0x0105),
    ]
    for c in cases:
        await kick_off(dut, c, arbitrary=True)
        words, kmasks = await collect_words(dut, ARB_WORDS)
        check_byte_replication(words, expected_arb_words(c))
        check_kmask(kmasks)
        # idle gap between markers
        await RisingEdge(dut.app_clk)


# -----------------------------------------------------------------------------
# TC 3 — Byte Replication / Majority Vote
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_byte_replication_majority_vote(dut):
    """Each byte is replicated 4× so a 1-bit flip is majority-recoverable.

    Shows the host-side property of the §8.2.2.1 character duplication on
    the DUT's own output words.

    Stimulus: default LineCfg, arbitrary form, ready held 1; 11 words
              captured. In Python each captured word is XORed with
              0x0080_0000 (lane 2, bit 7).
    Checks:   a per-bit 3-of-4 majority vote over the corrupted lanes
              recovers the expected `expected_arb_words` byte for every word.
    Note:     the fault is applied to the captured copy, not injected into
              the DUT, and only one lane/bit is ever flipped — the test adds
              nothing to the DUT coverage of TC 2.
    """
    dut.TESTCASE.value = 3
    await reset(dut)
    c = LineCfg()
    await kick_off(dut, c, arbitrary=True)
    words, _ = await collect_words(dut, ARB_WORDS)
    expected_bytes = expected_arb_words(c)

    for i, (w, exp) in enumerate(zip(words, expected_bytes)):
        # Flip bit 7 of lane 2 (bits [23:16])
        corrupted = w ^ 0x0080_0000
        lanes = [(corrupted >> 24) & 0xFF, (corrupted >> 16) & 0xFF,
                 (corrupted >>  8) & 0xFF, (corrupted >>  0) & 0xFF]
        recovered = 0
        for b in range(8):
            ones = sum((l >> b) & 1 for l in lanes)
            recovered |= (1 if ones >= 3 else 0) << b
        assert recovered == exp, (
            f"word {i}: majority vote recovered 0x{recovered:02x}, "
            f"expected 0x{exp:02x}"
        )


# -----------------------------------------------------------------------------
# TC 4 — No Emission Without line_start
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_no_emission_without_line_start(dut):
    """Without a `line_start` pulse the generator stays idle.

    Guards against a self-start after reset or on metadata changes.

    Stimulus: default LineCfg driven on the metadata ports,
              `cfg_arbitrary = 0`, `line_start = 0`, ready 1, for 64 cycles
              after reset.
    Checks:   `m_word_valid == 0` in ReadOnly of every one of the 64 cycles.
    """
    dut.TESTCASE.value = 4
    await reset(dut)
    drive_cfg(dut, LineCfg())
    saw_valid = False
    for _ in range(64):
        await RisingEdge(dut.app_clk)
        await ReadOnly()
        if int(dut.m_word_valid.value):
            saw_valid = True
            break
        await NextTimeStep()
    assert not saw_valid, "marker emitted without a line_start pulse"


# -----------------------------------------------------------------------------
# TC 5 — line_start During Emission Dropped
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_line_start_during_emission_dropped(dut):
    """A `line_start` mid-marker neither splits the marker nor is buffered.

    Verification plan §2.4 #3: the latch is gated by `!active_q`, so a pulse
    while a marker is in flight is ignored and the metadata is not
    re-latched.

    Stimulus: arbitrary marker 1 (Xsize 0xAAAAAA, Xoffs 0xBBBBBB, PixelF
              Mono10), ready 1. A forked task waits 4 edges, then drives
              marker-2 metadata (0x111111, 0x222222, Mono8) with a
              one-edge `line_start`, and restores marker 1's metadata.
    Checks:   the 11 captured words equal marker 1's `expected_arb_words`
              with kmask 0xF on word 0 only; then `m_word_valid == 0` in
              ReadOnly for 8 further cycles (no second marker).
    Note:     every field of marker 1 repeats one byte value, so byte order
              within a field is not checked here.
    """
    dut.TESTCASE.value = 5
    await reset(dut)
    c1 = LineCfg(xsize=0xAAAAAA, xoffs=0xBBBBBB, pixfmt=0x0102)
    c2 = LineCfg(xsize=0x111111, xoffs=0x222222, pixfmt=0x0101)

    await kick_off(dut, c1, arbitrary=True)

    async def inject_pulse():
        # Wait until the marker is well into emission.
        for _ in range(4):
            await RisingEdge(dut.app_clk)
        drive_cfg(dut, c2)
        dut.line_start.value = 1
        await RisingEdge(dut.app_clk)
        dut.line_start.value = 0
        drive_cfg(dut, c1)

    cocotb.start_soon(inject_pulse())

    words, kmasks = await collect_words(dut, ARB_WORDS)
    check_byte_replication(words, expected_arb_words(c1))
    check_kmask(kmasks)

    # The dropped pulse must NOT cause a fresh marker to start on its own.
    saw_second_valid = False
    for _ in range(8):
        await RisingEdge(dut.app_clk)
        await ReadOnly()
        if int(dut.m_word_valid.value):
            saw_second_valid = True
            break
        await NextTimeStep()
    assert not saw_second_valid, "spurious marker after dropped line_start pulse"


# -----------------------------------------------------------------------------
# TC 6 — Back-Pressure
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_backpressure(dut):
    """Holding `m_word_ready` low stalls the marker without corrupting it.

    Verification plan §2.4 #4: the word index advances only on valid &
    ready, and data / kmask hold during a stall.

    Stimulus: default LineCfg, arbitrary form; `m_word_ready` follows the
              repeating pattern 1, 0, 0, 0 from the first capture edge, so
              word 0 is taken at once and words 1..10 each wait 3 cycles.
    Checks:   the 11 accepted words equal `expected_arb_words` replicated
              4×, and kmask is 0xF on word 0 only.
    Note:     the K28.3 word itself is never stalled, and rectangular mode
              is never back-pressured.
    """
    dut.TESTCASE.value = 6
    await reset(dut)
    c = LineCfg()
    await kick_off(dut, c, arbitrary=True)
    pattern = [1, 0, 0, 0]
    words, kmasks = await collect_words(dut, ARB_WORDS, ready_pattern=pattern)
    check_byte_replication(words, expected_arb_words(c))
    check_kmask(kmasks)


# -----------------------------------------------------------------------------
# TC 7 — Consecutive Markers
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_back_to_back_markers(dut):
    """Three consecutive markers each carry their own metadata.

    Covers the end-of-marker return to idle followed by a fresh latch of
    Xsize / Xoffs / PixelF (hence DsizeL) for the next line.

    Stimulus: arbitrary form, ready 1, three markers with
              (Xsize, Xoffs, PixelF) = (0x100000, 0x200000, Mono12),
              (0x300000, 0x400000, Mono10), (0x500000, 0x600000, Mono14);
              one idle edge after each capture before the next pulse.
    Checks:   per marker, the 11 words equal `expected_arb_words` replicated
              4×, and kmask is 0xF on word 0 only.
    Note:     not truly back-to-back — the RTL would accept the next pulse
              one cycle earlier, so the minimum gap is not exercised; only
              the top bytes of Xsize / Xoffs differ, so a swap among the
              all-zero bytes would pass.
    """
    dut.TESTCASE.value = 7
    await reset(dut)
    cfgs = [
        LineCfg(xsize=0x100000, xoffs=0x200000, pixfmt=0x0103),
        LineCfg(xsize=0x300000, xoffs=0x400000, pixfmt=0x0102),
        LineCfg(xsize=0x500000, xoffs=0x600000, pixfmt=0x0104),
    ]
    for c in cfgs:
        await kick_off(dut, c, arbitrary=True)
        words, kmasks = await collect_words(dut, ARB_WORDS)
        check_byte_replication(words, expected_arb_words(c))
        check_kmask(kmasks)
        await RisingEdge(dut.app_clk)
