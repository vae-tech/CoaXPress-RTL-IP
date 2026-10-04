"""Cocotb TB for the transmit scheduler: `cxp_tx_arbiter` + `cxp_tx_inserter`.

The arbiter picks between the three long-packet sources — ack > linktest >
stream (Table 13 priority 2) — one packet at a time, never pre-empting.
The inserter puts one word per `tx_clk` on the wire: the second word of a
two-word packet, else a trigger (priority 0), else an I/O-ack (1), else an
IDLE once 95 words have gone since the last one, else the arbiter's long
word, else IDLE.  A source's `*_ready` pulses in the cycle its beat is
chosen; `m_data` / `m_kmask` / `idle_seen` are that cycle's choice (the
registered wire word, `wire_data`, follows one cycle later).

The TB drives all five source bundles through the
`tb_cxp_tx_arbiter_top` wrapper (8 ns clock).  A `SourceDriver` per source
presents its queue head and pops it when it sees `*_ready`; `tick()`
drives, waits for the edge and samples.  There is no scoreboard: each test
checks its own capture, and most drop IDLE samples (`idle_seen = 1`)
before comparing.  FSM coverage is collected on the arbiter's `owner_q`
(4 states, 6 arcs) and the inserter's `short_q` (3 states, 4 arcs).

Spec (CXP-001-2015 v1.1.1): §8.2.4 / Table 13 priority and word-boundary
insertion of triggers and I/O acknowledgments, §8.2.5.1 IDLE at least
every 100 words, §8.2.5.2 HS stretching with IDLE.  TestMode does not
reach the scheduler (the stream source holds new packets itself).

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Reset → IDLE words only.
  2  Single ack packet passes through.
  3  Single stream packet passes through.
  4  Priority: ack before stream when both offer SOP.
  5  Priority: trigger before ack when both offer SOP.
  6  Trigger inserted into a running stream packet; stream resumes.
  7  Trigger inserted into a running ack packet; ack resumes.
  8  No mid-packet interleave: stream SOP waits for ack EOP.
  9  A 300-word packet gets an IDLE after every 95 words, no beat lost.
 10  Source stall mid-packet → IDLE fill, packet resumes (§8.2.5.2).
 11  IDLE between packets when no SOP is offered.
 12  Single-beat packets (SOP+EOP same cycle), ack before stream.
 13  kmask passes through unchanged.
 14  Random stalls with ack + stream: no interleave, no loss.
 15  Priority: I/O-ack before ctrl-ack when both offer SOP.
 16  Priority: trigger before I/O-ack when both offer SOP.
 17  An I/O-ack is inserted into a running ack packet within 3 words.
 18  A trigger offered on an I/O-ack's first word waits for its second.
 22  Single link-test packet passes through.
 23  Trigger inserted into a running link-test packet; link-test resumes.
 25  Trigger, I/O-ack and both offered at every run position 85..104 of a
     long packet: two-word packets whole, at most 99 words between IDLEs,
     the trigger never waits, the I/O-ack at most 3 words.
 26  The wire word is the chosen word of the cycle before.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from cxp_testcase import cxp_test
from fsm_coverage import register_fsm


TX_PERIOD_NS = 8

# Canonical CoaXPress IDLE word — K28.5 K28.1 K28.1 D21.5 in P0..P3.
K28_5      = 0xBC
K28_1      = 0x3C
D21_5      = 0xB5
IDLE_DATA  = (D21_5 << 24) | (K28_1 << 16) | (K28_1 << 8) | K28_5
IDLE_KMASK = 0b0111


# -----------------------------------------------------------------------------
# FSM coverage
# -----------------------------------------------------------------------------
register_fsm(
    name="tx_arbiter",
    states=["S_NONE", "S_ACK", "S_LINKTEST", "S_STREAM"],
    state_path="cxp_tx_arbiter_i.owner_q",
    clk_path="tx_clk",
    arcs=[
        ("S_NONE", "S_ACK"), ("S_NONE", "S_LINKTEST"), ("S_NONE", "S_STREAM"),
        ("S_ACK", "S_NONE"), ("S_LINKTEST", "S_NONE"), ("S_STREAM", "S_NONE"),
    ],
)

register_fsm(
    name="tx_inserter",
    states=["SH_NONE", "SH_TRIG", "SH_IOACK"],
    state_path="cxp_tx_inserter_i.short_q",
    clk_path="tx_clk",
    arcs=[
        ("SH_NONE", "SH_TRIG"), ("SH_TRIG", "SH_NONE"),
        ("SH_NONE", "SH_IOACK"), ("SH_IOACK", "SH_NONE"),
    ],
)



# -----------------------------------------------------------------------------
# Beats and wire samples
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class SrcBeat:
    """One beat driven onto a source bundle (or expected on the wire)."""
    data:  int = 0
    kmask: int = 0
    valid: int = 0
    sop:   int = 0
    eop:   int = 0


def idle_beat() -> SrcBeat:
    """Return an all-zero beat with `valid = 0` (source idle)."""
    return SrcBeat(data=0, kmask=0, valid=0, sop=0, eop=0)


def packet_beats(start_data: int, length: int,
                 kmask_data: int = 0,
                 sop_kmask: int = 0xF,
                 eop_kmask: int = 0xF) -> list[SrcBeat]:
    """Build a `length`-beat packet with data `start_data + i`.

    Beat 0 is SOP with `sop_kmask`, beats 1..N-2 use `kmask_data`, beat N-1
    is EOP with `eop_kmask`.  A single-beat packet (length 1) has both SOP
    and EOP set and kmask `sop_kmask | eop_kmask`.
    """
    if length == 1:
        return [SrcBeat(data=start_data, kmask=sop_kmask | eop_kmask,
                        valid=1, sop=1, eop=1)]
    out: list[SrcBeat] = []
    for i in range(length):
        sop = 1 if i == 0          else 0
        eop = 1 if i == length - 1 else 0
        km  = sop_kmask if sop else (eop_kmask if eop else kmask_data)
        out.append(SrcBeat(data=start_data + i, kmask=km, valid=1, sop=sop, eop=eop))
    return out


@dataclass(frozen=True)
class WireSample:
    """What the arbiter emits in one cycle: wire word plus every ready."""
    data:           int
    kmask:          int
    idle_seen:      int
    trig_ready:     int
    ioack_ready:    int
    ack_ready:      int
    linktest_ready: int
    stream_ready:   int


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def bringup(dut):
    """Start the clock, reset 6 edges, idle every input, settle 2 edges."""
    cocotb.start_soon(Clock(dut.tx_clk, TX_PERIOD_NS, unit="ns").start(start_high=False))
    dut.tx_rst_n.value = 0
    # Every source idle unless a test drives it.
    _drive_source(dut, "trig",     idle_beat())
    _drive_source(dut, "ioack",    idle_beat())
    _drive_source(dut, "ack",      idle_beat())
    _drive_source(dut, "linktest", idle_beat())
    _drive_source(dut, "stream",   idle_beat())
    for _ in range(6):
        await RisingEdge(dut.tx_clk)
    dut.tx_rst_n.value = 1
    for _ in range(2):
        await RisingEdge(dut.tx_clk)


# -----------------------------------------------------------------------------
# Drivers and monitors
# -----------------------------------------------------------------------------
def _drive_source(dut, name: str, b: SrcBeat):
    """Drive beat `b` onto the `p_<name>_*` source bundle."""
    getattr(dut, f"p_{name}_data").value  = b.data
    getattr(dut, f"p_{name}_kmask").value = b.kmask
    getattr(dut, f"p_{name}_valid").value = b.valid
    getattr(dut, f"p_{name}_sop").value   = b.sop
    getattr(dut, f"p_{name}_eop").value   = b.eop


def _sample(dut) -> WireSample:
    """Sample the wire word, `idle_seen` and all five readies."""
    return WireSample(
        data           = int(dut.m_data.value),
        kmask          = int(dut.m_kmask.value),
        idle_seen      = int(dut.idle_seen.value),
        trig_ready     = int(dut.p_trig_ready.value),
        ioack_ready    = int(dut.p_ioack_ready.value),
        ack_ready      = int(dut.p_ack_ready.value),
        linktest_ready = int(dut.p_linktest_ready.value),
        stream_ready   = int(dut.p_stream_ready.value),
    )


def is_idle_sample(w: WireSample) -> bool:
    """True if `w` is the IDLE word with `idle_seen = 1` and no ready high."""
    return w.data == IDLE_DATA and w.kmask == IDLE_KMASK \
           and w.idle_seen == 1 and w.trig_ready == 0 \
           and w.ioack_ready == 0 and w.linktest_ready == 0 \
           and w.ack_ready == 0 and w.stream_ready == 0


class SourceDriver:
    """Beat queue for one source: presents the head, pops it on `*_ready`."""

    def __init__(self, dut, name: str):
        self.dut    = dut
        self.name   = name              # "trig", "ioack", "ack", ...
        self.queue: list[SrcBeat] = []

    def push_packet(self, beats: list[SrcBeat]):
        """Append a packet's beats to the queue."""
        self.queue.extend(beats)

    def head(self) -> SrcBeat:
        """Return the head-of-queue beat, or an idle beat if empty."""
        return self.queue[0] if self.queue else idle_beat()

    def drive(self):
        """Present the head beat on the source bundle."""
        _drive_source(self.dut, self.name, self.head())

    def advance_if_handshook(self):
        """Pop the head beat if the source's `*_ready` is high."""
        ready = int(getattr(self.dut, f"p_{self.name}_ready").value)
        if self.queue and ready and self.head().valid:
            self.queue.pop(0)


async def tick(dut, drivers: list[SourceDriver]) -> WireSample:
    """Drive all sources, advance one edge, pop and sample.

    Returns the post-edge sample (the cycle that just completed).
    """
    for d in drivers:
        d.drive()
    await RisingEdge(dut.tx_clk)
    for d in drivers:
        d.advance_if_handshook()
    return _sample(dut)


# -----------------------------------------------------------------------------
# TC 1 — Reset Emits IDLE
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_reset_emits_idle(dut):
    """With no source offering a SOP the scheduler emits only IDLE words.

    Nothing to send selects IDLE — the §8.2.5 filler the encoder needs
    between packets.

    Stimulus: reset, then 8 cycles with every source idle.
    Checks:   every sample is the IDLE word 0xB53C3CBC / kmask 0b0111 with
              `idle_seen = 1` and all five `*_ready = 0` (`is_idle_sample`).
    """
    dut.TESTCASE.value = 1
    await bringup(dut)
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    for _ in range(8):
        s = await tick(dut, [drv_ack, drv_linktest, drv_stream])
        assert is_idle_sample(s), (
            f"post-reset cycle was not an IDLE word: {s}"
        )


# -----------------------------------------------------------------------------
# TC 2 — Ack Packet Pass-Through
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_ack_packet_passthrough(dut):
    """A lone ctrl-ack packet is forwarded word for word.

    Covers `S_NONE → S_ACK` on the SOP and `S_ACK → S_NONE` on the EOP.

    Stimulus: one 5-beat ack packet, data 0xAA000000 + i, SOP/EOP kmask
              0xF, body kmask 0; 9 cycles.
    Checks:   exactly 5 non-IDLE samples; each has `ack_ready = 1`,
              `stream_ready = 0`, `idle_seen = 0`, and data and kmask equal
              to the driven beat, in order.
    """
    dut.TESTCASE.value = 2
    await bringup(dut)
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    pkt = packet_beats(start_data=0xAA00_0000, length=5,
                       sop_kmask=0xF, eop_kmask=0xF, kmask_data=0x0)
    drv_ack.push_packet(pkt)

    captured: list[WireSample] = []
    # length + 1 cycle of pre-roll (first cycle from reset is idle while
    # FSM still sees the SOP on the inputs we just drove).
    for _ in range(len(pkt) + 4):
        captured.append(await tick(dut, [drv_ack, drv_linktest, drv_stream]))

    # Drop leading idle cycles (FSM has 1-cycle latency: first beat
    # acked the cycle AFTER reset).  Then verify the next len(pkt)
    # cycles emit the packet, in order.
    pkt_samples = [s for s in captured if s.idle_seen == 0]
    assert len(pkt_samples) == len(pkt), (
        f"expected {len(pkt)} packet beats, got {len(pkt_samples)}: {pkt_samples}"
    )
    for i, (got, exp) in enumerate(zip(pkt_samples, pkt)):
        assert got.ack_ready == 1, f"beat {i}: ack_ready not asserted: {got}"
        assert got.stream_ready == 0, f"beat {i}: stream_ready leaked: {got}"
        assert got.idle_seen == 0, f"beat {i}: idle_seen set on a packet beat"
        assert got.data == exp.data, (
            f"beat {i} data: got {got.data:#010x}, expected {exp.data:#010x}"
        )
        assert got.kmask == exp.kmask, (
            f"beat {i} kmask: got {got.kmask:#x}, expected {exp.kmask:#x}"
        )


# -----------------------------------------------------------------------------
# TC 3 — Stream Packet Pass-Through
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_stream_packet_passthrough(dut):
    """A lone stream packet is forwarded word for word.

    Covers `S_NONE → S_STREAM → S_NONE` for the lowest-priority slot.

    Stimulus: one 6-beat stream packet, data 0x55000000 + i, SOP/EOP kmask
              0xF, body kmask 0; 10 cycles.
    Checks:   exactly 6 non-IDLE samples; each has `stream_ready = 1`,
              `ack_ready = 0`, `idle_seen = 0`, and data and kmask equal to
              the driven beat, in order.
    """
    dut.TESTCASE.value = 3
    await bringup(dut)
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    pkt = packet_beats(start_data=0x5500_0000, length=6,
                       sop_kmask=0xF, eop_kmask=0xF, kmask_data=0x0)
    drv_stream.push_packet(pkt)

    captured: list[WireSample] = []
    for _ in range(len(pkt) + 4):
        captured.append(await tick(dut, [drv_ack, drv_linktest, drv_stream]))

    pkt_samples = [s for s in captured if s.idle_seen == 0]
    assert len(pkt_samples) == len(pkt)
    for i, (got, exp) in enumerate(zip(pkt_samples, pkt)):
        assert got.stream_ready == 1
        assert got.ack_ready    == 0
        assert got.idle_seen    == 0
        assert got.data  == exp.data
        assert got.kmask == exp.kmask


# -----------------------------------------------------------------------------
# TC 4 — Priority: Ack Over Stream
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_priority_ack_over_stream(dut):
    """Ack wins over stream when both offer a SOP in the same cycle.

    Within Table 13 priority 2 this build ranks ctrl-ack > linktest >
    stream at the `S_NONE` choice.

    Stimulus: a 3-beat ack (0xA1A10000 + i) and a 4-beat stream
              (0xC0DE0000 + i) packet, both queued before the first cycle;
              13 cycles.
    Checks:   exactly 7 non-IDLE samples; the first 3 have `ack_ready = 1`
              and the ack data/kmask, the next 4 `stream_ready = 1` and the
              stream data/kmask.
    Note:     the gap between the packets is not checked (IDLE samples are
              dropped), and the other source's ready is not checked 0.
    """
    dut.TESTCASE.value = 4
    await bringup(dut)
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    ack_pkt    = packet_beats(start_data=0xA1A1_0000, length=3,
                              sop_kmask=0xF, eop_kmask=0xF)
    stream_pkt = packet_beats(start_data=0xC0DE_0000, length=4,
                              sop_kmask=0xF, eop_kmask=0xF)
    drv_ack.push_packet(ack_pkt)
    drv_stream.push_packet(stream_pkt)

    captured: list[WireSample] = []
    for _ in range(len(ack_pkt) + len(stream_pkt) + 6):
        captured.append(await tick(dut, [drv_ack, drv_linktest, drv_stream]))

    pkt_samples = [s for s in captured if s.idle_seen == 0]
    assert len(pkt_samples) == len(ack_pkt) + len(stream_pkt), (
        f"expected {len(ack_pkt)+len(stream_pkt)} beats, got {len(pkt_samples)}"
    )

    # Ack packet comes first.
    for i, exp in enumerate(ack_pkt):
        got = pkt_samples[i]
        assert got.ack_ready == 1, f"ack beat {i}: ack_ready=0"
        assert got.data == exp.data and got.kmask == exp.kmask

    # Then the stream packet.
    for i, exp in enumerate(stream_pkt):
        got = pkt_samples[len(ack_pkt) + i]
        assert got.stream_ready == 1, f"stream beat {i}: stream_ready=0"
        assert got.data == exp.data and got.kmask == exp.kmask


# -----------------------------------------------------------------------------
# TC 5 — Priority: Trigger Over Ack
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_priority_trig_over_ack(dut):
    """Trigger wins over ack when both offer a SOP in the same cycle.

    §8.2.4 / Table 13: the trigger is priority 0: the inserter sends it
    (`SH_NONE → SH_TRIG → SH_NONE`) before the arbiter's ack SOP.

    Stimulus: a 2-beat trig (0x9C9C9C9C + i, SOP kmask 0xF, EOP kmask 0)
              and a 3-beat ack (0xA1A10000 + i), both queued before the
              first cycle; 11 cycles.
    Checks:   exactly 5 non-IDLE samples; the 2 trig beats have
              `trig_ready = 1`, `ack_ready = 0` and matching data/kmask;
              then the 3 ack beats have `ack_ready = 1`, `trig_ready = 0`
              and matching data/kmask.
    """
    dut.TESTCASE.value = 5
    await bringup(dut)
    drv_trig     = SourceDriver(dut, "trig")
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    trig_pkt = packet_beats(start_data=0x9C9C_9C9C, length=2,
                            sop_kmask=0xF, eop_kmask=0x0)
    ack_pkt  = packet_beats(start_data=0xA1A1_0000, length=3,
                            sop_kmask=0xF, eop_kmask=0xF)
    drv_trig.push_packet(trig_pkt)
    drv_ack.push_packet(ack_pkt)

    captured: list[WireSample] = []
    for _ in range(len(trig_pkt) + len(ack_pkt) + 6):
        captured.append(await tick(dut,
            [drv_trig, drv_ack, drv_linktest, drv_stream]))

    pkt_samples = [s for s in captured if s.idle_seen == 0]
    assert len(pkt_samples) == len(trig_pkt) + len(ack_pkt), (
        f"expected {len(trig_pkt)+len(ack_pkt)} beats, got {len(pkt_samples)}"
    )
    # Trigger packet comes first — ack waits behind.
    for i, exp in enumerate(trig_pkt):
        got = pkt_samples[i]
        assert got.trig_ready == 1 and got.ack_ready == 0, (
            f"trig beat {i} not exclusive: {got}"
        )
        assert got.data == exp.data and got.kmask == exp.kmask
    # Then the ack packet drains.
    for i, exp in enumerate(ack_pkt):
        got = pkt_samples[len(trig_pkt) + i]
        assert got.ack_ready == 1 and got.trig_ready == 0
        assert got.data == exp.data and got.kmask == exp.kmask


# -----------------------------------------------------------------------------
# TC 6 — Trigger Preempts Mid-Stream
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_trig_preempts_mid_stream(dut):
    """A trigger SOP splices into a running stream packet, which resumes.

    §8.2.4: a trigger is inserted into a lower-priority packet at the next
    word boundary and the packet resumes at its next unsent word (the
    arbiter keeps `S_STREAM`; the inserter withholds the long ready).

    Stimulus: a 6-beat stream packet (0x55110000 + i, body kmask 0) queued
              at t0; a 2-beat trig (0x9C9C9C9C + i) queued before cycle 3,
              once stream beats are already on the wire; 16 cycles.
    Checks:   every non-IDLE sample is trig (with `stream_ready = 0`) or
              stream, never neither; each source's data matches its beats
              in order; all 2 trig and 6 stream beats are seen; the first
              trig beat is not first on the wire (preempted mid-stream),
              the last trig beat is not last (stream resumed), and the two
              trig beats are adjacent (contiguous trigger).
    Note:     kmask is not compared in this test.
    """
    dut.TESTCASE.value = 6
    await bringup(dut)
    drv_trig     = SourceDriver(dut, "trig")
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    stream_pkt = packet_beats(start_data=0x5511_0000, length=6,
                              sop_kmask=0xF, eop_kmask=0xF, kmask_data=0x0)
    trig_pkt   = packet_beats(start_data=0x9C9C_9C9C, length=2,
                              sop_kmask=0xF, eop_kmask=0x0)
    drv_stream.push_packet(stream_pkt)

    captured: list[WireSample] = []
    # Let the stream packet emit its first ~3 beats before the trigger
    # SOP appears.  The drivers see ready→handshake, so by cycle ~3 the
    # SOP and a couple of body beats have gone out.
    drivers = [drv_trig, drv_ack, drv_linktest, drv_stream]
    INJECT_AT = 3
    for c in range(len(stream_pkt) + len(trig_pkt) + 8):
        if c == INJECT_AT and not drv_trig.queue:
            drv_trig.push_packet(trig_pkt)
        captured.append(await tick(dut, drivers))

    pkt_samples = [s for s in captured if s.idle_seen == 0]

    # Total beats on the wire = stream + trig, in order:
    #   stream[0..K-1] (some K ≥ 1), trig[0..1], stream[K..N-1]
    # where K is the number of stream beats accepted before the trigger
    # preempts.  Trigger must appear before the stream EOP.
    stream_indices_in_wire: list[int] = []   # index into stream_pkt
    trig_indices_in_wire:   list[int] = []   # index into trig_pkt
    si = ti = 0
    for s in pkt_samples:
        if s.trig_ready == 1:
            assert s.stream_ready == 0, f"interleave: both trig+stream ready: {s}"
            assert s.data == trig_pkt[ti].data, (
                f"trig beat {ti} data {s.data:#010x} != {trig_pkt[ti].data:#010x}"
            )
            trig_indices_in_wire.append(ti)
            ti += 1
        elif s.stream_ready == 1:
            assert s.data == stream_pkt[si].data, (
                f"stream beat {si} data {s.data:#010x} != "
                f"{stream_pkt[si].data:#010x}"
            )
            stream_indices_in_wire.append(si)
            si += 1
        else:
            raise AssertionError(f"non-idle sample with no source ready: {s}")

    assert ti == len(trig_pkt),   f"only {ti} of {len(trig_pkt)} trig beats seen"
    assert si == len(stream_pkt), f"only {si} of {len(stream_pkt)} stream beats seen"

    # Preemption must have happened mid-stream: trigger beats must lie
    # between a stream beat and the stream EOP.  Find first trig position
    # in the wire stream and confirm at least one stream beat precedes
    # and at least one follows it.
    wire_owners = []
    for s in pkt_samples:
        wire_owners.append('trig' if s.trig_ready else 'stream')
    first_trig = wire_owners.index('trig')
    last_trig  = len(wire_owners) - 1 - wire_owners[::-1].index('trig')
    assert first_trig > 0, (
        f"trigger did not preempt mid-stream — fired at wire position 0"
    )
    assert last_trig < len(wire_owners) - 1, (
        f"trigger ran to end-of-wire — stream did not resume "
        f"(owners: {wire_owners})"
    )
    # Trigger packet contiguous (HDR immediately followed by DLY).
    assert last_trig - first_trig == len(trig_pkt) - 1, (
        f"trigger packet not contiguous on the wire (owners: {wire_owners})"
    )


# -----------------------------------------------------------------------------
# TC 7 — Trigger Preempts Mid-Ack
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_trig_preempts_mid_ack(dut):
    """A trigger SOP splices into a running ack packet, which resumes.

    Same §8.2.4 word-boundary insertion rule as TC 6, for the ctrl-ack
    port (the arbiter stays in `S_ACK`).

    Stimulus: a 5-beat ack packet (0xACAC0000 + i, body kmask 0) queued at
              t0; a 2-beat trig (0x5C5C5C5C + i) queued before cycle 2;
              15 cycles.
    Checks:   every non-IDLE sample is trig (with `ack_ready = 0`) or ack,
              never neither; data in order per source; all 2 trig and 5
              ack beats seen; the trigger is neither first nor last on the
              wire and its two beats are adjacent.
    Note:     kmask is not compared in this test.
    """
    dut.TESTCASE.value = 7
    await bringup(dut)
    drv_trig     = SourceDriver(dut, "trig")
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    ack_pkt  = packet_beats(start_data=0xACAC_0000, length=5,
                            sop_kmask=0xF, eop_kmask=0xF, kmask_data=0x0)
    trig_pkt = packet_beats(start_data=0x5C5C_5C5C, length=2,
                            sop_kmask=0xF, eop_kmask=0x0)
    drv_ack.push_packet(ack_pkt)

    captured: list[WireSample] = []
    drivers = [drv_trig, drv_ack, drv_linktest, drv_stream]
    INJECT_AT = 2
    for c in range(len(ack_pkt) + len(trig_pkt) + 8):
        if c == INJECT_AT and not drv_trig.queue:
            drv_trig.push_packet(trig_pkt)
        captured.append(await tick(dut, drivers))

    pkt_samples = [s for s in captured if s.idle_seen == 0]

    ai = ti = 0
    wire_owners = []
    for s in pkt_samples:
        if s.trig_ready == 1:
            assert s.ack_ready == 0, f"interleave: both trig+ack ready: {s}"
            assert s.data == trig_pkt[ti].data, (
                f"trig beat {ti} data mismatch: {s.data:#010x}"
            )
            wire_owners.append('trig')
            ti += 1
        elif s.ack_ready == 1:
            assert s.data == ack_pkt[ai].data, (
                f"ack beat {ai} data mismatch: {s.data:#010x}"
            )
            wire_owners.append('ack')
            ai += 1
        else:
            raise AssertionError(f"non-idle sample with no source ready: {s}")

    assert ti == len(trig_pkt)
    assert ai == len(ack_pkt)
    first_trig = wire_owners.index('trig')
    last_trig  = len(wire_owners) - 1 - wire_owners[::-1].index('trig')
    assert first_trig > 0, "trigger did not preempt mid-ack"
    assert last_trig < len(wire_owners) - 1, "ack did not resume after trigger"
    assert last_trig - first_trig == len(trig_pkt) - 1, "trig packet not contiguous"


# -----------------------------------------------------------------------------
# TC 8 — No Mid-Packet Interleave
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_no_mid_packet_interleave(dut):
    """A stream SOP arriving while an ack is in flight waits for the EOP.

    §8.2.4: non-trigger packets are never interleaved; `S_ACK` ignores a
    stream SOP and only the next `S_NONE` choice picks it up.

    Stimulus: a 5-beat ack packet (0x99990000 + i) queued at t0; a 3-beat
              stream packet (0x77770000 + i) queued before cycle 2, while
              the ack runs; 14 cycles.
    Checks:   the first 5 non-IDLE samples have `ack_ready = 1`,
              `stream_ready = 0` and the ack data in order; the next 3 have
              `stream_ready = 1`, `ack_ready = 0` and the stream data.
    Note:     kmask and the total beat count are not asserted.
    """
    dut.TESTCASE.value = 8
    await bringup(dut)
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    ack_pkt = packet_beats(start_data=0x9999_0000, length=5)
    drv_ack.push_packet(ack_pkt)

    # On cycle 2 of the ack packet we will also start offering a stream
    # SOP — the arbiter must keep selecting ack until ack EOP fires.
    stream_pkt = packet_beats(start_data=0x7777_0000, length=3)

    captured: list[WireSample] = []
    for c in range(len(ack_pkt) + len(stream_pkt) + 6):
        if c == 2 and not drv_stream.queue:
            drv_stream.push_packet(stream_pkt)
        captured.append(await tick(dut, [drv_ack, drv_linktest, drv_stream]))

    pkt_samples = [s for s in captured if s.idle_seen == 0]
    # First len(ack_pkt) beats: ack.
    for i, exp in enumerate(ack_pkt):
        got = pkt_samples[i]
        assert got.ack_ready == 1 and got.stream_ready == 0, (
            f"ack beat {i} not exclusive: {got}"
        )
        assert got.data == exp.data
    # After ack EOP: stream packet flows.
    for i, exp in enumerate(stream_pkt):
        got = pkt_samples[len(ack_pkt) + i]
        assert got.stream_ready == 1 and got.ack_ready == 0, (
            f"stream beat {i} not exclusive: {got}"
        )
        assert got.data == exp.data


# -----------------------------------------------------------------------------
# TC 9 — IDLE Cadence Inside A Long Packet
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_idle_cadence_midpacket(dut):
    """A long packet is stretched by one IDLE every 95 words, nothing lost.

    §8.2.5.1: an IDLE at least every 100 words; §8.2.5.2: IDLE words may
    stretch a high-speed packet.  The inserter sends the IDLE once 95
    words have gone, keeping room for a trigger and an I/O-ack before
    the hard limit.

    Stimulus: a 300-beat always-valid stream packet (0x57000000 + i)
              queued at t0; 330 cycles.
    Checks:   every run of non-IDLE samples between two IDLEs is exactly 95
              words (never 99 or more); the stream beats appear complete
              and in order.
    """
    dut.TESTCASE.value = 9
    await bringup(dut)
    drv_stream = SourceDriver(dut, "stream")
    pkt = packet_beats(start_data=0x5700_0000, length=300)
    drv_stream.push_packet(pkt)
    captured = [await tick(dut, [drv_stream]) for _ in range(330)]
    runs, run = [], 0
    for s in captured:
        if s.idle_seen:
            if run:
                runs.append(run)
            run = 0
        else:
            run += 1
    assert runs[:3] == [95, 95, 95], f"runs between IDLEs: {runs}"
    assert max(runs) <= 95, runs
    assert [s.data for s in captured if s.stream_ready] == [b.data for b in pkt]


# -----------------------------------------------------------------------------
# TC 10 — Source Stall IDLE Fill
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_source_stall_idle_fill(dut):
    """An owner dropping `valid` mid-packet gets IDLE fill, then resumes.

    §8.2.5.2 stretching: `S_ACK` holds while the owner's valid is low, the
    wire carries IDLE, and the next granted beat is the next unsent one.

    Stimulus: a 4-beat ack packet (0xCAFE0000 + i) driven directly (no
              `SourceDriver`) by the plan present/present/absent/absent/
              present…, i.e. valid low for 2 cycles after beat 1; the
              head advances on `ack_ready`; after the 4th beat, 3 tail
              cycles with all sources idle (loop bound 20 cycles).
    Checks:   exactly 4 samples with `ack_ready = 1`, data equal to the
              4 beats in order (the stalled beat is not repeated); at least
              2 samples with `idle_seen = 1`.
    Note:     the IDLE-count check is vacuous — the 3 tail cycles after the
              EOP are counted too, so it passes without the stall IDLEs.
    """
    dut.TESTCASE.value = 10
    await bringup(dut)
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    pkt = packet_beats(start_data=0xCAFE_0000, length=4)

    captured: list[WireSample] = []
    queue_iter = iter(pkt)
    cur: SrcBeat | None = next(queue_iter, None)

    # Pattern of (drive_or_stall) — True = present beat, False = drop valid
    # for one cycle (source stall).
    drive_plan = [True, True, False, False, True, True, True]  # 2 stall cycles mid-packet
    plan_idx = 0
    consumed = 0
    for c in range(20):
        # Decide what to drive for ack this cycle.
        do_drive = drive_plan[plan_idx] if plan_idx < len(drive_plan) else False
        plan_idx += 1

        if do_drive and cur is not None:
            _drive_source(dut, "ack", cur)
        else:
            _drive_source(dut, "ack", idle_beat())
        _drive_source(dut, "stream", idle_beat())

        await RisingEdge(dut.tx_clk)
        s = _sample(dut)
        captured.append(s)

        # Advance the source queue on handshake.
        if do_drive and cur is not None and s.ack_ready == 1:
            consumed += 1
            cur = next(queue_iter, None)
        if cur is None and consumed == len(pkt):
            # Tail: a few more idle ticks to confirm we returned to S_NONE.
            for _ in range(3):
                _drive_source(dut, "ack",    idle_beat())
                _drive_source(dut, "stream", idle_beat())
                await RisingEdge(dut.tx_clk)
                captured.append(_sample(dut))
            break

    # The stall cycles must show IDLE on the wire, but state must NOT
    # have left S_ACK — so the next packet beat after the stall must
    # be the next un-consumed beat from pkt, not a re-emission of the
    # one that stalled.
    pkt_samples = [s for s in captured if s.ack_ready == 1]
    assert len(pkt_samples) == len(pkt), (
        f"stall test: expected {len(pkt)} ack beats, got {len(pkt_samples)}"
    )
    for got, exp in zip(pkt_samples, pkt):
        assert got.data == exp.data, (
            f"stall test: beat {got.data:#010x} != expected {exp.data:#010x}"
        )

    # And at least one IDLE cycle must have appeared while no beat fired.
    idle_count = sum(1 for s in captured if s.idle_seen == 1)
    assert idle_count >= 2, (
        f"stall test: expected ≥2 IDLE fill cycles, got {idle_count}"
    )


# -----------------------------------------------------------------------------
# TC 11 — IDLE Between Packets
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_idle_between_packets(dut):
    """IDLE fills the wire after an EOP, and a new packet starts cleanly.

    Covers `S_ACK → S_NONE` and IDLE fill in `S_NONE` with no SOP
    offered, then a fresh `S_NONE → S_STREAM` grant.

    Stimulus: a 3-beat ack packet (0xAAAA0000 + i), 9 cycles; then a 4-beat
              stream packet (0xBBBB0000 + i), 8 more cycles.
    Checks:   the ack EOP beat is seen (`ack_ready = 1` with the last beat's
              data); after it, a run of at least 3 contiguous
              `idle_seen = 1` samples; in the second window exactly 4
              `stream_ready` samples with the stream data in order.
    """
    dut.TESTCASE.value = 11
    await bringup(dut)
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    pkt_a = packet_beats(start_data=0xAAAA_0000, length=3)
    drv_ack.push_packet(pkt_a)

    captured: list[WireSample] = []
    # Drain ack packet.
    for _ in range(len(pkt_a) + 6):
        captured.append(await tick(dut, [drv_ack, drv_linktest, drv_stream]))

    # Look for ≥ 3 contiguous IDLE samples after the EOP.
    found_eop = False
    idle_run = 0
    max_run  = 0
    for s in captured:
        if not found_eop:
            # EOP marker: ack_ready=1 and the data corresponds to the
            # last beat of pkt_a.
            if s.ack_ready == 1 and s.data == pkt_a[-1].data:
                found_eop = True
                idle_run = 0
            continue
        if s.idle_seen == 1:
            idle_run += 1
            max_run = max(max_run, idle_run)
        else:
            idle_run = 0
    assert found_eop, "did not observe the ack EOP beat"
    assert max_run >= 3, (
        f"expected ≥3 contiguous IDLE cycles between packets, saw {max_run}"
    )

    # Now offer a stream packet — must start cleanly.
    pkt_b = packet_beats(start_data=0xBBBB_0000, length=4)
    drv_stream.push_packet(pkt_b)
    captured2: list[WireSample] = []
    for _ in range(len(pkt_b) + 4):
        captured2.append(await tick(dut, [drv_ack, drv_linktest, drv_stream]))
    stream_samples = [s for s in captured2 if s.stream_ready == 1]
    assert len(stream_samples) == len(pkt_b)
    for got, exp in zip(stream_samples, pkt_b):
        assert got.data == exp.data


# -----------------------------------------------------------------------------
# TC 12 — Single-Beat Packet
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_single_beat_packet(dut):
    """One-word packets (SOP+EOP together) keep `S_NONE` and keep priority.

    A taken EOP closes the packet in the cycle its SOP opens it, so
    `owner_q` never leaves `S_NONE`, and the ack > stream order holds.

    Stimulus: a 1-beat ack (0xDEADBEEF) and a 1-beat stream (0xF00DF00D),
              both queued before the first cycle; 12 cycles.
    Checks:   exactly one `ack_ready` and one `stream_ready` sample, with
              data 0xDEADBEEF and 0xF00DF00D; the ack sample comes before
              the stream sample.
    """
    dut.TESTCASE.value = 12
    await bringup(dut)
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    single = packet_beats(start_data=0xDEAD_BEEF, length=1)
    drv_ack.push_packet(single)
    # Followed by a single-beat stream packet, no gap.
    drv_stream.push_packet(packet_beats(start_data=0xF00D_F00D, length=1))

    captured: list[WireSample] = []
    for _ in range(12):
        captured.append(await tick(dut, [drv_ack, drv_linktest, drv_stream]))

    ack_seen    = [s for s in captured if s.ack_ready    == 1]
    stream_seen = [s for s in captured if s.stream_ready == 1]
    assert len(ack_seen)    == 1, f"expected 1 ack beat, got {len(ack_seen)}"
    assert len(stream_seen) == 1, f"expected 1 stream beat, got {len(stream_seen)}"
    assert ack_seen[0].data    == 0xDEAD_BEEF
    assert stream_seen[0].data == 0xF00D_F00D
    # The ack must come before the stream on the wire.
    ack_idx    = captured.index(ack_seen[0])
    stream_idx = captured.index(stream_seen[0])
    assert ack_idx < stream_idx, (
        f"single-beat ack ({ack_idx}) should be before stream ({stream_idx})"
    )


# -----------------------------------------------------------------------------
# TC 13 — kmask Pass-Through
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_kmask_passthrough(dut):
    """The output mux passes each beat's kmask bit for bit.

    Guards against a kmask taken from the wrong source or lane-swapped.

    Stimulus: a hand-built 4-beat ack packet: 0xFBFBFBFB / kmask 0xF (SOP),
              0x01020304 / 0xA, 0x10203040 / 0x5, 0xFDFDFDFD / 0xF (EOP);
              8 cycles.
    Checks:   exactly 4 `ack_ready` samples; data and kmask equal to the
              driven beats in order.
    """
    dut.TESTCASE.value = 13
    await bringup(dut)
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    # Pick varied kmasks per beat — the K27.7 SOP/EOP gets 0xF, the body
    # uses an oddball mask 0xA, and the body data is non-trivial.
    pkt = [
        SrcBeat(data=0xFB_FB_FB_FB, kmask=0xF, valid=1, sop=1, eop=0),
        SrcBeat(data=0x01_02_03_04, kmask=0xA, valid=1, sop=0, eop=0),
        SrcBeat(data=0x10_20_30_40, kmask=0x5, valid=1, sop=0, eop=0),
        SrcBeat(data=0xFD_FD_FD_FD, kmask=0xF, valid=1, sop=0, eop=1),
    ]
    drv_ack.push_packet(pkt)

    captured: list[WireSample] = []
    for _ in range(len(pkt) + 4):
        captured.append(await tick(dut, [drv_ack, drv_linktest, drv_stream]))

    pkt_samples = [s for s in captured if s.ack_ready == 1]
    assert len(pkt_samples) == len(pkt)
    for got, exp in zip(pkt_samples, pkt):
        assert got.data  == exp.data
        assert got.kmask == exp.kmask, (
            f"kmask mismatch: got {got.kmask:#x}, expected {exp.kmask:#x}"
        )


# -----------------------------------------------------------------------------
# TC 14 — Random Mixed Stalls
# -----------------------------------------------------------------------------
@cxp_test()
async def test_14_random_mixed(dut):
    """Ack and stream packets survive random stalls intact.

    Stress for the no-interleave rule between two priority-2 sources while
    owners drop `valid`.

    Stimulus: `random.Random(0xC0FFEE)`; 4 ack packets of 1–4 beats and 4
              stream packets of 1–6 beats with random base data, driven
              directly; each cycle each source withholds its current beat
              with p = 0.15; up to 800 cycles, plus 4 drain cycles once
              both queues are empty.
    Checks:   every packet is entered on a SOP beat; the owner never changes
              before an EOP beat is granted; the sequence of handshaken
              beats per source equals its queue exactly (no loss, no
              duplicate, in order).
    Note:     wire data/kmask are not compared (the handshaken beat is the
              TB's own); ack-before-stream priority is not asserted; trig,
              ioack and linktest are not driven; one seed.
    """
    dut.TESTCASE.value = 14
    await bringup(dut)

    rng = random.Random(0xC0FFEE)

    # Build small packet queues.
    ack_packets:    list[list[SrcBeat]] = []
    stream_packets: list[list[SrcBeat]] = []
    for _ in range(4):
        ack_packets.append(packet_beats(rng.randrange(0, 1 << 32),
                                        rng.randint(1, 4)))
        stream_packets.append(packet_beats(rng.randrange(0, 1 << 32),
                                           rng.randint(1, 6)))

    ack_flat:    list[SrcBeat] = [b for p in ack_packets    for b in p]
    stream_flat: list[SrcBeat] = [b for p in stream_packets for b in p]

    ack_iter    = iter(ack_flat)
    stream_iter = iter(stream_flat)
    cur_ack    : SrcBeat | None = next(ack_iter,    None)
    cur_stream : SrcBeat | None = next(stream_iter, None)

    got_ack:    list[SrcBeat] = []
    got_stream: list[SrcBeat] = []
    captured:   list[WireSample] = []

    in_pkt = None   # 'ack', 'stream', or None
    for c in range(800):
        # 15% chance of source stall on each source.
        drive_ack    = (rng.random() > 0.15) and (cur_ack    is not None)
        drive_stream = (rng.random() > 0.15) and (cur_stream is not None)
        _drive_source(dut, "ack",    cur_ack    if drive_ack    else idle_beat())
        _drive_source(dut, "stream", cur_stream if drive_stream else idle_beat())
        await RisingEdge(dut.tx_clk)
        s = _sample(dut)
        captured.append(s)

        # Update queues.
        if s.ack_ready == 1 and cur_ack is not None and drive_ack:
            got_ack.append(cur_ack)
            cur_ack = next(ack_iter, None)
        if s.stream_ready == 1 and cur_stream is not None and drive_stream:
            got_stream.append(cur_stream)
            cur_stream = next(stream_iter, None)

        # Track that we never interleave.
        if s.ack_ready or s.stream_ready:
            owner = 'ack' if s.ack_ready else 'stream'
            if in_pkt is None:
                in_pkt = owner
                # On entry the beat must be SOP.
                beat = (got_ack[-1] if owner == 'ack' else got_stream[-1])
                assert beat.sop == 1, (
                    f"entered packet on non-SOP beat ({owner}): {beat}"
                )
            else:
                assert owner == in_pkt, (
                    f"interleave at cycle {c}: was in {in_pkt}, now {owner}"
                )
            last = (got_ack[-1] if owner == 'ack' else got_stream[-1])
            if last.eop == 1:
                in_pkt = None

        if cur_ack is None and cur_stream is None:
            # Drain a couple more cycles.
            for _ in range(4):
                _drive_source(dut, "ack",    idle_beat())
                _drive_source(dut, "stream", idle_beat())
                await RisingEdge(dut.tx_clk)
                captured.append(_sample(dut))
            break

    assert got_ack    == ack_flat,    "ack source corruption under random stress"
    assert got_stream == stream_flat, "stream source corruption under random stress"


# -----------------------------------------------------------------------------
# TC 15 — Priority: I/O-Ack Over Ctrl-Ack
# -----------------------------------------------------------------------------
@cxp_test()
async def test_15_priority_ioack_over_ack(dut):
    """I/O-ack wins over ctrl-ack when both offer a SOP in the same cycle.

    §8.2.4 Table 13: 0 Trigger > 1 I/O-ack > 2 all other, applied at the
    `S_NONE` packet-boundary choice.

    Stimulus: a 2-beat ioack (0xDCDCDCDC + i, SOP kmask 0xF, EOP kmask 0)
              and a 4-beat ack (0xA1A10000 + i), both queued before the
              first cycle; 12 cycles.
    Checks:   exactly 6 non-IDLE samples; the 2 ioack beats have
              `ioack_ready = 1`, `ack_ready = 0` and matching data/kmask;
              then the 4 ack beats have `ack_ready = 1`, `ioack_ready = 0`
              and matching data/kmask.
    """
    dut.TESTCASE.value = 15
    await bringup(dut)
    drv_trig     = SourceDriver(dut, "trig")
    drv_ioack    = SourceDriver(dut, "ioack")
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    ioack_pkt = packet_beats(start_data=0xDCDC_DCDC, length=2,
                             sop_kmask=0xF, eop_kmask=0x0)
    ack_pkt   = packet_beats(start_data=0xA1A1_0000, length=4,
                             sop_kmask=0xF, eop_kmask=0xF)
    drv_ioack.push_packet(ioack_pkt)
    drv_ack.push_packet(ack_pkt)

    drivers = [drv_trig, drv_ioack, drv_ack, drv_linktest, drv_stream]
    captured: list[WireSample] = []
    for _ in range(len(ioack_pkt) + len(ack_pkt) + 6):
        captured.append(await tick(dut, drivers))

    pkt_samples = [s for s in captured if s.idle_seen == 0]
    assert len(pkt_samples) == len(ioack_pkt) + len(ack_pkt), (
        f"expected {len(ioack_pkt)+len(ack_pkt)} beats, got {len(pkt_samples)}"
    )
    # I/O-ack first, exclusive.
    for i, exp in enumerate(ioack_pkt):
        got = pkt_samples[i]
        assert got.ioack_ready == 1 and got.ack_ready == 0, (
            f"ioack beat {i} not exclusive: {got}"
        )
        assert got.data == exp.data and got.kmask == exp.kmask
    # Then ctrl-ack.
    for i, exp in enumerate(ack_pkt):
        got = pkt_samples[len(ioack_pkt) + i]
        assert got.ack_ready == 1 and got.ioack_ready == 0
        assert got.data == exp.data and got.kmask == exp.kmask


# -----------------------------------------------------------------------------
# TC 16 — Priority: Trigger Over I/O-Ack
# -----------------------------------------------------------------------------
@cxp_test()
async def test_16_priority_trig_over_ioack(dut):
    """Trigger (priority 0) wins over I/O-ack (1) when both offer a SOP.

    Completes the Table 13 ordering at the `S_NONE` choice.

    Stimulus: a 2-beat trig (0x9C9C9C9C + i) and a 2-beat ioack
              (0xDCDCDCDC + i), both queued before the first cycle;
              10 cycles.
    Checks:   exactly 4 non-IDLE samples; the 2 trig beats have
              `trig_ready = 1`, `ioack_ready = 0` and matching data; then
              the 2 ioack beats have `ioack_ready = 1`, `trig_ready = 0`
              and matching data.
    Note:     kmask is not compared in this test.
    """
    dut.TESTCASE.value = 16
    await bringup(dut)
    drv_trig     = SourceDriver(dut, "trig")
    drv_ioack    = SourceDriver(dut, "ioack")
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    trig_pkt  = packet_beats(start_data=0x9C9C_9C9C, length=2,
                             sop_kmask=0xF, eop_kmask=0x0)
    ioack_pkt = packet_beats(start_data=0xDCDC_DCDC, length=2,
                             sop_kmask=0xF, eop_kmask=0x0)
    drv_trig.push_packet(trig_pkt)
    drv_ioack.push_packet(ioack_pkt)

    drivers = [drv_trig, drv_ioack, drv_ack, drv_linktest, drv_stream]
    captured: list[WireSample] = []
    for _ in range(len(trig_pkt) + len(ioack_pkt) + 6):
        captured.append(await tick(dut, drivers))

    pkt_samples = [s for s in captured if s.idle_seen == 0]
    assert len(pkt_samples) == len(trig_pkt) + len(ioack_pkt)
    for i, exp in enumerate(trig_pkt):
        got = pkt_samples[i]
        assert got.trig_ready == 1 and got.ioack_ready == 0, (
            f"trig beat {i} not exclusive over ioack: {got}"
        )
        assert got.data == exp.data
    for i, exp in enumerate(ioack_pkt):
        got = pkt_samples[len(trig_pkt) + i]
        assert got.ioack_ready == 1 and got.trig_ready == 0
        assert got.data == exp.data


# -----------------------------------------------------------------------------
# TC 17 — I/O-Ack Inserted Into A Running Ack
# -----------------------------------------------------------------------------
@cxp_test()
async def test_17_ioack_inserted_mid_ack(dut):
    """An I/O-ack arriving mid-packet goes out within 3 words.

    §8.2.4 / Table 13: the I/O acknowledgment is priority 1, inserted at
    the next word boundary of any lower-priority packet, which resumes
    afterwards; §8.3.3 gives the host one low-speed character to see it.

    Stimulus: a 20-beat ack packet (0xACAC0000 + i) queued at t0; a 2-beat
              ioack (0xDCDCDCDC + i) queued at cycle 2, while the ack runs;
              30 cycles.
    Checks:   the ioack SOP is on the wire within 3 cycles of being
              queued, its second word on the next cycle; the ack words
              appear complete and in order around it.
    """
    dut.TESTCASE.value = 17
    await bringup(dut)
    drv_trig     = SourceDriver(dut, "trig")
    drv_ioack    = SourceDriver(dut, "ioack")
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    ack_pkt   = packet_beats(start_data=0xACAC_0000, length=20,
                             sop_kmask=0xF, eop_kmask=0xF, kmask_data=0x0)
    ioack_pkt = packet_beats(start_data=0xDCDC_DCDC, length=2,
                             sop_kmask=0xF, eop_kmask=0x0)
    drv_ack.push_packet(ack_pkt)

    drivers = [drv_trig, drv_ioack, drv_ack, drv_linktest, drv_stream]
    captured: list[WireSample] = []
    INJECT_AT = 2
    for c in range(len(ack_pkt) + len(ioack_pkt) + 8):
        if c == INJECT_AT and not drv_ioack.queue:
            drv_ioack.push_packet(ioack_pkt)
        captured.append(await tick(dut, drivers))

    first = next((i for i, s in enumerate(captured)
                  if s.ioack_ready == 1 and s.data == ioack_pkt[0].data), None)
    assert first is not None, "I/O-ack never granted"
    assert first - INJECT_AT <= 3, (
        f"I/O-ack SOP {first - INJECT_AT} cycles after its request (>3)")
    nxt = captured[first + 1]
    assert nxt.ioack_ready == 1 and nxt.data == ioack_pkt[1].data, nxt
    ack_words = [s.data for s in captured if s.ack_ready == 1]
    assert ack_words == [b.data for b in ack_pkt], "ack words lost or reordered"


# -----------------------------------------------------------------------------
# TC 18 — Trigger Behind An I/O-Ack's First Word
# -----------------------------------------------------------------------------
@cxp_test()
async def test_18_trig_waits_for_ioack_code(dut):
    """A trigger never splits an I/O acknowledgment.

    Tables 16 / 17: both are two-word packets; a host reads the word after
    a leader as its code, so the I/O-ack's code word must follow its
    leader even when a trigger is offered in between (§8.2.4 lets the
    trigger go at the next word boundary after it).

    Stimulus: a 2-beat ioack (0xDCDCDCDC, 0x01010101) offered at t0 while
              a 12-beat ack packet runs; a 2-beat trigger (0x9C9C9C9C,
              0) offered in the cycle the ioack leader is chosen; 24
              cycles.
    Checks:   the wire order is ioack leader, ioack code, trigger leader,
              trigger Delay, on four consecutive words; the ack packet
              completes around them.
    """
    dut.TESTCASE.value = 18
    await bringup(dut)
    drv_trig  = SourceDriver(dut, "trig")
    drv_ioack = SourceDriver(dut, "ioack")
    drv_ack   = SourceDriver(dut, "ack")
    ack_pkt   = packet_beats(start_data=0xACAC_0000, length=12,
                             sop_kmask=0xF, eop_kmask=0xF, kmask_data=0x0)
    ioack_pkt = [SrcBeat(0xDCDC_DCDC, 0xF, 1, 1, 0), SrcBeat(0x0101_0101, 0x0, 1, 0, 1)]
    trig_pkt  = [SrcBeat(0x9C9C_9C9C, 0xF, 1, 1, 0), SrcBeat(0x0000_0000, 0x0, 1, 0, 1)]
    drv_ack.push_packet(ack_pkt)
    drivers = [drv_trig, drv_ioack, drv_ack]
    captured: list[WireSample] = []
    for c in range(24):
        if c == 3:
            drv_ioack.push_packet(ioack_pkt)
        s = await tick(dut, drivers)
        captured.append(s)
        if s.ioack_ready and s.data == ioack_pkt[0].data:
            drv_trig.push_packet(trig_pkt)
    lead = next(i for i, s in enumerate(captured) if s.ioack_ready)
    order = [(s.data, s.trig_ready, s.ioack_ready) for s in captured[lead:lead + 4]]
    assert order == [(0xDCDC_DCDC, 0, 1), (0x0101_0101, 0, 1),
                     (0x9C9C_9C9C, 1, 0), (0x0000_0000, 1, 0)], order
    assert [s.data for s in captured if s.ack_ready] == [b.data for b in ack_pkt]


# -----------------------------------------------------------------------------
# TC 22 — Link-Test Packet Pass-Through
# -----------------------------------------------------------------------------
@cxp_test()
async def test_22_linktest_packet_passthrough(dut):
    """A lone link-test packet is forwarded word for word.

    The only test that takes `p_linktest` from `S_NONE`: covers
    `S_NONE → S_LINKTEST → S_NONE` for the §8.7 connection-test slot.

    Stimulus: one 5-beat link-test packet (0x17000000 + i, SOP/EOP kmask
              0xF, body kmask 0); 9 cycles.
    Checks:   exactly 5 non-IDLE samples; each has `linktest_ready = 1`,
              `ack_ready = 0`, `stream_ready = 0`, `idle_seen = 0`, and data
              and kmask equal to the driven beat, in order.
    """
    dut.TESTCASE.value = 22
    await bringup(dut)
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    pkt = packet_beats(start_data=0x1700_0000, length=5,
                       sop_kmask=0xF, eop_kmask=0xF, kmask_data=0x0)
    drv_linktest.push_packet(pkt)

    captured: list[WireSample] = []
    for _ in range(len(pkt) + 4):
        captured.append(await tick(dut, [drv_ack, drv_linktest, drv_stream]))

    pkt_samples = [s for s in captured if s.idle_seen == 0]
    assert len(pkt_samples) == len(pkt), (
        f"expected {len(pkt)} link-test beats, got {len(pkt_samples)}: {pkt_samples}"
    )
    for i, (got, exp) in enumerate(zip(pkt_samples, pkt)):
        assert got.linktest_ready == 1, (
            f"beat {i}: linktest_ready not asserted: {got}"
        )
        assert got.ack_ready == 0 and got.stream_ready == 0, (
            f"beat {i}: a competing source leaked: {got}"
        )
        assert got.idle_seen == 0, f"beat {i}: idle_seen set on a packet beat"
        assert got.data == exp.data, (
            f"beat {i} data: got {got.data:#010x}, expected {exp.data:#010x}"
        )
        assert got.kmask == exp.kmask, (
            f"beat {i} kmask: got {got.kmask:#x}, expected {exp.kmask:#x}"
        )


# -----------------------------------------------------------------------------
# TC 23 — Trigger Preempts Mid-Link-Test
# -----------------------------------------------------------------------------
@cxp_test()
async def test_23_trig_preempts_mid_linktest(dut):
    """A trigger SOP splices into a running link-test packet, which resumes.

    Same word-boundary insertion rule as TC 6/7, for the link-test port
    (the arbiter stays in `S_LINKTEST`).

    Stimulus: a 6-beat link-test packet (0x17000000 + i, body kmask 0)
              queued at t0; a 2-beat trig (0x9C9C9C9C + i) queued before
              cycle 2; 16 cycles.
    Checks:   every non-IDLE sample is trig (with `linktest_ready = 0`) or
              link-test, never neither; data in order per source; all 2
              trig and 6 link-test beats seen; the trigger is neither first
              nor last on the wire and its two beats are adjacent.
    Note:     kmask is not compared.  `interface_top` 11 does the same
              with a real test packet under TestMode.
    """
    dut.TESTCASE.value = 23
    await bringup(dut)
    drv_trig     = SourceDriver(dut, "trig")
    drv_ack      = SourceDriver(dut, "ack")
    drv_linktest = SourceDriver(dut, "linktest")
    drv_stream   = SourceDriver(dut, "stream")

    linktest_pkt = packet_beats(start_data=0x1700_0000, length=6,
                                sop_kmask=0xF, eop_kmask=0xF, kmask_data=0x0)
    trig_pkt     = packet_beats(start_data=0x9C9C_9C9C, length=2,
                                sop_kmask=0xF, eop_kmask=0x0)
    drv_linktest.push_packet(linktest_pkt)

    drivers = [drv_trig, drv_ack, drv_linktest, drv_stream]
    captured: list[WireSample] = []
    INJECT_AT = 2
    for c in range(len(linktest_pkt) + len(trig_pkt) + 8):
        if c == INJECT_AT and not drv_trig.queue:
            drv_trig.push_packet(trig_pkt)
        captured.append(await tick(dut, drivers))

    pkt_samples = [s for s in captured if s.idle_seen == 0]
    li = ti = 0
    wire_owners: list[str] = []
    for s in pkt_samples:
        if s.trig_ready == 1:
            assert s.linktest_ready == 0, (
                f"interleave: both trig+linktest ready: {s}"
            )
            assert s.data == trig_pkt[ti].data, (
                f"trig beat {ti} data mismatch: {s.data:#010x}"
            )
            wire_owners.append('trig')
            ti += 1
        elif s.linktest_ready == 1:
            assert s.data == linktest_pkt[li].data, (
                f"linktest beat {li} data mismatch: {s.data:#010x}"
            )
            wire_owners.append('linktest')
            li += 1
        else:
            raise AssertionError(f"non-idle sample with no source ready: {s}")

    assert ti == len(trig_pkt),     f"only {ti}/{len(trig_pkt)} trig beats"
    assert li == len(linktest_pkt), f"only {li}/{len(linktest_pkt)} linktest beats"
    first_trig = wire_owners.index('trig')
    last_trig  = len(wire_owners) - 1 - wire_owners[::-1].index('trig')
    assert first_trig > 0, "trigger did not preempt mid-linktest"
    assert last_trig < len(wire_owners) - 1, (
        f"link-test did not resume after trigger (owners: {wire_owners})"
    )
    assert last_trig - first_trig == len(trig_pkt) - 1, (
        f"trigger packet not contiguous on the wire (owners: {wire_owners})"
    )


# -----------------------------------------------------------------------------
# TC 25 — Insertion At Every Run Position
# -----------------------------------------------------------------------------
TRIG_PKT  = [SrcBeat(0x9C9C_9C9C, 0xF, 1, 1, 0), SrcBeat(0x0000_0000, 0x0, 1, 0, 1)]
IOACK_PKT = [SrcBeat(0xDCDC_DCDC, 0xF, 1, 1, 0), SrcBeat(0x0101_0101, 0x0, 1, 0, 1)]


async def _reset_again(dut):
    dut.tx_rst_n.value = 0
    for _ in range(2):
        await RisingEdge(dut.tx_clk)
    dut.tx_rst_n.value = 1
    await RisingEdge(dut.tx_clk)


@cxp_test()
async def test_25_insert_at_every_run_position(dut):
    """Triggers and I/O-acks against the IDLE cadence: nothing split, no slip.

    §8.2.4 (insertion at the next word boundary), Tables 16 / 17 (two
    words, back to back), §8.2.5.1 (an IDLE at least every 100 words).

    Stimulus: for every offer cycle c = 85 .. 104 of a 300-beat always-valid
              stream packet, four runs from reset: a trigger offered at c;
              an I/O-ack offered at c; an I/O-ack at c and a trigger at
              c + 1; two I/O-acks back to back from c and a trigger at
              c + 3.
    Checks:   in every run the stream beats are complete and in order;
              every leader is followed by its second word; no run of more
              than 99 non-IDLE words; a trigger offered alone is sent in
              the cycle it is offered, one offered behind an I/O-ack at
              most 1 word later (it waits for the code word); every I/O-ack
              is sent at most 3 words after it is offered.
    """
    dut.TESTCASE.value = 25
    await bringup(dut)
    # mode: (I/O-ack offers, trigger offer), relative to c
    modes = {"trig": ([], 0), "ioack": ([0], None), "both": ([0], 1),
             "two_ioacks": ([0, 2], 3)}
    bad = []
    for c in range(85, 105):
        for mode, (io_at, t_rel) in modes.items():
            await _reset_again(dut)
            drv = {n: SourceDriver(dut, n) for n in ("trig", "ioack", "stream")}
            pkt = packet_beats(start_data=0x5700_0000, length=300)
            drv["stream"].push_packet(pkt)
            captured, io_offered, t_off = [], [], None
            for k in range(340):
                if k == c:
                    for _ in io_at:
                        drv["ioack"].push_packet(IOACK_PKT)
                if t_rel is not None and k == c + t_rel:
                    drv["trig"].push_packet(TRIG_PKT)
                    t_off = k
                s = await tick(dut, list(drv.values()))
                captured.append(s)
            tag = f"c={c} {mode}"
            if [x.data for x in captured if x.stream_ready] != [b.data for b in pkt]:
                bad.append(f"{tag}: stream beats lost or reordered")
            run = worst = 0
            for x in captured:
                run = 0 if x.idle_seen else run + 1
                worst = max(worst, run)
            if worst > 99:
                bad.append(f"{tag}: {worst} words without an IDLE")
            tr = [i for i, x in enumerate(captured) if x.trig_ready]
            io = [i for i, x in enumerate(captured) if x.ioack_ready]
            if len(tr) != (2 if t_rel is not None else 0) or any(
                    tr[i + 1] != tr[i] + 1 for i in range(0, len(tr), 2)):
                bad.append(f"{tag}: trigger words at {tr}")
            if len(io) != 2 * len(io_at) or any(
                    io[i + 1] != io[i] + 1 for i in range(0, len(io), 2)):
                bad.append(f"{tag}: I/O-ack words at {io}")
                continue
            if tr:
                limit = 0 if mode == "trig" else 1
                if tr[0] - t_off > limit:
                    bad.append(f"{tag}: trigger {tr[0] - t_off} words late")
            # the n-th I/O-ack is offered once the (n-1)-th has gone
            offered = c
            for n in range(len(io_at)):
                lead = io[2 * n]
                if lead - offered > 3:
                    bad.append(f"{tag}: I/O-ack {n} {lead - offered} words late")
                offered = lead + 2
    assert not bad, f"{len(bad)} problems: {bad[:6]}"


# -----------------------------------------------------------------------------
# TC 26 — Registered Wire Word
# -----------------------------------------------------------------------------
@cxp_test()
async def test_26_registered_wire_word(dut):
    """The wire word is the word chosen one cycle earlier.

    The downlink word leaves a register, not a mux of every source.

    Stimulus: an ack packet, a stream packet, a trigger and an I/O-ack
              offered together; 60 cycles, sampling the chosen word and
              the wire word each cycle.
    Checks:   wire word (data and kmask) at cycle k + 1 equals the chosen
              word at cycle k for every k; the wire word after reset is
              IDLE.
    """
    dut.TESTCASE.value = 26
    await bringup(dut)
    assert int(dut.wire_data.value) == IDLE_DATA and int(dut.wire_kmask.value) == IDLE_KMASK
    drv = {n: SourceDriver(dut, n) for n in ("trig", "ioack", "ack", "stream")}
    drv["ack"].push_packet(packet_beats(0xACAC_0000, 9))
    drv["stream"].push_packet(packet_beats(0x5700_0000, 20))
    drv["trig"].push_packet(TRIG_PKT)
    drv["ioack"].push_packet(IOACK_PKT)
    chosen, wire = [], []
    for _ in range(60):
        s = await tick(dut, list(drv.values()))
        chosen.append((s.data, s.kmask))
        wire.append((int(dut.wire_data.value), int(dut.wire_kmask.value)))
    assert wire[1:] == chosen[:-1], "wire word is not the previous cycle's choice"
