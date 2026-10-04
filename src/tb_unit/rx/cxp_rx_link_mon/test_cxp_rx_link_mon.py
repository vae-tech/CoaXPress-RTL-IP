"""Cocotb TB for `cxp_rx_link_mon`.

The DUT is the uplink IDLE monitor (§8.2.5.1, §10.1.1, §10.2): with the
sampler locked, `LOCK_IDLES` error-free Table 14 IDLE words bring the link
up; while up it forwards words and counts words since the last IDLE; after
`LOSS_WORDS` words without one it pulses `resync` (sampler back to hunt)
and `flush` (parser ends its packet) and goes down.  Loss of sampler lock
also drops the link and pulses `flush`, without `resync`.

The wrapper shrinks `LOSS_WORDS` to 64 (`p_SHORT_LOSS_OK = 1`).  One
10 ns `rx_clk`; `send()` drives one word per cycle with `valid_in`, and a
monitor task records `resync` / `flush` pulses and the forwarded words.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Reset → down, nothing forwarded.
  2  Two IDLEs with lock → up and `link_detected`; later words forwarded.
  3  IDLEs without lock, or with a code error, do not bring the link up.
  4  64 words without IDLE → one `resync`, one `flush`, link down.
  5  An IDLE every 63 words keeps the link up (no false loss).
  6  Sampler lock loss → `flush` without `resync`, link down.
  7  Locked but down, 200 words that are not clean IDLEs → one `resync`,
     so the sampler hunts again instead of waiting for ever.
  8  Up, a clean word with K28.5 in P1 (the lanes rotated by one
     character) → one `resync` and one `flush` at once, link down.
  9  Up, 40 words with a code error and no IDLE → `resync` before the
     64-word IDLE loss.
 10  Up, 200 words: an errored word every other word and a clean IDLE
     every 20 → no `resync` (errors alone, framed right, keep the link).
 11  Locked but down, clean data with one IDLE every 200 words (a host
     at the §8.2.5.1 / §8.7.3 minimum) → link up on the second IDLE, no
     `resync`; a clean word with K28.5 in P2 while down → one `resync`.
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, NextTimeStep

from cxp_protocol.kcodes import IDLE_KMASK, IDLE_WORD
from cxp_testcase import cxp_test

CLK = 10
LOCK_IDLES = 2
LOSS_WORDS = 64
DATA = 0x0302_0100


class Mon:
    """Pulses and forwarded words seen by `watch`."""

    def __init__(self):
        self.resync = 0
        self.flush = 0
        self.fwd = 0


async def watch(dut, mon: Mon):
    while True:
        await RisingEdge(dut.rx_clk)
        await ReadOnly()
        mon.resync += int(dut.resync.value)
        mon.flush += int(dut.flush.value)
        mon.fwd += int(dut.valid_out.value)
        await NextTimeStep()


async def reset(dut) -> Mon:
    cocotb.start_soon(Clock(dut.rx_clk, CLK, unit="ns").start())
    dut.rx_rst_n.value = 0
    dut.rx_lock.value = 0
    dut.data_in.value = 0
    dut.kmask_in.value = 0
    dut.err_in.value = 0
    dut.valid_in.value = 0
    for _ in range(3):
        await RisingEdge(dut.rx_clk)
    dut.rx_rst_n.value = 1
    await RisingEdge(dut.rx_clk)
    mon = Mon()
    cocotb.start_soon(watch(dut, mon))
    return mon


async def send(dut, n: int, idle: bool = False, err: int = 0):
    """Drive `n` words back to back: IDLE words or a data word."""
    for _ in range(n):
        dut.data_in.value = IDLE_WORD if idle else DATA
        dut.kmask_in.value = IDLE_KMASK if idle else 0
        dut.err_in.value = err
        dut.valid_in.value = 1
        await RisingEdge(dut.rx_clk)
    dut.valid_in.value = 0
    dut.err_in.value = 0


async def send_word(dut, data: int, kmask: int, err: int = 0):
    """Drive one word."""
    dut.data_in.value = data
    dut.kmask_in.value = kmask
    dut.err_in.value = err
    dut.valid_in.value = 1
    await RisingEdge(dut.rx_clk)
    dut.valid_in.value = 0
    dut.err_in.value = 0


async def bring_up(dut):
    dut.rx_lock.value = 1
    await send(dut, LOCK_IDLES, idle=True)
    await RisingEdge(dut.rx_clk)
    assert int(dut.up.value) == 1


# -----------------------------------------------------------------------------
# TC 1 — Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_reset(dut):
    """After reset the link is down and no word is forwarded.

    Stimulus: reset; 4 data words with `rx_lock` = 0.
    Checks:   `up`, `link_detected`, `resync`, `flush` all 0; nothing
              forwarded.
    """
    dut.TESTCASE.value = 1
    mon = await reset(dut)
    await send(dut, 4)
    await RisingEdge(dut.rx_clk)
    assert int(dut.up.value) == 0 and int(dut.link_detected.value) == 0
    assert (mon.resync, mon.flush, mon.fwd) == (0, 0, 0)


# -----------------------------------------------------------------------------
# TC 2 — Link Up
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_link_up(dut):
    """Two error-free IDLEs with the sampler locked bring the link up.

    §10.1.1: Detected once low-level lock is achieved.

    Stimulus: `rx_lock` = 1; 2 IDLE words; then 5 data words.
    Checks:   `up` and `link_detected` after the second IDLE; the 5 data
              words are forwarded; no pulse.
    """
    dut.TESTCASE.value = 2
    mon = await reset(dut)
    await bring_up(dut)
    assert int(dut.link_detected.value) == 1
    await send(dut, 5)
    await RisingEdge(dut.rx_clk)
    assert mon.fwd == 5, f"forwarded {mon.fwd}"
    assert (mon.resync, mon.flush) == (0, 0)


# -----------------------------------------------------------------------------
# TC 3 — No Lock, No Link
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_up_needs_lock_and_clean_idles(dut):
    """IDLEs without sampler lock, or with a code error, do not count.

    Stimulus: 4 IDLEs with `rx_lock` = 0; then with `rx_lock` = 1: IDLE,
              IDLE with `err_in`, IDLE.
    Checks:   still down after each phase (the error restarts the count);
              one more IDLE brings the link up.
    """
    dut.TESTCASE.value = 3
    await reset(dut)
    await send(dut, 4, idle=True)
    assert int(dut.up.value) == 0
    dut.rx_lock.value = 1
    await send(dut, 1, idle=True)
    await send(dut, 1, idle=True, err=1)
    await send(dut, 1, idle=True)
    await RisingEdge(dut.rx_clk)
    assert int(dut.up.value) == 0, "an errored IDLE must restart the count"
    await send(dut, 1, idle=True)
    await RisingEdge(dut.rx_clk)
    assert int(dut.up.value) == 1


# -----------------------------------------------------------------------------
# TC 4 — Loss Of IDLE
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_loss_of_idle(dut):
    """`LOSS_WORDS` words without an IDLE drop the link (§10.2).

    Stimulus: link up; 63 data words; then 1 more.
    Checks:   still up after 63; after the 64th one `resync` and one
              `flush` pulse, `up` = 0 and nothing more is forwarded.
    """
    dut.TESTCASE.value = 4
    mon = await reset(dut)
    await bring_up(dut)
    await send(dut, LOSS_WORDS - 1)
    await RisingEdge(dut.rx_clk)
    assert int(dut.up.value) == 1 and mon.resync == 0
    await send(dut, 1)
    await RisingEdge(dut.rx_clk)
    await RisingEdge(dut.rx_clk)
    assert int(dut.up.value) == 0
    assert (mon.resync, mon.flush) == (1, 1)
    fwd = mon.fwd
    await send(dut, 3)
    assert mon.fwd == fwd, "words forwarded while down"


# -----------------------------------------------------------------------------
# TC 5 — IDLE Keeps The Link
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_idle_refreshes(dut):
    """An IDLE inside every `LOSS_WORDS`-word window keeps the link up.

    Stimulus: link up; 4 × (63 data words, 1 IDLE).
    Checks:   `up` stays 1; no pulse.
    """
    dut.TESTCASE.value = 5
    mon = await reset(dut)
    await bring_up(dut)
    for _ in range(4):
        await send(dut, LOSS_WORDS - 1)
        await send(dut, 1, idle=True)
    await RisingEdge(dut.rx_clk)
    assert int(dut.up.value) == 1
    assert (mon.resync, mon.flush) == (0, 0)


# -----------------------------------------------------------------------------
# TC 6 — Sampler Lock Loss
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_lock_loss(dut):
    """Losing sampler lock drops the link at once, without a resync.

    Stimulus: link up; 3 data words; `rx_lock` = 0.
    Checks:   one `flush` pulse, no `resync`; `up` and `link_detected` 0.
    """
    dut.TESTCASE.value = 6
    mon = await reset(dut)
    await bring_up(dut)
    await send(dut, 3)
    dut.rx_lock.value = 0
    await RisingEdge(dut.rx_clk)
    await RisingEdge(dut.rx_clk)
    await RisingEdge(dut.rx_clk)
    assert int(dut.up.value) == 0 and int(dut.link_detected.value) == 0
    assert (mon.resync, mon.flush) == (0, 1)


# -----------------------------------------------------------------------------
# TC 7 — Locked, Down, No IDLE
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_down_locked_no_idle(dut):
    """A locked sampler that never delivers a clean IDLE is re-hunted.

    §10.2: a device that cannot find the IDLE pattern must keep looking.
    The sampler leaves lock only on `resync`, and loss is counted only
    while up, so without this exit a lock on the wrong character phase
    (cable re-plug before link-up) lasts until reset.

    Stimulus: `rx_lock` = 1, link down; 100 data words, then 100 IDLE
              words each with `err_in` = 1.
    Checks:   at least one `resync` pulse; the link stays down.
    """
    dut.TESTCASE.value = 7
    mon = await reset(dut)
    dut.rx_lock.value = 1
    await send(dut, 100)
    await send(dut, 100, idle=True, err=1)
    for _ in range(4):
        await RisingEdge(dut.rx_clk)
    assert int(dut.up.value) == 0
    assert mon.resync >= 1, "no resync while locked and down"


# -----------------------------------------------------------------------------
# TC 8 — K28.5 Off P0 While Up
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_k28_5_off_p0(dut):
    """A comma outside P0 shows the lanes are rotated (Table 14).

    §10.2 / Table 14: K28.5 is only ever sent in P0 of an IDLE word, so a
    clean word with K28.5 in another lane means the word framing slipped
    by whole characters.  Waiting for the IDLE loss window instead leaves
    every command unanswered for up to 38 ms.

    Stimulus: link up; 3 data words; one IDLE rotated by one character
              (D21.5 K28.5 K28.1 K28.1, kmask 1110); 3 data words.
    Checks:   one `resync` and one `flush` within 2 cycles of the rotated
              word; `up` = 0 afterwards.
    """
    dut.TESTCASE.value = 8
    mon = await reset(dut)
    await bring_up(dut)
    await send(dut, 3)
    rot = ((IDLE_WORD & 0x00FF_FFFF) << 8) | (IDLE_WORD >> 24)
    await send_word(dut, rot, ((IDLE_KMASK << 1) | (IDLE_KMASK >> 3)) & 0xF)
    await RisingEdge(dut.rx_clk)
    await RisingEdge(dut.rx_clk)
    assert (mon.resync, mon.flush) == (1, 1), f"resync {mon.resync} flush {mon.flush}"
    assert int(dut.up.value) == 0
    await send(dut, 3)
    assert mon.resync == 1


# -----------------------------------------------------------------------------
# TC 9 — Error Run While Up
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_error_run_while_up(dut):
    """Words that keep failing to decode mean the character phase is lost.

    §10.2: "the receiver for that connection is reset (so e.g. character
    and word alignment re-established)".  A bit slip while up leaves every
    later character undecodable; the IDLE loss window (20 000 words in the
    product) is the only exit today.

    Stimulus: link up; 40 data words with `err_in` = 1, no IDLE.
    Checks:   one `resync` before the 40th word ends; `up` = 0.
    """
    dut.TESTCASE.value = 9
    mon = await reset(dut)
    await bring_up(dut)
    await send(dut, 40, err=1)
    await RisingEdge(dut.rx_clk)
    await RisingEdge(dut.rx_clk)
    assert mon.resync == 1, f"{mon.resync} resync after 40 errored words"
    assert int(dut.up.value) == 0


# -----------------------------------------------------------------------------
# TC 10 — Sparse Errors Keep The Link
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_sparse_errors_keep_link(dut):
    """Errored words between clean IDLEs do not re-align a framed link.

    §10.2 comment: burst errors should not reset the connection.  Each
    clean IDLE shows the framing is right.

    Stimulus: link up; 10 x (19 words alternating errored / clean data,
              1 clean IDLE).
    Checks:   `up` stays 1; no `resync`, no `flush`.
    """
    dut.TESTCASE.value = 10
    mon = await reset(dut)
    await bring_up(dut)
    for _ in range(10):
        for i in range(19):
            await send(dut, 1, err=i & 1)
        await send(dut, 1, idle=True)
    await RisingEdge(dut.rx_clk)
    assert int(dut.up.value) == 1
    assert (mon.resync, mon.flush) == (0, 0)


# -----------------------------------------------------------------------------
# TC 11 — Link Up Under Sparse IDLE
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_sparse_idle_link_up(dut):
    """Clean data between rare IDLEs does not keep the link down.

    §8.2.5.1: a low-speed host needs to send an IDLE only once per 10 000
    words; §8.7.3: one IDLE between 1027-word test packets.  A device that
    takes every clean non-IDLE word as a sign of wrong framing re-hunts
    after a few dozen words and never comes up while such a host keeps
    its spacing.

    Stimulus: `rx_lock` = 1, link down; three times: 200 clean data words
              then one IDLE.  Then link reset (lock drop), lock again, 5
              data words and one clean word with K28.5 in P2.
    Checks:   no `resync` during the data; `up` = 1 after the second IDLE
              and still 1 at the end of the third block (IDLE loss is 64
              words in this wrapper, so the third block runs while up:
              stimulus stops at 63 words there); then exactly one `resync`
              for the rotated comma while down.
    """
    dut.TESTCASE.value = 11
    mon = await reset(dut)
    dut.rx_lock.value = 1
    for blk in range(2):
        await send(dut, 200)
        await send(dut, 1, idle=True)
        await RisingEdge(dut.rx_clk)
        assert mon.resync == 0, f"resync during clean data while down (block {blk})"
    assert int(dut.up.value) == 1, "link not up after two IDLEs 200 words apart"
    await send(dut, 63)
    await send(dut, 1, idle=True)
    await RisingEdge(dut.rx_clk)
    assert int(dut.up.value) == 1 and mon.resync == 0
    dut.rx_lock.value = 0
    await RisingEdge(dut.rx_clk)
    await RisingEdge(dut.rx_clk)
    assert int(dut.up.value) == 0
    dut.rx_lock.value = 1
    await send(dut, 5)
    rot = ((IDLE_WORD & 0x0000_FFFF) << 16) | (IDLE_WORD >> 16)
    rot_k = ((IDLE_KMASK & 0x3) << 2) | (IDLE_KMASK >> 2)
    await send_word(dut, rot, rot_k)
    for _ in range(3):
        await RisingEdge(dut.rx_clk)
    assert mon.resync == 1, f"{mon.resync} resync for K28.5 in P2 while down"
    assert int(dut.up.value) == 0
