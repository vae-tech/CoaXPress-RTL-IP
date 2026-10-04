"""Cocotb TB for `cxp_rx_8b10b_decoder`.

Combinational module — Python drives `din` and `rd_in`, waits a delta
cycle, samples outputs.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Encode→decode roundtrip for every D-code (0..255) at both RD-/RD+.
  2  All CXP K-codes (K28.0..K28.7, K27.7, K29.7, K23.7, K30.7) decode
     with k_out=1, dout matching, no errors.
  3  Running-disparity chain: rd_out follows the encoded RD transition.
  4  Code error — invalid 10-bit symbol → code_err=1.
  5  Disparity error — valid pattern presented with wrong RD → disp_err=1.
  6  Neutral-but-RD-specific D.7 and D.x.3 forms at the wrong RD →
     disp_err=1, and at the right RD → 0.
  8  Exhaustive: all 1024 symbols at both RD against an IEEE 802.3
     Clause 36 model written here from Tables 36-1 / 36-2 (not the golden
     encoder) → every legal (symbol, RD) decodes exactly with no error;
     every other pair flags `code_err` or `disp_err`.
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer

import cxp_8b10b as cxp
from cxp_testcase import cxp_test


CLK_PERIOD_NS = 10


async def init(dut):
    cocotb.start_soon(Clock(dut.tb_clk, CLK_PERIOD_NS, unit="ns").start())
    dut.din.value   = 0
    dut.rd_in.value = 0
    await Timer(1, unit="ns")


async def apply(dut, din: int, rd_in: int):
    dut.din.value   = din & 0x3FF
    dut.rd_in.value = rd_in & 1
    # Comb DUT — wait for value propagation.
    await Timer(2, unit="ns")


# -----------------------------------------------------------------------------
# TC 1 — D-code roundtrip
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_d_code_roundtrip(dut):
    """Every data byte decodes back to itself from both running disparities.

    Exercises every D row of the 5b/6b and 3b/4b decode tables together
    with the RD bookkeeping for legal data symbols.

    Stimulus: bytes 0x00..0xFF, each encoded by the Python model
              (`cxp_8b10b.encode_byte`, k_flag=False) at RD- and at RD+
              and applied with the matching `rd_in` — 512 vectors.
    Checks:   per vector `dout == byte`, `k_out == 0`, `code_err == 0`,
              `disp_err == 0`, and `rd_out` equals the model's leaving RD.
    Note:     the model never emits the D.x.A7 alternate form, so the six
              A7-mandatory bytes are applied in their P7 form, which the
              decoder accepts silently; legal A7 symbols are not covered.
    """
    dut.TESTCASE.value = 1
    await init(dut)

    for byte in range(256):
        for rd in (0, 1):
            sym, rd_exp = cxp.encode_byte(byte, k_flag=False, rd_in=rd)
            await apply(dut, sym, rd)
            assert int(dut.code_err.value) == 0, (
                f"D-code 0x{byte:02x} rd={rd} -> sym=0x{sym:03x}: code_err"
            )
            assert int(dut.disp_err.value) == 0, (
                f"D-code 0x{byte:02x} rd={rd}: disp_err"
            )
            assert int(dut.k_out.value) == 0, (
                f"D-code 0x{byte:02x}: k_out asserted"
            )
            assert int(dut.dout.value) == byte, (
                f"D-code 0x{byte:02x} rd={rd}: got 0x{int(dut.dout.value):02x}"
            )
            assert int(dut.rd_out.value) == rd_exp, (
                f"D-code 0x{byte:02x} rd={rd}: rd_out={int(dut.rd_out.value)} "
                f"vs {rd_exp}"
            )


# -----------------------------------------------------------------------------
# TC 2 — All CXP K-codes
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_k_codes(dut):
    """Every K-code used by CXP decodes as a control character.

    Covers the K28.x marker path (including the RD+ y-alias correction for
    K28.1/2/5/6) and the Kx.7 alternate forms for x in {23, 27, 29, 30}.

    Stimulus: the 12 K bytes K28.0..K28.7 (0x1C..0xFC), K23.7 (0xF7),
              K27.7 (0xFB), K29.7 (0xFD) and K30.7 (0xFE), each encoded
              with k_flag=True at RD- and at RD+ — 24 vectors.
    Checks:   per vector `k_out == 1`, `dout == kbyte`, `code_err == 0`,
              `disp_err == 0`, and `rd_out` equals the model's leaving RD.
    """
    dut.TESTCASE.value = 2
    await init(dut)

    k_set = [0x1C, 0x3C, 0x5C, 0x7C, 0x9C, 0xBC, 0xDC, 0xFC,
             0xF7, 0xFB, 0xFD, 0xFE]
    for kbyte in k_set:
        for rd in (0, 1):
            sym, rd_exp = cxp.encode_byte(kbyte, k_flag=True, rd_in=rd)
            await apply(dut, sym, rd)
            assert int(dut.code_err.value) == 0, f"K 0x{kbyte:02x} rd={rd}: code_err"
            assert int(dut.disp_err.value) == 0, f"K 0x{kbyte:02x} rd={rd}: disp_err"
            assert int(dut.k_out.value) == 1, f"K 0x{kbyte:02x}: k_out=0"
            assert int(dut.dout.value) == kbyte, (
                f"K 0x{kbyte:02x} rd={rd}: got 0x{int(dut.dout.value):02x}"
            )
            assert int(dut.rd_out.value) == rd_exp, (
                f"K 0x{kbyte:02x} rd={rd}: rd_out={int(dut.rd_out.value)} "
                f"vs {rd_exp}"
            )


# -----------------------------------------------------------------------------
# TC 3 — RD-chain through a sequence of bytes
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_rd_chain(dut):
    """`rd_out` can be fed back as the next `rd_in` across a symbol run.

    Mimics how `cxp_rx_link` chains the decoder: the leaving disparity of
    one symbol becomes the entering disparity of the next.

    Stimulus: mixed D/K sequence 0x00, K28.5, 0xFF, 0xAA, 0x55, K28.3,
              K28.0, K29.7, starting at RD-; each symbol is encoded and
              applied with the RD left by the previous one, so the run
              crosses both RD signs.
    Checks:   after each symbol `dout` equals the byte sent and `rd_out`
              equals the model's leaving RD.
    """
    dut.TESTCASE.value = 3
    await init(dut)

    byte_seq = [0x00, 0xBC, 0xFF, 0xAA, 0x55, 0x7C, 0x1C, 0xFD]
    kmask    = [0,    1,    0,    0,    0,    1,    1,    1]
    rd = 0
    for byte, k in zip(byte_seq, kmask):
        sym, rd_exp = cxp.encode_byte(byte, k_flag=bool(k), rd_in=rd)
        await apply(dut, sym, rd)
        assert int(dut.dout.value) == byte, (
            f"chain byte 0x{byte:02x}: dout=0x{int(dut.dout.value):02x}"
        )
        assert int(dut.rd_out.value) == rd_exp, (
            f"chain byte 0x{byte:02x}: rd_out={int(dut.rd_out.value)} vs {rd_exp}"
        )
        rd = rd_exp


# -----------------------------------------------------------------------------
# TC 4 — Code error on illegal symbol
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_code_error(dut):
    """Illegal 6-bit sub-blocks raise `code_err`.

    Stimulus: two 10-bit symbols whose 6-bit sub-block is 000000 or
              111111 (run length 6, absent from every table), paired with
              an arbitrary 4-bit sub-block and applied at RD-.
    Checks:   `code_err == 1` for both symbols.
    Note:     only the 5b/6b error term is exercised; 3b/4b errors and the
              forced `dout == 0` / `k_out == 0` on error are not checked.
    """
    dut.TESTCASE.value = 4
    await init(dut)

    # 6-bit sub-blocks not in the table: 000000 and 111111 are both
    # forbidden (run length 6).  Pair with arbitrary 4-bit sub-block.
    bad_subs = [
        0b0000_001111,   # bad sub6 = 000000 (din[5:0]=000000)
        0b0001_111111,   # bad sub6 = 111111
    ]
    for sym in bad_subs:
        await apply(dut, sym, 0)
        assert int(dut.code_err.value) == 1, (
            f"sym 0x{sym:03x}: expected code_err"
        )


# -----------------------------------------------------------------------------
# TC 5 — Disparity error when running disparity is wrong sign
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_disparity_error(dut):
    """A legal symbol applied at the wrong running disparity raises `disp_err`.

    Stimulus: D.0.0 encoded at RD- (6-bit block is the +2 form) and
              applied with `rd_in = 1`, i.e. entering at RD+.
    Checks:   `disp_err == 1`, and the data still decodes to 0x00.
    Note:     only the 6-bit "negative form at RD+" term is exercised; the
              "positive form at RD-" and 4-bit-only disparity terms are not.
    """
    dut.TESTCASE.value = 5
    await init(dut)

    # Pick a D-code whose encoding has non-neutral disparity (D.0 RD-
    # encoding requires entering at RD- → applying it at RD+ flags
    # disp_err).  D.0: x=0, polarity 'P' (+2 sub-block disparity).
    sym, _ = cxp.encode_byte(0x00, k_flag=False, rd_in=0)  # encoded at RD-
    # Drive with rd_in = 1 (i.e. RD+) → illegal disparity.
    await apply(dut, sym, 1)
    assert int(dut.disp_err.value) == 1, (
        f"D.0 RD- sym 0x{sym:03x} presented at RD+: expected disp_err"
    )
    assert int(dut.dout.value) == 0x00, "data should still decode to 0x00"


# -----------------------------------------------------------------------------
# TC 6 — D.7 / D.x.3 at the wrong running disparity
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_neutral_rd_forms(dut):
    """D.7 and D.x.3 have one form per RD even though both are neutral.

    IEEE 802.3 Table 36-1a: D.7 is 111000 at RD- and 000111 at RD+; y = 3
    is 1100 at RD- and 0011 at RD+.  The other form is a disparity error.

    Stimulus: D7.0 (0x07) and D3.3 (0x63) encoded by the golden model at
              RD- and at RD+, each applied with the matching and with the
              opposite `rd_in`.
    Checks:   `disp_err` = 0 with the matching RD, 1 with the opposite one;
              the byte decodes correctly either way.
    """
    dut.TESTCASE.value = 6
    await init(dut)
    for byte in (0x07, 0x63):
        for rd in (0, 1):
            sym, _ = cxp.encode_byte(byte, False, rd)
            await apply(dut, sym, rd)
            assert int(dut.disp_err.value) == 0, f"0x{byte:02x} at its own RD {rd}"
            await apply(dut, sym, 1 - rd)
            assert int(dut.disp_err.value) == 1, f"0x{byte:02x} RD{rd} form at RD{1 - rd}"
            assert int(dut.dout.value) == byte


# -----------------------------------------------------------------------------
# TC 8 — Exhaustive Against An Independent IEEE 802.3 Model
# -----------------------------------------------------------------------------
# IEEE 802.3 Table 36-1: 5b/6b "abcdei" and 3b/4b "fghj", (RD-, RD+) columns.
_6B = ["100111 011000", "011101 100010", "101101 010010", "110001 110001",
       "110101 001010", "101001 101001", "011001 011001", "111000 000111",
       "111001 000110", "100101 100101", "010101 010101", "110100 110100",
       "001101 001101", "101100 101100", "011100 011100", "010111 101000",
       "011011 100100", "100011 100011", "010011 010011", "110010 110010",
       "001011 001011", "101010 101010", "011010 011010", "111010 000101",
       "110011 001100", "100110 100110", "010110 010110", "110110 001001",
       "001110 001110", "101110 010001", "011110 100001", "101011 010100"]
_4B = ["1011 0100", "1001 1001", "0101 0101", "1100 0011",
       "1101 0010", "1010 1010", "0110 0110", "1110 0001"]
_A7 = ("0111", "1000")
# Table 36-2: the twelve special code-groups, (RD-, RD+).
_K = {0x1C: "0011110100 1100001011", 0x3C: "0011111001 1100000110",
      0x5C: "0011110101 1100001010", 0x7C: "0011110011 1100001100",
      0x9C: "0011110010 1100001101", 0xBC: "0011111010 1100000101",
      0xDC: "0011110110 1100001001", 0xFC: "0011111000 1100000111",
      0xF7: "1110101000 0001010111", 0xFB: "1101101000 0010010111",
      0xFD: "1011101000 0100010111", 0xFE: "0111101000 1000010111"}


def _din(bits: str) -> int:
    """'abcdeifghj' (a first on the line) -> din, a at bit 0."""
    return sum(int(c) << i for i, c in enumerate(bits))


def _rd_after(rd: int, block: str) -> int:
    ones = block.count("1")
    return rd if 2 * ones == len(block) else int(2 * ones > len(block))


def ieee_table() -> dict:
    """{(din, rd_in): (byte, k, rd_out)} for every legal pair."""
    t = {}
    for byte in range(256):
        x, y = byte & 0x1F, byte >> 5
        for rd in (0, 1):
            b6 = _6B[x].split()[rd]
            mid = _rd_after(rd, b6)
            if y == 7 and ((mid == 0 and x in (17, 18, 20)) or (mid == 1 and x in (11, 13, 14))):
                b4 = _A7[mid]
            else:
                b4 = _4B[y].split()[mid]
            t[(_din(b6 + b4), rd)] = (byte, 0, _rd_after(mid, b4))
    for byte, forms in _K.items():
        for rd in (0, 1):
            sym = forms.split()[rd]
            t[(_din(sym), rd)] = (byte, 1, _rd_after(_rd_after(rd, sym[:6]), sym[6:]))
    return t


@cxp_test()
async def test_08_exhaustive_2048(dut):
    """Every (symbol, RD) pair either decodes exactly or flags an error.

    §8.2.1: the uplink is 8B/10B coded as IEEE 802.3 Clause 36.  An
    illegal code-group that decodes silently turns a line error into
    wrong data with no error mark.

    Stimulus: din = 0..1023 at `rd_in` 0 and 1.
    Checks:   legal pairs (536): `dout`, `k_out`, `rd_out` as the model,
              no error; all others: `code_err` or `disp_err`.
    """
    dut.TESTCASE.value = 8
    await init(dut)
    table = ieee_table()
    assert len(table) == 2 * (256 + 12)
    wrong, silent = [], []
    for rd in (0, 1):
        for din in range(1024):
            await apply(dut, din, rd)
            err = int(dut.code_err.value) | int(dut.disp_err.value)
            exp = table.get((din, rd))
            if exp is None:
                if not err:
                    silent.append((din, rd, int(dut.dout.value), int(dut.k_out.value)))
                continue
            got = (int(dut.dout.value), int(dut.k_out.value), int(dut.rd_out.value))
            if err or got != exp:
                wrong.append((din, rd, got, exp, err))
    assert not wrong, f"{len(wrong)} legal pairs misdecoded, first {wrong[:6]}"
    assert not silent, (f"{len(silent)} illegal pairs without an error, first "
                        f"{[(f'{d:010b}'[::-1], r, hex(b), k) for d, r, b, k in silent[:8]]}")
