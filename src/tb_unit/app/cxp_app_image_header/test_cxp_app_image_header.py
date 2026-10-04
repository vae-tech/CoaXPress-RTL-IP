"""Cocotb TB for `cxp_app_image_header`.

The DUT emits one image header per 1-cycle `meta_valid` pulse as a burst of
32-bit words under valid/ready: 25 words for the rectangular form
(`cfg_arbitrary = 0`, Table 38) or 16 for the arbitrary form (Table 40).
Word 0 is 4×K28.3 with kmask 0xF; every other word carries one
metadata byte replicated in all four lanes, multi-byte fields MSB first.

The wrapper only renames the ports (no `_i`/`_o` suffix) and adds
`TESTCASE`; `app_clk` is 10 ns. `kick_off` drives the metadata (left driven
afterwards) plus a one-edge `meta_valid` pulse; `collect_words` records
data/kmask on every valid & ready edge, optionally driving
`m_word_ready` from a repeating 0/1 pattern. The golden model
(`expected_rect_words` / `expected_arb_words`) encodes the Table 38 / 40
byte order; DsizeL is the 24-bit count of 32-bit words per line, which the
DUT derives from Xsize and PixelF (`dsizel_words` in cxp_pkg) and the model
takes from the golden `cxp_protocol.stream.dsizel`.

The cases derive from the module plan in `docs/design/cxp_camera_ip_modules.md`
§2.3, which cites "table 26" / "table 30" and a 27-word rectangular header.
The authoritative layouts are CXP-001-2015 Table 38 (25 words) and Table 40
(16 words) — Tables 37 / 39 in v1.0 — and both RTL and TB follow them.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Rectangular header — 25 words byte-exact, marker kmask.
  2  Arbitrary header — 16 words byte-exact, marker kmask.
  3  4× byte replication — majority vote recovers each byte after a
     1-bit lane flip.
  4  StreamID[7:0] carried in word 2 over 6 successive headers.
  5  No `meta_valid` pulse → no header.
  6  A second `meta_valid` pulse mid-header is dropped, not queued.
  7  Back-pressure (ready 1,0,0,0) stalls without corrupting the header.
  8  Three successive headers, each byte-exact.
  9  DsizeL is 32-bit words per line, all 24 bits (Mono8/10/12/16).
"""

from __future__ import annotations

from dataclasses import dataclass

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly, NextTimeStep

from cxp_protocol import DEVICE
from cxp_protocol import stream as gs
from cxp_testcase import cxp_test


CLK_PERIOD_NS = 10
K28_3         = 0x7C
HDR_TYPE_REC  = 0x01
HDR_TYPE_ARB  = 0x03

RECT_WORDS = 25
ARB_WORDS  = 16


# -----------------------------------------------------------------------------
# Reference model
# -----------------------------------------------------------------------------
@dataclass
class Meta:
    """Frame metadata driven on the `meta_*` ports (defaults = test 1)."""
    streamid:  int = 0x0001
    sourcetag: int = 0x1234
    xsize:     int = 0x012345
    ysize:     int = 0x067890
    xoffs:     int = 0x000010
    yoffs:     int = 0x000020
    pixfmt:    int = 0x0101
    tapg:      int = 0x1111
    flags:     int = 0x02


def expected_dsizel(m: Meta) -> int:
    """Table 38 DsizeL: 32-bit words per line of Xsize pixels."""
    return gs.dsizel(m.xsize, gs.device_bits(m.pixfmt, DEVICE) or 8)


def expected_rect_words(m: Meta) -> list[int]:
    """Per-byte expected sequence for the rectangular header (Table 38)."""
    dsl = expected_dsizel(m)
    return [
        K28_3,
        HDR_TYPE_REC,
        m.streamid & 0xFF,
        (m.sourcetag >> 8) & 0xFF,
        m.sourcetag & 0xFF,
        (m.xsize >> 16) & 0xFF,
        (m.xsize >>  8) & 0xFF,
        (m.xsize >>  0) & 0xFF,
        (m.xoffs >> 16) & 0xFF,
        (m.xoffs >>  8) & 0xFF,
        (m.xoffs >>  0) & 0xFF,
        (m.ysize >> 16) & 0xFF,
        (m.ysize >>  8) & 0xFF,
        (m.ysize >>  0) & 0xFF,
        (m.yoffs >> 16) & 0xFF,
        (m.yoffs >>  8) & 0xFF,
        (m.yoffs >>  0) & 0xFF,
        (dsl >> 16) & 0xFF,
        (dsl >>  8) & 0xFF,
        (dsl >>  0) & 0xFF,
        (m.pixfmt >> 8) & 0xFF,
        (m.pixfmt >> 0) & 0xFF,
        (m.tapg   >> 8) & 0xFF,
        (m.tapg   >> 0) & 0xFF,
        m.flags & 0xFF,
    ]


def expected_arb_words(m: Meta) -> list[int]:
    """Per-byte expected sequence for the arbitrary header (Table 40)."""
    return [
        K28_3,
        HDR_TYPE_ARB,
        m.streamid & 0xFF,
        (m.sourcetag >> 8) & 0xFF,
        m.sourcetag & 0xFF,
        (m.ysize >> 16) & 0xFF,
        (m.ysize >>  8) & 0xFF,
        (m.ysize >>  0) & 0xFF,
        (m.yoffs >> 16) & 0xFF,
        (m.yoffs >>  8) & 0xFF,
        (m.yoffs >>  0) & 0xFF,
        (m.pixfmt >> 8) & 0xFF,
        (m.pixfmt >> 0) & 0xFF,
        (m.tapg   >> 8) & 0xFF,
        (m.tapg   >> 0) & 0xFF,
        m.flags & 0xFF,
    ]


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def reset(dut):
    """Start app_clk, zero all inputs (ready = 1), reset 4 edges, then 2."""
    cocotb.start_soon(Clock(dut.app_clk, CLK_PERIOD_NS, unit="ns").start())
    dut.app_rst_n.value      = 0
    dut.cfg_arbitrary.value  = 0
    dut.meta_streamid.value  = 0
    dut.meta_sourcetag.value = 0
    dut.meta_xsize.value     = 0
    dut.meta_ysize.value     = 0
    dut.meta_xoffs.value     = 0
    dut.meta_yoffs.value     = 0
    dut.meta_pixfmt.value    = 0
    dut.meta_tapg.value      = 0
    dut.meta_flags.value     = 0
    dut.meta_valid.value     = 0
    dut.m_word_ready.value   = 1
    for _ in range(4):
        await RisingEdge(dut.app_clk)
    dut.app_rst_n.value = 1
    for _ in range(2):
        await RisingEdge(dut.app_clk)


# -----------------------------------------------------------------------------
# Drivers / monitor
# -----------------------------------------------------------------------------
def drive_meta(dut, m: Meta) -> None:
    """Drive every `meta_*` field port from `m` (not `meta_valid`)."""
    dut.meta_streamid.value  = m.streamid
    dut.meta_sourcetag.value = m.sourcetag
    dut.meta_xsize.value     = m.xsize
    dut.meta_ysize.value     = m.ysize
    dut.meta_xoffs.value     = m.xoffs
    dut.meta_yoffs.value     = m.yoffs
    dut.meta_pixfmt.value    = m.pixfmt
    dut.meta_tapg.value      = m.tapg
    dut.meta_flags.value     = m.flags


async def kick_off(dut, m: Meta, arbitrary: bool) -> None:
    """Set the header form, drive `m` and pulse `meta_valid` for one edge."""
    dut.cfg_arbitrary.value = 1 if arbitrary else 0
    drive_meta(dut, m)
    dut.meta_valid.value = 1
    await RisingEdge(dut.app_clk)
    dut.meta_valid.value = 0


async def collect_words(dut, n: int, ready_pattern: list[int] | None = None,
                        max_idle_cycles: int = 4096):
    """Capture exactly `n` accepted words and return (data, kmask).

    `ready_pattern`, if given, is a list of 0/1 values cycled through to
    drive `m_word_ready` (used to exercise back-pressure).  Otherwise
    `m_word_ready` is held at 1.
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
    """Length matches and each word is its expected byte in all 4 lanes."""
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
    """Only word 0 is the K28.3 marker; kmask=0xF there only."""
    assert kmasks[0] == 0b1111, f"kmask[0] = {kmasks[0]:04b}, expected 1111"
    for i, km in enumerate(kmasks[1:], start=1):
        assert km == 0, f"kmask[{i}] = {km:04b}, expected 0000"


# -----------------------------------------------------------------------------
# TC 1 — Rectangular Header Content
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_rect_header_content(dut):
    """The 25-word rectangular header matches Table 38 byte-exact.

    Covers the metadata latch, every rectangular byte-select arm (idx
    1–24, including DsizeL = ceil(0x012345 × 8 / 32) = 0x0048D2) and the
    marker kmask.

    Stimulus: default `Meta` (StreamID 0x0001, SourceTag 0x1234, Xsize
              0x012345, Ysize 0x067890, Xoffs 0x10, Yoffs 0x20, PixelF
              0x0101, TapG 0x1111, Flags 0x02),
              `cfg_arbitrary = 0`, one pulse; ready held 1; 25 words
              captured.
    Checks:   `check_byte_replication` against `expected_rect_words` (25
              words, each byte in all 4 lanes); `check_kmask` (kmask 0xF on
              word 0 only).
    Note:     the metadata stays driven after the pulse, so a
              combinational pass-through would pass as well as a latch.
    """
    dut.TESTCASE.value = 1
    await reset(dut)
    m = Meta()
    await kick_off(dut, m, arbitrary=False)
    words, kmasks = await collect_words(dut, RECT_WORDS)
    check_byte_replication(words, expected_rect_words(m))
    check_kmask(kmasks)


# -----------------------------------------------------------------------------
# TC 2 — Arbitrary Header Content
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_arb_header_content(dut):
    """The 16-word arbitrary header matches Table 40 byte-exact.

    Covers the arbitrary byte-select arms; Xsize, Xoffs and DsizeL must
    not appear in this form.

    Stimulus: `Meta` with every field distinct and non-zero (StreamID
              0x0042, SourceTag 0xCAFE, Xsize 0xABCDEF, Ysize 0x123456,
              Xoffs 0x010101, Yoffs 0x020202, PixelF
              0x0102, TapG 0x2222, Flags 0x01), `cfg_arbitrary = 1`, one
              pulse; ready held 1; 16 words captured.
    Checks:   `check_byte_replication` against `expected_arb_words`;
              `check_kmask`.
    Note:     capture stops at 16 words and nothing checks that valid
              drops afterwards, so a wrong `last_idx` (e.g. 24) passes.
    """
    dut.TESTCASE.value = 2
    await reset(dut)
    m = Meta(
        streamid=0x0042, sourcetag=0xCAFE,
        xsize=0xABCDEF, ysize=0x123456,
        xoffs=0x010101, yoffs=0x020202,
        pixfmt=0x0102, tapg=0x2222,
        flags=0x01,
    )
    await kick_off(dut, m, arbitrary=True)
    words, kmasks = await collect_words(dut, ARB_WORDS)
    check_byte_replication(words, expected_arb_words(m))
    check_kmask(kmasks)


# -----------------------------------------------------------------------------
# TC 3 — Byte Replication Majority Vote
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_byte_replication_majority_vote(dut):
    """A per-bit majority vote over the 4 lanes survives a 1-bit lane error.

    Each header byte is sent 4× so the host can correct a single-bit
    error per byte by majority vote.

    Stimulus: as test 1 (default `Meta`, rectangular, ready 1, 25 words);
              in Python each captured word gets bit 8 flipped (lane
              [15:8], bit 0).
    Checks:   for every word, the per-bit ≥ 3-of-4 vote over the four
              lanes equals the expected Table 38 byte.
    Note:     the error is applied to the captured copy, not injected into
              the DUT, and only one lane/bit is ever flipped; this adds no
              RTL coverage beyond test 1 (kmask is not checked).
    """
    dut.TESTCASE.value = 3
    await reset(dut)
    m = Meta()
    await kick_off(dut, m, arbitrary=False)
    words, _ = await collect_words(dut, RECT_WORDS)
    expected_bytes = expected_rect_words(m)

    # Inject a 1-bit error into one of the four lanes for every word and
    # verify the reconstructed byte still matches the expected value.
    for i, (w, exp) in enumerate(zip(words, expected_bytes)):
        # Flip bit 0 of lane 1 (bits [15:8])
        corrupted = w ^ 0x0000_0100
        lanes = [(corrupted >> 24) & 0xFF, (corrupted >> 16) & 0xFF,
                 (corrupted >>  8) & 0xFF, (corrupted >>  0) & 0xFF]
        # Majority vote per bit.
        recovered = 0
        for b in range(8):
            ones = sum((l >> b) & 1 for l in lanes)
            recovered |= (1 if ones >= 3 else 0) << b
        assert recovered == exp, (
            f"word {i}: majority vote recovered 0x{recovered:02x}, "
            f"expected 0x{exp:02x}"
        )


# -----------------------------------------------------------------------------
# TC 4 — StreamID Passthrough
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_streamid_passthrough(dut):
    """The header carries `meta_streamid[7:0]` in word 2 for every header.

    The 16-bit StreamID port is truncated to the single StreamID byte of
    Table 38; each new pulse must re-latch it.

    Stimulus: six rectangular headers with StreamID 0x0000, 0x0001,
              0x0042, 0x00FF, 0xAA00 and 0xFFFF (other fields default),
              ready 1; 25 words captured per header and one extra edge
              before the next pulse.
    Checks:   word 2 bits [31:24] == StreamID & 0xFF (0xAA00 → 0x00,
              0xFFFF → 0xFF prove the truncation).
    Note:     only lane 3 of word 2 is checked; the other words are not.
    """
    dut.TESTCASE.value = 4
    await reset(dut)
    for sid in (0x0000, 0x0001, 0x0042, 0x00FF, 0xAA00, 0xFFFF):
        m = Meta(streamid=sid)
        await kick_off(dut, m, arbitrary=False)
        words, _ = await collect_words(dut, RECT_WORDS)
        # The header carries streamid[7:0]; that's word 2 (index 2) in the
        # rectangular table, immediately after the marker and the type byte.
        byte = (words[2] >> 24) & 0xFF
        assert byte == (sid & 0xFF), (
            f"streamid 0x{sid:04x}: header byte 0x{byte:02x} != 0x{sid & 0xFF:02x}"
        )
        # Wait an idle cycle between back-to-back frames.
        await RisingEdge(dut.app_clk)


# -----------------------------------------------------------------------------
# TC 5 — Header Gated by meta_valid
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_header_gating_no_meta_valid(dut):
    """Without a `meta_valid` pulse no header is emitted.

    The header is gated by the SOF pulse: idle must hold with metadata
    present, and nothing may self-start after reset.

    Stimulus: default `Meta` driven, `meta_valid` held 0, ready 1, for 64
              cycles after reset.
    Checks:   `m_word_valid == 0` at `ReadOnly` in every cycle.
    """
    dut.TESTCASE.value = 5
    await reset(dut)
    drive_meta(dut, Meta())
    # Run for a generous number of cycles without pulsing meta_valid.
    saw_valid = False
    for _ in range(64):
        await RisingEdge(dut.app_clk)
        await ReadOnly()
        if int(dut.m_word_valid.value):
            saw_valid = True
            break
        await NextTimeStep()
    assert not saw_valid, "header was emitted without a meta_valid pulse"


# -----------------------------------------------------------------------------
# TC 6 — meta_valid Pulse During Active Header Dropped
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_meta_valid_pulse_during_active_dropped(dut):
    """A second `meta_valid` pulse mid-header neither splits nor queues.

    The latch is gated by `!active_q`: the in-flight header must complete
    unchanged and the dropped pulse must not start a second header.

    Stimulus: header 1 (SourceTag 0xAAAA), rectangular, ready 1; a forked
              task waits 7 edges, then drives SourceTag 0x5555 with a
              one-edge `meta_valid` pulse (sampled while word 7 is
              accepted) and restores header 1's metadata.
    Checks:   the 25 captured words match header 1
              (`check_byte_replication`, `check_kmask`); then
              `m_word_valid == 0` at `ReadOnly` for 8 cycles.
    Note:     the second pulse differs only in SourceTag, whose words (idx
              3–4) are already out when it lands, so a bug that re-latched
              `params_q` without restarting would also pass.
    """
    dut.TESTCASE.value = 6
    await reset(dut)
    m1 = Meta(sourcetag=0xAAAA)
    m2 = Meta(sourcetag=0x5555)

    await kick_off(dut, m1, arbitrary=False)

    async def inject_pulse():
        # Wait several cycles so the first header is well into emission.
        for _ in range(7):
            await RisingEdge(dut.app_clk)
        drive_meta(dut, m2)
        dut.meta_valid.value = 1
        await RisingEdge(dut.app_clk)
        dut.meta_valid.value = 0
        drive_meta(dut, m1)

    cocotb.start_soon(inject_pulse())

    words, kmasks = await collect_words(dut, RECT_WORDS)
    check_byte_replication(words, expected_rect_words(m1))
    check_kmask(kmasks)

    # After the in-flight header finishes the RTL must NOT have buffered
    # the dropped pulse — it should not start a second header on its own.
    saw_second_valid = False
    for _ in range(8):
        await RisingEdge(dut.app_clk)
        await ReadOnly()
        if int(dut.m_word_valid.value):
            saw_second_valid = True
            break
        await NextTimeStep()
    assert not saw_second_valid, (
        "spurious header started — meta_valid pulse mid-emission must be dropped, "
        "not buffered"
    )


# -----------------------------------------------------------------------------
# TC 7 — Back-Pressure
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_backpressure(dut):
    """Holding `m_word_ready` low stalls the header without corruption.

    The word index may advance only on `valid & ready`, and the outputs
    must hold their value through a stall.

    Stimulus: default `Meta`, rectangular; `m_word_ready` follows the
              repeating pattern 1, 0, 0, 0 from the first capture edge, so
              word 0 is accepted at once and words 1–24 each stall 3
              cycles (97 edges in total).
    Checks:   `check_byte_replication` against `expected_rect_words`;
              `check_kmask`.
    Note:     the marker word is never stalled, and the arbitrary form is
              never run under back-pressure.
    """
    dut.TESTCASE.value = 7
    await reset(dut)
    m = Meta()
    await kick_off(dut, m, arbitrary=False)

    # Drive a "stop-go" ready pattern: 1 cycle ready, 3 cycles not-ready.
    pattern = [1, 0, 0, 0]
    words, kmasks = await collect_words(dut, RECT_WORDS, ready_pattern=pattern)
    check_byte_replication(words, expected_rect_words(m))
    check_kmask(kmasks)


# -----------------------------------------------------------------------------
# TC 8 — Back-to-Back Headers
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_back_to_back_headers(dut):
    """Successive headers are each complete, with no lost or repeated word.

    Covers the return to idle after idx 24 and a fresh `params_q` latch
    per header.

    Stimulus: three rectangular headers (SourceTag 0x1111, 0x2222,
              0x3333), ready 1; after each 25-word capture one idle edge,
              so each pulse is sampled 2 edges after the previous
              header's last accept.
    Checks:   each header's 25 words pass `check_byte_replication` against
              its own `expected_rect_words`.
    Note:     the pulse lands one cycle later than the earliest slot the
              RTL accepts; the minimum gap, and a pulse in the last-accept
              cycle (which is dropped), are never exercised. kmask is not
              checked.
    """
    dut.TESTCASE.value = 8
    await reset(dut)
    seqs = []
    metas = [
        Meta(sourcetag=0x1111),
        Meta(sourcetag=0x2222),
        Meta(sourcetag=0x3333),
    ]
    for m in metas:
        await kick_off(dut, m, arbitrary=False)
        words, _ = await collect_words(dut, RECT_WORDS)
        seqs.append(words)
        # Single idle cycle between frames.
        await RisingEdge(dut.app_clk)

    for m, words in zip(metas, seqs):
        check_byte_replication(words, expected_rect_words(m))


# -----------------------------------------------------------------------------
# TC 9 — DsizeL In Words
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_dsizel_words(dut):
    """DsizeL is the 24-bit count of 32-bit data words per line (Table 38).

    Stimulus: rectangular headers for (Xsize, PixelF) = (641, Mono8),
              (641, Mono10), (641, Mono12), (0xFFFFFF, Mono16).
    Checks:   header words 17..19 carry ceil(Xsize × bits / 32), MSB first:
              0x0000A1, 0x0000C9, 0x0000F1, 0x800000 — the last needs the
              top byte.
    """
    dut.TESTCASE.value = 9
    await reset(dut)
    cases = [(641, 0x0101, 161), (641, 0x0102, 201), (641, 0x0103, 241),
             (0xFFFFFF, gs.PIXFMT_MONO16, 0x800000)]
    for xsize, pixfmt, want in cases:
        m = Meta(xsize=xsize, pixfmt=pixfmt)
        assert expected_dsizel(m) == want
        await kick_off(dut, m, arbitrary=False)
        words, _ = await collect_words(dut, RECT_WORDS)
        got = ((words[17] & 0xFF) << 16) | ((words[18] & 0xFF) << 8) | (words[19] & 0xFF)
        assert got == want, f"Xsize {xsize} PixelF 0x{pixfmt:04x}: DsizeL 0x{got:06x}, want 0x{want:06x}"
