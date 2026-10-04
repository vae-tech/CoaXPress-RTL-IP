"""Cocotb TB for `cxp_app_pixel_packer`.

The DUT packs a one-pixel-per-cycle Mono stream into 32-bit P0..P3 words
(P0 = `m_word_data[31:24]`), zero-pads each line's last word, and moves
SOL/SOF to the first and EOL/EOF to the last word of a line/frame
(CXP 1.1.1 §9.4.2 Figures 27–31; v1.0 §7.4.2 figs 19–23). Verification
plan: `cxp_camera_ip_modules.md` §2.2 plus corner cases.

Single 10 ns `app_clk`; `reset()` holds `app_rst_n` low for 4 edges with
Mono8 selected and `m_word_ready` = 1. `drive_pixel` holds each pixel on
the `s_pix_*` handshake until `s_pix_ready`; `collect_words` records
accepted output words while driving `m_word_ready` from a repeating
pattern. `pack_line` is an independent bit-stream reference model; most
tests compare every word's data, `lane_vld` and flags against it.

`pack_line` packs Mono14 at 14 bits (Figure 30) and uses the Table 25 codes
(Mono16 = 0x0105). Known blind spot: `collect_words` returns
exactly n words, so a surplus output word is never seen and the word-count
asserts cannot fail.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Mono8 packing, 2 lines × 16 px, words and flags vs model.
  2  Mono10 packing, 2 × 16 px, words and flags vs model.
  3  Mono12 packing, 2 × 16 px, words and flags vs model.
  4  Mono14 packing (14-bit dense, Figure 30), 2 × 16 px, vs model.
  5  Mono16 (0x0105) packing, 2 × 8 px, vs model.
  6  Mono10 4-pixel line vs hand-built Figure 28 bytes (case B flush).
  7  Mono12 4-pixel line vs hand-built Figure 29 bytes.
  8  Mono8 xsize = 1023: last word per line has `lane_vld` = 0111.
  9  Mono8 xsize = 5, 3 lines: full word + 1-byte flush per line.
 10  Mono10 xsize = 3: one 30-bit word, `lane_vld` = 1111.
 11  Mono16 single-pixel line: 0xBEEF0000, `lane_vld` = 0011, all flags.
 12  PixelF change between frames: Mono8 frame then Mono16 frame.
 13  Mono10 under `m_word_ready` back-pressure, lossless incl. flags.
 14  Mono12 under `m_word_ready` back-pressure, lossless incl. flags.
 15  Mono12 with 3 idle cycles between input pixels.
 16  Reset mid-line, then a clean Mono10 line.
 17  Randomised mixed-format multi-frame regression (6 frames).
 18  A SOF with no EOL before it (a truncated line): the new frame's first
     word holds only its own pixels.
 19  Sensor samples (`s_pix_w` = 8, 12, 16): MSB-aligned into every Mono
     format (Figure 32) — a narrower format keeps the sample's MSBs.
"""

from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly, NextTimeStep

from cxp_testcase import cxp_test


CLK_PERIOD_NS = 10

PIXFMT_MONO8  = 0x0101
PIXFMT_MONO10 = 0x0102
PIXFMT_MONO12 = 0x0103
PIXFMT_MONO14 = 0x0104
PIXFMT_MONO16 = 0x0105   # Table 25

BPP = {
    PIXFMT_MONO8:  8,
    PIXFMT_MONO10: 10,
    PIXFMT_MONO12: 12,
    PIXFMT_MONO14: 14,   # source bits
    PIXFMT_MONO16: 16,
}

# Bits per pixel in the packed byte stream (§9.4.2: maximum density).
CBITS = dict(BPP)


# -----------------------------------------------------------------------------
# Reference model — bit-accurate, independent of the RTL
# -----------------------------------------------------------------------------
def pack_line(pixels: list[int], pixfmt: int) -> list[tuple[int, int]]:
    """Pack one line of pixels into a list of (word, lane_vld) tuples per
    the CXP §7.4.2 byte ordering.

    Returns a list of 32-bit words; the last word may have lane_vld < 0b1111.
    """
    cbits = CBITS[pixfmt]
    bpp   = BPP[pixfmt]
    mask  = (1 << bpp) - 1

    # Build a single MSB-first bit string by concatenating the pixels.
    bits = 0
    nbits = 0
    for px in pixels:
        bits = (bits << cbits) | (px & mask)
        nbits += cbits

    # Output bytes MSB-first.
    out_bytes = []
    # Pad to byte boundary at the LSB (the residual padding bits are zero).
    pad = (8 - (nbits % 8)) % 8
    bits_padded = bits << pad
    nbytes = (nbits + 7) // 8
    for i in range(nbytes - 1, -1, -1):
        out_bytes.append((bits_padded >> (8 * i)) & 0xFF)

    # Now chunk into 32-bit big-endian words, with lane_vld per the
    # number of valid bytes touched in the final word.
    words = []
    for w in range(0, len(out_bytes), 4):
        chunk = out_bytes[w:w + 4]
        nvalid = len(chunk)
        word = 0
        # P0 (byte 0) -> MSB of word; pad missing bytes with 0.
        for i in range(4):
            byte = chunk[i] if i < nvalid else 0
            word |= byte << (8 * (3 - i))
        # lane_vld[0] = P0 valid, lane_vld[1] = P1, ...
        lane_vld = (1 << nvalid) - 1
        words.append((word, lane_vld))
    return words


def pack_line_byte_count(xsize: int, pixfmt: int) -> int:
    """Number of bytes emitted on the wire for one line (= bytes per line)."""
    total_bits = xsize * CBITS[pixfmt]
    return (total_bits + 7) // 8


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def reset(dut, *, ready_init: int = 1):
    """Start `app_clk`, zero all inputs, pulse `app_rst_n` low for 4 edges."""
    cocotb.start_soon(Clock(dut.app_clk, CLK_PERIOD_NS, unit="ns").start())
    dut.app_rst_n.value     = 0
    dut.cfg_pixfmt.value    = PIXFMT_MONO8
    dut.s_pix_data.value    = 0
    dut.s_pix_w.value       = 0
    dut.s_pix_valid.value   = 0
    dut.s_pix_sol.value     = 0
    dut.s_pix_eol.value     = 0
    dut.s_pix_sof.value     = 0
    dut.s_pix_eof.value     = 0
    dut.m_word_ready.value  = ready_init
    for _ in range(4):
        await RisingEdge(dut.app_clk)
    dut.app_rst_n.value = 1
    for _ in range(2):
        await RisingEdge(dut.app_clk)


# -----------------------------------------------------------------------------
# Drivers / monitors
# -----------------------------------------------------------------------------
async def drive_pixel(dut, value, *, sol=0, eol=0, sof=0, eof=0, timeout=2000):
    """Drive a single pixel onto the DUT, honouring s_pix_ready."""
    dut.s_pix_data.value  = value & 0xFFFF
    dut.s_pix_valid.value = 1
    dut.s_pix_sol.value   = sol
    dut.s_pix_eol.value   = eol
    dut.s_pix_sof.value   = sof
    dut.s_pix_eof.value   = eof
    # Wait for handshake.
    for _ in range(timeout):
        await ReadOnly()
        if dut.s_pix_ready.value:
            break
        await RisingEdge(dut.app_clk)
    else:
        raise TimeoutError("DUT never asserted s_pix_ready")
    await RisingEdge(dut.app_clk)
    dut.s_pix_valid.value = 0
    dut.s_pix_sol.value   = 0
    dut.s_pix_eol.value   = 0
    dut.s_pix_sof.value   = 0
    dut.s_pix_eof.value   = 0


async def drive_pixel_stream(dut, pixels_with_tags, *, gap_cycles: int = 0,
                              timeout: int = 4000):
    """Drive a list of (value, tags) where tags is a dict with optional
    sol/eol/sof/eof bits.  Inserts `gap_cycles` idle cycles between pixels."""
    for value, tags in pixels_with_tags:
        await drive_pixel(dut, value, **tags, timeout=timeout)
        for _ in range(gap_cycles):
            dut.s_pix_valid.value = 0
            await RisingEdge(dut.app_clk)


async def collect_words(dut, n: int, ready_pattern: list[int] | None = None,
                         max_idle_cycles: int = 8192):
    """Capture the next `n` accepted output words from the DUT.

    Returns a list of dicts: {data, lane_vld, sol, eol, sof, eof}.
    """
    rp  = ready_pattern or [1]
    rpi = 0
    words: list[dict] = []
    idle = 0
    while len(words) < n:
        dut.m_word_ready.value = rp[rpi % len(rp)]
        rpi += 1
        await ReadOnly()
        if int(dut.m_word_valid.value) and int(dut.m_word_ready.value):
            words.append({
                "data":     int(dut.m_word_data.value),
                "lane_vld": int(dut.m_word_lane_vld.value),
                "sol":      int(dut.m_word_sol.value),
                "eol":      int(dut.m_word_eol.value),
                "sof":      int(dut.m_word_sof.value),
                "eof":      int(dut.m_word_eof.value),
            })
            idle = 0
        else:
            idle += 1
            if idle > max_idle_cycles:
                raise TimeoutError(f"No output progress after {idle} cycles; "
                                    f"got {len(words)}/{n} words")
        await RisingEdge(dut.app_clk)
    return words


async def drain_and_collect(dut, send_pixels, expected_words: int,
                             *, ready_pattern: list[int] | None = None,
                             gap_cycles: int = 0):
    """Drive pixels and collect expected_words concurrently."""
    collector = cocotb.start_soon(collect_words(dut, expected_words,
                                                ready_pattern=ready_pattern))
    await drive_pixel_stream(dut, send_pixels, gap_cycles=gap_cycles)
    # Allow the pipeline to drain.
    for _ in range(32):
        await RisingEdge(dut.app_clk)
    return await collector


# -----------------------------------------------------------------------------
# Checkers
# -----------------------------------------------------------------------------
async def _run_basic_format_check(dut, pixfmt: int, xsize: int, lines: int = 2,
                                   ready_pattern: list[int] | None = None):
    """Reset, send one random frame of `lines` × `xsize` px in `pixfmt`, and
    check every word's data, `lane_vld` and SOL/EOL/SOF/EOF vs `pack_line`."""
    await reset(dut)
    dut.cfg_pixfmt.value = pixfmt
    await RisingEdge(dut.app_clk)

    rng = random.Random(0x1234 ^ pixfmt ^ xsize)
    mask = (1 << BPP[pixfmt]) - 1

    # Build per-line pixel arrays.
    frame_pixels = [
        [rng.randint(0, mask) for _ in range(xsize)] for _ in range(lines)
    ]

    # Reference: words per frame.
    expected_words: list[tuple[int, int]] = []
    for li, line in enumerate(frame_pixels):
        expected_words.extend(pack_line(line, pixfmt))

    # Driving sequence with sol/eol/sof/eof tags.
    tagged: list[tuple[int, dict]] = []
    for li, line in enumerate(frame_pixels):
        for pi, px in enumerate(line):
            tags = {}
            if pi == 0:
                tags["sol"] = 1
                if li == 0:
                    tags["sof"] = 1
            if pi == len(line) - 1:
                tags["eol"] = 1
                if li == len(frame_pixels) - 1:
                    tags["eof"] = 1
            tagged.append((px, tags))

    got = await drain_and_collect(dut, tagged, len(expected_words),
                                    ready_pattern=ready_pattern)

    assert len(got) == len(expected_words), (
        f"word count mismatch: got {len(got)}, expected {len(expected_words)}"
    )

    # Compute the expected sol/eol/sof/eof markers per output word index.
    # SOL/SOF land on the FIRST output word of the line/frame; EOL/EOF on
    # the LAST output word of the line/frame.
    sol_idx, eol_idx = [], []
    sof_idx, eof_idx = [], []
    cursor = 0
    for li, line in enumerate(frame_pixels):
        n = len(pack_line(line, pixfmt))
        sol_idx.append(cursor)
        eol_idx.append(cursor + n - 1)
        if li == 0:
            sof_idx.append(cursor)
        if li == len(frame_pixels) - 1:
            eof_idx.append(cursor + n - 1)
        cursor += n

    for i, ((wexp, lvexp), wgot) in enumerate(zip(expected_words, got)):
        assert wgot["data"] == wexp, (
            f"word {i}: data mismatch got 0x{wgot['data']:08x} "
            f"vs exp 0x{wexp:08x} (fmt=0x{pixfmt:04x}, xsize={xsize})"
        )
        assert wgot["lane_vld"] == lvexp, (
            f"word {i}: lane_vld mismatch got 0x{wgot['lane_vld']:x} "
            f"vs exp 0x{lvexp:x}"
        )
        assert bool(wgot["sol"]) == (i in sol_idx), (
            f"word {i}: SOL marker mismatch (idx in sol_idx={i in sol_idx})"
        )
        assert bool(wgot["eol"]) == (i in eol_idx), (
            f"word {i}: EOL marker mismatch"
        )
        assert bool(wgot["sof"]) == (i in sof_idx), (
            f"word {i}: SOF marker mismatch"
        )
        assert bool(wgot["eof"]) == (i in eof_idx), (
            f"word {i}: EOF marker mismatch"
        )


# -----------------------------------------------------------------------------
# TC 1 — Mono8 Packing
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_mono8_packing(dut):
    """Mono8 lines pack four pixels per word with P0 in the MSB byte.

    §9.4.2 Figure 27 (plan test 1): 16 px per line ends every line on a
    full word (case A); SOL/SOF carry across the accumulate-only pixels.

    Stimulus: `cfg_pixfmt` = Mono8 (0x0101); one frame of 2 lines × 16
              random 8-bit px (seed `0x1234 ^ fmt ^ xsize`), SOF+SOL on
              pixel 0, EOL per line, EOF on the last; back-to-back pixels,
              `m_word_ready` = 1.
    Checks:   8 words; per word `data` and `lane_vld` equal `pack_line`,
              and each flag is set exactly where the model puts it: SOL on
              words 0 and 4, SOF on 0, EOL on 3 and 7, EOF on 7.
    """
    dut.TESTCASE.value = 1
    await _run_basic_format_check(dut, PIXFMT_MONO8, xsize=16, lines=2)


# -----------------------------------------------------------------------------
# TC 2 — Mono10 Packing
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_mono10_packing(dut):
    """Mono10 lines pack four pixels into five bytes, MSB-first.

    §9.4.2 Figure 28 (plan test 2): 16 px = 160 bits walks every Mono10
    accumulator phase and ends each line exactly on a word (case A).

    Stimulus: as TC 1 with `cfg_pixfmt` = Mono10 (0x0102) and random
              10-bit px; 2 lines × 16 px, `m_word_ready` = 1.
    Checks:   10 words (5 per line); per word `data`, `lane_vld` and all
              four flags equal the `pack_line` model.
    """
    dut.TESTCASE.value = 2
    await _run_basic_format_check(dut, PIXFMT_MONO10, xsize=16, lines=2)


# -----------------------------------------------------------------------------
# TC 3 — Mono12 Packing
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_mono12_packing(dut):
    """Mono12 lines pack two pixels into three bytes, MSB-first.

    §9.4.2 Figure 29 (plan test 3): 16 px = 192 bits = 6 words per line,
    ending in case A.

    Stimulus: as TC 1 with `cfg_pixfmt` = Mono12 (0x0103) and random
              12-bit px; 2 lines × 16 px, `m_word_ready` = 1.
    Checks:   12 words; per word `data`, `lane_vld` and all four flags
              equal the `pack_line` model.
    Note:     the Mono12 case B (residual flush) is not reached — it needs
              xsize mod 8 ∈ {3, 6}.
    """
    dut.TESTCASE.value = 3
    await _run_basic_format_check(dut, PIXFMT_MONO12, xsize=16, lines=2)


# -----------------------------------------------------------------------------
# TC 4 — Mono14 Packing
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_mono14_packing(dut):
    """Mono14 pixels pack densely at 14 bits (§9.4.2, Figure 30).

    Stimulus: as TC 1 with `cfg_pixfmt` = Mono14 (0x0104) and random
              14-bit px; 2 lines × 16 px, `m_word_ready` = 1.
    Checks:   14 words (7 per line: 16 × 14 bits = 224 bits); per word
              `data`, `lane_vld` and all four flags equal the `pack_line`
              model.
    """
    dut.TESTCASE.value = 4
    await _run_basic_format_check(dut, PIXFMT_MONO14, xsize=16, lines=2)


# -----------------------------------------------------------------------------
# TC 5 — Mono16 Packing
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_mono16_packing(dut):
    """Mono16 lines pack two pixels per word as big-endian halves.

    §9.4.2 Figure 31 (plan test 5): pixel bits [15:8] land in P0.

    Stimulus: as TC 1 with `cfg_pixfmt` = 0x0105 and random 16-bit px;
              2 lines × 8 px, `m_word_ready` = 1.
    Checks:   8 words (4 per line); per word `data`, `lane_vld` and all
              four flags equal the `pack_line` model.
    """
    dut.TESTCASE.value = 5
    await _run_basic_format_check(dut, PIXFMT_MONO16, xsize=8, lines=2)


# -----------------------------------------------------------------------------
# TC 6 — Mono10 Explicit Bit Layout
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_mono10_bit_layout_explicit(dut):
    """Four known Mono10 pixels produce the exact Figure 28 byte sequence.

    Cross-checks the bit order against a hand-written byte formula rather
    than `pack_line`; 40 bits at EOL exercises case B — a full word plus a
    queued flush word that carries EOL/EOF.

    Stimulus: Mono10 px 0x3AB, 0x123, 0x2C9, 0x07E as one line/frame
              (SOF+SOL on the first, EOL+EOF on the last), back-to-back,
              `m_word_ready` = 1.
    Checks:   2 words; word 0 = hand-built bytes 0–3, `lane_vld` = 1111,
              SOL = SOF = 1, EOL = EOF = 0; word 1 = byte 4 in P0,
              `lane_vld` = 0001, EOL = EOF = 1.
    Note:     word 1's SOL/SOF are not checked.
    """
    dut.TESTCASE.value = 6
    await reset(dut)
    dut.cfg_pixfmt.value = PIXFMT_MONO10
    await RisingEdge(dut.app_clk)

    pixels = [0x3AB, 0x123, 0x2C9, 0x07E]  # arbitrary 10-bit values
    # 40 bits = 5 bytes:
    #   byte0 = P0[9:2]
    #   byte1 = P0[1:0]|P1[9:4]
    #   byte2 = P1[3:0]|P2[9:6]
    #   byte3 = P2[5:0]|P3[9:8]
    #   byte4 = P3[7:0]
    P0, P1, P2, P3 = pixels
    bytes_exp = [
        (P0 >> 2) & 0xFF,
        ((P0 & 0x3) << 6) | ((P1 >> 4) & 0x3F),
        ((P1 & 0xF) << 4) | ((P2 >> 6) & 0xF),
        ((P2 & 0x3F) << 2) | ((P3 >> 8) & 0x3),
        P3 & 0xFF,
    ]

    tagged = [
        (pixels[0], {"sol": 1, "sof": 1}),
        (pixels[1], {}),
        (pixels[2], {}),
        (pixels[3], {"eol": 1, "eof": 1}),
    ]
    got = await drain_and_collect(dut, tagged, 2)
    assert len(got) == 2
    # Word 0 = bytes 0..3
    word0 = (bytes_exp[0] << 24) | (bytes_exp[1] << 16) | (bytes_exp[2] << 8) | bytes_exp[3]
    word1 = bytes_exp[4] << 24
    assert got[0]["data"] == word0
    assert got[0]["lane_vld"] == 0b1111
    assert got[0]["sol"] == 1 and got[0]["sof"] == 1
    assert got[0]["eol"] == 0 and got[0]["eof"] == 0
    assert got[1]["data"] == word1
    assert got[1]["lane_vld"] == 0b0001  # only P0 (byte 0) valid
    assert got[1]["eol"] == 1 and got[1]["eof"] == 1


# -----------------------------------------------------------------------------
# TC 7 — Mono12 Explicit Bit Layout
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_mono12_bit_layout_explicit(dut):
    """Four known Mono12 pixels produce the exact Figure 29 byte sequence.

    Hand-written byte formula, independent of `pack_line`: 48 bits give a
    full word at pixel 3 (case C) and a 2-byte EOL word at pixel 4 (case D).

    Stimulus: Mono12 px 0xABC, 0x123, 0xDEF, 0x456 as one line/frame
              (SOF+SOL on the first, EOL+EOF on the last), back-to-back,
              `m_word_ready` = 1.
    Checks:   word 0 = hand-built bytes 0–3 with `lane_vld` = 1111;
              word 1 = bytes 4–5 in P0/P1 with `lane_vld` = 0011 and
              EOL = EOF = 1.
    Note:     word 0's flags and word 1's SOL/SOF are not checked.
    """
    dut.TESTCASE.value = 7
    await reset(dut)
    dut.cfg_pixfmt.value = PIXFMT_MONO12
    await RisingEdge(dut.app_clk)

    # 2 px = 3 bytes; do 4 px to get one full word + one partial flush.
    pixels = [0xABC, 0x123, 0xDEF, 0x456]
    bytes_exp = []
    P0, P1 = pixels[0], pixels[1]
    bytes_exp += [
        (P0 >> 4) & 0xFF,
        ((P0 & 0xF) << 4) | ((P1 >> 8) & 0xF),
        P1 & 0xFF,
    ]
    P2, P3 = pixels[2], pixels[3]
    bytes_exp += [
        (P2 >> 4) & 0xFF,
        ((P2 & 0xF) << 4) | ((P3 >> 8) & 0xF),
        P3 & 0xFF,
    ]

    tagged = [
        (pixels[0], {"sol": 1, "sof": 1}),
        (pixels[1], {}),
        (pixels[2], {}),
        (pixels[3], {"eol": 1, "eof": 1}),
    ]
    got = await drain_and_collect(dut, tagged, 2)
    # 4 px × 12b = 48 bits = 6 bytes = 1 word(32b)+1 flush(16b)
    word0 = sum(bytes_exp[i] << (8 * (3 - i)) for i in range(4))
    word1 = (bytes_exp[4] << 24) | (bytes_exp[5] << 16)
    assert got[0]["data"] == word0 and got[0]["lane_vld"] == 0b1111
    assert got[1]["data"] == word1 and got[1]["lane_vld"] == 0b0011  # P0,P1
    assert got[1]["eol"] == 1 and got[1]["eof"] == 1


# -----------------------------------------------------------------------------
# TC 8 — Mono8 Non-Word-Aligned Line
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_mono8_xsize_not_aligned(dut):
    """A 1023-pixel Mono8 line ends on a 3-byte word with `lane_vld` = 0111.

    Plan test 6 (EOL boundary): the partial last word is flushed with zero
    padding (case D, 24 bits) and the next line restarts in P0.

    Stimulus: Mono8, one frame of 2 lines × 1023 random px (seed
              `0x1234 ^ fmt ^ xsize`), framed as in TC 1,
              `m_word_ready` = 1.
    Checks:   512 words; per word `data`, `lane_vld` (0111 on each line's
              last word) and all four flags equal the `pack_line` model.
    """
    dut.TESTCASE.value = 8
    await _run_basic_format_check(dut, PIXFMT_MONO8, xsize=1023, lines=2)


# -----------------------------------------------------------------------------
# TC 9 — Mono8 Five-Pixel Lines
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_mono8_xsize_5(dut):
    """Five-pixel Mono8 lines give one full word plus a 1-byte flush each.

    Case D with the previous word consumed in the same cycle, and no
    bubble between back-to-back lines.

    Stimulus: Mono8, one frame of 3 lines × 5 random px, framed as in
              TC 1, back-to-back, `m_word_ready` = 1.
    Checks:   6 words; per line a full word with SOL then a `lane_vld` =
              0001 word with EOL; SOF on word 0, EOF on word 5 — all words'
              `data`, `lane_vld` and flags vs the `pack_line` model.
    """
    dut.TESTCASE.value = 9
    await _run_basic_format_check(dut, PIXFMT_MONO8, xsize=5, lines=3)


# -----------------------------------------------------------------------------
# TC 10 — Mono10 Three-Pixel Line
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_mono10_xsize_3(dut):
    """A 3-pixel Mono10 line (30 bits) flushes as one word with 4 lanes valid.

    Case D with fill = 30: the two padding LSBs must be zero and all four
    bytes count as touched.

    Stimulus: Mono10 px 0x355, 0x2AA, 0x1F1 as one line/frame (SOF+SOL on
              the first, EOL+EOF on the last), `m_word_ready` = 1.
    Checks:   word 0 `data` equals an inline independent 30-bit pack padded
              to 32 bits; `lane_vld` = 1111.
    Note:     flags are not checked.
    """
    dut.TESTCASE.value = 10
    await reset(dut)
    dut.cfg_pixfmt.value = PIXFMT_MONO10
    await RisingEdge(dut.app_clk)

    pixels = [0x355, 0x2AA, 0x1F1]
    bytes_exp = []
    nbits_total = 30
    # Independent reference: pack into a single integer.
    big = 0
    for p in pixels:
        big = (big << 10) | (p & 0x3FF)
    big <<= (8 - (nbits_total % 8)) % 8  # pad to byte boundary
    nbytes = (nbits_total + 7) // 8
    for i in range(nbytes - 1, -1, -1):
        bytes_exp.append((big >> (8 * i)) & 0xFF)
    tagged = [
        (pixels[0], {"sol": 1, "sof": 1}),
        (pixels[1], {}),
        (pixels[2], {"eol": 1, "eof": 1}),
    ]
    got = await drain_and_collect(dut, tagged, 1)
    word = sum(bytes_exp[i] << (8 * (3 - i)) for i in range(4))
    assert got[0]["data"] == word
    assert got[0]["lane_vld"] == 0b1111  # 30 bits → 4 bytes touched


# -----------------------------------------------------------------------------
# TC 11 — Mono16 Single-Pixel Line
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_mono16_single_pixel_line(dut):
    """A 1-pixel Mono16 line flushes as P0/P1 with all four flags on one word.

    Case D on the very first pixel: SOL/SOF come from the live pixel
    (nothing pending) and EOL/EOF land on the same word.

    Stimulus: `cfg_pixfmt` = 0x0105, one pixel 0xBEEF with SOL, EOL, SOF
              and EOF all set, `m_word_ready` = 1.
    Checks:   `data` = 0xBEEF0000, `lane_vld` = 0011, SOL = SOF = EOL =
              EOF = 1.
    """
    dut.TESTCASE.value = 11
    await reset(dut)
    dut.cfg_pixfmt.value = PIXFMT_MONO16
    await RisingEdge(dut.app_clk)

    tagged = [(0xBEEF, {"sol": 1, "sof": 1, "eol": 1, "eof": 1})]
    got = await drain_and_collect(dut, tagged, 1)
    assert got[0]["data"] == 0xBEEF_0000
    assert got[0]["lane_vld"] == 0b0011  # P0 (lane0), P1 (lane1) valid
    assert got[0]["sol"] and got[0]["sof"] and got[0]["eol"] and got[0]["eof"]


# -----------------------------------------------------------------------------
# TC 12 — PixelF Change Between Frames
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_pixfmt_change_between_frames(dut):
    """A `cfg_pixfmt` change between frames takes effect at the next SOF.

    Plan test 7: the SOF pixel uses the live `cfg_pixfmt`, which is then
    latched for the frame.

    Stimulus: frame 1 — Mono8, 1 line × 8 random px (seed 0x55), SOF+SOL
              on pixel 0, EOL on the last, no EOF; then `cfg_pixfmt` =
              0x0105 for 8 idle cycles; frame 2 — 1 line × 8 random 16-bit
              px with SOF+SOL / EOL+EOF. `m_word_ready` = 1; the collector
              runs across both frames.
    Checks:   6 words; `data` and `lane_vld` equal the Mono8 model of
              frame 1 (2 words) followed by the Mono16 model of frame 2
              (4 words).
    Note:     the change lands after frame 1's last pixel, so a change
              inside a frame (latch hold) is never driven; flags are not
              checked.
    """
    dut.TESTCASE.value = 12
    await reset(dut)
    # Frame 1: Mono8, 8 px / line, 1 line
    dut.cfg_pixfmt.value = PIXFMT_MONO8
    await RisingEdge(dut.app_clk)

    rng = random.Random(0x55)
    frame1_pixels = [rng.randint(0, 0xFF) for _ in range(8)]
    tagged = []
    for i, p in enumerate(frame1_pixels):
        tags = {}
        if i == 0:
            tags["sol"] = 1
            tags["sof"] = 1
        if i == len(frame1_pixels) - 1:
            tags["eol"] = 1
        tagged.append((p, tags))

    collector = cocotb.start_soon(collect_words(dut, 2 + 4))
    await drive_pixel_stream(dut, tagged)

    # Change cfg_pixfmt after frame 1's last pixel (BEFORE the next SOF) —
    # it must take effect only at SOF of frame 2.
    dut.cfg_pixfmt.value = PIXFMT_MONO16
    for _ in range(8):
        await RisingEdge(dut.app_clk)

    # Frame 2: Mono16, 8 px / line, 1 line — but EOF needed
    frame2_pixels = [rng.randint(0, 0xFFFF) for _ in range(8)]
    tagged2 = []
    for i, p in enumerate(frame2_pixels):
        tags = {}
        if i == 0:
            tags["sol"] = 1
            tags["sof"] = 1
        if i == len(frame2_pixels) - 1:
            tags["eol"] = 1
            tags["eof"] = 1
        tagged2.append((p, tags))
    await drive_pixel_stream(dut, tagged2)
    for _ in range(16):
        await RisingEdge(dut.app_clk)
    got = await collector

    # Frame 1: 8 Mono8 px = 2 words.
    exp1 = pack_line(frame1_pixels, PIXFMT_MONO8)
    # Frame 2: 8 Mono16 px = 4 words.
    exp2 = pack_line(frame2_pixels, PIXFMT_MONO16)
    expected = exp1 + exp2
    assert len(got) == len(expected), f"got {len(got)}, expected {len(expected)}"
    for i, ((wexp, lvexp), wgot) in enumerate(zip(expected, got)):
        assert wgot["data"] == wexp, (
            f"word {i}: data 0x{wgot['data']:08x} != exp 0x{wexp:08x}"
        )
        assert wgot["lane_vld"] == lvexp


# -----------------------------------------------------------------------------
# TC 13 — Mono10 Back-Pressure
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_backpressure_mono10(dut):
    """Mono10 packing is lossless under intermittent `m_word_ready` = 0.

    While a word is held no pixel is accepted; 23 px (≡ 7 mod 16) ends each
    line in case B, so the queued flush word holds `s_pix_ready` low while
    the next line's first pixel is waiting.

    Stimulus: Mono10, one frame of 3 lines × 23 random px, framed as in
              TC 1, back-to-back pixels; `m_word_ready` driven every cycle
              from the repeating pattern 1,0,0,1,0,1,1.
    Checks:   24 words; per word `data`, `lane_vld` and all four flags
              equal the `pack_line` model.
    """
    dut.TESTCASE.value = 13
    await _run_basic_format_check(dut, PIXFMT_MONO10, xsize=23, lines=3,
                                   ready_pattern=[1, 0, 0, 1, 0, 1, 1])


# -----------------------------------------------------------------------------
# TC 14 — Mono12 Back-Pressure
# -----------------------------------------------------------------------------
@cxp_test()
async def test_14_backpressure_mono12(dut):
    """Mono12 packing is lossless under intermittent `m_word_ready` = 0.

    Same stall rule as TC 13 for the Mono12 phases; 17 px lines end in
    case D (a 2-byte last word).

    Stimulus: Mono12, one frame of 2 lines × 17 random px, framed as in
              TC 1, back-to-back pixels; `m_word_ready` driven every cycle
              from the repeating pattern 0,1,0,0,1.
    Checks:   14 words; per word `data`, `lane_vld` and all four flags
              equal the `pack_line` model.
    """
    dut.TESTCASE.value = 14
    await _run_basic_format_check(dut, PIXFMT_MONO12, xsize=17, lines=2,
                                   ready_pattern=[0, 1, 0, 0, 1])


# -----------------------------------------------------------------------------
# TC 15 — Input Gaps
# -----------------------------------------------------------------------------
@cxp_test()
async def test_15_input_gaps(dut):
    """Idle `s_pix_valid` = 0 cycles between pixels do not change the output.

    The accumulator must hold its partial word across input bubbles.

    Stimulus: Mono12, one line/frame of 32 random 12-bit px (seed
              0xC0FFEE) with SOF+SOL / EOL+EOF, 3 idle cycles after every
              accepted pixel, `m_word_ready` = 1.
    Checks:   per word `data` and `lane_vld` equal the `pack_line` model
              (12 words).
    Note:     flags are not checked.
    """
    dut.TESTCASE.value = 15
    await reset(dut)
    dut.cfg_pixfmt.value = PIXFMT_MONO12
    await RisingEdge(dut.app_clk)
    rng = random.Random(0xC0FFEE)
    pixels = [rng.randint(0, 0xFFF) for _ in range(32)]
    tagged = []
    for i, p in enumerate(pixels):
        tags = {}
        if i == 0:
            tags["sol"] = 1
            tags["sof"] = 1
        if i == len(pixels) - 1:
            tags["eol"] = 1
            tags["eof"] = 1
        tagged.append((p, tags))
    exp = pack_line(pixels, PIXFMT_MONO12)
    got = await drain_and_collect(dut, tagged, len(exp), gap_cycles=3)
    for i, ((wexp, lvexp), wgot) in enumerate(zip(exp, got)):
        assert wgot["data"] == wexp, f"word {i}"
        assert wgot["lane_vld"] == lvexp


# -----------------------------------------------------------------------------
# TC 16 — Reset Mid-Frame
# -----------------------------------------------------------------------------
@cxp_test()
async def test_16_reset_mid_frame(dut):
    """A reset in the middle of a line leaves the packer clean for a new frame.

    The async reset must discard the partially filled accumulator so the
    next frame starts in P0 with fresh flags.

    Stimulus: Mono10 px 0x111 (SOF+SOL), 0x222, 0x333 — 30 bits, no word
              out — then `app_rst_n` low for 4 edges with `s_pix_valid` =
              0; after release a clean Mono10 line 0x101, 0x202, 0x303,
              0x404 with SOF+SOL / EOL+EOF, `m_word_ready` = 1.
    Checks:   one edge after release `m_word_valid` = 0 and `s_pix_ready`
              = 1; the clean line's `data`/`lane_vld` equal `pack_line`,
              with SOF+SOL on the first word and EOL+EOF on the last.
    Note:     at the reset no output word is held and no flush is queued,
              so clearing of those registers is not exercised.
    """
    dut.TESTCASE.value = 16
    await reset(dut)
    dut.cfg_pixfmt.value = PIXFMT_MONO10
    await RisingEdge(dut.app_clk)

    # Drive a few pixels of a frame, then reset.
    tagged = [
        (0x111, {"sol": 1, "sof": 1}),
        (0x222, {}),
        (0x333, {}),
    ]
    await drive_pixel_stream(dut, tagged)

    # Now reset.
    dut.app_rst_n.value   = 0
    dut.s_pix_valid.value = 0
    for _ in range(4):
        await RisingEdge(dut.app_clk)
    dut.app_rst_n.value = 1
    await RisingEdge(dut.app_clk)
    # State should be cleared: m_word_valid=0, s_pix_ready=1.
    await ReadOnly()
    assert int(dut.m_word_valid.value) == 0
    assert int(dut.s_pix_ready.value) == 1
    await NextTimeStep()

    # Run a clean frame after reset.
    await RisingEdge(dut.app_clk)
    dut.cfg_pixfmt.value = PIXFMT_MONO10
    await RisingEdge(dut.app_clk)
    pixels = [0x101, 0x202, 0x303, 0x404]
    tagged = []
    for i, p in enumerate(pixels):
        tags = {}
        if i == 0:
            tags["sol"] = 1
            tags["sof"] = 1
        if i == len(pixels) - 1:
            tags["eol"] = 1
            tags["eof"] = 1
        tagged.append((p, tags))
    exp = pack_line(pixels, PIXFMT_MONO10)
    got = await drain_and_collect(dut, tagged, len(exp))
    for (wexp, lvexp), wgot in zip(exp, got):
        assert wgot["data"] == wexp
        assert wgot["lane_vld"] == lvexp
    # First post-reset word must carry SOF.
    assert got[0]["sof"] == 1
    assert got[0]["sol"] == 1
    assert got[-1]["eol"] == 1
    assert got[-1]["eof"] == 1


# -----------------------------------------------------------------------------
# TC 17 — Randomised Mixed-Format Multi-Frame
# -----------------------------------------------------------------------------
@cxp_test()
async def test_17_random_multiframe(dut):
    """Random formats, sizes, stalls and gaps pack correctly frame after frame.

    Exercises `cfg_pixfmt` changes between frames combined with output
    back-pressure and input bubbles.

    Stimulus: `random.Random(0xDEADBEEF)`, 6 frames; per frame a format
              from the five decoded codes, xsize 1–24, 1–4 lines, a ready
              pattern from {1}, {1,0}, {1,1,0}, {1,0,1,1} and a gap of
              0/0/1/2 cycles; `cfg_pixfmt` set one edge before each frame,
              4 idle edges after it. Drawn: Mono8 14×4, Mono14 22×1,
              Mono12 17×2, Mono14 16×4, 0x0105 4×4, Mono12 23×1.
    Checks:   per word `data` and `lane_vld` equal the `pack_line` model.
    Note:     flags are not checked; Mono10 is never drawn and no line ends
              in case B.
    """
    dut.TESTCASE.value = 17
    await reset(dut)
    rng = random.Random(0xDEADBEEF)
    formats = [PIXFMT_MONO8, PIXFMT_MONO10, PIXFMT_MONO12,
               PIXFMT_MONO14, PIXFMT_MONO16]
    for trial in range(6):
        fmt = rng.choice(formats)
        xsize = rng.randint(1, 24)
        lines = rng.randint(1, 4)
        ready = rng.choice([[1], [1, 0], [1, 1, 0], [1, 0, 1, 1]])
        gap   = rng.choice([0, 0, 1, 2])
        cocotb.log.info(
            "trial %d: fmt=0x%04x xsize=%d lines=%d ready=%s gap=%d",
            trial, fmt, xsize, lines, ready, gap
        )
        dut.cfg_pixfmt.value = fmt
        await RisingEdge(dut.app_clk)

        mask = (1 << BPP[fmt]) - 1
        frame_pixels = [
            [rng.randint(0, mask) for _ in range(xsize)] for _ in range(lines)
        ]
        expected = []
        for li, line in enumerate(frame_pixels):
            expected.extend(pack_line(line, fmt))

        tagged = []
        for li, line in enumerate(frame_pixels):
            for pi, px in enumerate(line):
                tags = {}
                if pi == 0:
                    tags["sol"] = 1
                    if li == 0:
                        tags["sof"] = 1
                if pi == len(line) - 1:
                    tags["eol"] = 1
                    if li == lines - 1:
                        tags["eof"] = 1
                tagged.append((px, tags))

        got = await drain_and_collect(dut, tagged, len(expected),
                                        ready_pattern=ready, gap_cycles=gap)
        for i, ((wexp, lvexp), wgot) in enumerate(zip(expected, got)):
            assert wgot["data"] == wexp, (
                f"trial {trial} word {i}: data 0x{wgot['data']:08x} "
                f"!= exp 0x{wexp:08x} (fmt=0x{fmt:04x}, xsize={xsize}, line {i})"
            )
            assert wgot["lane_vld"] == lvexp, (
                f"trial {trial} word {i}: lane_vld 0x{wgot['lane_vld']:x} "
                f"!= exp 0x{lvexp:x}"
            )
        # Idle between frames.
        for _ in range(4):
            await RisingEdge(dut.app_clk)


# -----------------------------------------------------------------------------
# TC 18 — SOF Without A Prior EOL
# -----------------------------------------------------------------------------
@cxp_test()
async def test_18_sof_without_prior_eol(dut):
    """A new frame starts on an empty accumulator.

    §9.4.2: every line starts in P0 of a new word.  A frame cut off
    mid-line (no EOL, no EOF) must not leak into the next one.

    Stimulus: Mono8, `m_word_ready` = 1: pixels 0xA1 (SOF+SOL), 0xA2 — no
              EOL; then a 4-pixel frame 0x01..0x04 (SOF+SOL first,
              EOL+EOF last); 40 cycles.
    Checks:   exactly one output word, equal to `pack_line([1, 2, 3, 4])`
              with SOF+SOL and EOL+EOF set.
    """
    dut.TESTCASE.value = 18
    await reset(dut)
    words = []

    async def watch():
        while True:
            await ReadOnly()
            if int(dut.m_word_valid.value) and int(dut.m_word_ready.value):
                words.append((int(dut.m_word_data.value), int(dut.m_word_lane_vld.value),
                              int(dut.m_word_sof.value), int(dut.m_word_eof.value)))
            await RisingEdge(dut.app_clk)

    w = cocotb.start_soon(watch())
    await drive_pixel_stream(dut, [(0xA1, {"sol": 1, "sof": 1}), (0xA2, {})])
    await drive_pixel_stream(dut, [(0x01, {"sol": 1, "sof": 1}), (0x02, {}), (0x03, {}),
                                   (0x04, {"eol": 1, "eof": 1})])
    for _ in range(40):
        await RisingEdge(dut.app_clk)
    w.cancel()
    (data, lanes), = pack_line([1, 2, 3, 4], PIXFMT_MONO8)
    assert words == [(data, lanes, 1, 1)], [tuple(hex(x) for x in w_) for w_ in words]


# -----------------------------------------------------------------------------
# TC 19 — Sensor Samples MSB-Aligned Into The Format
# -----------------------------------------------------------------------------
def msb_align(sample: int, width: int, bits: int) -> int:
    """§9.4.2 / Figure 32: a `width`-bit sample in a `bits`-bit format,
    MSB-aligned — zero LSBs when the format is wider, the sample's MSBs
    when it is narrower."""
    if bits >= width:
        return sample << (bits - width)
    return sample >> (width - bits)


@cxp_test()
async def test_19_sensor_sample_msb_aligned(dut):
    """A sensor sample keeps its most significant bits in every format.

    §9.4.1 / Table 25: PixelF names the format on the wire; §9.4.2 Figure
    32: in-between sizes are MSB-aligned into the next larger size.  A
    12-bit sensor sending Mono8 must send its top 8 bits, not the low
    byte; sending Mono16 it must fill the LSBs with zeros.

    Stimulus: for `s_pix_w` in (8, 12, 16) and each of Mono8..Mono16: one
              frame of 2 lines x 9 random `s_pix_w`-bit samples (SOF on the
              first, EOL per line, EOF on the last).
    Checks:   every word's data and `lane_vld` equal `pack_line` of the
              MSB-aligned values; a 12-bit 0xABC as Mono8 is 0xAB.
    """
    dut.TESTCASE.value = 19
    await reset(dut)
    rng = random.Random(19)
    for width in (8, 12, 16):
        for fmt in (PIXFMT_MONO8, PIXFMT_MONO10, PIXFMT_MONO12, PIXFMT_MONO14, PIXFMT_MONO16):
            dut.cfg_pixfmt.value = fmt
            dut.s_pix_w.value = width
            await RisingEdge(dut.app_clk)
            lines = [[rng.randrange(1 << width) for _ in range(9)] for _ in range(2)]
            if width == 12:
                lines[0][0] = 0xABC
            exp = []
            tagged = []
            for li, line in enumerate(lines):
                exp.extend(pack_line([msb_align(v, width, BPP[fmt]) for v in line], fmt))
                for pi, v in enumerate(line):
                    tags = {}
                    if pi == 0:
                        tags["sol"] = 1
                        if li == 0:
                            tags["sof"] = 1
                    if pi == len(line) - 1:
                        tags["eol"] = 1
                        if li == len(lines) - 1:
                            tags["eof"] = 1
                    tagged.append((v, tags))
            got = await drain_and_collect(dut, tagged, len(exp))
            for i, ((wexp, lvexp), g) in enumerate(zip(exp, got)):
                assert (g["data"], g["lane_vld"]) == (wexp, lvexp), (
                    f"s_pix_w {width} fmt 0x{fmt:04x} word {i}: "
                    f"got 0x{g['data']:08x}/{g['lane_vld']:x}, exp 0x{wexp:08x}/{lvexp:x}")
            if width == 12 and fmt == PIXFMT_MONO8:
                assert got[0]["data"] >> 24 == 0xAB, f"first Mono8 byte 0x{got[0]['data'] >> 24:02x}"
