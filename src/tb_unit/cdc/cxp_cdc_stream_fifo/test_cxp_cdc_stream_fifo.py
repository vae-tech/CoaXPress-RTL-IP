"""Cocotb TB for `cxp_cdc_stream_fifo`.

Asynchronous (app_clk → tx_clk) gray-pointer packet FIFO with a
{data, kmask, sop, eop} slot, valid/ready on both sides (`s_ready` =
not almost-full), an EOP counter for `m_pkt_avail` and skip-until-SOP after
a tx-side reset. The wrapper `tb_cxp_cdc_stream_fifo_top` reduces DEPTH to 64
(ALMOST_FULL_MARGIN 4, so at most 60 beats are accepted) so the almost-full
and wrap behaviour is reached in a few hundred cycles.

Default clocks are app_clk 10 ns / tx_clk 8 ns, overridden per test;
`bringup` holds both resets for 8 app_clk edges, releases them together and
waits 4 edges of each clock. `producer` offers beats and advances on an
app_clk edge with `s_ready = 1`, optionally inserting bubbles (seed 0xC0DE);
`consumer` drives `m_ready` each tx_clk cycle, optionally dropping it (seed
0xBABE), and records `m_valid & m_ready` beats. `make_packets` builds
seeded random-length packets with random data and kmask. There is no shared
checker and no FSM; each test compares against the list of beats it sent.

TC 1–7 implement verification-plan items #1–#5 of
`docs/design/cxp_camera_ip_modules.md` §2.5 (CDC integrity, almost-full
backpressure, packet boundaries, asymmetric reset, `m_pkt_avail`); TC 8–9
are extra coverage.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  CDC round trip, app_clk 4 ns / tx_clk 16 ns, consumer gaps.
  2  CDC round trip, app_clk 16 ns / tx_clk 4 ns, producer gaps.
  3  CDC round trip, app_clk 10 ns / tx_clk 11 ns, gaps on both sides.
  4  `s_ready` falls exactly at DEPTH − margin with the consumer held off.
  5  SOP / EOP framing preserved over 24 random packets (10 / 7 ns).
  6  Tx-only reset does not deadlock; first beat afterwards has `sop`.
  7  `m_pkt_avail` rises after a full packet; drain has no underrun.
  8  20 short packets back-to-back, no gaps, bit-exact.
  9  All 16 kmask patterns round-trip.
 10  `m_len` / `m_streamid` describe each packet at its SOP; no `m_pkt_avail`
     before its EOP.
 11  App-only reset after traffic: the next packet arrives alone and
     intact, `s_ready` does not stick.
 12  Tx-only reset after the pointers wrapped (slot 0 mid-packet): the
     next packet arrives alone, nothing delivered is replayed.
 13  Flush: with packets and a partial packet inside, `flush` empties both
     sides and is acknowledged; a packet in flight at the reader is not
     cut; after the release the write side resumes at the next SOP.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly, NextTimeStep, Timer

from cxp_reset import pulse_reset
from cxp_testcase import cxp_test


DEPTH               = 64
DATA_W              = 32
ALMOST_FULL_MARGIN  = 4
MAX_FILL_ALLOWED    = DEPTH - ALMOST_FULL_MARGIN  # = 60

APP_PERIOD_DEFAULT  = 10
TX_PERIOD_DEFAULT   = 8


# -----------------------------------------------------------------------------
# Beat helpers
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Beat:
    """One FIFO slot: 32-bit data, 4-bit kmask, sop and eop flags."""
    data:  int
    kmask: int
    sop:   int
    eop:   int


def make_packet(seed: int, length: int) -> list[Beat]:
    """Build one `length`-beat packet with seeded random data and kmask."""
    rng = random.Random(seed)
    pkt: list[Beat] = []
    for i in range(length):
        d  = rng.randrange(0, 1 << DATA_W)
        km = rng.randrange(0, 16)
        pkt.append(Beat(
            data  = d,
            kmask = km,
            sop   = 1 if i == 0          else 0,
            eop   = 1 if i == length - 1 else 0,
        ))
    return pkt


def make_packets(seed: int, num_packets: int,
                 min_len: int = 1, max_len: int = 8) -> list[Beat]:
    """Build `num_packets` packets of random length, flattened to beats."""
    rng = random.Random(seed)
    out: list[Beat] = []
    for p in range(num_packets):
        plen = rng.randint(min_len, max_len)
        out.extend(make_packet(seed + p * 1009, plen))
    return out


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def bringup(dut, app_period: int = APP_PERIOD_DEFAULT,
                  tx_period: int = TX_PERIOD_DEFAULT,
                  app_phase: int = 0, tx_phase: int = 0):
    """Start both clocks, wait any `*_phase` ns, then pulse both resets.

    Both clocks start at 0 ns; the `*_phase` delays only shift reset
    assertion, they do not offset one clock against the other.
    """
    cocotb.start_soon(Clock(dut.app_clk, app_period, unit="ns").start(start_high=False))
    cocotb.start_soon(Clock(dut.tx_clk,  tx_period,  unit="ns").start(start_high=False))

    if app_phase:
        await Timer(app_phase, unit="ns")
    if tx_phase:
        await Timer(tx_phase, unit="ns")

    dut.app_rst_n.value = 0
    dut.tx_rst_n.value  = 0
    dut.s_valid.value   = 0
    dut.s_data.value    = 0
    dut.s_kmask.value   = 0
    dut.s_sop.value     = 0
    dut.s_eop.value     = 0
    dut.s_streamid.value = 0
    dut.m_ready.value   = 0
    dut.m_busy.value    = 0
    dut.flush.value     = 0

    # Hold reset for a few cycles of each clock.
    for _ in range(8):
        await RisingEdge(dut.app_clk)
    dut.app_rst_n.value = 1
    dut.tx_rst_n.value  = 1
    # Both sides settle and see each other (cxp_cdc_link) before the
    # write side takes a beat: a few cycles of each clock.
    for _ in range(24):
        await RisingEdge(dut.app_clk)
    for _ in range(4):
        await RisingEdge(dut.tx_clk)


# -----------------------------------------------------------------------------
# Producer / consumer driver helpers
# -----------------------------------------------------------------------------
async def producer(dut, beats: list[Beat], rng_seed: int = 0xC0DE,
                   gap_prob: float = 0.0):
    """Push `beats` onto the app side; honour s_ready backpressure."""
    rng = random.Random(rng_seed)
    for b in beats:
        # Optional bubble cycles before this beat.
        while gap_prob > 0.0 and rng.random() < gap_prob:
            dut.s_valid.value = 0
            await RisingEdge(dut.app_clk)

        dut.s_data.value  = b.data
        dut.s_kmask.value = b.kmask
        dut.s_sop.value   = b.sop
        dut.s_eop.value   = b.eop
        dut.s_valid.value = 1
        # Wait for an edge where s_ready was asserted (transaction accepted).
        while True:
            await RisingEdge(dut.app_clk)
            if int(dut.s_ready.value) == 1:
                break
    dut.s_valid.value = 0


async def consumer(dut, n_beats: int, rng_seed: int = 0xBABE,
                   gap_prob: float = 0.0,
                   start_delay_cycles: int = 0,
                   timeout_cycles: int = 100_000) -> list[Beat]:
    """Pop `n_beats` from the tx side; return captured beats."""
    rng = random.Random(rng_seed)
    out: list[Beat] = []

    for _ in range(start_delay_cycles):
        await RisingEdge(dut.tx_clk)

    idle = 0
    while len(out) < n_beats:
        # Toggle ready randomly (or always 1).
        dut.m_ready.value = 0 if (gap_prob > 0.0 and rng.random() < gap_prob) else 1
        await RisingEdge(dut.tx_clk)
        if int(dut.m_valid.value) and int(dut.m_ready.value):
            out.append(Beat(
                data  = int(dut.m_data.value),
                kmask = int(dut.m_kmask.value),
                sop   = int(dut.m_sop.value),
                eop   = int(dut.m_eop.value),
            ))
            idle = 0
        else:
            idle += 1
            if idle > timeout_cycles:
                raise TimeoutError(
                    f"consumer: {idle} cycles without progress "
                    f"(captured {len(out)}/{n_beats})"
                )
    dut.m_ready.value = 0
    return out


# -----------------------------------------------------------------------------
# CDC round-trip helper (verification plan §2.5 #1)
# -----------------------------------------------------------------------------
async def _cdc_round_trip(dut, app_period: int, tx_period: int,
                          app_phase: int = 0, tx_phase: int = 0,
                          *, prod_gap: float = 0.0, cons_gap: float = 0.0):
    """Bring up at the given clocks, send 12 packets, compare bit-exact."""
    await bringup(dut, app_period=app_period, tx_period=tx_period,
                  app_phase=app_phase, tx_phase=tx_phase)
    beats = make_packets(seed=0x5EED, num_packets=12, min_len=1, max_len=10)

    prod_h = cocotb.start_soon(producer(dut, beats, gap_prob=prod_gap))
    cons_h = cocotb.start_soon(consumer(dut, len(beats), gap_prob=cons_gap))
    await prod_h
    got = await cons_h

    assert len(got) == len(beats), \
        f"beat count mismatch: got {len(got)}, expected {len(beats)}"
    for i, (g, e) in enumerate(zip(got, beats)):
        assert g == e, f"beat {i} mismatch: got {g}, expected {e}"


# -----------------------------------------------------------------------------
# TC 1 — CDC, App Clock Faster
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_cdc_app_faster(dut):
    """Beats cross intact when app_clk runs 4× faster than tx_clk.

    Verification plan §2.5 #1: gray-pointer crossing at 4:1 with the
    producer outpacing the consumer, so the FIFO fills to its threshold and
    `s_ready` throttles; 81 beats also wrap the 64-entry RAM.

    Stimulus: app_clk 4 ns, tx_clk 16 ns; 12 packets of 1–10 beats
              (`make_packets` seed 0x5EED, 81 beats); producer back-to-back,
              consumer drops `m_ready` with probability 0.1 (seed 0xBABE).
    Checks:   received beat count == 81 and every beat equals the sent one
              in `data`, `kmask`, `sop` and `eop`.
    """
    dut.TESTCASE.value = 1
    await _cdc_round_trip(dut, app_period=4, tx_period=16,
                          prod_gap=0.0, cons_gap=0.1)


# -----------------------------------------------------------------------------
# TC 2 — CDC, TX Clock Faster
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_cdc_tx_faster(dut):
    """Beats cross intact when tx_clk runs 4× faster than app_clk.

    Verification plan §2.5 #1: the consumer outruns the producer, so the
    FIFO runs nearly empty and the app → tx empty-flag crossing gates every
    pop.

    Stimulus: app_clk 16 ns, tx_clk 4 ns; the same 81 beats as TC 1;
              producer bubbles with probability 0.1 (seed 0xC0DE), consumer
              always ready.
    Checks:   as TC 1 — count == 81, every beat bit-exact.
    """
    dut.TESTCASE.value = 2
    await _cdc_round_trip(dut, app_period=16, tx_period=4,
                          prod_gap=0.1, cons_gap=0.0)


# -----------------------------------------------------------------------------
# TC 3 — CDC, Close Clock Ratio
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_cdc_close_ratio(dut):
    """Beats cross intact at near-equal clock periods.

    Verification plan §2.5 #1: the 10 / 11 ns ratio slowly sweeps the
    relative phase of the two clocks across the synchronisers.

    Stimulus: app_clk 10 ns, tx_clk 11 ns, `tx_phase = 1`; the same 81
              beats as TC 1; producer bubbles and consumer `m_ready` drops
              with probability 0.2 each.
    Checks:   as TC 1 — count == 81, every beat bit-exact.
    Note:     `bringup` applies `tx_phase` as a delay before reset, after
              both clocks have started at 0 ns, so there is no actual 1 ns
              clock phase offset.
    """
    dut.TESTCASE.value = 3
    await _cdc_round_trip(dut, app_period=10, tx_period=11, tx_phase=1,
                          prod_gap=0.2, cons_gap=0.2)


# -----------------------------------------------------------------------------
# TC 4 — Almost-Full Backpressure
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_almost_full_backpressure(dut):
    """`s_ready` falls exactly at DEPTH − ALMOST_FULL_MARGIN and stays low.

    Verification plan §2.5 #2: with the consumer held off the FIFO must
    stop accepting at the threshold (60 of 64) and never overflow.

    Stimulus: default clocks, `m_ready = 0` throughout; one beat per app_clk
              cycle (`sop` on the first only, no `eop`) until `s_ready` reads
              0 after an edge; then `s_valid = 0` for 8 app_clk edges.
    Checks:   aborts if more than DEPTH + 8 = 72 beats are accepted;
              `s_ready == 0` on each of the 8 idle edges; accepted beats
              == 60 (asserted as <= 60 and >= 60).
    """
    dut.TESTCASE.value = 4
    await bringup(dut)

    accepted = 0

    # Hold the consumer; drive the producer until s_ready goes low.
    dut.m_ready.value = 0
    pkt_idx = 0
    while True:
        # Construct a beat with sop on first, eop never (not relevant here).
        dut.s_data.value  = 0xA5A5_0000 | accepted
        dut.s_kmask.value = 0
        dut.s_sop.value   = 1 if pkt_idx == 0 else 0
        dut.s_eop.value   = 0
        dut.s_valid.value = 1
        await RisingEdge(dut.app_clk)
        if int(dut.s_ready.value) == 1:
            accepted += 1
            pkt_idx  += 1
        # Stop once we've watched ready stay low for several cycles.
        if int(dut.s_ready.value) == 0:
            break
        if accepted > DEPTH + 8:
            raise AssertionError("s_ready never deasserted (overflow risk)")

    # Confirm s_ready stays low for a few more cycles (no glitch back high).
    dut.s_valid.value = 0
    for _ in range(8):
        await RisingEdge(dut.app_clk)
        assert int(dut.s_ready.value) == 0, \
            "s_ready glitched high while FIFO is almost full"

    # Exactly MAX_FILL_ALLOWED beats must have been accepted before the
    # deassertion.  (FIFO holds at most DEPTH-ALMOST_FULL_MARGIN beats
    # while s_ready is high.)
    assert accepted <= MAX_FILL_ALLOWED, (
        f"s_ready stayed high past the almost-full threshold: "
        f"accepted {accepted}, expected <= {MAX_FILL_ALLOWED}"
    )
    assert accepted >= MAX_FILL_ALLOWED, (
        f"s_ready deasserted too early: accepted {accepted}, "
        f"expected exactly {MAX_FILL_ALLOWED}"
    )


# -----------------------------------------------------------------------------
# TC 5 — Packet Boundaries
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_packet_boundaries(dut):
    """SOP / EOP markers travel with their slot across the clock crossing.

    Verification plan §2.5 #3; 154 beats also wrap the full pointer
    (> 128 = 2^PTR_W).

    Stimulus: app_clk 10 ns, tx_clk 7 ns; 24 packets of 1–12 beats
              (seed 0xBEEF, 154 beats); producer bubbles and consumer
              `m_ready` drops with probability 0.15.
    Checks:   EOP count out == EOP count in; SOP count out == EOP count in;
              every packet begins with `sop`; the stream does not end
              mid-packet.
    Note:     data and kmask are not compared.
    """
    dut.TESTCASE.value = 5
    await bringup(dut, app_period=10, tx_period=7)
    beats = make_packets(seed=0xBEEF, num_packets=24, min_len=1, max_len=12)
    eop_in = sum(b.eop for b in beats)

    prod_h = cocotb.start_soon(producer(dut, beats, gap_prob=0.15))
    cons_h = cocotb.start_soon(consumer(dut, len(beats), gap_prob=0.15))
    await prod_h
    got = await cons_h

    eop_out = sum(b.eop for b in got)
    sop_out = sum(b.sop for b in got)
    assert eop_in == eop_out, (
        f"eop count mismatch: in={eop_in} out={eop_out}"
    )
    assert sop_out == eop_in, (
        f"sop count mismatch: out_sop={sop_out} expected={eop_in}"
    )
    # Each packet must start with sop and end with eop, with no boundary
    # markers in between.
    in_packet = False
    for i, b in enumerate(got):
        if not in_packet:
            assert b.sop == 1, f"beat {i}: missing sop at packet start"
            in_packet = True
        if b.eop:
            in_packet = False
    assert not in_packet, "stream ended mid-packet"


# -----------------------------------------------------------------------------
# TC 6 — TX-Only Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_tx_only_reset(dut):
    """A tx-only reset does not deadlock and output restarts on an SOP beat.

    Verification plan §2.5 #4 (asymmetric reset): `tx_rst_n` sets the
    skip-until-SOP flag so the reader must resume on a packet boundary.

    Stimulus: default clocks. Phase 1: 2 packets (seed 0x1111, 7 beats)
              pushed and drained. Phase 2: 3 packets (seed 0x2222, 6 beats)
              pushed with the consumer idle; after 40 app_clk edges
              `tx_rst_n = 0` for 8 tx_clk edges, then 4 idle edges.
              Phase 3: wait for the producer, push a 4-beat packet (seed
              0x3333) and drain with `m_ready = 1`.
    Checks:   phase 1 output starts with `sop` and ends with `eop`; after
              the reset some beat is emitted, the first one has `sop == 1`,
              and at least one EOP is seen within 4000 tx_clk cycles. The
              final drain loop asserts nothing.
    Note:     does not prove skip-until-SOP — the read pointer restarts at
              slot 0 and replays the already-delivered phase-1 packet, whose
              first beat is an SOP; per docs/design/modules/cdc/cxp_cdc_stream_fifo.md a mutant
              with `skip_q` forced to 0 still passes.
    """
    dut.TESTCASE.value = 6
    await bringup(dut)

    # Phase 1 — push two complete packets and drain them.
    beats0 = make_packets(seed=0x1111, num_packets=2, min_len=3, max_len=5)
    prod_h = cocotb.start_soon(producer(dut, beats0))
    cons_h = cocotb.start_soon(consumer(dut, len(beats0)))
    await prod_h
    pre = await cons_h
    assert pre[0].sop == 1 and pre[-1].eop == 1

    # Phase 2 — push more packets but do not drain.  Now tx-only reset.
    beats1 = make_packets(seed=0x2222, num_packets=3, min_len=2, max_len=6)
    prod1_h = cocotb.start_soon(producer(dut, beats1))
    # Wait until at least a few beats are inside the FIFO.
    for _ in range(40):
        await RisingEdge(dut.app_clk)

    dut.tx_rst_n.value = 0
    for _ in range(8):
        await RisingEdge(dut.tx_clk)
    dut.tx_rst_n.value = 1
    for _ in range(4):
        await RisingEdge(dut.tx_clk)

    # Phase 3 — let the producer complete then push one more clean packet.
    await prod1_h
    beats2 = make_packet(seed=0x3333, length=4)
    prod2_h = cocotb.start_soon(producer(dut, beats2))

    # Drain.  We do not know the exact post-reset content (skip-until-SOP
    # may re-emit stale packets that survived in FIFO RAM), but the very
    # first beat we see must be sop=1, and we must eventually see EOPs
    # without deadlock.
    dut.m_ready.value = 1
    first_beat = None
    eops_seen  = 0
    timeout    = 4000
    cycle      = 0
    while eops_seen < 1 and cycle < timeout:
        await RisingEdge(dut.tx_clk)
        cycle += 1
        if int(dut.m_valid.value):
            if first_beat is None:
                first_beat = Beat(
                    data  = int(dut.m_data.value),
                    kmask = int(dut.m_kmask.value),
                    sop   = int(dut.m_sop.value),
                    eop   = int(dut.m_eop.value),
                )
            if int(dut.m_eop.value):
                eops_seen += 1
    assert first_beat is not None, "no data emitted after tx reset"
    assert first_beat.sop == 1, \
        f"first emitted beat after reset has sop=0 (got {first_beat})"
    assert eops_seen >= 1, "FIFO did not drain a complete packet after reset"

    # Drain everything pending; producer has completed beats2 by now.
    await prod2_h
    # Pull a generous number of beats with a timeout so the FIFO empties.
    end_cycle = 0
    while end_cycle < 500:
        await RisingEdge(dut.tx_clk)
        end_cycle += 1
        if int(dut.m_valid.value) == 0:
            break


# -----------------------------------------------------------------------------
# TC 7 — Packet Available
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_pkt_avail(dut):
    """`m_pkt_avail` rises for a stored packet, which then drains gap-free.

    Verification plan §2.5 #5: the EOP counter crosses to tx_clk so a
    consumer can wait for a whole packet and then send it without underrun.

    Stimulus: app_clk 8 ns, tx_clk 10 ns; one 12-beat packet (seed 0x77)
              written with `m_ready = 0`; poll `m_pkt_avail` for up to 20
              tx_clk edges; then `m_ready = 1` for up to 120 edges until the
              `eop` beat.
    Checks:   `m_pkt_avail` seen high; `m_valid` never drops after the first
              valid beat; 12 beats received and equal to the packet.
    Note:     never checks that `m_pkt_avail` is 0 before the EOP is
              written, so a stuck-at-1 output still passes.
    """
    dut.TESTCASE.value = 7
    await bringup(dut, app_period=8, tx_period=10)

    pkt_len = 12
    beats   = make_packet(seed=0x77, length=pkt_len)

    # Hold the consumer off until the whole packet has been written.
    dut.m_ready.value = 0
    prod_h = cocotb.start_soon(producer(dut, beats))
    await prod_h

    # Wait for the EOP-counter sync (≤ ~6 tx clocks).
    saw = False
    for _ in range(20):
        await RisingEdge(dut.tx_clk)
        if int(dut.m_pkt_avail.value):
            saw = True
            break
    assert saw, "m_pkt_avail never asserted after a full packet was written"

    # Now drain back-to-back at full rate — m_valid must stay high for the
    # entire packet (no mid-packet underrun).
    dut.m_ready.value = 1
    got: list[Beat] = []
    underran = False
    saw_first_valid = False
    for _ in range(10 * pkt_len):
        await RisingEdge(dut.tx_clk)
        if int(dut.m_valid.value):
            saw_first_valid = True
            got.append(Beat(
                data  = int(dut.m_data.value),
                kmask = int(dut.m_kmask.value),
                sop   = int(dut.m_sop.value),
                eop   = int(dut.m_eop.value),
            ))
            if got[-1].eop:
                break
        elif saw_first_valid:
            underran = True
            break
    assert not underran, \
        "m_valid dropped mid-packet — m_pkt_avail allowed an underrun"
    assert len(got) == pkt_len, \
        f"drained {len(got)} beats, expected {pkt_len}"
    assert got == beats, "data mismatch after pkt_avail draining"


# -----------------------------------------------------------------------------
# TC 8 — Back-to-Back Packets
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_back_to_back_packets(dut):
    """Many short packets flow at one beat per cycle without loss.

    Stimulus: default clocks; 20 packets of 1–4 beats (seed 0xABCD, 52
              beats); no producer bubbles, consumer always ready.
    Checks:   the received beat list equals the sent list (data, kmask,
              sop, eop).
    """
    dut.TESTCASE.value = 8
    await bringup(dut)
    beats = make_packets(seed=0xABCD, num_packets=20, min_len=1, max_len=4)
    prod_h = cocotb.start_soon(producer(dut, beats))
    cons_h = cocotb.start_soon(consumer(dut, len(beats)))
    await prod_h
    got = await cons_h
    assert got == beats, "back-to-back packet round-trip mismatch"


# -----------------------------------------------------------------------------
# TC 9 — Kmask Round-Trip
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_kmask_roundtrip(dut):
    """All four kmask bits are stored per slot.

    Stimulus: default clocks; 16 single-beat packets (`sop = eop = 1`) with
              `kmask = k` and `data = 0xDEAD0000 | k` for k = 0..15.
    Checks:   each received `kmask` equals the sent one.
    Note:     data, `sop` and `eop` are not compared.
    """
    dut.TESTCASE.value = 9
    await bringup(dut)
    beats = []
    for km in range(16):
        beats.append(Beat(data=0xDEAD0000 | km, kmask=km,
                          sop=1, eop=1))
    prod_h = cocotb.start_soon(producer(dut, beats))
    cons_h = cocotb.start_soon(consumer(dut, len(beats)))
    await prod_h
    got = await cons_h
    for i, (g, e) in enumerate(zip(got, beats)):
        assert g.kmask == e.kmask, \
            f"beat {i}: kmask {g.kmask:04b} vs expected {e.kmask:04b}"


# -----------------------------------------------------------------------------
# TC 10 — Packet Length at the Head
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_len_at_head(dut):
    """`m_len` / `m_streamid` describe the head packet; `m_pkt_avail` waits.

    cxp_tx_stream_pkt takes DsizeP from `m_len` and the StreamID from
    `m_streamid` and starts only on `m_pkt_avail` (store and forward), so
    all three must be right before the first word of a packet leaves; the
    StreamID is the one presented with the packet's SOP.

    Stimulus: app_clk 8 ns, tx_clk 10 ns, `m_ready` = 0; 4 of 6 beats of a
              packet, 20 tx_clk edges; the last 2 beats, then packets of 1, 5
              and 3 beats; then `m_ready` = 1 until 15 beats are out.
              StreamIDs 0x11, 0x22, 0x33, 0x44 set with each packet's SOP
              (and changed to 0xEE after the first packet's SOP).
    Checks:   `m_pkt_avail` = 0 while the first packet lacks its EOP and 1
              after; at every `sop` beat taken, `m_len` / `m_streamid` equal
              that packet's (6, 1, 5, 3 / 0x11 .. 0x44); the beats equal
              those sent.
    """
    dut.TESTCASE.value = 10
    await bringup(dut, app_period=8, tx_period=10)
    lens = [6, 1, 5, 3]
    pkts = [make_packet(seed=0x100 + i, length=n) for i, n in enumerate(lens)]
    sids = [0x11, 0x22, 0x33, 0x44]
    dut.m_ready.value = 0

    dut.s_streamid.value = sids[0]
    await producer(dut, pkts[0][:1])
    dut.s_streamid.value = 0xEE          # changes after the SOP: not taken
    await producer(dut, pkts[0][1:4])
    for _ in range(20):
        await RisingEdge(dut.tx_clk)
        assert int(dut.m_pkt_avail.value) == 0, "m_pkt_avail before the packet's EOP"
    await producer(dut, pkts[0][4:])
    for sid, p in zip(sids[1:], pkts[1:]):
        dut.s_streamid.value = sid
        await producer(dut, p)
    for _ in range(20):
        await RisingEdge(dut.tx_clk)
    assert int(dut.m_pkt_avail.value) == 1, "m_pkt_avail low with whole packets stored"

    got: list[Beat] = []
    seen_len: list[int] = []
    seen_sid: list[int] = []
    dut.m_ready.value = 1
    for _ in range(200):
        await RisingEdge(dut.tx_clk)
        if int(dut.m_valid.value):
            b = Beat(data=int(dut.m_data.value), kmask=int(dut.m_kmask.value),
                     sop=int(dut.m_sop.value), eop=int(dut.m_eop.value))
            if b.sop:
                seen_len.append(int(dut.m_len.value))
                seen_sid.append(int(dut.m_streamid.value))
            got.append(b)
            if len(got) == sum(lens):
                break
    assert seen_len == lens, f"m_len at the SOPs {seen_len}, expected {lens}"
    assert seen_sid == sids, f"m_streamid at the SOPs {seen_sid}, expected {sids}"
    assert got == [b for p in pkts for b in p], "data mismatch"


# -----------------------------------------------------------------------------
# One-sided reset helpers
# -----------------------------------------------------------------------------
async def drain_all(dut, cycles: int = 400) -> list[Beat]:
    """Hold m_ready = 1 for `cycles` tx_clk edges; return every beat."""
    out: list[Beat] = []
    dut.m_ready.value = 1
    for _ in range(cycles):
        await RisingEdge(dut.tx_clk)
        if int(dut.m_valid.value) and int(dut.m_ready.value):
            out.append(Beat(int(dut.m_data.value), int(dut.m_kmask.value),
                            int(dut.m_sop.value), int(dut.m_eop.value)))
    dut.m_ready.value = 0
    return out


# -----------------------------------------------------------------------------
# TC 11 — App-Only Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_app_only_reset(dut):
    """An app-only reset loses nothing it should keep and replays nothing.

    Stimulus: 7 beats pushed and drained; `app_rst_n` alone for 4 app_clk
              cycles; 60 cycles; one fresh 5-beat packet pushed; drain 400
              tx_clk cycles.
    Checks:   exactly the fresh packet is received, beat for beat.
    Note:     the write pointer returns to 0 while the read pointer is at
              7: the fill goes negative and `s_ready` sticks low, or stale
              slots are read.
    """
    dut.TESTCASE.value = 11
    await bringup(dut)
    first = make_packets(seed=0x7777, num_packets=2, min_len=3, max_len=4)[:7]
    first[-1] = Beat(first[-1].data, first[-1].kmask, first[-1].sop, 1)
    prod = cocotb.start_soon(producer(dut, first))
    await consumer(dut, len(first))
    await prod
    await pulse_reset(dut.app_rst_n, dut.app_clk, cycles=4)
    for _ in range(60):
        await RisingEdge(dut.app_clk)
    fresh = make_packet(0x8888, 5)
    prod = cocotb.start_soon(producer(dut, fresh))
    got = await drain_all(dut)
    prod.cancel()
    assert got == fresh, f"got {len(got)} beats: {got[:3]} ..."


# -----------------------------------------------------------------------------
# TC 12 — Tx-Only Reset After The Pointers Wrapped
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_tx_reset_after_wrap(dut):
    """A tx-only reset with slot 0 mid-packet replays nothing.

    Stimulus: 14 five-beat packets (70 beats) pushed and drained, so the
              pointers wrapped and slot 0 holds the middle of a packet;
              `tx_rst_n` alone for 4 tx_clk cycles; 60 cycles; one fresh
              5-beat packet; drain 400 tx_clk cycles.
    Checks:   exactly the fresh packet is received; its first beat has
              `sop`.
    """
    dut.TESTCASE.value = 12
    await bringup(dut)
    beats = []
    for p in range(14):
        beats += make_packet(16384 + p, 5)     # RNG seed, not an address
    prod = cocotb.start_soon(producer(dut, beats))
    await consumer(dut, len(beats))
    await prod
    await pulse_reset(dut.tx_rst_n, dut.tx_clk, cycles=4)
    for _ in range(60):
        await RisingEdge(dut.tx_clk)
    fresh = make_packet(0x9999, 5)
    prod = cocotb.start_soon(producer(dut, fresh))
    got = await drain_all(dut)
    prod.cancel()
    assert got == fresh, f"got {len(got)} beats, first {got[:2]}"


# -----------------------------------------------------------------------------
# TC 13 — Flush
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_flush(dut):
    """A flush empties both sides without cutting the packet being read.

    Stimulus: two 5-beat packets and 3 beats of a third pushed, consumer
              held; the reader takes 2 beats of the first packet with
              `m_busy` = 1 (mid-packet); `flush` = 1; the rest of the
              first packet is read; `m_busy` = 0; wait for `flush_ack`;
              the producer offers the last 2 beats of the third packet
              (dropped) while `s_flush` is high; `flush` = 0; a fresh
              4-beat packet (first offered with `sop` = 0 beats, which the
              write side must also drop until its SOP).
    Checks:   the first packet comes out whole; `flush_ack` rises; after
              it nothing but the fresh packet comes out.
    """
    dut.TESTCASE.value = 13
    await bringup(dut)
    p1, p2, p3 = make_packet(0xA1, 5), make_packet(0xA2, 5), make_packet(0xA3, 5)
    await producer(dut, p1 + p2 + p3[:3])
    got = await consumer(dut, 2)
    dut.m_busy.value = 1
    dut.flush.value = 1
    got += await consumer(dut, 3)
    dut.m_busy.value = 0
    for _ in range(100):
        await RisingEdge(dut.tx_clk)
        if int(dut.flush_ack.value):
            break
    assert int(dut.flush_ack.value) == 1, "no flush_ack"
    assert int(dut.s_flush.value) == 1
    await producer(dut, p3[3:])
    dut.flush.value = 0
    for _ in range(20):
        await RisingEdge(dut.app_clk)
    fresh = make_packet(0xA4, 4)
    stray = [Beat(0x1234, 0, 0, 0)]
    prod = cocotb.start_soon(producer(dut, stray + fresh))
    after = await drain_all(dut)
    prod.cancel()
    assert got == p1, "the packet being read was cut"
    assert after == fresh, f"after the flush: {len(after)} beats, first {after[:2]}"
