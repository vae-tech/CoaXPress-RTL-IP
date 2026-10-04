"""Cocotb TB for `cxp_lib_crc32`.

Parallel CRC-32 accumulator: each clock folds the enabled bytes of `din`
(P0 = `din[7:0]` first, bit 0 first) into a reflected 0x04C11DB7 register
seeded with 0xFFFFFFFF, and outputs the register itself (§8.2.2.2: no final
XOR). The wrapper
instantiates a word-wise (`p_IN_W = 32`, ports `w_*`) and a byte-wise
(`p_IN_W = 8`, ports `b_*`) instance on a shared 10 ns `clk` and async
`rst_n`, so one run exercises both widths and cross-checks them.

Drivers present one valid beat per call (`w_feed` / `b_feed`, back-to-back
when chained), a data-less `init` cycle (`w_seed` / `b_seed`) or idle cycles
(`w_stall`). Each test samples `w_crc` / `b_crc` in `ReadOnly` — normally one
idle cycle after the last beat — and compares with the golden
`cxp_protocol.crc.crc32` register over the same byte stream (zlib without its
final XOR). There is no shared checker and no FSM.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Reset leaves both instances at seed (`crc == 0xFFFFFFFF`).
  2  Data-less `init` pulse gives the seed at both widths.
  3  Byte-wise "123456789" gives ~0xCBF43926 (check value, no final XOR).
  4  Word-wise fold over two full words 00..07 matches the reference.
  5  20 random buffers: word-wise and byte-wise both match the reference.
  6  Partial word with BE 0x1/0x3/0x7/0xF folds only the enabled lanes.
  7  Idle gaps of 0–4 cycles between beats do not perturb the CRC.
  8  `init` co-asserted with the first data beat seeds, then folds it.
  9  8 packets, each reseeded by `init`, each matches its own reference.
 10  Single-bit flip: RTL tracks the reference on both, CRCs differ.
 11  `init` then 5 idle cycles gives the seed.
 12  Valid beats with BE = 0x0 are no-ops.
 13  Stream-packet shape (data words only, Table 19) matches the reference.
 14  Async reset mid-packet returns the accumulator to seed.
 15  1 KB random payload matches the reference.
 16  §8.2.2.2 worked example: CRC word 56 86 5D 6F, residue 0 after it.
"""

from __future__ import annotations

import random
import zlib

from cxp_protocol import crc as gcrc

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly, NextTimeStep

from cxp_testcase import cxp_test


CLK_PERIOD_NS = 10


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def bringup(dut):
    """Start the clock and assert async reset for 4 cycles."""
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start(start_high=False))
    dut.rst_n.value       = 0
    dut.w_init.value      = 0
    dut.w_din.value       = 0
    dut.w_din_be.value    = 0
    dut.w_din_valid.value = 0
    dut.b_init.value      = 0
    dut.b_din.value       = 0
    dut.b_din_be.value    = 0
    dut.b_din_valid.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


SEED = 0xFFFFFFFF


def ref_crc32(data: bytes) -> int:
    """Reference: the §8.2.2.2 register over a byte string (zlib without
    its final XOR)."""
    return (zlib.crc32(data) ^ 0xFFFFFFFF) & 0xFFFFFFFF


# -----------------------------------------------------------------------------
# Word-wise drivers
# -----------------------------------------------------------------------------
async def w_seed(dut):
    """Pulse word-wise `init` for one cycle with no data."""
    dut.w_init.value      = 1
    dut.w_din_valid.value = 0
    await RisingEdge(dut.clk)
    dut.w_init.value      = 0


async def w_feed(dut, word: int, be: int = 0xF, *, init: int = 0):
    """Drive one valid beat into the word-wise instance."""
    dut.w_init.value      = init
    dut.w_din.value       = word & 0xFFFFFFFF
    dut.w_din_be.value    = be & 0xF
    dut.w_din_valid.value = 1
    await RisingEdge(dut.clk)
    dut.w_init.value      = 0
    dut.w_din_valid.value = 0
    dut.w_din.value       = 0
    dut.w_din_be.value    = 0


async def w_stall(dut, n: int = 1):
    """Insert `n` idle cycles into the word-wise instance."""
    dut.w_init.value      = 0
    dut.w_din_valid.value = 0
    dut.w_din.value       = 0
    dut.w_din_be.value    = 0
    for _ in range(n):
        await RisingEdge(dut.clk)


async def w_sample_crc(dut) -> int:
    """Sample word-wise `w_crc` in ReadOnly, then step out of it."""
    await ReadOnly()
    v = int(dut.w_crc.value)
    await NextTimeStep()
    return v


# -----------------------------------------------------------------------------
# Byte-wise drivers
# -----------------------------------------------------------------------------
async def b_seed(dut):
    """Pulse byte-wise `init` for one cycle with no data."""
    dut.b_init.value      = 1
    dut.b_din_valid.value = 0
    await RisingEdge(dut.clk)
    dut.b_init.value      = 0


async def b_feed(dut, byte: int, *, init: int = 0):
    """Drive one valid byte into the byte-wise instance."""
    dut.b_init.value      = init
    dut.b_din.value       = byte & 0xFF
    dut.b_din_be.value    = 1
    dut.b_din_valid.value = 1
    await RisingEdge(dut.clk)
    dut.b_init.value      = 0
    dut.b_din_valid.value = 0
    dut.b_din.value       = 0
    dut.b_din_be.value    = 0


async def b_sample_crc(dut) -> int:
    """Sample byte-wise `b_crc` in ReadOnly, then step out of it."""
    await ReadOnly()
    v = int(dut.b_crc.value)
    await NextTimeStep()
    return v


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def bytes_to_words(data: bytes) -> list[tuple[int, int]]:
    """Pack a byte string into (word, be) pairs as the CXP wire sees them.

    Per §8.2.1, byte 0 of the wire word lives in din[7:0] (P0), so the
    first byte of the buffer maps to the LSB of the 32-bit input.  The
    last (possibly partial) word reports a contiguous low-side BE.
    """
    out = []
    for i in range(0, len(data), 4):
        chunk = data[i:i+4]
        word = 0
        for b_idx, byte in enumerate(chunk):
            word |= byte << (8 * b_idx)
        be = (1 << len(chunk)) - 1     # 1 → 0x1, 2 → 0x3, 3 → 0x7, 4 → 0xF
        out.append((word, be))
    return out


# -----------------------------------------------------------------------------
# TC 1 — Reset State
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_reset_state(dut):
    """Reset loads the seed 0xFFFFFFFF into both accumulators.

    There is no final XOR, so a seeded register reads as the seed; every
    other test relies on this starting point.

    Stimulus: bring-up only (4 edges of `rst_n = 0`, release, 1 edge).
    Checks:   `w_crc == 0xFFFFFFFF` and `b_crc == 0xFFFFFFFF`.
    """
    dut.TESTCASE.value = 1
    await bringup(dut)
    assert (await w_sample_crc(dut)) == SEED, "w_crc not at seed after reset"
    assert (await b_sample_crc(dut)) == SEED, "b_crc not at seed after reset"


# -----------------------------------------------------------------------------
# TC 2 — Empty Init Is Seed
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_empty_init_is_zero(dut):
    """A data-less `init` pulse leaves the CRC at the seed.

    Targets the `{init, valid} = 10` case: reseed without folding.

    Stimulus: `w_seed`, 1 idle cycle, sample; then `b_seed`, 1 idle cycle,
              sample.
    Checks:   `w_crc == b_crc == 0xFFFFFFFF` (reference of the empty string).
    Note:     both accumulators are already at seed from reset, so this
              still passes with `init_i` stuck at 0; reseed from a dirty
              state is covered by TC 5 and TC 9.
    """
    dut.TESTCASE.value = 2
    await bringup(dut)
    await w_seed(dut)
    await RisingEdge(dut.clk)
    assert (await w_sample_crc(dut)) == ref_crc32(b""), "empty CRC must be the seed"
    await b_seed(dut)
    await RisingEdge(dut.clk)
    assert (await b_sample_crc(dut)) == ref_crc32(b""), "empty CRC must be the seed"


# -----------------------------------------------------------------------------
# TC 3 — Known Vector Check
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_known_vector_check(dut):
    """The byte-wise instance produces ~0xCBF43926 for "123456789".

    "123456789" is the classic CRC-32 reference vector (0xCBF43926 with the
    802.3 final XOR); without the final XOR it pins polynomial, seed and
    reflection independently of the random tests.

    Stimulus: byte-wise instance only — the 9 ASCII bytes as back-to-back
              beats, the first with `init = 1`, then 1 idle cycle.
    Checks:   self-check `ref_crc32(b"123456789") == 0x340BC6D9`, then
              `b_crc == 0x340BC6D9`.
    Note:     the word-wise instance is not driven.
    """
    dut.TESTCASE.value = 3
    await bringup(dut)
    data = b"123456789"
    expected = 0xCBF43926 ^ 0xFFFFFFFF
    assert ref_crc32(data) == expected, "reference vector self-check failed"

    # Byte-wise.
    await b_feed(dut, data[0], init=1)
    for byte in data[1:]:
        await b_feed(dut, byte)
    await RisingEdge(dut.clk)
    assert (await b_sample_crc(dut)) == expected, (
        f"byte-wise CRC of '123456789' got 0x{int(dut.b_crc.value):08x}, "
        f"expected 0x{expected:08x}"
    )


# -----------------------------------------------------------------------------
# TC 4 — Word-Wise Full Words
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_wordwise_full_words(dut):
    """The word-wise instance folds lanes P0-first within and across words.

    Stimulus: bytes 00..07 packed as 0x03020100 (with `init = 1`) and
              0x07060504, BE = 0xF, back-to-back, then 1 idle cycle.
    Checks:   the packing yields exactly 2 words; `w_crc == ref(00..07)`.
    """
    dut.TESTCASE.value = 4
    await bringup(dut)
    data = bytes(range(8))    # 00..07
    expected = ref_crc32(data)
    words = bytes_to_words(data)
    assert len(words) == 2

    await w_feed(dut, words[0][0], be=words[0][1], init=1)
    await w_feed(dut, words[1][0], be=words[1][1])
    await RisingEdge(dut.clk)
    assert (await w_sample_crc(dut)) == expected, (
        f"wordwise CRC of 8 bytes got 0x{int(dut.w_crc.value):08x}, "
        f"expected 0x{expected:08x}"
    )


# -----------------------------------------------------------------------------
# TC 5 — Word-Wise vs Byte-Wise Random
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_wordwise_vs_bytewise_random(dut):
    """Random buffers give the reference CRC at both widths.

    Cross-validates the 32-bit and 8-bit instances and exercises a partial
    last word with every low-side BE (lengths hit all residues mod 4).

    Stimulus: `random.Random(0xC0FFEE)`, 20 trials of 1–256 random bytes.
              Per trial: `w_seed`, ⌈n/4⌉ back-to-back words (last one with
              a low-side BE), 1 idle cycle, sample; then the same bytes
              byte-wise (`b_seed`, n beats, 1 idle cycle, sample). The two
              instances are driven one after the other, not in parallel.
    Checks:   per trial `got_w == ref`, `got_b == ref`, `got_w == got_b`.
    Note:     from trial 2 on, `w_seed`/`b_seed` reseed a dirty accumulator,
              so this also proves `{init, valid} = 10` from a non-seed state.
    """
    dut.TESTCASE.value = 5
    await bringup(dut)
    rng = random.Random(0xC0FFEE)

    for trial in range(20):
        n = rng.randint(1, 256)
        data = bytes(rng.randint(0, 255) for _ in range(n))
        expected = ref_crc32(data)

        # Word-wise drive (partial last word possible).
        await w_seed(dut)
        for w, be in bytes_to_words(data):
            await w_feed(dut, w, be=be)
        await RisingEdge(dut.clk)
        got_w = await w_sample_crc(dut)

        # Byte-wise drive.
        await b_seed(dut)
        for byte in data:
            await b_feed(dut, byte)
        await RisingEdge(dut.clk)
        got_b = await b_sample_crc(dut)

        assert got_w == expected, (
            f"trial {trial} len={n}: word-wise CRC 0x{got_w:08x} != "
            f"expected 0x{expected:08x}"
        )
        assert got_b == expected, (
            f"trial {trial} len={n}: byte-wise CRC 0x{got_b:08x} != "
            f"expected 0x{expected:08x}"
        )
        assert got_w == got_b, "word-wise vs byte-wise disagree"


# -----------------------------------------------------------------------------
# TC 6 — Byte-Enable Partial Last Word
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_byte_enable_partial_last_word(dut):
    """`din_be` lanes that are 0 are skipped by the fold.

    A partial last word of a stream packet must give the same CRC as a
    byte-wise fold of only the enabled bytes.

    Stimulus: `random.Random(0x12345)`; for BE = 0x1, 0x3, 0x7, 0xF:
              `w_seed`, one random 32-bit word with that BE, 1 idle cycle.
    Checks:   4 times, `w_crc == ref(low n bytes of the word)`.
    Note:     only contiguous low-side masks are tested.
    """
    dut.TESTCASE.value = 6
    await bringup(dut)
    rng = random.Random(0x12345)
    for be_pattern, n_valid in [(0x1, 1), (0x3, 2), (0x7, 3), (0xF, 4)]:
        # Build a random 32-bit word; only n_valid low bytes count.
        word = rng.randint(0, 0xFFFFFFFF)
        ref_bytes = bytes((word >> (8*i)) & 0xFF for i in range(n_valid))
        expected = ref_crc32(ref_bytes)

        await w_seed(dut)
        await w_feed(dut, word, be=be_pattern)
        await RisingEdge(dut.clk)
        got = await w_sample_crc(dut)
        assert got == expected, (
            f"be=0x{be_pattern:x} n={n_valid} got 0x{got:08x} "
            f"expected 0x{expected:08x}"
        )


# -----------------------------------------------------------------------------
# TC 7 — Stalls Don't Perturb CRC
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_stalls_dont_perturb_crc(dut):
    """`din_valid = 0` cycles hold the accumulator.

    Stimulus: `random.Random(0xDEADBEEF)`, 64 random bytes = 16 full words;
              `w_seed`, then each word followed by 0–4 idle cycles (drawn
              1,3,2,4,1,0,0,4,2,2,1,1,3,0,3,3 — so gaps of 0 are included),
              then 1 idle cycle.
    Checks:   `w_crc == ref(64 bytes)`.
    """
    dut.TESTCASE.value = 7
    await bringup(dut)
    rng = random.Random(0xDEADBEEF)
    data = bytes(rng.randint(0, 255) for _ in range(64))
    expected = ref_crc32(data)
    words = bytes_to_words(data)

    await w_seed(dut)
    for w, be in words:
        await w_feed(dut, w, be=be)
        await w_stall(dut, rng.randint(0, 4))
    await RisingEdge(dut.clk)
    got = await w_sample_crc(dut)
    assert got == expected, (
        f"stalled wordwise CRC 0x{got:08x} != expected 0x{expected:08x}"
    )


# -----------------------------------------------------------------------------
# TC 8 — Init And Data Same Cycle
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_init_and_data_same_cycle(dut):
    """`init` co-asserted with a valid beat seeds first, then folds the beat.

    Consumers raise `init` on the first beat of a packet, so the beat must
    fold onto the seed, not onto the previous (dirty) accumulator.

    Stimulus: dirty the accumulator with 0xDEADBEEF (`init = 1`) and
              0xCAFEBABE, 1 idle cycle; then 0x04030201 with `init = 1`
              co-asserted, 0x08070605, 1 idle cycle.
    Checks:   `w_crc == ref(01..08)` — a fresh fold over the post-init data.
    """
    dut.TESTCASE.value = 8
    await bringup(dut)
    # Pre-corrupt the accumulator with arbitrary data.
    await w_feed(dut, 0xDEADBEEF, be=0xF, init=1)
    await w_feed(dut, 0xCAFEBABE, be=0xF)
    await RisingEdge(dut.clk)
    # Now restart with init+data on the same cycle and finish a small frame.
    data = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    expected = ref_crc32(data)
    words = bytes_to_words(data)
    await w_feed(dut, words[0][0], be=words[0][1], init=1)   # init+data
    await w_feed(dut, words[1][0], be=words[1][1])
    await RisingEdge(dut.clk)
    got = await w_sample_crc(dut)
    assert got == expected, (
        f"co-asserted init+data CRC 0x{got:08x} != expected 0x{expected:08x}"
    )


# -----------------------------------------------------------------------------
# TC 9 — Back-to-Back Packets
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_back_to_back_packets(dut):
    """Packets separated by an `init` pulse each yield an independent CRC.

    Stimulus: `random.Random(0xABCDEF)`, 8 packets of 4–40 random bytes
              (drawn 9, 15, 4, 22, 22, 21, 32, 26). Per packet: `w_seed`,
              the words (last one partial where needed), 1 idle cycle,
              sample.
    Checks:   8 times, `w_crc == ref(pkt)`.
    Note:     the packets are not truly back-to-back — each is preceded by
              an idle cycle and a data-less `init` cycle; `init` on the
              first beat right after the previous packet's last beat is not
              exercised.
    """
    dut.TESTCASE.value = 9
    await bringup(dut)
    rng = random.Random(0xABCDEF)
    for trial in range(8):
        pkt = bytes(rng.randint(0, 255) for _ in range(rng.randint(4, 40)))
        expected = ref_crc32(pkt)
        words = bytes_to_words(pkt)
        await w_seed(dut)
        for w, be in words:
            await w_feed(dut, w, be=be)
        await RisingEdge(dut.clk)
        got = await w_sample_crc(dut)
        assert got == expected, (
            f"trial {trial}: CRC 0x{got:08x} != expected 0x{expected:08x}"
        )


# -----------------------------------------------------------------------------
# TC 10 — Single-Bit Flip Detected
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_single_bit_flip_detected(dut):
    """The RTL tracks the reference on a message and on its single-bit-flipped copy.

    Stimulus: `random.Random(0x600D)`, 48 random bytes: `w_seed`, fold,
              1 idle cycle, sample. Then flip one random bit (bit 4 of
              byte 2), `w_seed`, fold again, 1 idle cycle, sample.
    Checks:   `got_orig == ref(orig)`, `got_bad == ref(flipped)`, and
              `got_bad != got_orig`.
    Note:     the last assert tests a property of CRC-32 (all 1-bit errors
              detected), not of the RTL.
    """
    dut.TESTCASE.value = 10
    await bringup(dut)
    rng = random.Random(0x600D)
    orig = bytearray(rng.randint(0, 255) for _ in range(48))

    # Reference CRC.
    crc_ref = ref_crc32(bytes(orig))

    # Drive original, capture word-wise CRC.
    await w_seed(dut)
    for w, be in bytes_to_words(bytes(orig)):
        await w_feed(dut, w, be=be)
    await RisingEdge(dut.clk)
    got_orig = await w_sample_crc(dut)
    assert got_orig == crc_ref

    # Flip a single random bit and re-fold.
    flip_byte = rng.randrange(len(orig))
    flip_bit  = rng.randrange(8)
    orig[flip_byte] ^= (1 << flip_bit)
    crc_bad = ref_crc32(bytes(orig))

    await w_seed(dut)
    for w, be in bytes_to_words(bytes(orig)):
        await w_feed(dut, w, be=be)
    await RisingEdge(dut.clk)
    got_bad = await w_sample_crc(dut)

    assert got_bad == crc_bad, "DUT CRC drifted from the reference under flip"
    assert got_bad != got_orig, "single-bit flip didn't change the CRC!"


# -----------------------------------------------------------------------------
# TC 11 — Zero Payload Is Seed
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_zero_payload_is_seed(dut):
    """A fresh `init` followed only by idle cycles keeps the seed.

    Stimulus: `w_seed`, then 5 idle cycles (`w_stall(5)`), sample.
    Checks:   `w_crc == 0xFFFFFFFF`.
    Note:     same weakness as TC 2 — the accumulator is already at seed
              from reset, so a stuck-at-0 `init_i` also passes.
    """
    dut.TESTCASE.value = 11
    await bringup(dut)
    await w_seed(dut)
    await w_stall(dut, 5)
    assert (await w_sample_crc(dut)) == SEED


# -----------------------------------------------------------------------------
# TC 12 — Disabled Byte-Enable Skipped
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_disabled_byte_enable_skipped(dut):
    """A valid beat with all byte enables 0 is a no-op, like a stall.

    Stimulus: `random.Random(0xABCD)`, 12 random bytes = 3 words; `w_seed`,
              then each real word preceded by a valid beat with
              `din = 0xFFFFFFFF`, BE = 0x0 — 6 back-to-back beats — then
              1 idle cycle.
    Checks:   `w_crc == ref(12 bytes)`.
    """
    dut.TESTCASE.value = 12
    await bringup(dut)
    rng = random.Random(0xABCD)
    data = bytes(rng.randint(0, 255) for _ in range(12))
    expected = ref_crc32(data)
    words = bytes_to_words(data)
    await w_seed(dut)
    for w, be in words:
        # Insert a "valid but all-disabled" beat between each real beat.
        await w_feed(dut, 0xFFFFFFFF, be=0x0)
        await w_feed(dut, w, be=be)
    await RisingEdge(dut.clk)
    got = await w_sample_crc(dut)
    assert got == expected, (
        f"all-disabled-be NOP perturbed CRC: 0x{got:08x} != 0x{expected:08x}"
    )


# -----------------------------------------------------------------------------
# TC 13 — Stream Packet Shape
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_stream_packet_shape(dut):
    """The fold matches the stream-packet CRC stream of `cxp_tx_stream_pkt`.

    The stream-packet CRC (§8.5.1, Table 19) covers the stream data words
    4..N+3 only, not StreamID / PacketTag / DsizeP.

    Stimulus: `random.Random(0xCAFEC0DE)`, 5 packets with DsizeP 1–64:
              `w_seed`, DsizeP random data words, BE = 0xF, back-to-back,
              1 idle cycle.
    Checks:   5 times, `w_crc == crc32(data words)` from `cxp_protocol`.
    """
    dut.TESTCASE.value = 13
    await bringup(dut)
    rng = random.Random(0xCAFEC0DE)

    for trial in range(5):
        stream_id = rng.randint(0, 0xFF)
        pkt_tag   = rng.randint(0, 0xFF)
        dsize_p   = rng.randint(1, 64)
        data_words = [rng.randint(0, 0xFFFFFFFF) for _ in range(dsize_p)]

        expected = gcrc.crc32(data_words)

        await w_seed(dut)
        for w in data_words:
            await w_feed(dut, w, be=0xF)
        await RisingEdge(dut.clk)
        got = await w_sample_crc(dut)
        assert got == expected, (
            f"trial {trial}: stream-packet CRC 0x{got:08x} != "
            f"expected 0x{expected:08x}"
        )


# -----------------------------------------------------------------------------
# TC 14 — Reset Clears Partial
# -----------------------------------------------------------------------------
@cxp_test()
async def test_14_reset_clears_partial(dut):
    """Reset mid-packet returns the accumulator to seed.

    Stimulus: 0xDEADBEEF (`init = 1`) then 0xCAFEBABE; `rst_n = 0` written
              in the step of the second beat's edge, held for 3 edges,
              released, then 1 more edge.
    Checks:   `w_crc == 0xFFFFFFFF`.
    Note:     the sample is taken after clock edges, so a synchronous reset
              would pass too; the asynchronous path is not observed.
    """
    dut.TESTCASE.value = 14
    await bringup(dut)
    # Start feeding garbage, then assert reset.
    await w_feed(dut, 0xDEADBEEF, be=0xF, init=1)
    await w_feed(dut, 0xCAFEBABE, be=0xF)
    # Assert reset.
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    assert (await w_sample_crc(dut)) == SEED


# -----------------------------------------------------------------------------
# TC 15 — Long Payload
# -----------------------------------------------------------------------------
@cxp_test()
async def test_15_long_payload(dut):
    """A 1 KB continuous fold matches the reference.

    The longest vector in-tree; guards against drift that only shows up
    over many consecutive folds.

    Stimulus: `random.Random(0x1024)`, 1024 random bytes = 256 back-to-back
              full words after `w_seed`, then 1 idle cycle.
    Checks:   `w_crc == ref(1024 bytes)`.
    """
    dut.TESTCASE.value = 15
    await bringup(dut)
    rng = random.Random(0x1024)
    data = bytes(rng.randint(0, 255) for _ in range(1024))
    expected = ref_crc32(data)

    await w_seed(dut)
    for w, be in bytes_to_words(data):
        await w_feed(dut, w, be=be)
    await RisingEdge(dut.clk)
    got = await w_sample_crc(dut)
    assert got == expected, (
        f"1KB payload CRC 0x{got:08x} != expected 0x{expected:08x}"
    )


# -----------------------------------------------------------------------------
# TC 16 — §8.2.2.2 Worked Example
# -----------------------------------------------------------------------------
@cxp_test()
async def test_16_spec_worked_example(dut):
    """The control read of address 0 from §8.2.2.2 gives CRC 56 86 5D 6F.

    The register is sent as is, LSByte in P0; folding that word after the
    data leaves a zero register.

    Stimulus: words 0x04000000 (Cmd 0x00, Size 4) with `init = 1` and
              0x00000000 (Addr), 1 idle cycle, sample; then the CRC word
              itself, 1 idle cycle, sample.
    Checks:   lanes P0..P3 of `w_crc` are 56 86 5D 6F; the residue is 0.
    """
    dut.TESTCASE.value = 16
    await bringup(dut)
    await w_feed(dut, 0x0400_0000, be=0xF, init=1)
    await w_feed(dut, 0x0000_0000, be=0xF)
    await RisingEdge(dut.clk)
    got = await w_sample_crc(dut)
    lanes = [(got >> (8 * i)) & 0xFF for i in range(4)]
    assert lanes == [0x56, 0x86, 0x5D, 0x6F], f"CRC lanes {[hex(x) for x in lanes]}"
    await w_feed(dut, got, be=0xF)
    await RisingEdge(dut.clk)
    assert (await w_sample_crc(dut)) == 0, "residue after the CRC word is not 0"
