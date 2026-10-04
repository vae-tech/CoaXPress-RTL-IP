"""Cocotb TB for `cxp_rx_linktest`.

The DUT is the §8.7.1 Device Test Receiver: it compares the body of every
host connection-test packet (type 0x04) against the Table 23 sequence,
counts differing words into TestErrorCount (`err_count`, 32-bit,
saturating) and counts packets into TestPacketCountRx (`pkt_count`,
64-bit).  All 1024 words are compared (Table 23 has no CRC); a K-character
word, a word beyond index 1023 and a word that never arrived each count
one error.  `clr_err` clears TestErrorCount and `clr_pkt` TestPacketCountRx
(§10.3.37 / §10.3.39); ConnectionReset drives both.

The TB drives the gated `long_*` body stream directly, as
`cxp_rx_packet_parser` would with `gate = long_valid & (type == 0x04)`,
through `tb_cxp_rx_linktest_top` (10 ns clock).  `send_packet()` emits the
4×0x04 TYPE word with `long_sop`, n data words from the golden
`cxp_protocol.packets.linktest_word` (bit 0 flipped at the listed
indices, or a 4×K28.5 word at the listed K indices), the 4×K29.7 word
(kmask 0xF) with `long_eop`, then one cycle with `gate = 0`.  Tests read
`err_count` / `pkt_count` after the last edge; there is no shared checker
and no FSM coverage (the module has no FSM).

Spec (CXP-001-2015 v1.1.1): §8.7.1–§8.7.3 connection test, Table 23 test
packet, §10.3.37 TestErrorCount, §10.3.39 TestPacketCountRx, §10.3.28
ConnectionReset.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Reset → `err_count = 0`.
  2  Clean 1027-word test packets → `err_count` stays 0.
  3  Injected mismatches, including word 1023 → one count per word.
  4  `clr_err` pulse → `err_count = 0`, `pkt_count` kept.
  5  Counter saturates at 0xFFFFFFFF (no rollover).
  6  K-character word in the body → exactly one error.
  7  TestPacketCountRx increments per packet and clears with `clr_pkt`
     alone (§10.3.39).
  8  Short packet → every missing word counts; long packet → the extra
     word counts.
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from cxp_protocol.packets import LT_DATA_WORDS, linktest_word
from cxp_testcase import cxp_test


CLK = 10


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def reset(dut):
    """Start the clock, hold reset 4 edges with inputs 0, settle 1 edge."""
    cocotb.start_soon(Clock(dut.rx_clk, CLK, unit="ns").start())
    dut.rx_rst_n.value = 0
    dut.gate.value = 0
    dut.long_data.value = 0
    dut.long_kmask.value = 0
    dut.long_sop.value = 0
    dut.long_eop.value = 0
    dut.long_err.value = 0
    dut.clr_err.value = 0
    dut.clr_pkt.value = 0
    for _ in range(4):
        await RisingEdge(dut.rx_clk)
    dut.rx_rst_n.value = 1
    await RisingEdge(dut.rx_clk)


# -----------------------------------------------------------------------------
# Stimulus helpers
# -----------------------------------------------------------------------------
async def send_packet(dut, n_data: int = LT_DATA_WORDS,
                      error_indices: list[int] | None = None,
                      k_indices: list[int] | None = None):
    """Send a Table 23 packet body: TYPE (4×0x04, `long_sop`), `n_data`
    data words, K29.7 (`long_eop`).  Words at `error_indices` get bit 0
    flipped; words at `k_indices` are replaced by 4×K28.5 (kmask 0xF)."""
    error_indices = set(error_indices or [])
    k_indices = set(k_indices or [])

    dut.gate.value = 1
    dut.long_data.value = 0x0404_0404
    dut.long_kmask.value = 0
    dut.long_sop.value = 1
    dut.long_eop.value = 0
    await RisingEdge(dut.rx_clk)
    dut.long_sop.value = 0

    for i in range(n_data):
        if i in k_indices:
            dut.long_data.value = 0xBCBC_BCBC
            dut.long_kmask.value = 0b1111
        else:
            word = linktest_word(i)
            if i in error_indices:
                word ^= 0x0000_0001
            dut.long_data.value = word
            dut.long_kmask.value = 0
        await RisingEdge(dut.rx_clk)

    dut.long_data.value = 0xFDFD_FDFD
    dut.long_kmask.value = 0b1111
    dut.long_eop.value = 1
    await RisingEdge(dut.rx_clk)
    dut.long_eop.value = 0
    dut.gate.value = 0
    await RisingEdge(dut.rx_clk)


# -----------------------------------------------------------------------------
# TC 1 — Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_reset(dut):
    """TestErrorCount reads 0 after reset.

    Guards the reset branch of the §10.3.37 counter register.

    Stimulus: reset sequence only; `gate` stays 0.
    Checks:   `err_count == 0`.
    Note:     `pkt_count` is not read.
    """
    dut.TESTCASE.value = 1
    await reset(dut)
    assert int(dut.err_count.value) == 0


# -----------------------------------------------------------------------------
# TC 2 — Clean Packets
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_clean(dut):
    """Correct test packets never increment TestErrorCount.

    Proves the word index restarts on each TYPE (`long_sop`) word and
    that the sequence rolls over every 64 words, so a compliant body
    produces no false errors.

    Stimulus: 3 full Table 23 packets (1024 data words, no CRC), one
              `gate = 0` cycle between packets.
    Checks:   `err_count == 0` after the last packet.
    """
    dut.TESTCASE.value = 2
    await reset(dut)
    for _ in range(3):
        await send_packet(dut)
    assert int(dut.err_count.value) == 0, f"err_count = {int(dut.err_count.value)}"


# -----------------------------------------------------------------------------
# TC 3 — Injected Errors
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_inject_errors(dut):
    """Each corrupted body word adds exactly one to TestErrorCount.

    §8.7.1 counts every word that differs from the Table 23 pattern; the
    count must carry across packets.

    Stimulus: full packet A, bit 0 flipped in words 1, 5 and 1022; full
              packet B, bit 0 flipped in word 1023 only.
    Checks:   `err_count == 3` after A and `== 4` after B — the last word
              before K29.7 is compared like every other.
    """
    dut.TESTCASE.value = 3
    await reset(dut)
    await send_packet(dut, error_indices=[1, 5, 1022])
    assert int(dut.err_count.value) == 3, f"err_count = {int(dut.err_count.value)}"
    await send_packet(dut, error_indices=[1023])
    expected = 3 + 1
    assert int(dut.err_count.value) == expected, (
        f"err_count = {int(dut.err_count.value)} expected {expected}"
    )


# -----------------------------------------------------------------------------
# TC 4 — Clear Pulse
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_clear(dut):
    """A `clr_err` pulse zeroes TestErrorCount and nothing else.

    Models the host write of 0 / ConnectionReset (§10.3.37, §10.3.28).

    Stimulus: a full packet with bit 0 flipped in words 1, 2 and 3;
              then `clr_err = 1` for one cycle, then one more cycle.
    Checks:   `err_count == 3` before the clear; `err_count == 0` one
              cycle after `clr_err` falls; `pkt_count` still 1.
    Note:     only the idle case — no word is in flight during the clear.
    """
    dut.TESTCASE.value = 4
    await reset(dut)
    await send_packet(dut, error_indices=[1, 2, 3])
    assert int(dut.err_count.value) == 3
    dut.clr_err.value = 1
    await RisingEdge(dut.rx_clk)
    dut.clr_err.value = 0
    await RisingEdge(dut.rx_clk)
    assert int(dut.err_count.value) == 0
    assert int(dut.pkt_count.value) == 1, "clr_err also cleared TestPacketCountRx"


# -----------------------------------------------------------------------------
# TC 5 — Saturation
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_saturation(dut):
    """TestErrorCount stops at 0xFFFFFFFF instead of wrapping to 0.

    §10.3.37 defines a saturating counter; covers the
    `err_count_q != 32'hFFFF_FFFF` term of the increment condition.

    Stimulus: backdoor write `cxp_rx_linktest_i.err_count_q = 0xFFFFFFFE`,
              one edge, then a full packet with words 0–9 corrupted, so
              the counter is asked to advance 10 times.
    Checks:   `err_count == 0xFFFFFFFF`.
    Note:     saturation is reached only through the backdoor preload, not
              through the datapath.
    """
    dut.TESTCASE.value = 5
    await reset(dut)
    # 2^32 errors cannot be reached through the datapath in a unit test:
    # preload the internal counter one below saturation via backdoor.
    dut.cxp_rx_linktest_i.err_count_q.value = 0xFFFFFFFE
    await RisingEdge(dut.rx_clk)
    await send_packet(dut, error_indices=list(range(10)))
    cnt = int(dut.err_count.value)
    assert cnt == 0xFFFFFFFF, f"saturated counter = 0x{cnt:08x}"


# -----------------------------------------------------------------------------
# TC 6 — K-Character in Body
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_kchar_in_body(dut):
    """A K-character word inside the body costs exactly one error.

    §8.7.1 counts each word that differs; the word index still advances,
    so the words after it compare against the right sequence position.

    Stimulus: a full packet with word 3 replaced by 4×K28.5 (kmask 0xF),
              then a clean full packet.
    Checks:   `err_count == 1` after both packets.
    """
    dut.TESTCASE.value = 6
    await reset(dut)
    await send_packet(dut, k_indices=[3])
    await send_packet(dut)
    assert int(dut.err_count.value) == 1, f"err_count = {int(dut.err_count.value)}"


# -----------------------------------------------------------------------------
# TC 7 — TestPacketCountRx
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_pkt_count_rx(dut):
    """TestPacketCountRx counts each received packet and clears on request.

    v1.1.1 §10.3.39 (new since v1.0): one increment per received
    connection-test packet (on the K29.7 trailer), independent of word
    errors, cleared by `clr_pkt` alone (host write 0 to TestPacketCountRx;
    ConnectionReset drives both clears).

    Stimulus: read after reset; three clean full packets; a full packet
              with bit 0 flipped in words 2 and 4; then
              `clr_pkt = 1` for one cycle and one more cycle.
    Checks:   `pkt_count == 0` after reset, then 1, 2, 3 after each clean
              packet; `pkt_count == 4` and `err_count == 2` after the
              errored packet; after the clear `pkt_count == 0` and
              `err_count` still 2.
    """
    dut.TESTCASE.value = 7
    await reset(dut)
    assert int(dut.pkt_count.value) == 0

    for n in range(1, 4):
        await send_packet(dut)
        assert int(dut.pkt_count.value) == n, (
            f"TestPacketCountRx = {int(dut.pkt_count.value)}, expected {n}"
        )

    # Errors must not affect the packet count.
    await send_packet(dut, error_indices=[2, 4])
    assert int(dut.pkt_count.value) == 4
    assert int(dut.err_count.value) == 2

    # Host write 0 to TestPacketCountRx clears that counter only.
    dut.clr_pkt.value = 1
    await RisingEdge(dut.rx_clk)
    dut.clr_pkt.value = 0
    await RisingEdge(dut.rx_clk)
    assert int(dut.pkt_count.value) == 0, "TestPacketCountRx not cleared"
    assert int(dut.err_count.value) == 2, "clr_pkt also cleared TestErrorCount"


# -----------------------------------------------------------------------------
# TC 8 — Short And Long Packets
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_short_and_long_packets(dut):
    """Missing and surplus words each count one error.

    A word that never arrived differs from the sequence (§8.7.1), as does
    a word beyond index 1023.

    Stimulus: a packet of 1000 correct words; then one of 1025 words
              (1024 correct plus one extra).
    Checks:   `err_count == 24` after the short packet, `== 25` after the
              long one; `pkt_count == 2`.
    """
    dut.TESTCASE.value = 8
    await reset(dut)
    await send_packet(dut, n_data=1000)
    assert int(dut.err_count.value) == 24, f"err_count = {int(dut.err_count.value)}"
    await send_packet(dut, n_data=1025)
    assert int(dut.err_count.value) == 25, f"err_count = {int(dut.err_count.value)}"
    assert int(dut.pkt_count.value) == 2
