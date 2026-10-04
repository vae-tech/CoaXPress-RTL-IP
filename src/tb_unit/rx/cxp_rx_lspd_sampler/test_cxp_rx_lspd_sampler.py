"""Cocotb TB for `cxp_rx_lspd_sampler`.

The DUT is the device-side low-speed uplink soft receiver: it oversamples
`rx_serial` on `os_clk`, recovers bits with an edge-reset phase counter,
locks on the K28.5 comma and packs four 10-bit symbols into a 40-bit word
(`sym_out`, P0 in bits [9:0], one `sym_valid` pulse per word). The wrapper
shrinks the parameters (`OS_RATIO = 8`, `LOCK_HITS = 2`) so lock fits in a
few thousand cycles.  Loss of lock is decided by `cxp_rx_link_mon`, which
pulses `resync`; the sampler itself never drops lock on its own.

Python encodes 40-bit words with `cxp_8b10b` and drives them bit-serially on
`rx_serial` (bit 'a' of P0 first), holding each bit for `OS_RATIO` cycles of
the 10 ns `os_clk`; a fractional accumulator allows frequency offsets.
`wait_for_lock` polls `rx_lock`, `collect_words` samples `sym_out` on every
`sym_valid`. The lock FSM (`state_q`) is sampled by `fsm_coverage`.

Notes: `idle_words` encodes K28.5 K28.1 K28.1 K28.1 (kmask 0b1111), not the
Table 14 IDLE word (P3 = D21.5); captured IDLEs are compared against the RD-
and RD+ encodings of that word. `os_rst_n` is asserted asynchronously in the
RTL. Spec: §6.7 (bit rate, ±100 ppm), §8.2.1 (bit order), §8.2.5 / Table 14
(IDLE word), §10.2 (loss of lock); cases derive from the module plan in
`docs/design/cxp_camera_ip_modules.md` §4.1 / §4.3 / §4.5.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Pristine lock — 20 IDLE words, `rx_lock` asserts within 3000 cycles.
  2  Bit-exact IDLE — 8 words after lock are valid IDLE encodings.
  3  Mixed traffic — 4 D-code data words recovered bit-exact and in order.
  4  Frequency offset — IDLE at +200 ppm bit period; lock, 12 IDLE words.
  5  Sliding phase — seeded 1-cycle pre-roll before IDLE; lock, 6 IDLE
     words.
  6  Resync — line held low after lock: lock holds; a `resync` pulse
     drops it.
  7  Async reset — `os_rst_n` mid-stream clears `rx_lock` / `sym_valid`;
     lock is regained.
  8  Recovery after resync — lock, flat line, `resync`, IDLE again;
     re-lock and 6 IDLE words.
  9  Bit slip while locked — one bit lost after lock, no `resync`: lock
     holds and the words stay misframed (re-framing is the link
     monitor's decision).
 10  Sample point, fast host — IDLE with bits 6 % short (runs of five):
     lock and bit-exact words.
 11  Sample point, slow host — the same with bits 5 % long.
 12  Resync before lock — `resync` while the first K28.5s are still being
     confirmed returns to the hunt; lock follows on the same stream.
"""

from __future__ import annotations

import random
from typing import Iterable

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

import cxp_8b10b as cxp
from cxp_testcase import cxp_test
from fsm_coverage import register_fsm


# Must match `tb_cxp_rx_lspd_sampler_top` parameters.
OS_CLK_PERIOD_NS = 10
OS_RATIO         = 8
LOCK_HITS        = 2

K28_5 = cxp.K28_5
K28_1 = cxp.K28_1


# -----------------------------------------------------------------------------
# FSM coverage
# -----------------------------------------------------------------------------
# `resync` sends any state back to ST_HUNT (written before the case
# statement); from ST_PRELOCK that is its own arc.
register_fsm(
    name="rx_lspd_sampler",
    states=["ST_HUNT", "ST_PRELOCK", "ST_LOCKED"],
    state_path="cxp_rx_lspd_sampler_i.state_q",
    clk_path="os_clk",
    arcs=[
        ("ST_HUNT", "ST_PRELOCK"), ("ST_PRELOCK", "ST_LOCKED"),
        ("ST_LOCKED", "ST_HUNT"), ("ST_PRELOCK", "ST_HUNT"),
    ],
)


# -----------------------------------------------------------------------------
# Bit-stream helpers
# -----------------------------------------------------------------------------
def word_to_bits(word40: int) -> list[int]:
    """40-bit 4×10b word → list of 40 bits in transmission order.

    Bit 0 of the 40-bit value is the FIRST transmitted bit (= bit 'a'
    of lane P0).  Bit 39 is the last (= bit 'j' of lane P3).
    """
    return [(word40 >> i) & 1 for i in range(40)]


def idle_words(n: int, rd_in: int = 0) -> tuple[list[int], int]:
    """Encode n IDLE words (K28.5 K28.1 K28.1 K28.1).

    Returns (40-bit words list, final RD).
    """
    words: list[int] = []
    rd = rd_in
    for _ in range(n):
        w, rd = cxp.encode_word([K28_5, K28_1, K28_1, K28_1], 0b1111, rd)
        words.append(w)
    return words, rd


def encode_byte_lanes(lanes_kmask: list[tuple[list[int], int]],
                      rd_in: int = 0) -> tuple[list[int], int]:
    """Encode a sequence of (4-byte lanes, kmask) tuples → 40-bit words."""
    words: list[int] = []
    rd = rd_in
    for byte_lanes, kmask in lanes_kmask:
        w, rd = cxp.encode_word(byte_lanes, kmask, rd)
        words.append(w)
    return words, rd


def words_to_bits(words: Iterable[int]) -> list[int]:
    """Flatten 40-bit words into one bit list in transmission order."""
    bits: list[int] = []
    for w in words:
        bits.extend(word_to_bits(w))
    return bits


# -----------------------------------------------------------------------------
# Driving / monitoring
# -----------------------------------------------------------------------------
async def reset(dut):
    """Start the os_clk, hold rst_n low for a few cycles, then release."""
    cocotb.start_soon(Clock(dut.os_clk, OS_CLK_PERIOD_NS, unit="ns").start())
    dut.os_rst_n.value  = 0
    dut.rx_serial.value = 0
    dut.resync.value    = 0
    for _ in range(5):
        await RisingEdge(dut.os_clk)
    dut.os_rst_n.value = 1
    for _ in range(2):
        await RisingEdge(dut.os_clk)


async def drive_bits(dut, bits: list[int], os_per_bit: float = OS_RATIO):
    """Drive a list of bits on dut.rx_serial.

    Each bit is held for `os_per_bit` os_clk cycles.  Fractional
    values are supported via a running accumulator — this lets the TB
    inject small frequency offsets (e.g. +200 ppm) without aliasing.
    """
    accum = 0.0
    for b in bits:
        n_float = os_per_bit + accum
        n = int(n_float)
        accum = n_float - n
        dut.rx_serial.value = b
        for _ in range(n):
            await RisingEdge(dut.os_clk)


async def drive_constant(dut, level: int, n_cycles: int):
    """Hold `rx_serial` at `level` for `n_cycles` os_clk cycles."""
    dut.rx_serial.value = level & 1
    for _ in range(n_cycles):
        await RisingEdge(dut.os_clk)


async def wait_for_lock(dut, timeout_cycles: int = 20000) -> int:
    """Wait until rx_lock asserts; return os_clk cycle count waited."""
    for n in range(timeout_cycles):
        await RisingEdge(dut.os_clk)
        if int(dut.rx_lock.value):
            return n
    raise TimeoutError(
        f"rx_lock did not assert within {timeout_cycles} os_clk cycles"
    )


async def collect_words(dut, n: int, max_idle: int = 10000) -> list[int]:
    """Collect `n` sym_valid pulses; return the captured 40-bit words."""
    out: list[int] = []
    idle = 0
    while len(out) < n:
        await RisingEdge(dut.os_clk)
        if int(dut.sym_valid.value):
            out.append(int(dut.sym_out.value))
            idle = 0
        else:
            idle += 1
            if idle > max_idle:
                raise TimeoutError(
                    f"collect_words: no sym_valid for {idle} cycles "
                    f"(captured {len(out)}/{n})"
                )
    return out


# -----------------------------------------------------------------------------
# TC 1 — Pristine Lock
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_pristine_lock(dut):
    """IDLE traffic from reset drives the lock FSM into `ST_LOCKED`.

    Proves HUNT → PRELOCK → LOCKED: an anchoring K28.5 followed by
    `LOCK_HITS` boundary-aligned commas 40 bits apart (§8.2.5 IDLE word).

    Stimulus: 20 IDLE words (800 bits) at 8 cycles/bit, started right
              after reset release.
    Checks:   `rx_lock` asserts within 3000 `os_clk` cycles (≈ 9 IDLE
              words); the measured cycle count is only logged.
    Note:     the bound is a loose timeout, not a lock-time requirement;
              lock needs 3 commas (≈ 131 bit-times from the first bit).
    """
    dut.TESTCASE.value = 1
    await reset(dut)

    words, _ = idle_words(20)
    bits = words_to_bits(words)
    drv = cocotb.start_soon(drive_bits(dut, bits))

    # 5 IDLE words = 200 bits = 1600 os_clk cycles.  Allow a bit more.
    cycles = await wait_for_lock(dut, timeout_cycles=3000)
    dut._log.info(f"TC1: rx_lock asserted after {cycles} os_clk cycles "
                  f"(~{cycles // OS_RATIO} recovered bits)")
    await drv


# -----------------------------------------------------------------------------
# TC 2 — Bit-Exact Recovery on IDLE Traffic
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_bit_exact_idle(dut):
    """Words emitted after lock are bit-exact IDLE words with the comma in P0.

    Proves the packer's lane order (P0 = bits [9:0] … P3 = bits [39:30]),
    the LSB-first bit order of §8.2.1, and that lock frames K28.5 into P0.

    Stimulus: 30 IDLE words at 8 cycles/bit; after `rx_lock` rises the
              next 8 `sym_valid` words are captured.
    Checks:   every captured word equals the model's IDLE encoding for
              entering RD- or RD+ (`cxp_8b10b.encode_word`).
    Note:     the TB IDLE has K28.1 in P3, not Table 14's D21.5.
    """
    dut.TESTCASE.value = 2
    await reset(dut)

    n_idle = 30
    words, _ = idle_words(n_idle)
    bits = words_to_bits(words)
    drv = cocotb.start_soon(drive_bits(dut, bits))

    await wait_for_lock(dut, timeout_cycles=3000)
    captured = await collect_words(dut, 8)

    # Every captured word must equal the IDLE word for the prevailing RD.
    # We don't know the RD-phase the sampler latched on, but both IDLE
    # encodings differ — so we compare against the set of valid IDLEs.
    idle_rd0, _ = cxp.encode_word([K28_5, K28_1, K28_1, K28_1], 0b1111, 0)
    idle_rd1, _ = cxp.encode_word([K28_5, K28_1, K28_1, K28_1], 0b1111, 1)
    valid_idles = {idle_rd0, idle_rd1}
    for i, w in enumerate(captured):
        assert w in valid_idles, (
            f"word {i}: got 0x{w:010x}, expected IDLE (one of "
            f"0x{idle_rd0:010x}, 0x{idle_rd1:010x})"
        )
    await drv


# -----------------------------------------------------------------------------
# TC 3 — Mixed Traffic (IDLE + Data Words)
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_mixed_traffic(dut):
    """Comma-free data words pass through the sampler unchanged and in order.

    Proves that non-comma symbols are packed without re-framing once
    locked, and that 160 comma-free bits do not trip the loss counter.

    Stimulus: 8 IDLE words, then 4 D-code-only words (11 22 33 44,
              A5 5A C3 3C, 00 FF 55 AA, 10 20 30 40; kmask 0, RD carried
              across), then 4 IDLE words; 8 cycles/bit.
    Checks:   after lock 14 words are captured; the first data word must
              be present in the capture and the three captured words
              after it must equal data words 1..3 bit-exact.
    Note:     the IDLE words around the data block are not checked, nor
              is the block's position in the capture.
    """
    dut.TESTCASE.value = 3
    await reset(dut)

    # Warm-up with many IDLEs so we lock + skip the prelock zone.
    warmup, rd = idle_words(8)
    # A handful of arbitrary D-code-only words.
    data_byte_lanes = [
        ([0x11, 0x22, 0x33, 0x44], 0b0000),
        ([0xA5, 0x5A, 0xC3, 0x3C], 0b0000),
        ([0x00, 0xFF, 0x55, 0xAA], 0b0000),
        ([0x10, 0x20, 0x30, 0x40], 0b0000),
    ]
    data_words: list[int] = []
    for lanes, km in data_byte_lanes:
        w, rd = cxp.encode_word(lanes, km, rd)
        data_words.append(w)
    # Trailing IDLEs.
    trailer, _ = idle_words(4, rd)

    full_words = warmup + data_words + trailer
    bits = words_to_bits(full_words)
    drv = cocotb.start_soon(drive_bits(dut, bits))

    await wait_for_lock(dut, timeout_cycles=3000)

    # Capture enough words to span the data block.  We don't know
    # exactly which captured-word index corresponds to which input-
    # word index because lock may consume a few IDLE words.  Instead
    # we look for the start of the data sequence in the capture.
    captured = await collect_words(dut, 14)
    # Find data_words[0] in the capture.
    try:
        i0 = captured.index(data_words[0])
    except ValueError as e:
        raise AssertionError(
            f"data word #0 (0x{data_words[0]:010x}) not in capture {captured}"
        ) from e
    for k, expected in enumerate(data_words):
        got = captured[i0 + k]
        assert got == expected, (
            f"data word #{k}: expected 0x{expected:010x}, got 0x{got:010x}"
        )
    await drv


# -----------------------------------------------------------------------------
# TC 4 — Frequency Offset (+200 ppm)
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_freq_offset(dut):
    """Lock and bit-exact IDLE recovery hold at a +200 ppm bit period.

    The free-wheeling phase counter must track a slightly slow
    transmitter between edges (§6.7 allows ±100 ppm).

    Stimulus: 30 IDLE words at 8 × 1.0002 = 8.0016 cycles/bit through
              the fractional accumulator — over the 1200-bit stream this
              stretches a single bit by one `os_clk` cycle.
    Checks:   `rx_lock` within 4000 cycles; the next 12 captured words are
              all valid IDLE encodings (no bit slip).
    Note:     the offset is positive only and far inside the measured
              −5 %/+10 % tolerance, so no plausible CDR bug can fail it.
    """
    dut.TESTCASE.value = 4
    await reset(dut)

    n_idle = 30
    words, _ = idle_words(n_idle)
    bits = words_to_bits(words)
    # 8 × (1 + 200e-6) ≈ 8.0016 os_clk/bit
    drv = cocotb.start_soon(drive_bits(dut, bits,
                                       os_per_bit=OS_RATIO * 1.0002))

    await wait_for_lock(dut, timeout_cycles=4000)
    captured = await collect_words(dut, 12)
    idle_rd0, _ = cxp.encode_word([K28_5, K28_1, K28_1, K28_1], 0b1111, 0)
    idle_rd1, _ = cxp.encode_word([K28_5, K28_1, K28_1, K28_1], 0b1111, 1)
    valid_idles = {idle_rd0, idle_rd1}
    for i, w in enumerate(captured):
        assert w in valid_idles, (
            f"freq-offset word {i}: 0x{w:010x} not a valid IDLE — bit slip?"
        )
    await drv


# -----------------------------------------------------------------------------
# TC 5 — Sliding Sampling Phase
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_sliding_phase(dut):
    """Lock is acquired whatever the offset of the first bit edge.

    `phase_q` is reset on every transition, so the sample point realigns
    to the incoming bit boundary regardless of its phase at reset release.

    Stimulus: `rx_serial` held high for `randrange(1, OS_RATIO - 1)`
              cycles from `random.Random(0xDEADBEEF)` (draws 1 cycle,
              0.125 UI), then 30 IDLE words at 8 cycles/bit.
    Checks:   `rx_lock` within 4000 cycles; the next 6 captured words are
              valid IDLE encodings.
    Note:     the fixed seed exercises one offset only; the draw's range
              is 0.125–0.75 UI.
    """
    dut.TESTCASE.value = 5
    await reset(dut)

    rng = random.Random(0xDEADBEEF)
    # Hold rx_serial high for a random fraction-of-bit before the IDLE
    # train begins.  This shifts the bit boundary 1..6 os_cycles
    # relative to the local phase counter.
    prebit = rng.randrange(1, OS_RATIO - 1)
    await drive_constant(dut, 1, prebit)

    words, _ = idle_words(30)
    bits = words_to_bits(words)
    drv = cocotb.start_soon(drive_bits(dut, bits))

    await wait_for_lock(dut, timeout_cycles=4000)
    # And one full IDLE word collected.
    captured = await collect_words(dut, 6)
    idle_rd0, _ = cxp.encode_word([K28_5, K28_1, K28_1, K28_1], 0b1111, 0)
    idle_rd1, _ = cxp.encode_word([K28_5, K28_1, K28_1, K28_1], 0b1111, 1)
    valid_idles = {idle_rd0, idle_rd1}
    for i, w in enumerate(captured):
        assert w in valid_idles, (
            f"phase-shifted word {i}: 0x{w:010x} not IDLE (initial offset "
            f"= {prebit} os-cycles)"
        )
    await drv


# -----------------------------------------------------------------------------
# TC 6 — Loss of Signal
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_loss_of_signal(dut):
    """Lock holds on a flat line until the link monitor asks for a resync.

    §8.2.5.1 lets the host send 10 000 words between IDLEs, so the
    sampler must not judge the link from missing commas; `resync`
    (from `cxp_rx_link_mon`, §10.2) drops LOCKED → HUNT at once.

    Stimulus: 30 IDLE words at 8 cycles/bit; `rx_serial` then held at 0
              for 8192 cycles (1024 bit periods, over 25 words); then a
              one-cycle `resync` pulse.
    Checks:   lock is acquired within 3000 cycles; `rx_lock` is still 1
              after the flat stretch; `rx_lock == 0` right after `resync`.
    """
    dut.TESTCASE.value = 6
    await reset(dut)

    words, _ = idle_words(30)
    drv = cocotb.start_soon(drive_bits(dut, words_to_bits(words)))
    await wait_for_lock(dut, timeout_cycles=3000)
    await drv
    await drive_constant(dut, 0, 8192)
    assert int(dut.rx_lock.value) == 1, "rx_lock dropped without a resync"
    dut.resync.value = 1
    await RisingEdge(dut.os_clk)
    dut.resync.value = 0
    await RisingEdge(dut.os_clk)
    assert int(dut.rx_lock.value) == 0, "rx_lock did not drop on resync"


# -----------------------------------------------------------------------------
# TC 7 — Async Reset Behaviour
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_async_reset(dut):
    """Asynchronous `os_rst_n` assertion clears the outputs; lock recovers.

    Every `always_ff` resets on `negedge os_rst_n`, so the outputs must
    clear without a clock edge, and lock must then recover on live data.

    Stimulus: 30 IDLE words; right after `rx_lock` rises `os_rst_n` is
              driven low between clock edges, released on the next edge
              while the first stream keeps running; then 30 more IDLE
              words.
    Checks:   3 ns after the reset assertion `rx_lock == 0` and
              `sym_valid == 0`; after the second stream starts `rx_lock`
              is high within 4000 cycles.
    Note:     the FSM state is not checked directly; re-lock normally
              occurs on the tail of the first stream, so the final wait
              confirms lock rather than timing a fresh acquisition.
    """
    dut.TESTCASE.value = 7
    await reset(dut)

    words, _ = idle_words(30)
    bits = words_to_bits(words)
    drv = cocotb.start_soon(drive_bits(dut, bits))
    await wait_for_lock(dut, timeout_cycles=3000)

    # Assert reset asynchronously (no clock wait).
    dut.os_rst_n.value = 0
    await Timer(3, unit="ns")
    assert int(dut.rx_lock.value) == 0, "rx_lock did not clear on reset"
    assert int(dut.sym_valid.value) == 0, "sym_valid did not clear on reset"

    # Re-release and check it locks again.
    await RisingEdge(dut.os_clk)
    dut.os_rst_n.value = 1
    await drv

    words2, _ = idle_words(30)
    drv2 = cocotb.start_soon(drive_bits(dut, words_to_bits(words2)))
    await wait_for_lock(dut, timeout_cycles=4000)
    await drv2


# -----------------------------------------------------------------------------
# TC 8 — Recovery After Signal Loss
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_recovery_after_loss(dut):
    """The sampler re-locks and decodes cleanly after a resync.

    Proves HUNT → PRELOCK → LOCKED a second time, with the hit, lane and
    bit counters cleared by the resync arc.

    Stimulus: 30 IDLE words; line held 0 for 1024 cycles; one-cycle
              `resync`; then 30 more IDLE words; 8 cycles/bit.
    Checks:   first lock within 3000 cycles; `rx_lock == 0` after the
              resync; `rx_lock` within 4000 cycles of the new stream; the
              next 6 captured words are valid IDLE encodings.
    """
    dut.TESTCASE.value = 8
    await reset(dut)

    words, _ = idle_words(30)
    drv = cocotb.start_soon(drive_bits(dut, words_to_bits(words)))
    await wait_for_lock(dut, timeout_cycles=3000)
    await drv

    # The link monitor gives up on the flat line and asks for a resync.
    await drive_constant(dut, 0, 1024)
    dut.resync.value = 1
    await RisingEdge(dut.os_clk)
    dut.resync.value = 0
    await RisingEdge(dut.os_clk)
    assert int(dut.rx_lock.value) == 0, "rx_lock did not drop on resync"

    # Re-IDLE.
    words2, _ = idle_words(30)
    drv2 = cocotb.start_soon(drive_bits(dut, words_to_bits(words2)))
    await wait_for_lock(dut, timeout_cycles=4000)
    captured = await collect_words(dut, 6)
    idle_rd0, _ = cxp.encode_word([K28_5, K28_1, K28_1, K28_1], 0b1111, 0)
    idle_rd1, _ = cxp.encode_word([K28_5, K28_1, K28_1, K28_1], 0b1111, 1)
    valid_idles = {idle_rd0, idle_rd1}
    for i, w in enumerate(captured):
        assert w in valid_idles, (
            f"post-recovery word {i}: 0x{w:010x} not IDLE"
        )
    await drv2


# -----------------------------------------------------------------------------
# TC 9 — Bit Slip While Locked
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_locked_bit_slip(dut):
    """Once locked the sampler keeps its character phase until `resync`.

    `cxp_rx_link_mon` owns the decision that the framing is wrong (§10.2);
    a second judge here would race it.

    Stimulus: 40 IDLE words; the first bit after `rx_lock` rises is not
              sent; no `resync`.
    Checks:   `rx_lock` stays 1; in the 20 words captured after the lost
              bit (skipping 2) no lane holds a K28.5 or K28.1 symbol: every
              character stays one bit off.
    """
    dut.TESTCASE.value = 9
    await reset(dut)
    words, _ = idle_words(40)
    bits = words_to_bits(words)
    st = {"dropped": False}

    async def drive():
        for b in bits:
            if not st["dropped"] and int(dut.rx_lock.value):
                st["dropped"] = True
                continue
            dut.rx_serial.value = b
            for _ in range(OS_RATIO):
                await RisingEdge(dut.os_clk)

    drv = cocotb.start_soon(drive())
    await wait_for_lock(dut, timeout_cycles=3000)
    captured = await collect_words(dut, 22)
    assert st["dropped"]
    assert int(dut.rx_lock.value) == 1, "the sampler left lock by itself"
    framed = {cxp.encode_byte(k, True, rd)[0] for k in (K28_5, K28_1) for rd in (0, 1)}
    hits = [i for i, w in enumerate(captured[2:])
            if any(((w >> (10 * n)) & 0x3FF) in framed for n in range(4))]
    assert not hits, f"the sampler re-framed by itself: framed commas in words {hits}"
    await drv



# -----------------------------------------------------------------------------
# TC 10 / 11 — Sample Point In The Eye
# -----------------------------------------------------------------------------
async def _margin(dut, scale: float):
    await reset(dut)
    idle_rd0, _ = cxp.encode_word([K28_5, K28_1, K28_1, K28_1], 0b1111, 0)
    idle_rd1, _ = cxp.encode_word([K28_5, K28_1, K28_1, K28_1], 0b1111, 1)
    words, _ = idle_words(24)
    drv = cocotb.start_soon(drive_bits(dut, words_to_bits(words),
                                       os_per_bit=OS_RATIO * scale))
    await wait_for_lock(dut, timeout_cycles=4000)
    captured = await collect_words(dut, 10)
    bad = [f"0x{w:010x}" for w in captured if w not in (idle_rd0, idle_rd1)]
    assert not bad, f"bit period x{scale}: words {bad} are not IDLE"
    await drv


@cxp_test()
async def test_10_sample_point_fast_host(dut):
    """The recovered-bit sample falls near the middle of the bit, so a
    transmitter whose bits are short keeps a margin.

    §6.7 allows ±100 ppm; the margin that matters is how far into the bit
    the sample lands after an edge.  The phase counter restarts on an
    edge the 2-FF synchroniser and the edge detector have already delayed,
    and counts from 0 in the cycle after; read OS/2 + 2 cycles after the
    edge (0.75 UI at OS 8, 1.0 UI at OS 4), the fifth bit of a five-bit
    run is read after the next edge once the bit is 5 % short.

    Stimulus: 24 IDLE words (K28.5 carries runs of five) at 8 x 0.94 =
              7.52 cycles per bit.
    Checks:   `rx_lock`; the next 10 captured words are valid IDLE
              encodings.
    """
    dut.TESTCASE.value = 10
    await _margin(dut, 0.94)


@cxp_test()
async def test_11_sample_point_slow_host(dut):
    """The same with bits 5 % long: the sample is not too early either.

    Measured with edges on os_clk edges (this bench): the window is
    -7 % .. +6 % (it was -5 % .. +10 % with the sample one cycle later);
    an asynchronous host spreads the edge over one more cycle.

    Stimulus: 24 IDLE words at 8 x 1.05 = 8.4 cycles per bit.
    Checks:   as test 10.
    """
    dut.TESTCASE.value = 11
    await _margin(dut, 1.05)


# -----------------------------------------------------------------------------
# TC 12 — Resync Before Lock
# -----------------------------------------------------------------------------
ST_HUNT, ST_PRELOCK, ST_LOCKED = 0, 1, 2


@cxp_test()
async def test_12_resync_in_prelock(dut):
    """A `resync` that lands while the sampler is confirming its first
    comma sends it back to the hunt without locking.

    The link monitor can ask for a resync at any time (§10.2); one that
    arrives before lock must not leave a half-counted anchor behind.
    Stimulus: 40 IDLE words, 8 cycles/bit; one-cycle `resync` the cycle
              after `state_q` first enters ST_PRELOCK.
    Checks:   `state_q` == ST_HUNT and `rx_lock` == 0 right after the
              resync; `rx_lock` never rose before it; lock within 4000
              cycles on the same stream; the next 6 words are valid IDLE
              encodings.
    """
    dut.TESTCASE.value = 12
    await reset(dut)
    words, _ = idle_words(40)
    drv = cocotb.start_soon(drive_bits(dut, words_to_bits(words)))

    state = dut.cxp_rx_lspd_sampler_i.state_q
    for _ in range(3000):
        await RisingEdge(dut.os_clk)
        assert int(dut.rx_lock.value) == 0, "locked before the resync"
        if int(state.value) == ST_PRELOCK:
            break
    else:
        raise TimeoutError("never entered ST_PRELOCK")
    dut.resync.value = 1
    await RisingEdge(dut.os_clk)
    dut.resync.value = 0
    await RisingEdge(dut.os_clk)
    assert int(state.value) == ST_HUNT, f"state {int(state.value)} after resync in PRELOCK"
    assert int(dut.rx_lock.value) == 0

    await wait_for_lock(dut, timeout_cycles=4000)
    captured = await collect_words(dut, 6)
    idle_rd0, _ = cxp.encode_word([K28_5, K28_1, K28_1, K28_1], 0b1111, 0)
    idle_rd1, _ = cxp.encode_word([K28_5, K28_1, K28_1, K28_1], 0b1111, 1)
    bad = [f"0x{w:010x}" for w in captured if w not in (idle_rd0, idle_rd1)]
    assert not bad, f"words after re-lock {bad} are not IDLE"
    await drv
