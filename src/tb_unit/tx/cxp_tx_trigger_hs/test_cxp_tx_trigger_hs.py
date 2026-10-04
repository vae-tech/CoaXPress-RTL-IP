"""Cocotb TB for `cxp_tx_trigger_hs`.

The DUT sends the device trigger pin to the host as 2-word high-speed
trigger packets (§8.3.2.2, Table 16) on the inserter's trigger port:

    HDR : data = 4×K28.4 (asserted) or 4×K28.2 (de-asserted), kmask = 0xF, m_sop = 1
    DLY : data = 4×Delay, always 0 (Table 16 makes Delay optional),
          kmask = 0x0, m_eop = 1

The pin passes a two-flop synchroniser; `cfg_polarity` is the pin sense
(1 = active low) and the packet carries the logical level.  One packet is
outstanding at a time (§8.3.3): after it the source waits for `ack` (the
host's I/O acknowledgment) or `ACK_TIMEOUT_P` = 64 cycles, then sends the
pin's level if it differs from the last one sent.  Nothing is sent while
`link_up` is low; `mask` (ConnectionReset) holds the level de-asserted,
and after it (and after reset) the pin is followed only once seen
de-asserted.

8 ns `tx_clk`; the wrapper only adds `TESTCASE`.  Python drives
`trigger_in` / `cfg_polarity` / `link_up` / `mask` / `ack` / `m_ready`;
`Capture` records every `m_valid & m_ready` beat and can answer each
packet with an `ack` pulse a given number of cycles after its EOP;
`split_into_packets` checks SOP/EOP framing and `check_packet_shape` the
Table 16 layout.  FSM coverage is collected on the shared short-packet
FSM's `state_q` (3 states, 3 arcs).

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Reset → no packet.
  2  Pin asserted → one well-formed K28.4 packet.
  3  Active-low pin: asserted (low) → K28.4, released (high) → K28.2.
  4  No edge → no packet.
  5  A pin held asserted through reset is not sent until it has been
     de-asserted and asserted again (both senses).
  6  Back-pressure holds the packet word stable; a change of the pin
     meanwhile follows once the packet is acknowledged.
  7  A one-cycle pulse is sent as K28.4, then K28.2 once acknowledged.
  8  Random toggles against an acknowledging host: packets alternate,
     never two without an acknowledgment between them, the host ends at
     the pin's level.
  9  Rising, falling, rising edges while the first packet is held: the
     level the host is left with is the pin's.
 10  Without acknowledgments the pin's level goes out once per timeout;
     an unchanged level is not re-sent.
 11  Nothing is sent while the link is down; the level goes out once it
     is up.
 12  The ConnectionReset mask sends K28.2 to a host left asserted; a pin
     held asserted across it is not re-sent; the next real edge is.
 13  Pin to SOP latency: two synchroniser flops, one cycle to start the
     packet.
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
ACK_TIMEOUT = 64          # wrapper ACK_TIMEOUT_P

# K-code byte values (Kx.y → y*32 + x).
K28_2 = 0x5C   # de-asserted
K28_4 = 0x9C   # asserted


# -----------------------------------------------------------------------------
# FSM coverage
# -----------------------------------------------------------------------------
# The packet FSM lives in the shared cxp_tx_short_pkt instance; its
# second state is the generic ST_COD (the Delay word here).  One packet at
# a time: the back-to-back ST_COD -> ST_HDR arc is not used.
register_fsm(
    name="tx_trigger_hs",
    states=["ST_IDLE", "ST_HDR", "ST_COD"],
    state_path="cxp_tx_trigger_hs_i.cxp_tx_short_pkt_i.state_q",
    clk_path="tx_clk",
    arcs=[
        ("ST_IDLE", "ST_HDR"), ("ST_HDR", "ST_COD"), ("ST_COD", "ST_IDLE"),
    ],
)


# -----------------------------------------------------------------------------
# Wire-beat model
# -----------------------------------------------------------------------------
def rep4(b: int) -> int:
    """Replicate one byte into all four lanes of a 32-bit word."""
    b &= 0xFF
    return (b << 24) | (b << 16) | (b << 8) | b


@dataclass
class WireBeat:
    """One accepted output beat, with the cycle it was taken in."""

    data:  int
    kmask: int
    sop:   int
    eop:   int
    cycle: int = 0


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
_clock = {"task": None}


async def reset(dut, *, ready: int = 1, cfg_polarity: int = 0,
                initial_trig: int = 0, link: int = 1):
    """Start `tx_clk` (once per test), preset inputs, hold reset 4 cycles,
    release + 1."""
    task = _clock["task"]
    if task is None or task.done():
        _clock["task"] = cocotb.start_soon(
            Clock(dut.tx_clk, CLK_NS, unit="ns").start(start_high=False))
    dut.tx_rst_n.value     = 0
    dut.trigger_in.value   = initial_trig
    dut.cfg_polarity.value = cfg_polarity
    dut.link_up.value      = link
    dut.mask.value         = 0
    dut.ack.value          = 0
    dut.m_ready.value      = ready
    for _ in range(4):
        await RisingEdge(dut.tx_clk)
    dut.tx_rst_n.value = 1
    await RisingEdge(dut.tx_clk)


# -----------------------------------------------------------------------------
# Monitor / checkers
# -----------------------------------------------------------------------------
def sample(dut, cycle: int = 0) -> WireBeat:
    """Read the current output word into a `WireBeat`."""
    return WireBeat(
        data  = int(dut.m_data.value),
        kmask = int(dut.m_kmask.value),
        sop   = int(dut.m_sop.value),
        eop   = int(dut.m_eop.value),
        cycle = cycle,
    )


class Capture:
    """Records accepted beats; answers each packet with an `ack` pulse
    `ack_delay` cycles after its EOP (None: never), and remembers the
    cycles the pulses were given."""

    def __init__(self, dut, ack_delay: int | None = None):
        self.dut, self.ack_delay = dut, ack_delay
        self.beats: list[WireBeat] = []
        self.acks: list[int] = []
        self.cycle = 0
        self._ack_at: list[int] = []

    async def run(self, cycles: int, drive=None) -> "Capture":
        """Run `cycles` edges; `drive(cycle)` may set inputs before each."""
        for _ in range(cycles):
            if drive is not None:
                drive(self.cycle)
            pulse = self.cycle in self._ack_at
            self.dut.ack.value = int(pulse)
            if pulse:
                self.acks.append(self.cycle)
            await RisingEdge(self.dut.tx_clk)
            if int(self.dut.m_valid.value) and int(self.dut.m_ready.value):
                b = sample(self.dut, self.cycle)
                self.beats.append(b)
                if b.eop and self.ack_delay is not None:
                    self._ack_at.append(self.cycle + 1 + self.ack_delay)
            self.cycle += 1
        self.dut.ack.value = 0
        return self


async def capture_accepted(dut, cycles: int) -> list[WireBeat]:
    """Run for ``cycles`` tx_clk ticks and return the accepted beats."""
    return (await Capture(dut).run(cycles)).beats


def split_into_packets(beats: list[WireBeat]) -> list[list[WireBeat]]:
    """Group accepted beats into 2-beat trigger packets delimited by
    SOP (start) and EOP (end)."""
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


def check_packet_shape(pkt: list[WireBeat], expected_kcode: int) -> None:
    """Assert `pkt` is [4×K HDR (kmask 0xF, sop), 0 DLY (kmask 0, eop)]."""
    assert len(pkt) == 2, f"trigger packet should be 2 beats, got {len(pkt)}"
    assert pkt[0].data  == rep4(expected_kcode), (
        f"HDR data {pkt[0].data:#010x} != rep4(K=0x{expected_kcode:02x})"
    )
    assert pkt[0].kmask == 0xF, f"HDR kmask {pkt[0].kmask:#x}"
    assert pkt[0].sop == 1 and pkt[0].eop == 0
    assert pkt[1].data  == 0x0000_0000, f"DLY data {pkt[1].data:#010x} != 0"
    assert pkt[1].kmask == 0x0,         f"DLY kmask {pkt[1].kmask:#x}"
    assert pkt[1].sop == 0 and pkt[1].eop == 1


def kcodes(pkts) -> list[int]:
    return [p[0].data & 0xFF for p in pkts]


def unpaced(pkts, acks: list[int]) -> list[str]:
    """Packets that followed the previous one with neither an ack nor the
    timeout in between (§8.3.3)."""
    bad = []
    for a, b in zip(pkts, pkts[1:]):
        done, nxt = a[-1].cycle, b[0].cycle
        if not any(done < k < nxt for k in acks) and nxt - done < ACK_TIMEOUT:
            bad.append(f"packet at {nxt} {nxt - done} cycles after {done}")
    return bad


# -----------------------------------------------------------------------------
# TC 1 — Reset Idle
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_reset_idle(dut):
    """Nothing is sent out of reset while the pin stays de-asserted.

    Stimulus: default `reset` (`cfg_polarity = 0`, pin 0, link up), then
              16 cycles with no input change.
    Checks:   `m_valid == 0` after each of the 16 edges.
    """
    dut.TESTCASE.value = 1
    await reset(dut)
    for _ in range(16):
        await RisingEdge(dut.tx_clk)
        assert int(dut.m_valid.value) == 0, (
            "no packet should fire while trigger_in stays at de-asserted level"
        )


# -----------------------------------------------------------------------------
# TC 2 — Asserted Sends K28.4
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_rising_edge_kcode(dut):
    """The pin asserted sends exactly one K28.4 trigger packet.

    Table 16: 4×K28.4 marks the trigger asserted.  A steady asserted level
    after it sends nothing more.

    Stimulus: `cfg_polarity = 0`, pin 0 through reset; pin 1 after
              release and held; 40-cycle capture, the packet acknowledged
              4 cycles after its EOP.
    Checks:   exactly one packet, `check_packet_shape(K28.4)`.
    """
    dut.TESTCASE.value = 2
    await reset(dut, cfg_polarity=0, initial_trig=0)
    for _ in range(4):
        await RisingEdge(dut.tx_clk)
    dut.trigger_in.value = 1
    pkts = split_into_packets((await Capture(dut, ack_delay=4).run(40)).beats)
    assert len(pkts) == 1, f"expected 1 packet on rising edge, got {len(pkts)}"
    check_packet_shape(pkts[0], K28_4)


# -----------------------------------------------------------------------------
# TC 3 — Active-Low Pin
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_falling_edge_kcode(dut):
    """With `cfg_polarity = 1` the low pin is asserted: K28.4, then K28.2.

    §8.3.2: the packet carries the trigger's logical edge; the polarity
    only says which pin level is asserted.

    Stimulus: `cfg_polarity = 1`, pin 1 (de-asserted) through reset; pin 0
              after 4 cycles; pin 1 after 40 more; packets acknowledged 4
              cycles after their EOP; 100 cycles.
    Checks:   two packets, K28.4 then K28.2.
    """
    dut.TESTCASE.value = 3
    await reset(dut, cfg_polarity=1, initial_trig=1)

    def drive(c):
        if c == 4:
            dut.trigger_in.value = 0
        if c == 44:
            dut.trigger_in.value = 1

    pkts = split_into_packets((await Capture(dut, ack_delay=4).run(100, drive)).beats)
    assert kcodes(pkts) == [K28_4, K28_2], [hex(k) for k in kcodes(pkts)]
    check_packet_shape(pkts[0], K28_4)
    check_packet_shape(pkts[1], K28_2)


# -----------------------------------------------------------------------------
# TC 4 — No Edge, No Packet
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_no_edge_no_packet(dut):
    """A steady de-asserted pin produces no output beats over a long window.

    Stimulus: `cfg_polarity = 0`, pin 0 through reset and held for a
              64-cycle capture.
    Checks:   the list of accepted beats is empty.
    """
    dut.TESTCASE.value = 4
    await reset(dut, cfg_polarity=0, initial_trig=0)
    quiet = await capture_accepted(dut, cycles=64)
    assert quiet == [], f"emitted {len(quiet)} beats with trigger_in steady"


# -----------------------------------------------------------------------------
# TC 5 — Pin Held Asserted Through Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_cfg_polarity_reset_level(dut):
    """A pin asserted through reset is not an edge; the next real one is.

    §8.3.2: the trigger is de-asserted at link discovery; §10.3.28: a
    connection reset sets the device trigger to 0.

    Stimulus: for `cfg_polarity` 0 and 1: reset with the pin asserted
              (1, resp. 0); 30 quiet cycles; pin de-asserted; 20 cycles;
              pin asserted; 40 cycles; acknowledgments 4 cycles after
              each EOP.
    Checks:   no packet while the pin stays asserted from reset, none for
              its release; one K28.4 for the assertion that follows.
    """
    dut.TESTCASE.value = 5
    for pol in (0, 1):
        on, off = 1 ^ pol, pol
        await reset(dut, cfg_polarity=pol, initial_trig=on)
        cap = Capture(dut, ack_delay=4)
        await cap.run(30)
        assert cap.beats == [], f"polarity {pol}: pin held through reset was sent"
        dut.trigger_in.value = off
        await cap.run(20)
        assert cap.beats == [], f"polarity {pol}: release sent a packet"
        dut.trigger_in.value = on
        await cap.run(40)
        pkts = split_into_packets(cap.beats)
        assert kcodes(pkts) == [K28_4], f"polarity {pol}: {[hex(k) for k in kcodes(pkts)]}"


# -----------------------------------------------------------------------------
# TC 6 — Back-Pressure Holds The Packet
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_backpressure_holds_packet(dut):
    """Back-pressure holds the HDR word stable; a later change follows.

    Stimulus: `m_ready = 0` from reset; pin 0→1, 8 edges; pin 1→0, 4
              edges; then `m_ready = 1` and a 40-cycle capture with the
              packets acknowledged 4 cycles after their EOP.
    Checks:   after the first 8 edges the DUT holds `m_valid`, `m_sop`,
              kmask 0xF and 4×K28.4; after 4 more edges the word is
              unchanged; the capture holds K28.4 then K28.2.
    """
    dut.TESTCASE.value = 6
    await reset(dut, ready=0, cfg_polarity=0, initial_trig=0)
    for _ in range(4):
        await RisingEdge(dut.tx_clk)
    dut.trigger_in.value = 1
    for _ in range(8):
        await RisingEdge(dut.tx_clk)
    assert int(dut.m_valid.value) == 1, "DUT must hold m_valid during stall"
    assert int(dut.m_sop.value)   == 1, "DUT must still be on HDR word"
    assert int(dut.m_kmask.value) == 0xF
    assert int(dut.m_data.value)  == rep4(K28_4)
    dut.trigger_in.value = 0
    for _ in range(4):
        await RisingEdge(dut.tx_clk)
    assert int(dut.m_data.value) == rep4(K28_4), "stalled HDR mutated"
    dut.m_ready.value = 1
    pkts = split_into_packets((await Capture(dut, ack_delay=4).run(40)).beats)
    assert kcodes(pkts) == [K28_4, K28_2], [hex(k) for k in kcodes(pkts)]
    check_packet_shape(pkts[0], K28_4)
    check_packet_shape(pkts[1], K28_2)


# -----------------------------------------------------------------------------
# TC 7 — One-Cycle Pulse
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_burst_two_edges(dut):
    """A one-cycle pulse is sent as K28.4, then K28.2 after the ack.

    §8.3.3: no new trigger packet before the acknowledgment.

    Stimulus: pin 1 for one cycle, then 0; the packet acknowledged 10
              cycles after its EOP; 60 cycles.
    Checks:   K28.4 then K28.2; the K28.2 packet starts after the ack.
    """
    dut.TESTCASE.value = 7
    await reset(dut, cfg_polarity=0, initial_trig=0)
    for _ in range(4):
        await RisingEdge(dut.tx_clk)

    def drive(c):
        dut.trigger_in.value = int(c == 0)

    cap = await Capture(dut, ack_delay=10).run(60, drive)
    pkts = split_into_packets(cap.beats)
    assert kcodes(pkts) == [K28_4, K28_2], [hex(k) for k in kcodes(pkts)]
    assert pkts[1][0].cycle > cap.acks[0], "second packet before the acknowledgment"


# -----------------------------------------------------------------------------
# TC 8 — Random Toggle Stream
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_random_toggle_stream(dut):
    """Random toggles: alternating packets, paced by the acks, level right.

    Stimulus: `random.Random(0xC0FFEE)`; 60 toggles 1–25 cycles apart;
              every packet acknowledged 0, 5 or 12 cycles after its EOP
              (three runs); 120 settle cycles.
    Checks:   packets alternate K28.4 / K28.2 starting with K28.4; no
              packet follows another without an ack between them; the
              last packet's level is the pin's.
    """
    dut.TESTCASE.value = 8
    rng = random.Random(0xC0FFEE)
    for delay in (0, 5, 12):
        await reset(dut, cfg_polarity=0, initial_trig=0)
        sched, c, val = {}, 4, 0
        for _ in range(60):
            c += rng.randint(1, 25)
            val ^= 1
            sched[c] = val

        def drive(cy):
            if cy in sched:
                dut.trigger_in.value = sched[cy]

        cap = await Capture(dut, ack_delay=delay).run(c + 120, drive)
        pkts = split_into_packets(cap.beats)
        want = [K28_4 if i % 2 == 0 else K28_2 for i in range(len(pkts))]
        assert kcodes(pkts) == want, f"delay {delay}: {[hex(k) for k in kcodes(pkts)]}"
        assert not unpaced(pkts, cap.acks), f"delay {delay}: {unpaced(pkts, cap.acks)[:3]}"
        assert kcodes(pkts)[-1] == (K28_4 if val else K28_2), \
            f"delay {delay}: host ends {'high' if kcodes(pkts)[-1] == K28_4 else 'low'}, pin {val}"


# -----------------------------------------------------------------------------
# TC 9 — Pending Overflow Keeps The Level
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_pending_overflow_level(dut):
    """Edges piling up behind a held packet leave the host at the pin level.

    §8.3.2: the host rebuilds the device's trigger level from the edges
    it receives; §8.3.3: one packet in flight until acknowledged.

    Stimulus: `m_ready = 0` from reset; `trigger_in` 0 -> 1, 1 -> 0,
              0 -> 1, 6 cycles apart; `m_ready = 1`; every packet
              acknowledged 20 cycles after its EOP; 300-cycle capture.
    Checks:   the last packet sent is a rising edge (K28.4): the host's
              level equals the pin (1).
    """
    dut.TESTCASE.value = 9
    await reset(dut, ready=0, cfg_polarity=0, initial_trig=0)
    for _ in range(4):
        await RisingEdge(dut.tx_clk)
    for v in (1, 0, 1):
        dut.trigger_in.value = v
        for _ in range(6):
            await RisingEdge(dut.tx_clk)
    dut.m_ready.value = 1
    pkts = split_into_packets((await Capture(dut, ack_delay=20).run(300)).beats)
    assert pkts, "no packet"
    last = pkts[-1][0].data & 0xFF
    assert last == K28_4, (
        f"packets {[hex(p[0].data & 0xFF) for p in pkts]}: host level ends "
        f"{'high' if last == K28_4 else 'low'}, pin is high"
    )


# -----------------------------------------------------------------------------
# TC 10 — Timeout Without Acknowledgment
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_timeout_without_ack(dut):
    """No acknowledgment: one packet per timeout, only for a new level.

    §8.3.3: without an acknowledgment for the defined time the device may
    send a new trigger packet.

    Stimulus: no acks; the pin toggled every 3 cycles for 400 cycles, then
              held 1; 3 × ACK_TIMEOUT settle cycles.
    Checks:   consecutive packets are at least ACK_TIMEOUT cycles apart
              (EOP to SOP); they alternate; the last is K28.4; nothing is
              sent in the settle time once the host has the pin's level.
    """
    dut.TESTCASE.value = 10
    await reset(dut, cfg_polarity=0, initial_trig=0)
    for _ in range(4):
        await RisingEdge(dut.tx_clk)

    def drive(c):
        if c < 400 and c % 3 == 0:
            dut.trigger_in.value = 1 - int(dut.trigger_in.value)
        if c == 400:
            dut.trigger_in.value = 1

    cap = await Capture(dut).run(400 + 3 * ACK_TIMEOUT, drive)
    pkts = split_into_packets(cap.beats)
    gaps = [b[0].cycle - a[-1].cycle for a, b in zip(pkts, pkts[1:])]
    assert gaps and min(gaps) >= ACK_TIMEOUT, f"gaps {gaps}"
    want = [K28_4 if i % 2 == 0 else K28_2 for i in range(len(pkts))]
    assert kcodes(pkts) == want and want[-1] == K28_4, [hex(k) for k in kcodes(pkts)]
    assert pkts[-1][-1].cycle < 400 + ACK_TIMEOUT + 8, "packet re-sent with the level unchanged"


# -----------------------------------------------------------------------------
# TC 11 — Link Gate
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_link_gate(dut):
    """Nothing goes out while the link is down; the level follows it up.

    §8.3.2: both sides de-assert the trigger at link discovery; with no
    host there is nobody to acknowledge a trigger.

    Stimulus: reset with `link_up = 0`, pin 0; pin 1, 0, 1 20 cycles
              apart; 100 cycles; `link_up = 1`; 40 cycles (acks after 4);
              `link_up = 0`, 20 cycles, `link_up = 1`, 40 cycles.
    Checks:   no packet before the link is up; then one K28.4; after the
              link drops and returns with the pin still asserted, one
              K28.4 again (the host restarted de-asserted).
    """
    dut.TESTCASE.value = 11
    await reset(dut, cfg_polarity=0, initial_trig=0, link=0)
    cap = Capture(dut, ack_delay=4)
    for v in (1, 0, 1):
        dut.trigger_in.value = v
        await cap.run(20)
    await cap.run(100)
    assert cap.beats == [], "packet sent with the link down"
    dut.link_up.value = 1
    await cap.run(40)
    assert kcodes(split_into_packets(cap.beats)) == [K28_4]
    dut.link_up.value = 0
    await cap.run(20)
    dut.link_up.value = 1
    await cap.run(40)
    assert kcodes(split_into_packets(cap.beats)) == [K28_4, K28_4]


# -----------------------------------------------------------------------------
# TC 12 — ConnectionReset Mask
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_mask_deasserts(dut):
    """The ConnectionReset mask de-asserts the trigger, once.

    §10.3.28: a connection reset sets the device trigger to 0; §8.3.2:
    the host's level follows.

    Stimulus: pin 1 (K28.4 sent, acked); `mask` for 30 cycles with the pin
              held; 60 cycles; pin 0 for 10 cycles, then 1; 40 cycles.
              Acks 4 cycles after each EOP.
    Checks:   K28.4, then one K28.2 for the mask, nothing when the mask
              ends with the pin held, nothing for its release, K28.4 for
              the next assertion.
    """
    dut.TESTCASE.value = 12
    await reset(dut, cfg_polarity=0, initial_trig=0)
    cap = Capture(dut, ack_delay=4)
    await cap.run(4)
    dut.trigger_in.value = 1
    await cap.run(30)
    dut.mask.value = 1
    await cap.run(30)
    dut.mask.value = 0
    await cap.run(60)
    assert kcodes(split_into_packets(cap.beats)) == [K28_4, K28_2]
    dut.trigger_in.value = 0
    await cap.run(10)
    dut.trigger_in.value = 1
    await cap.run(40)
    assert kcodes(split_into_packets(cap.beats)) == [K28_4, K28_2, K28_4]


# -----------------------------------------------------------------------------
# TC 13 — Pin To SOP Latency
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_edge_to_sop_latency(dut):
    """The leader is offered 3 cycles after the pin changes.

    Two synchroniser flops, then one cycle for the packet to start.

    Stimulus: pin 0 → 1 written after an edge; count edges until
              `m_valid & m_sop`.
    Checks:   exactly 4 edges: the write lands on the first, then the two
              flops and the start.
    """
    dut.TESTCASE.value = 13
    await reset(dut, cfg_polarity=0, initial_trig=0)
    for _ in range(4):
        await RisingEdge(dut.tx_clk)
    dut.trigger_in.value = 1
    n = 0
    while True:
        await RisingEdge(dut.tx_clk)
        n += 1
        if int(dut.m_valid.value) and int(dut.m_sop.value):
            break
        assert n < 10, "no leader"
    assert n == 4, f"leader offered {n} edges after the write"
