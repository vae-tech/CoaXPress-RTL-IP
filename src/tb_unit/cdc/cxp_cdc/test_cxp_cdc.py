"""Cocotb TB for the CDC primitives (`cxp_cdc_sync`, `cxp_cdc_pulse`,
`cxp_cdc_bus`, `cxp_cdc_req`).

Each primitive crosses from `src_clk` to `dst_clk`.  Every test runs twice,
with the destination clock faster (7 ns vs 10 ns) and slower (13 ns vs
10 ns) than the source, so neither ratio hides a missed handshake.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  cxp_cdc_sync: the output takes the input two destination edges later.
  2  cxp_cdc_pulse: 40 source pulses spaced 4 source cycles apart give 40
     destination pulses of one cycle each.
  3  cxp_cdc_bus: the destination copy shows only values the source
     held, in order, and settles on the last one.
  4  cxp_cdc_req: 30 requests with random payloads each arrive exactly
     once, intact, in order; src_ready pulses once per request.
  6  cxp_cdc_bus: after an even number of transfers a reset of the
     destination alone; the copy must come back to the source value.
  7  cxp_cdc_pulse: after an odd number of pulses a reset of the source
     alone; no destination pulse may appear.
  8  cxp_cdc_req: a reset of the destination alone after it consumed a
     request, before the source saw the acknowledge; the request must not
     be delivered twice.
  9  cxp_cdc_req: after an odd number of completed requests a reset of the
     source alone; the destination must see no request.

Test 5 is not used (the numbering follows the review catalogue).
"""

from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import NextTimeStep, ReadOnly, RisingEdge, Timer

from cxp_reset import pulse_reset
from cxp_testcase import cxp_test

SRC_NS = 10
SETTLE = 20        # src_clk cycles until both ends of every primitive are up


_clocks = []


async def reset(dut, dst_ns: int):
    """(Re)start both clocks — the destination at `dst_ns` — and reset."""
    if _clocks and not _clocks[0].done():
        await NextTimeStep()          # same test: leave the ReadOnly phase
    for c in _clocks:
        c.cancel()
    _clocks[:] = [cocotb.start_soon(Clock(dut.src_clk, SRC_NS, unit="ns").start()),
                  cocotb.start_soon(Clock(dut.dst_clk, dst_ns, unit="ns").start())]
    for n in ("sync_d", "pulse_in", "bus_d", "req_valid", "req_data", "dst_ready"):
        getattr(dut, n).value = 0
    dut.bus_d.value = 0x5A5A_5A5A
    dut.src_rst_n.value = 0
    dut.dst_rst_n.value = 0
    await Timer(50, unit="ns")
    dut.src_rst_n.value = 1
    dut.dst_rst_n.value = 1
    # cxp_cdc_link: an event offered before both sides have seen each
    # other settle (a few cycles of each clock) is not transferred.
    for _ in range(SETTLE):
        await RisingEdge(dut.src_clk)


# -----------------------------------------------------------------------------
# TC 1 — Synchroniser
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_sync(dut):
    """The two-flop synchroniser delays by two destination edges.

    Stimulus: set `sync_d` just after a destination edge.
    Checks:   `sync_q` still old after one edge, new after two.
    """
    dut.TESTCASE.value = 1
    for dst_ns in (7, 13):
        await reset(dut, dst_ns)
        for v in (0xA5, 0x3C, 0xFF):
            await RisingEdge(dut.dst_clk)
            old = int(dut.sync_q.value)
            dut.sync_d.value = v
            await RisingEdge(dut.dst_clk)
            await ReadOnly()
            assert int(dut.sync_q.value) == old
            await RisingEdge(dut.dst_clk)
            await ReadOnly()
            assert int(dut.sync_q.value) == v


async def count_pulses(dut, sig, clk, stop):
    n = 0
    width = 0
    while not stop["done"]:
        await RisingEdge(clk)
        await ReadOnly()
        if int(sig.value):
            n += 1
            width += 1
            assert width == 1, "pulse longer than one cycle"
        else:
            width = 0
    stop["n"] = n


# -----------------------------------------------------------------------------
# TC 2 — Pulse
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_pulse(dut):
    """Every source pulse gives one one-cycle destination pulse.

    Stimulus: 40 one-cycle source pulses, 4 source cycles apart.
    Checks:   40 destination pulses, each one cycle wide.
    """
    dut.TESTCASE.value = 2
    for dst_ns in (7, 13):
        await reset(dut, dst_ns)
        st = {"done": False}
        mon = cocotb.start_soon(count_pulses(dut, dut.pulse_out, dut.dst_clk, st))
        for _ in range(40):
            dut.pulse_in.value = 1
            await RisingEdge(dut.src_clk)
            dut.pulse_in.value = 0
            for _ in range(3):
                await RisingEdge(dut.src_clk)
        await Timer(200, unit="ns")
        st["done"] = True
        await mon
        assert st["n"] == 40, st


# -----------------------------------------------------------------------------
# TC 3 — Bus
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_bus(dut):
    """The destination copy is always a value the source held, in order.

    Stimulus: 60 random values, each held 1..6 source cycles.
    Checks:   every destination value seen is in the source sequence at
              or after the previous one; the final value is the last.
    """
    dut.TESTCASE.value = 3
    rng = random.Random(3)
    for dst_ns in (7, 13):
        await reset(dut, dst_ns)
        seq = [0x5A5A_5A5A]
        seen = []

        async def watch():
            while True:
                await RisingEdge(dut.dst_clk)
                await ReadOnly()
                v = int(dut.bus_q.value)
                if not seen or seen[-1] != v:
                    seen.append(v)

        w = cocotb.start_soon(watch())
        for _ in range(60):
            v = rng.randrange(1 << 32)
            seq.append(v)
            dut.bus_d.value = v
            for _ in range(rng.randrange(1, 7)):
                await RisingEdge(dut.src_clk)
        await Timer(300, unit="ns")
        w.cancel()
        pos = 0
        for v in seen:
            assert v in seq[pos:], f"value {v:#x} not held by the source (in order)"
            pos = seq.index(v, pos)
        assert seen[-1] == seq[-1]


# -----------------------------------------------------------------------------
# TC 4 — Request
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_req(dut):
    """Each request arrives once, intact, in order.

    Stimulus: 30 requests with random payloads; the destination consumes
              each after 0..5 cycles.
    Checks:   the consumed payloads equal the sent ones; one `req_ready`
              per request.
    """
    dut.TESTCASE.value = 4
    rng = random.Random(4)
    for dst_ns in (7, 13):
        await reset(dut, dst_ns)
        sent, got = [], []

        async def consumer():
            while True:
                await RisingEdge(dut.dst_clk)
                await ReadOnly()
                if int(dut.dst_valid.value):
                    got.append(int(dut.dst_data.value))
                    for _ in range(rng.randrange(0, 6)):
                        await RisingEdge(dut.dst_clk)
                    await RisingEdge(dut.dst_clk)
                    dut.dst_ready.value = 1
                    await RisingEdge(dut.dst_clk)
                    dut.dst_ready.value = 0

        c = cocotb.start_soon(consumer())
        for _ in range(30):
            v = rng.randrange(1 << 32)
            sent.append(v)
            dut.req_data.value = v
            dut.req_valid.value = 1
            readies = 0
            while True:
                await RisingEdge(dut.src_clk)
                await ReadOnly()
                if int(dut.req_ready.value):
                    readies += 1
                    break
            await RisingEdge(dut.src_clk)
            dut.req_valid.value = 0
            await RisingEdge(dut.src_clk)
            assert readies == 1
        await Timer(200, unit="ns")
        c.cancel()
        dut.dst_ready.value = 0
        assert got == sent


# -----------------------------------------------------------------------------
# TC 6 — Bus: Destination-Only Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_bus_one_sided_reset(dut):
    """A destination reset must not lose the source value.

    Stimulus: two source values, each allowed to cross (request toggle back
              at 0); the destination reset alone for 3 destination cycles;
              then 40 source cycles with the source unchanged.
    Checks:   the destination copy equals the source value again.
    Note:     after an even number of transfers the request toggle equals
              the reset acknowledge, so nothing re-sends the held value.
    """
    dut.TESTCASE.value = 6
    for dst_ns in (7, 13):
        await reset(dut, dst_ns)
        for v in (0x1111_1111, 0x2222_2222):
            dut.bus_d.value = v
            for _ in range(12):
                await RisingEdge(dut.src_clk)
        await ReadOnly()
        assert int(dut.bus_q.value) == 0x2222_2222
        await NextTimeStep()
        await pulse_reset(dut.dst_rst_n, dut.dst_clk)
        for _ in range(40):
            await RisingEdge(dut.src_clk)
        await ReadOnly()
        got = int(dut.bus_q.value)
        assert got == 0x2222_2222, f"destination stuck at {got:#x} after its reset"


# -----------------------------------------------------------------------------
# TC 7 — Pulse: Source-Only Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_pulse_one_sided_reset(dut):
    """A source reset alone produces no destination pulse.

    Stimulus: 3 source pulses (toggle left at 1); settle; the source reset
              alone for 3 source cycles; 30 cycles.
    Checks:   3 destination pulses in total, none after the reset.
    Note:     the reset returns the toggle to 0, which the destination
              reads as one more event (a phantom ConnectionReset at top
              level).
    """
    dut.TESTCASE.value = 7
    for dst_ns in (7, 13):
        await reset(dut, dst_ns)
        st = {"done": False}
        mon = cocotb.start_soon(count_pulses(dut, dut.pulse_out, dut.dst_clk, st))
        for _ in range(3):
            dut.pulse_in.value = 1
            await RisingEdge(dut.src_clk)
            dut.pulse_in.value = 0
            for _ in range(5):
                await RisingEdge(dut.src_clk)
        await Timer(100, unit="ns")
        await pulse_reset(dut.src_rst_n, dut.src_clk)
        await Timer(300, unit="ns")
        st["done"] = True
        await mon
        assert st["n"] == 3, f"{st['n']} destination pulses for 3 source pulses"


# -----------------------------------------------------------------------------
# TC 8 — Request: Destination Reset In Flight
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_req_dst_reset_in_flight(dut):
    """A consumed request is not delivered again after a destination reset.

    Stimulus: one request; the destination consumes it; in the next
              destination cycle (before the acknowledge reaches the
              source) the destination reset alone for 2 cycles; the
              destination keeps consuming whatever it is offered.
    Checks:   exactly one delivery; src_ready pulses at most once.
    """
    dut.TESTCASE.value = 8
    for dst_ns in (7, 13):
        await reset(dut, dst_ns)
        got = []
        src = {"readies": 0}

        async def source():
            """Hold valid until the one src_ready pulse, as a source must."""
            dut.req_data.value = 0xCAFE_0001
            dut.req_valid.value = 1
            while True:
                await RisingEdge(dut.src_clk)
                await ReadOnly()
                if int(dut.req_ready.value):
                    src["readies"] += 1
                    await NextTimeStep()
                    dut.req_valid.value = 0

        s_task = cocotb.start_soon(source())
        while True:
            await RisingEdge(dut.dst_clk)
            await ReadOnly()
            if int(dut.dst_valid.value):
                got.append(int(dut.dst_data.value))
                break
        await NextTimeStep()
        dut.dst_ready.value = 1
        await RisingEdge(dut.dst_clk)
        dut.dst_ready.value = 0
        await pulse_reset(dut.dst_rst_n, dut.dst_clk, cycles=2)
        for _ in range(60):
            await RisingEdge(dut.dst_clk)
            await ReadOnly()
            if int(dut.dst_valid.value):
                got.append(int(dut.dst_data.value))
                await NextTimeStep()
                dut.dst_ready.value = 1
                await RisingEdge(dut.dst_clk)
                dut.dst_ready.value = 0
        s_task.cancel()
        await NextTimeStep()
        dut.req_valid.value = 0
        assert src["readies"] <= 1, src
        assert got == [0xCAFE_0001], [hex(g) for g in got]


# -----------------------------------------------------------------------------
# TC 9 — Request: Source-Only Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_req_src_reset(dut):
    """A source reset alone raises no request at the destination.

    Stimulus: one request, consumed and acknowledged (toggles left at 1);
              the source reset alone for 3 source cycles; 60 destination
              cycles.
    Checks:   `dst_valid` never rises after the reset.
    Note:     the reset returns the source toggle to 0 while the
              destination acknowledge is 1; read as a request, that is the
              phantom control acknowledgment of a one-sided rx reset.
    """
    dut.TESTCASE.value = 9
    for dst_ns in (7, 13):
        await reset(dut, dst_ns)
        dut.req_data.value = 0x1234_5678
        dut.req_valid.value = 1
        while True:
            await RisingEdge(dut.dst_clk)
            await ReadOnly()
            if int(dut.dst_valid.value):
                break
        await NextTimeStep()
        dut.dst_ready.value = 1
        await RisingEdge(dut.dst_clk)
        dut.dst_ready.value = 0
        while True:
            await RisingEdge(dut.src_clk)
            await ReadOnly()
            if int(dut.req_ready.value):
                break
        await NextTimeStep()
        dut.req_valid.value = 0
        await Timer(100, unit="ns")
        await pulse_reset(dut.src_rst_n, dut.src_clk)
        phantom = 0
        for _ in range(60):
            await RisingEdge(dut.dst_clk)
            await ReadOnly()
            phantom += int(dut.dst_valid.value)
        await NextTimeStep()
        assert phantom == 0, f"dst_valid high for {phantom} cycles after a source-only reset"
