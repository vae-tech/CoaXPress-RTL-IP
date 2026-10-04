"""Cocotb TB for `cxp_tx_linktest`.

TX-side connection-test packet source (CoaXPress 1.1.1, CXP-001-2015 §8.7,
Table 23; §10.3.35 TestMode, §10.3.38 TestPacketCountTx).  Migrated from the
v1.0 numbering (old §6.7.4 / Table 22).  While the host sets
`cfg_test_mode=1` the DUT emits link-test packets back to back, separated by
an idle gap, and drives `suppress_traffic` to mask other traffic.

The wrapper shrinks DATA_WORDS to 16 and GAP_WORDS to 4 so each packet plus
gap fits in 23 cycles — every observable behaviour scales.  `tx_clk` is
8 ns; `reset()` holds `tx_rst_n` low 4 edges with `cfg_test_mode = 0`,
`clr_pkt_count = 0` and `m_ready = 1`.  Monitors sample after each rising
edge and keep only accepted (`m_valid & m_ready`) beats; `split_into_packets`
asserts SOP/EOP framing and `check_packet_shape` compares a packet with the
model below.  FSM state/arc coverage of `cxp_tx_linktest_i.state_q` is
collected via `fsm_coverage`.

Wire-level model of one link-test packet (Table 23):

    SOP   : data = 4×K27.7 (0xFB), kmask=1111,  m_sop=1
    TYPE  : data = 4×0x04,        kmask=0000
    DATA0 : data = 0x03_02_01_00, kmask=0000   <- counting sequence
    DATA1 : data = 0x07_06_05_04
    ...
    DATA[N-1]
    EOP   : data = 4×K29.7 (0xFD), kmask=1111,  m_eop=1

After EOP the DUT must hold m_valid=0 for `GAP_WORDS` cycles before
asserting the next SOP, regardless of m_ready behaviour during the gap.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Reset → idle, no traffic, no suppress.
  2  `cfg_test_mode = 0` → no packets for 80 cycles.
  3  Single packet wire-level shape (SOP, TYPE, N data, EOP).
  4  Five consecutive packets, same canonical content.
  5  Inter-packet spacing ≥ GAP_WORDS.
  6  `suppress_traffic` follows TestMode up and falls only after drain.
  7  Mode exit mid-packet completes the current packet, then silence.
  8  Back-pressure: random `m_ready` stalls keep every packet intact.
  9  Every packet restarts the sequence at 0x03020100 (named "rollover").
 10  TestPacketCountTx counts accepted EOPs and clears on `clr_pkt_count`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from cxp_testcase import cxp_test
from fsm_coverage import register_fsm


CLK_NS = 8

# 8B/10B control character byte values used in the linktest packet.
K27_7   = 0xFB
K29_7   = 0xFD
TYPE_LT = 0x04


# -----------------------------------------------------------------------------
# FSM coverage
# -----------------------------------------------------------------------------
# Three states: the per-word packet framing moved into cxp_tx_pkt_framer,
# leaving this module the test-mode gate and the inter-packet gap.
register_fsm(
    name="tx_linktest",
    states=["ST_IDLE", "ST_PKT", "ST_GAP"],
    state_path="cxp_tx_linktest_i.state_q",
    clk_path="tx_clk",
    arcs=[
        ("ST_IDLE", "ST_PKT"), ("ST_PKT", "ST_GAP"),
        ("ST_GAP", "ST_PKT"), ("ST_GAP", "ST_IDLE"),
    ],
)

# The packet itself comes from the shared cxp_tx_pkt_framer, here the one
# instance without a CRC (p_HAS_CRC = 0): the payload ends straight in the
# trailer, and ST_CRC cannot be reached.
register_fsm(
    name="tx_linktest_framer",
    states=["ST_IDLE", "ST_HDR", "ST_DATA", "ST_CRC", "ST_EOP"],
    state_path="cxp_tx_linktest_i.cxp_tx_pkt_framer_i.state_q",
    clk_path="tx_clk",
    arcs=[
        ("ST_IDLE", "ST_HDR"), ("ST_HDR", "ST_DATA"),
        ("ST_DATA", "ST_EOP"), ("ST_EOP", "ST_IDLE"),
    ],
    unreachable=["ST_CRC"],
)


# -----------------------------------------------------------------------------
# Reference model
# -----------------------------------------------------------------------------
def rep4(b: int) -> int:
    """Replicate byte `b` into all four lanes of a 32-bit word."""
    b &= 0xFF
    return (b << 24) | (b << 16) | (b << 8) | b


def expected_data_word(seq: int) -> int:
    """The i-th data word for sequence start `seq` — {seq+3,seq+2,seq+1,seq}
    laid out P3..P0 in the 32-bit word with P0 in the LSB lane."""
    s0 = seq & 0xFF
    s1 = (seq + 1) & 0xFF
    s2 = (seq + 2) & 0xFF
    s3 = (seq + 3) & 0xFF
    return (s3 << 24) | (s2 << 16) | (s1 << 8) | s0


@dataclass
class WireBeat:
    """One output beat: data, kmask, SOP and EOP flags."""
    data:  int
    kmask: int
    sop:   int
    eop:   int


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def reset(dut, *, ready: int = 1):
    """Start `tx_clk`, hold reset 4 edges with TestMode/clear 0, release."""
    cocotb.start_soon(Clock(dut.tx_clk, CLK_NS, unit="ns").start(start_high=False))
    dut.tx_rst_n.value      = 0
    dut.cfg_test_mode.value = 0
    dut.clr_pkt_count.value = 0
    dut.m_ready.value       = ready
    for _ in range(4):
        await RisingEdge(dut.tx_clk)
    dut.tx_rst_n.value = 1
    await RisingEdge(dut.tx_clk)


def n_data(dut) -> int:
    """Elaborated DATA_WORDS of the DUT instance."""
    return int(dut.cxp_tx_linktest_i.DATA_WORDS.value)


def n_gap(dut) -> int:
    """Elaborated GAP_WORDS of the DUT instance."""
    return int(dut.cxp_tx_linktest_i.GAP_WORDS.value)


# -----------------------------------------------------------------------------
# Monitors
# -----------------------------------------------------------------------------
def sample(dut) -> WireBeat:
    """Snapshot the output beat currently on the bus."""
    return WireBeat(
        data  = int(dut.m_data.value),
        kmask = int(dut.m_kmask.value),
        sop   = int(dut.m_sop.value),
        eop   = int(dut.m_eop.value),
    )


async def capture_accepted(dut, cycles: int, *,
                           ready_pattern=None) -> list[WireBeat]:
    """Drive m_ready (default constant 1, or via callable returning 0/1)
    for `cycles` cycles and capture the per-cycle accepted-beat history.
    Returns the list of WireBeat values whose m_valid & m_ready was high."""
    out: list[WireBeat] = []
    for c in range(cycles):
        if ready_pattern is not None:
            dut.m_ready.value = ready_pattern(c)
        await RisingEdge(dut.tx_clk)
        if int(dut.m_valid.value) and int(dut.m_ready.value):
            out.append(sample(dut))
    return out


# -----------------------------------------------------------------------------
# Checkers
# -----------------------------------------------------------------------------
def split_into_packets(beats: list[WireBeat]) -> list[list[WireBeat]]:
    """Group accepted beats into packets, delimited by SOP (start) and
    EOP (end).  A well-formed run is SOP, body..., EOP repeated."""
    pkts: list[list[WireBeat]] = []
    cur: list[WireBeat] = []
    in_pkt = False
    for b in beats:
        if b.sop:
            assert not in_pkt, "SOP arrived inside a packet"
            in_pkt = True
            cur = [b]
        else:
            assert in_pkt, "non-SOP beat outside a packet"
            cur.append(b)
        if b.eop:
            assert in_pkt, "EOP outside a packet"
            in_pkt = False
            pkts.append(cur)
            cur = []
    return pkts


def check_packet_shape(pkt: list[WireBeat], data_words: int) -> None:
    """Assert `pkt` is SOP, TYPE, `data_words` counting words, EOP."""
    expected_len = 1 + 1 + data_words + 1  # SOP + TYPE + N + EOP
    assert len(pkt) == expected_len, (
        f"packet length {len(pkt)} != expected {expected_len}"
    )

    # SOP: K27.7 replicated, kmask=0xF, sop=1.
    assert pkt[0].data == rep4(K27_7), f"SOP data {pkt[0].data:#010x}"
    assert pkt[0].kmask == 0xF,         f"SOP kmask {pkt[0].kmask:#x}"
    assert pkt[0].sop == 1 and pkt[0].eop == 0

    # TYPE word: 4×0x04, kmask=0.
    assert pkt[1].data == rep4(TYPE_LT), f"TYPE data {pkt[1].data:#010x}"
    assert pkt[1].kmask == 0,             f"TYPE kmask {pkt[1].kmask:#x}"
    assert pkt[1].sop == 0 and pkt[1].eop == 0

    # Data body: counting sequence with seq advancing by 4 per word.
    seq = 0
    for i, b in enumerate(pkt[2:2 + data_words]):
        assert b.kmask == 0, f"data[{i}] kmask {b.kmask:#x} (should be 0)"
        assert b.sop == 0 and b.eop == 0
        exp = expected_data_word(seq)
        assert b.data == exp, (
            f"data[{i}]: got {b.data:#010x}, expected {exp:#010x} (seq={seq:#x})"
        )
        seq = (seq + 4) & 0xFF

    # EOP: K29.7 replicated, kmask=0xF, eop=1.
    last = pkt[-1]
    assert last.data == rep4(K29_7), f"EOP data {last.data:#010x}"
    assert last.kmask == 0xF,         f"EOP kmask {last.kmask:#x}"
    assert last.sop == 0 and last.eop == 1


# -----------------------------------------------------------------------------
# TC 1 — Reset Leaves the Source Idle
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_reset_idle(dut):
    """After reset with TestMode off the source is silent and not suppressing.

    Guards the ST_IDLE reset state and both terms of `suppress_traffic`
    (`cfg_test_mode` and `state_q != ST_IDLE`) being low.

    Stimulus: `reset()`, then 8 cycles with every input at its reset value
              (`cfg_test_mode = 0`, `m_ready = 1`).
    Checks:   after each of the 8 edges `m_valid == 0` and
              `suppress_traffic == 0`.
    """
    dut.TESTCASE.value = 1
    await reset(dut)
    # cfg_test_mode is 0 — m_valid must be 0 and suppress_traffic must be 0.
    for _ in range(8):
        await RisingEdge(dut.tx_clk)
        assert int(dut.m_valid.value)          == 0
        assert int(dut.suppress_traffic.value) == 0


# -----------------------------------------------------------------------------
# TC 2 — Idle While Disabled
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_idle_when_disabled(dut):
    """With TestMode off the source stays quiet indefinitely.

    ST_IDLE may only leave on `cfg_test_mode` (§10.3.35).

    Stimulus: `reset()`, `cfg_test_mode = 0`, 80 cycles of capture with
              `m_ready = 1`.
    Checks:   no accepted beat in the 80 cycles; `suppress_traffic == 0`
              at the end.
    """
    dut.TESTCASE.value = 2
    await reset(dut)
    dut.cfg_test_mode.value = 0
    beats = await capture_accepted(dut, cycles=80)
    assert beats == [], f"DUT emitted {len(beats)} beats while disabled"
    assert int(dut.suppress_traffic.value) == 0


# -----------------------------------------------------------------------------
# TC 3 — Single Packet Format
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_single_packet_format(dut):
    """One link-test packet has the exact Table 23 wire format.

    Covers the SOP → TYPE → DATA → EOP walk, the `rep4` framing words and
    the byte order of the counting payload.

    Stimulus: `cfg_test_mode = 1`, `m_ready = 1`; up to 60 cycles of
              capture, dropping `cfg_test_mode` to 0 as soon as the first
              accepted EOP is seen.
    Checks:   an EOP is seen within 60 cycles; `split_into_packets`
              yields exactly 1 packet; `check_packet_shape` passes with
              N = 16 (length N+3, SOP 4×0xFB kmask 0xF, TYPE 4×0x04 kmask
              0, words {s+3,s+2,s+1,s} for s = 0,4,…,0x3C with kmask 0 and
              no flags, EOP 4×0xFD kmask 0xF).
    """
    dut.TESTCASE.value = 3
    await reset(dut)
    N = n_data(dut)

    dut.cfg_test_mode.value = 1
    # Capture exactly one packet, then drop test_mode and let the gap drain.
    pkt_beats: list[WireBeat] = []
    seen_eop = False
    for _ in range(60):
        await RisingEdge(dut.tx_clk)
        if int(dut.m_valid.value) and int(dut.m_ready.value):
            pkt_beats.append(sample(dut))
            if pkt_beats[-1].eop:
                seen_eop = True
                dut.cfg_test_mode.value = 0
                break
    assert seen_eop, "did not observe EOP within 60 cycles"
    pkts = split_into_packets(pkt_beats)
    assert len(pkts) == 1, f"expected 1 packet, got {len(pkts)}"
    check_packet_shape(pkts[0], data_words=N)


# -----------------------------------------------------------------------------
# TC 4 — Repeated Packets Pattern
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_repeated_packets_pattern(dut):
    """Back-to-back packets under TestMode all carry the canonical pattern.

    Covers the ST_GAP → ST_SOP re-arm and the per-packet reset of the
    sequence byte and data index.

    Stimulus: `cfg_test_mode = 1`, `m_ready = 1`, capture for
              5 × (3 + N + G) + 10 = 125 cycles (N = 16, G = 4), then
              `cfg_test_mode = 0`.
    Checks:   `split_into_packets` framing holds and yields ≥ 5 packets;
              `check_packet_shape` passes on the first 5.
    """
    dut.TESTCASE.value = 4
    await reset(dut)
    N = n_data(dut)
    G = n_gap(dut)

    dut.cfg_test_mode.value = 1
    # Generously size the capture window.  Each packet takes
    # (3 + N) cycles, plus G cycles gap; aim for at least 5 packets.
    target_pkts  = 5
    cycles       = target_pkts * (3 + N + G) + 10
    beats        = await capture_accepted(dut, cycles=cycles)
    pkts = split_into_packets(beats)
    assert len(pkts) >= target_pkts, (
        f"expected ≥{target_pkts} packets, got {len(pkts)}"
    )
    for i, p in enumerate(pkts[:target_pkts]):
        check_packet_shape(p, data_words=N)
    dut.cfg_test_mode.value = 0


# -----------------------------------------------------------------------------
# TC 5 — Inter-Packet Gap Spacing
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_gap_spacing(dut):
    """Consecutive packets are separated by at least GAP_WORDS silent cycles.

    Guards the `gap_q` / `gap_done` counter that spaces test packets: the
    next SOP may not come earlier than GAP_WORDS cycles after the EOP.

    Stimulus: `cfg_test_mode = 1`, `m_ready = 1` for
              4 × (3 + N + G) + 10 = 102 cycles, recording the cycle index
              of every accepted SOP and EOP; then `cfg_test_mode = 0`.
    Checks:   ≥ 2 EOPs and ≥ 3 SOPs recorded; for every EOP followed by a
              SOP, `next_sop − eop − 1 ≥ GAP_WORDS` (4).
    Note:     only the lower bound is checked — the gap is never asserted
              to be exactly GAP_WORDS, and `m_valid` inside the gap is not
              sampled (only SOP/EOP positions are).
    """
    dut.TESTCASE.value = 5
    await reset(dut)
    G = n_gap(dut)
    N = n_data(dut)

    dut.cfg_test_mode.value = 1
    # Watch for EOP→SOP transitions and count the silent cycles between.
    cycles = 4 * (3 + N + G) + 10
    eops_at: list[int] = []
    sops_at: list[int] = []
    for c in range(cycles):
        await RisingEdge(dut.tx_clk)
        if int(dut.m_valid.value) and int(dut.m_ready.value):
            if int(dut.m_sop.value): sops_at.append(c)
            if int(dut.m_eop.value): eops_at.append(c)
    dut.cfg_test_mode.value = 0
    assert len(eops_at) >= 2 and len(sops_at) >= 3, (
        f"need >= 2 EOPs and 3 SOPs to measure spacing "
        f"(eops={eops_at}, sops={sops_at})"
    )
    for k in range(len(eops_at)):
        # The next SOP after eops_at[k] is the start of packet k+1.
        next_sops = [s for s in sops_at if s > eops_at[k]]
        if not next_sops:
            continue
        gap = next_sops[0] - eops_at[k] - 1
        assert gap >= G, (
            f"gap after EOP@{eops_at[k]} = {gap} cycles, < GAP_WORDS={G}"
        )


# -----------------------------------------------------------------------------
# TC 6 — Suppress-Traffic Level
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_suppress_traffic(dut):
    """`suppress_traffic` rises with TestMode and falls only once drained.

    suppress_traffic asserts whenever cfg_test_mode is high or the FSM is
    mid-packet/gap; deasserts only when fully back in ST_IDLE, so other
    TX traffic stays masked until the last test packet and its gap end.

    Stimulus: 4 cycles with `cfg_test_mode = 0`; `cfg_test_mode = 1` for
              1 + 23 cycles (23 = 3 + N + 4); then `cfg_test_mode = 0`
              and a drain window of up to 4 × (3 + N + G) + 10 = 102
              cycles; `m_ready = 1` throughout.
    Checks:   `suppress_traffic == 0` on each of the first 4 samples; `== 1`
              on the first sample after TestMode rises and on all 23
              samples after that; returns to 0 within the drain window.
    Note:     the drain is only bounded, not timed — `suppress_traffic`
              is not checked to stay high until the gap actually ends.
    """
    dut.TESTCASE.value = 6
    await reset(dut)

    # Disabled → must be 0.
    for _ in range(4):
        await RisingEdge(dut.tx_clk)
        assert int(dut.suppress_traffic.value) == 0

    # Enable test mode.  suppress_traffic must rise on the next sample.
    dut.cfg_test_mode.value = 1
    await RisingEdge(dut.tx_clk)
    assert int(dut.suppress_traffic.value) == 1, (
        "suppress_traffic must rise as soon as cfg_test_mode does"
    )

    # Hold for a full packet.
    N = n_data(dut)
    cycles_for_one_pkt = 3 + N + 4
    for _ in range(cycles_for_one_pkt):
        await RisingEdge(dut.tx_clk)
        assert int(dut.suppress_traffic.value) == 1

    # Drop cfg_test_mode mid-stream — must remain asserted until the
    # in-flight packet + gap drain back to ST_IDLE.
    dut.cfg_test_mode.value = 0
    drained = False
    for _ in range(4 * (3 + N + n_gap(dut)) + 10):
        await RisingEdge(dut.tx_clk)
        if int(dut.suppress_traffic.value) == 0:
            drained = True
            break
    assert drained, "suppress_traffic never returned to 0 after cfg_test_mode=0"


# -----------------------------------------------------------------------------
# TC 7 — Mode Exit Finishes the Packet
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_mode_exit_finishes_pkt(dut):
    """Dropping TestMode mid-packet still ends that packet with an EOP.

    §10.3.35: on TestMode 1→0 the in-flight packet completes, then the
    source returns to ST_IDLE and stays silent (ST_GAP → ST_IDLE arc).

    Stimulus: `cfg_test_mode = 1`, `m_ready = 1`; once `m_valid & m_sop`
              is seen (within 10 cycles) 2 more edges pass and
              `cfg_test_mode` drops to 0; then up to 3 + N + 5 = 24 cycles
              waiting for the EOP, then 4 × G + 16 = 32 drain cycles.
    Checks:   an accepted EOP appears within the 24 cycles; no
              `m_valid & m_sop` in the 32-cycle drain.
    Note:     the beats collected in `pkt` are never compared, so the
              in-flight packet's length and payload are unchecked — a
              mutant that jumps ST_DATA → ST_EOP on TestMode fall passes.
    """
    dut.TESTCASE.value = 7
    await reset(dut)
    N = n_data(dut)

    dut.cfg_test_mode.value = 1
    # Wait for an SOP to appear.
    for _ in range(10):
        await RisingEdge(dut.tx_clk)
        if int(dut.m_valid.value) and int(dut.m_sop.value):
            break
    else:
        raise AssertionError("never saw the first SOP")

    # We're now mid-SOP; race a couple of cycles into the body before
    # dropping cfg_test_mode.
    for _ in range(2):
        await RisingEdge(dut.tx_clk)
    dut.cfg_test_mode.value = 0

    # The in-flight packet must complete to EOP.
    pkt: list[WireBeat] = [sample(dut)]   # current cycle
    saw_eop = False
    for _ in range(3 + N + 5):
        await RisingEdge(dut.tx_clk)
        if int(dut.m_valid.value) and int(dut.m_ready.value):
            b = sample(dut)
            pkt.append(b)
            if b.eop:
                saw_eop = True
                break
    assert saw_eop, "in-flight packet did not finish after cfg_test_mode=0"

    # No further SOPs may follow — drain plenty of cycles.
    for _ in range(4 * n_gap(dut) + 16):
        await RisingEdge(dut.tx_clk)
        if int(dut.m_valid.value) and int(dut.m_sop.value):
            raise AssertionError(
                "new SOP fired after cfg_test_mode dropped — should be silent"
            )


# -----------------------------------------------------------------------------
# TC 8 — Random Ready Stalls
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_random_ready_stalls(dut):
    """Random back-pressure neither drops, repeats nor corrupts packet words.

    Every state advance and the `seq_q` / `data_idx_q` updates must be
    gated by the `m_valid & m_ready` handshake.

    Stimulus: `cfg_test_mode = 1` for 4 × (3 + N + G) × 3 = 276 cycles
              with `m_ready` low on each cycle with probability 0.30
              (`random.Random(0xBEEF)`); afterwards `m_ready = 1` and
              `cfg_test_mode = 0`.
    Checks:   `split_into_packets` framing holds and yields ≥ 2 complete
              packets; `check_packet_shape` passes on every one.
    Note:     a packet still in flight at the end of the window is not
              checked.
    """
    dut.TESTCASE.value = 8
    await reset(dut)
    N = n_data(dut)
    G = n_gap(dut)

    rng = random.Random(0xBEEF)
    dut.cfg_test_mode.value = 1
    # Capture ~3 packets worth of cycles with random ready bursts.
    cycles = 4 * (3 + N + G) * 3
    beats = await capture_accepted(
        dut, cycles=cycles,
        ready_pattern=lambda c: 0 if rng.random() < 0.30 else 1,
    )
    # Make sure ready is back to 1 before disabling, so the inflight
    # packet drains and we re-enter ST_IDLE cleanly.
    dut.m_ready.value = 1
    dut.cfg_test_mode.value = 0

    pkts = split_into_packets(beats)
    assert len(pkts) >= 2, (
        f"random-stall stress only produced {len(pkts)} packets — too few"
    )
    for p in pkts:
        check_packet_shape(p, data_words=N)


# -----------------------------------------------------------------------------
# TC 9 — Sequence Byte Rollover
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_seq_byte_rollover(dut):
    """Each packet starts its counting sequence from 0 again.

    Within a long body `seq` wraps at 256 (a > 64-word payload), but every
    packet must restart at seq = 0 (first data word 0x03020100).

    Stimulus: `cfg_test_mode = 1`, `m_ready = 1`, capture for
              5 × (3 + N + G) + 10 = 125 cycles, then `cfg_test_mode = 0`.
    Checks:   ≥ 4 packets; the first data word of each of the first 4 is
              0x03020100.
    Note:     despite the name, DATA_WORDS is fixed at 16 by the wrapper,
              so the payload only reaches byte 0x3F and `seq_q` never wraps
              in any unit test; this re-checks the per-packet reset already
              covered by TC 4.
    """
    dut.TESTCASE.value = 9
    await reset(dut)
    N = n_data(dut)
    G = n_gap(dut)
    dut.cfg_test_mode.value = 1
    cycles = 5 * (3 + N + G) + 10
    beats  = await capture_accepted(dut, cycles=cycles)
    dut.cfg_test_mode.value = 0

    pkts = split_into_packets(beats)
    assert len(pkts) >= 4
    for p in pkts[:4]:
        # Word 0 of body must be 0x03_02_01_00 — proves seq reset on SOP.
        first_data = p[2]   # SOP, TYPE, then DATA[0]
        assert first_data.data == 0x03020100, (
            f"packet started with {first_data.data:#010x}, "
            f"expected 0x03020100 (seq must reset on SOP)"
        )


# -----------------------------------------------------------------------------
# TC 10 — TestPacketCountTx
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_pkt_count_tx(dut):
    """TestPacketCountTx counts every transmitted packet and clears on demand.

    v1.1.1 §10.3.38: TestPacketCountTx increments per transmitted packet
    and clears on `clr_pkt_count` (host write 0 / ConnectionReset,
    §10.3.28).  This register did not exist in CoaXPress v1.0.

    Stimulus: `cfg_test_mode = 1`, `m_ready = 1` for up to 4000 cycles,
              until 4 accepted EOPs are seen; `cfg_test_mode = 0` and 40
              idle cycles; then `clr_pkt_count` high for one cycle.
    Checks:   `pkt_count == 0` right after reset; after every edge
              `pkt_count` equals the EOP tally or trails it by one (never
              ahead); 4 EOPs are reached; `pkt_count == 4` after the
              stop; `pkt_count == 0` one edge after the clear pulse.
    Note:     a clear coinciding with an EOP or landing mid-packet is not
              exercised.
    """
    dut.TESTCASE.value = 10
    await reset(dut)
    assert int(dut.pkt_count.value) == 0

    dut.cfg_test_mode.value = 1
    eops = 0
    # Run long enough for several packets + gaps to complete.  pkt_count_q
    # increments the edge AFTER the accepted EOP beat, so compare against
    # the running EOP tally one cycle late (it must never run ahead).
    for _ in range(4000):
        await RisingEdge(dut.tx_clk)
        assert int(dut.pkt_count.value) in (eops, eops - 1 if eops else 0), (
            f"TestPacketCountTx={int(dut.pkt_count.value)} ahead of EOPs={eops}"
        )
        if int(dut.m_valid.value) and int(dut.m_ready.value) and int(dut.m_eop.value):
            eops += 1
            if eops >= 4:
                break
    assert eops >= 4, "did not transmit enough test packets"

    # Stop generating, then clear (host write 0 / ConnectionReset).
    dut.cfg_test_mode.value = 0
    for _ in range(40):
        await RisingEdge(dut.tx_clk)
    held = int(dut.pkt_count.value)
    assert held == eops, f"count after stop: {held} != EOPs {eops}"

    dut.clr_pkt_count.value = 1
    await RisingEdge(dut.tx_clk)
    dut.clr_pkt_count.value = 0
    await RisingEdge(dut.tx_clk)
    assert int(dut.pkt_count.value) == 0, "TestPacketCountTx not cleared"
