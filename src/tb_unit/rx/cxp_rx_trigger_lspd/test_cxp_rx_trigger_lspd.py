"""Cocotb TB for `cxp_rx_trigger_lspd`.

Low-speed trigger receiver, CoaXPress 1.1.1 (CXP-001-2015 §8.3.2, §8.3.2.1
Figure 20, Table 15). The TB stands in for `cxp_rx_lspd_sampler`: it
presents one `trig_valid` strobe with the edge and the three Delay
characters as 10-bit symbols (encoded by the golden `cxp_8b10b` with the
running disparity chained, so a non-neutral Delay alternates forms as on
the wire), plus `cfg_polarity`, on a 10 ns `rx_clk`.  The wrapper sets
`OS_RATIO` = 8, so one Delay unit (1/24 bit) is 1/3 cycle and the wait for
Delay D is floor((8 D + 12) / 24) cycles.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Reset → outputs low.
  2  Rising edge, Delay 0, polarity 0 → app pulse, one `trig_ok`.
  3  Falling edge, polarity 1 → app pulse.
  4  Polarity mismatch → no app pulse, but `trig_ok` (the ack is owed).
  5  Delay 0, 3, 24, 120, 147, 239 → latency minus the Delay-0 latency is
     exactly floor((8 D + 12) / 24) cycles.
  6  Three different Delay characters → glitch, no `trig_ok`, no pulse.
  7  One Delay character with a bit error (Delay 147, forms alternate) →
     the latency of the clean packet, `trig_ok`, no glitch.
  8  Delay 240 and 255 → glitch.
  9  Delay 235 (D11.7: P7 / A7 / P7 on the wire) → pulse; three K28.5 as
     Delay → glitch.
 10  A second event while the first is armed replaces it: one pulse, at
     the second event's wait.
 11  An event on the cycle the armed one fires: both pulse.
 12  A rising packet re-sent (the level does not change): acknowledged,
     no second pulse; the falling packet after it changes the level.
 13  ConnectionReset (`deassert`) with the host left asserted: the falling
     edge reaches the application at polarity 1, not at 0, and a countdown
     still running is dropped; left de-asserted: nothing.

A rising packet between rising packets would be a resend (test 12): the
tests that need several rising events put a falling one between them,
which changes only the level at polarity 0.
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

import cxp_8b10b as cxp
from cxp_testcase import cxp_test


CLK      = 10
OS_RATIO = 8      # tb_cxp_rx_trigger_lspd_top
K28_5    = cxp.K28_5


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def reset(dut):
    """Start the clock, zero the inputs, hold `rx_rst_n` low 4 cycles."""
    cocotb.start_soon(Clock(dut.rx_clk, CLK, unit="ns").start())
    dut.rx_rst_n.value     = 0
    dut.cfg_polarity.value = 0
    dut.deassert.value     = 0
    dut.trig_edge.value    = 0
    dut.trig_dly.value     = 0
    dut.trig_valid.value   = 0
    for _ in range(4):
        await RisingEdge(dut.rx_clk)
    dut.rx_rst_n.value = 1
    await RisingEdge(dut.rx_clk)


# -----------------------------------------------------------------------------
# Stimulus helpers
# -----------------------------------------------------------------------------
def dly_chars(values, k: bool = False, rd: int = 0, flip: dict | None = None) -> int:
    """Three Delay characters as 10b symbols, RD chained from `rd`, first
    in bits [9:0]; `flip` = {index: bit} inverts one bit of a symbol."""
    word = 0
    for i, v in enumerate(values):
        sym, rd = cxp.encode_byte(v, k, rd)
        if flip and i in flip:
            sym ^= 1 << flip[i]
        word |= sym << (10 * i)
    return word


def delay_for(wait: int) -> int:
    """Delay giving a wait of `wait` cycles at OS_RATIO 8 (24 / 8 units)."""
    return wait * 24 // OS_RATIO


def wait_of(delay: int) -> int:
    """Cycles counted for Delay `delay` (rounded Delay x OS_RATIO / 24)."""
    return (delay * OS_RATIO + 12) // 24


async def drive_event(dut, edge: int, delay: int = 0, polarity: int = 0,
                      wait: int | None = None, chars: int | None = None):
    """Present one trigger packet for a single `rx_clk` edge.

    `delay` (or `wait` in cycles) is sent as three identical Delay
    characters unless `chars` gives the 30 bits."""
    if wait is not None:
        delay = delay_for(wait)
    dut.cfg_polarity.value = polarity
    dut.trig_edge.value    = edge
    dut.trig_dly.value     = dly_chars([delay] * 3) if chars is None else chars
    dut.trig_valid.value   = 1
    await RisingEdge(dut.rx_clk)
    dut.trig_valid.value   = 0


async def rearm(dut, polarity: int = 0):
    """A falling packet (the host de-asserts), so the next rising packet
    is a new edge rather than a resend; one edge, then one edge of gap."""
    await drive_event(dut, edge=0b10, polarity=polarity)
    await RisingEdge(dut.rx_clk)


class Seen:
    """Pulses on the three outputs over a window."""

    def __init__(self):
        self.app = self.glitch = self.ok = 0


async def watch(dut, n: int) -> Seen:
    """Sample from the edge that took the packet (post-edge values) on."""
    s = Seen()
    for _ in range(n):
        await ReadOnly()
        s.app += int(dut.trigger_out_app.value)
        s.glitch += int(dut.trigger_glitch_pulse.value)
        s.ok += int(dut.trig_ok.value)
        await RisingEdge(dut.rx_clk)
    return s


async def wait_for_app_pulse(dut, timeout: int = 300) -> int:
    """Return the edge index at which `trigger_out_app` reads 1, else -1."""
    for n in range(timeout):
        await RisingEdge(dut.rx_clk)
        if int(dut.trigger_out_app.value):
            return n
    return -1


# -----------------------------------------------------------------------------
# TC 1 — Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_reset(dut):
    """All outputs are low after reset.

    Stimulus: reset sequence only.
    Checks:   `trigger_out_app`, `trigger_glitch_pulse`, `trig_ok` are 0.
    """
    dut.TESTCASE.value = 1
    await reset(dut)
    assert int(dut.trigger_out_app.value) == 0
    assert int(dut.trigger_glitch_pulse.value) == 0
    assert int(dut.trig_ok.value) == 0


# -----------------------------------------------------------------------------
# TC 2 — Rising Edge, Delay 0
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_rising_zero_delay(dut):
    """A rising trigger with Delay 0 and matching polarity fires at once.

    Stimulus: polarity 0, edge 01, Delay 0 ×3.
    Checks:   one app pulse, one `trig_ok`, no glitch within 20 edges.
    """
    dut.TESTCASE.value = 2
    await reset(dut)
    await drive_event(dut, edge=0b01, delay=0)
    s = await watch(dut, 20)
    assert (s.app, s.ok, s.glitch) == (1, 1, 0), vars(s)


# -----------------------------------------------------------------------------
# TC 3 — Falling Edge, Polarity Match
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_falling_pol_match(dut):
    """A falling trigger fires when `cfg_polarity` selects falling edges.

    Stimulus: polarity 1: rising (asserts the host trigger; not the
              selected edge), then falling, Delay 0.
    Checks:   one app pulse within 20 edges of the falling packet.
    """
    dut.TESTCASE.value = 3
    await reset(dut)
    await drive_event(dut, edge=0b01, delay=0, polarity=1)    # assert first
    await watch(dut, 20)
    await drive_event(dut, edge=0b10, delay=0, polarity=1)
    s = await watch(dut, 20)
    assert (s.app, s.ok) == (1, 1), vars(s)


# -----------------------------------------------------------------------------
# TC 4 — Polarity Mismatch
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_polarity_mismatch(dut):
    """The unselected edge reaches no application pulse but is acknowledged.

    §8.3.3: every trigger packet is acknowledged; the polarity only picks
    which edge the application sees.

    Stimulus: polarity 0 (rising), edge 10 (falling), Delay 0.
    Checks:   no app pulse, one `trig_ok`, no glitch within 40 edges.
    """
    dut.TESTCASE.value = 4
    await reset(dut)
    await drive_event(dut, edge=0b10, delay=0, polarity=0)
    s = await watch(dut, 40)
    assert (s.app, s.ok, s.glitch) == (0, 1, 0), vars(s)


# -----------------------------------------------------------------------------
# TC 5 — Delay Countdown
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_delay_countdown(dut):
    """The wait is Delay units of 1/24 bit (§8.3.2.1, Figure 20).

    Stimulus: rising events, polarity 0, Delay 0, 3, 24, 120, 147, 239,
              each after the previous pulse.
    Checks:   latency(D) - latency(0) == floor((8 D + 12) / 24) cycles
              exactly (0, 1, 8, 40, 49, 80).
    """
    dut.TESTCASE.value = 5
    await reset(dut)
    lat = {}
    for i, d in enumerate([0, 3, 24, 120, 147, 239]):
        if i:
            await rearm(dut)
        await drive_event(dut, edge=0b01, delay=d)
        lat[d] = await wait_for_app_pulse(dut)
        assert lat[d] >= 0, f"Delay {d}: no pulse"
        for _ in range(4):
            await RisingEdge(dut.rx_clk)
    got = {d: n - lat[0] for d, n in lat.items()}
    exp = {d: wait_of(d) for d in lat}
    assert got == exp, f"waits {got}, expected {exp}"


# -----------------------------------------------------------------------------
# TC 6 — No Two Delay Characters Alike
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_delay_no_majority(dut):
    """Three different Delay characters cannot be voted: a glitch.

    Stimulus: rising, polarity 0, Delay characters 10, 20, 30.
    Checks:   one glitch pulse; no app pulse, no `trig_ok` within 40 edges.
    """
    dut.TESTCASE.value = 6
    await reset(dut)
    await drive_event(dut, edge=0b01, chars=dly_chars([10, 20, 30]))
    s = await watch(dut, 40)
    assert (s.app, s.ok, s.glitch) == (0, 0, 1), vars(s)


# -----------------------------------------------------------------------------
# TC 7 — One Bad Delay Character
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_one_bad_delay_char(dut):
    """One Delay character hit by a bit error is out-voted (§8.2.2).

    Delay 147 (D19.4) has a non-neutral 3b/4b block, so its three copies
    alternate RD forms on the wire: the two good copies differ as symbols
    and agree only once decoded.

    Stimulus: rising, polarity 0: Delay 147 clean (latency L measured);
              then Delay 147 with bit 1 of the first character inverted;
              then with bit 7 of the last character inverted.
    Checks:   each faulty packet pulses at L, one `trig_ok`, no glitch.
    """
    dut.TESTCASE.value = 7
    await reset(dut)
    await drive_event(dut, edge=0b01, delay=147)
    lat = await wait_for_app_pulse(dut)
    assert lat >= 0
    for flip in ({0: 1}, {2: 7}):
        for _ in range(8):
            await RisingEdge(dut.rx_clk)
        await rearm(dut)
        await drive_event(dut, edge=0b01, chars=dly_chars([147] * 3, flip=flip))
        n = await wait_for_app_pulse(dut)
        assert n == lat, f"flip {flip}: pulse at {n}, clean at {lat}"


# -----------------------------------------------------------------------------
# TC 8 — Delay Out Of Range
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_delay_out_of_range(dut):
    """A Delay above 239 is not a Table 15 value: a glitch.

    Stimulus: rising, polarity 0, Delay 240; then Delay 255.
    Checks:   each: one glitch pulse, no app pulse, no `trig_ok`.
    """
    dut.TESTCASE.value = 8
    await reset(dut)
    for d in (240, 255):
        await drive_event(dut, edge=0b01, delay=d)
        s = await watch(dut, 40)
        assert (s.app, s.ok, s.glitch) == (0, 0, 1), (d, vars(s))


# -----------------------------------------------------------------------------
# TC 9 — Delay Character Forms
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_delay_char_forms(dut):
    """Every wire form of a data Delay decodes; a K-character does not.

    Delay 235 is D11.7: P7 at RD-, the alternate A7 form at RD+ (IEEE
    802.3 Table 36-1), so the three copies go P7 / A7 / P7.

    Stimulus: rising, polarity 0: Delay 235 from RD-; Delay 235 from RD+;
              three K28.5 as the Delay characters.
    Checks:   235: app pulse at wait_of(235) after the Delay-0 latency,
              `trig_ok`; K28.5: glitch, no app pulse, no `trig_ok`.
    """
    dut.TESTCASE.value = 9
    await reset(dut)
    await drive_event(dut, edge=0b01, delay=0)
    lat0 = await wait_for_app_pulse(dut)
    for rd in (0, 1):
        for _ in range(8):
            await RisingEdge(dut.rx_clk)
        await rearm(dut)
        await drive_event(dut, edge=0b01, chars=dly_chars([235] * 3, rd=rd))
        n = await wait_for_app_pulse(dut)
        assert n - lat0 == wait_of(235), f"RD{rd}: wait {n - lat0}"
    for _ in range(8):
        await RisingEdge(dut.rx_clk)
    await drive_event(dut, edge=0b01, chars=dly_chars([K28_5] * 3, k=True))
    s = await watch(dut, 40)
    assert (s.app, s.ok, s.glitch) == (0, 0, 1), vars(s)


async def pulses(dut, n_edges: int) -> list[int]:
    """Edge indices (from now) at which `trigger_out_app` reads 1."""
    out = []
    for n in range(n_edges):
        await RisingEdge(dut.rx_clk)
        if int(dut.trigger_out_app.value):
            out.append(n)
    return out


# -----------------------------------------------------------------------------
# TC 10 — Retrigger While Armed
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_retrigger_while_armed(dut):
    """A new event overwrites the one still counting down.

    The module promises that a later event reloads the countdown; the
    decrement of the armed event must not win over the load.

    Stimulus: polarity 0; a lone rising event with a 3-cycle wait (Delay
              9, its latency L measured); then event A with a 20-cycle
              wait and, 5 edges later, event B with the 3-cycle wait.
    Checks:   exactly one app pulse after A, and it comes L edges after B
              (±1).
    """
    dut.TESTCASE.value = 10
    await reset(dut)
    await drive_event(dut, edge=0b01, wait=3)
    lat = await wait_for_app_pulse(dut)
    assert lat >= 0
    for _ in range(40):
        await RisingEdge(dut.rx_clk)
    await rearm(dut)
    await drive_event(dut, edge=0b01, wait=20)
    # A falling packet between A and B (a real new edge, not a resend).
    await RisingEdge(dut.rx_clk)
    await rearm(dut)
    await RisingEdge(dut.rx_clk)
    await drive_event(dut, edge=0b01, wait=3)
    seen = await pulses(dut, 60)
    assert len(seen) == 1 and abs(seen[0] - lat) <= 1, (
        f"pulses at {seen} after B, expected one at {lat}")


# -----------------------------------------------------------------------------
# TC 11 — Event On The Fire Cycle
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_event_on_fire_cycle(dut):
    """The cycle that fires one event can arm the next.

    Stimulus: polarity 0; a lone event with a 6-cycle wait (latency L
              measured); then event A with that wait and event B with a
              2-cycle wait presented so that B is sampled on the edge that
              fires A.
    Checks:   two app pulses.
    """
    dut.TESTCASE.value = 11
    await reset(dut)
    await drive_event(dut, edge=0b01, wait=6)
    # Edges from the event's sampling edge to the edge that raises the
    # output (post-edge values, so B can be sampled on that same edge).
    lat = 0
    while True:
        await RisingEdge(dut.rx_clk)
        lat += 1
        await ReadOnly()
        if int(dut.trigger_out_app.value):
            break
        assert lat < 300
    for _ in range(40):
        await RisingEdge(dut.rx_clk)
    count = {"n": 0}

    async def watch():
        while True:
            await RisingEdge(dut.rx_clk)
            await ReadOnly()
            count["n"] += int(dut.trigger_out_app.value)

    w = cocotb.start_soon(watch())
    await rearm(dut)
    await drive_event(dut, edge=0b01, wait=6)
    # lat - 1 edges to B, a falling packet among them.
    assert lat - 1 >= 3
    await RisingEdge(dut.rx_clk)
    await rearm(dut)
    for _ in range(lat - 1 - 3):
        await RisingEdge(dut.rx_clk)
    await drive_event(dut, edge=0b01, wait=2)
    for _ in range(60):
        await RisingEdge(dut.rx_clk)
    w.cancel()
    assert count["n"] == 2, f"{count['n']} app pulses for A and B"



# -----------------------------------------------------------------------------
# TC 12 — A Re-Sent Packet Is One Event
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_resent_packet(dut):
    """A trigger packet re-sent after a lost acknowledgment is the same edge.

    §8.3.3: a transmitter with no acknowledgment within its timeout "can
    resend the last trigger packet"; the host's trigger is a level, so a
    second rising packet with no falling one between does not assert it
    again.  Every packet is still acknowledged.

    Stimulus: polarity 0: rising (Delay 0); after its pulse the same rising
              packet twice more; then falling; then rising.
    Checks:   the three rising packets give one pulse and three `trig_ok`;
              the rising packet after the falling one pulses again.
    """
    dut.TESTCASE.value = 12
    await reset(dut)
    await drive_event(dut, edge=0b01)
    s1 = await watch(dut, 30)
    await drive_event(dut, edge=0b01)
    s2 = await watch(dut, 30)
    await drive_event(dut, edge=0b01)
    s3 = await watch(dut, 30)
    assert (s1.app, s2.app, s3.app) == (1, 0, 0), (vars(s1), vars(s2), vars(s3))
    assert (s1.ok, s2.ok, s3.ok) == (1, 1, 1), "every packet is acknowledged"
    await rearm(dut)
    await drive_event(dut, edge=0b01)
    s4 = await watch(dut, 30)
    assert s4.app == 1, f"rising after falling: {vars(s4)}"


# -----------------------------------------------------------------------------
# TC 13 — ConnectionReset De-Asserts The Trigger
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_connection_reset_deasserts(dut):
    """Link discovery de-asserts the host's trigger (§8.3.2: "the effect of
    a falling edge trigger packet").

    Stimulus: (a) polarity 1: rising (asserts; not the selected edge), then
              `deassert` for 4 cycles; (b) polarity 1, trigger already
              de-asserted: `deassert` again; (c) polarity 0: rising with a
              60-cycle wait, `deassert` 10 cycles later; (d) polarity 0:
              rising then falling (de-asserted), `deassert`.
    Checks:   (a) one app pulse from the reset; (b) none; (c) the countdown
              is dropped: no pulse; (d) none.
    """
    dut.TESTCASE.value = 13
    await reset(dut)

    async def reset_window():
        dut.deassert.value = 1
        s = await watch(dut, 4)
        dut.deassert.value = 0
        t = await watch(dut, 80)
        return s.app + t.app

    await drive_event(dut, edge=0b01, polarity=1)
    assert (await watch(dut, 20)).app == 0
    assert await reset_window() == 1, "(a) no falling edge for a host left asserted"
    assert await reset_window() == 0, "(b) a falling edge for a de-asserted host"
    await drive_event(dut, edge=0b01, polarity=0, wait=60)
    for _ in range(10):
        await RisingEdge(dut.rx_clk)
    assert await reset_window() == 0, "(c) the countdown fired through the reset"
    await drive_event(dut, edge=0b01, polarity=0)
    await watch(dut, 20)
    await rearm(dut)
    assert await reset_window() == 0, "(d) a falling edge for a de-asserted host"
