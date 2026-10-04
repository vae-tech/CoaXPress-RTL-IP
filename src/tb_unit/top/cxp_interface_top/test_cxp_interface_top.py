"""Cocotb integration TB for `cxp_interface_top`.

The DUT is the device-side integration top: pixel source mux (TPG or
sensor bus), stream framing, LS uplink → APB master, and a TX arbiter that
merges trigger, I/O-ack, control-ack, link-test and stream packets plus
IDLE into one raw 32-bit word + kmask per `tx_clk` (`cxp_if_data_o` /
`cxp_if_kmask_o`). 8B/10B line coding is outside the IP, so Python samples
that pair directly and checks framing.

The wrapper ties all clocks/resets to one 10 ns `clk` / `rst_n`, pins the
TPG to 8 × 4 px and the FIFO to 256 words, and hosts a real
`cxp_ctrl_bootstrap_regs` behind an inline APB3 bridge with the `lrst_clr_*`
strobes looped back into it. The uplink is driven only by the trigger
tests (`uplink`: a host at 16x oversampling, because the device sends
triggers only to a detected host); elsewhere `rx_serial` is held 1 and
the APB bridge carries no traffic.  Python drives `link_reset_req` and
`trigger_in_app` directly and backdoor-writes regfile rows
(`reg_q[row_test_mode]` …) via Verilator public-flat-rw.  The device
trigger's acknowledgment timeout is 64 cycles here. `sample_wire` reads the wire at ReadOnly before advancing,
so a SOP on the cycle right after a stimulus edge is not skipped.

Test numbers are not contiguous (1–4, 8–13, 15–20); the existing names and
numbers are kept because docs cite them. No FSM is registered for
coverage.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Post-reset wire carries only IDLE words (K28.5 K28.1 K28.1 D21.5).
  2  TPG streaming puts a type-0x01 stream packet on the wire.
  3  `link_reset_req` puts no acknowledgment on the wire.
  4  TestMode → type-0x04 link-test packets, no new stream packets.
  8  Trigger rising edge → one 4×K28.4 / 4×Delay packet, no repeats.
  9  Rising then falling trigger edge → K28.4 then K28.2 packets.
 10  Trigger edge mid-stream-packet is spliced in within 20 words.
 11  A trigger under TestMode is inserted into the test packet.
 12  `link_reset_active` window opens, `link_reset_done` fires, closes.
 13  LinkReset clears the four §10.3.28 bootstrap registers.
 15  LinkReset masks a held trigger → falling-edge K28.2 packet.
 16  Two back-to-back LinkResets both complete.
 17  The end of the LinkReset window puts no acknowledgment on the wire.
 18  A ConnectionConfig write restarts the stream PacketTag at 0.
 19  Triggers paced by the acknowledgment timeout while 200-word stream
     packets, then test packets, leave back to back and the pin toggles
     every few cycles: every
     leader is followed by its Delay word, at 80+ cadence positions.
 20  A trigger pin held asserted across a ConnectionReset, and across a
     reset, sends no rising packet when the window ends; the next real
     edge does.
"""

from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly

import cxp_8b10b as cxp
from cxp_host import Host
from cxp_link_check import WireMonitor, check_downlink, split_short_packets
from cxp_protocol import DEVICE
from cxp_testcase import at_teardown, cxp_test


CLK_PERIOD_NS = 10

# K-code byte values (from cxp_8b10b common reference).
K27_7 = cxp.K27_7
K28_1 = cxp.K28_1
K28_2 = cxp.K28_2
K28_3 = cxp.K28_3
K28_4 = cxp.K28_4
K28_5 = cxp.K28_5
K29_7 = cxp.K29_7
D21_5 = cxp.D21_5


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def reset(dut, cycles=4):
    """Hold `rst_n` low `cycles` edges with all inputs at defaults, run 2.

    Releasing the reset also starts the passive `WireMonitor` that feeds
    the golden deframer, and registers the teardown check for framing
    errors and the §8.2.5.1 IDLE rule.
    """
    dut.rst_n.value = 0
    # Defaults
    dut.cfg_use_tpg.value        = 0
    dut.cfg_run.value            = 0
    dut.cfg_arbitrary.value      = 0
    dut.cfg_dsizeP.value         = 8
    dut.cfg_trig_polarity.value  = 0
    dut.clr_lt_err.value         = 0
    dut.clr_lt_pkt_tx.value      = 0
    dut.clr_lt_pkt_rx.value      = 0
    dut.s_pix_data.value         = 0
    dut.s_pix_valid.value        = 0
    dut.s_pix_sof.value          = 0
    dut.s_pix_eol.value          = 0
    dut.s_pix_eof.value          = 0
    dut.ext_meta_xsize.value     = 0
    dut.ext_meta_ysize.value     = 0
    dut.ext_meta_xoffs.value     = 0
    dut.ext_meta_yoffs.value     = 0
    dut.ext_meta_pixfmt.value    = 0
    dut.ext_meta_tapg.value      = 0
    dut.ext_meta_streamid.value  = 0
    dut.ext_meta_sourcetag.value = 0
    dut.ext_meta_flags.value     = 0
    dut.rx_serial.value          = 1
    dut.link_reset_req.value     = 0
    dut.conn_cfg_wr_inject.value = 0
    dut.trigger_in_app.value     = 0
    for _ in range(cycles):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    for _ in range(2):
        await RisingEdge(dut.clk)
    mon = WireMonitor(dut.clk, dut.cxp_if_data_o, dut.cxp_if_kmask_o).start()
    at_teardown(lambda: check_downlink(mon))


async def uplink(dut, trig_ack: str = "ack") -> Host:
    """Run a host on the uplink and wait until the device detects it.

    The device sends trigger packets only to a connected host (§8.3.2) and
    waits for each one's I/O acknowledgment or for the wrapper's 64-cycle
    timeout (§8.3.3).  The host keeps the uplink at IDLE and, with
    `trig_ack = "ack"`, answers every trigger; at 16x oversampling an
    acknowledgment takes about 2000 cycles, so the timeout paces here.
    """
    host = Host(dut, q=DEVICE, os_ratio=16, rx_clk=dut.clk, tx_clk=dut.clk,
                rx_pin=dut.rx_serial, data=dut.cxp_if_data_o,
                kmask=dut.cxp_if_kmask_o, link=dut.link_detected).start()
    host.trig_ack = trig_ack
    await host.link_up(timeout=40000)
    return host


# -----------------------------------------------------------------------------
# Wire monitor
# -----------------------------------------------------------------------------
async def sample_wire(dut):
    """Sample THIS cycle's transmitted word and advance to the next edge.

    The DUT drives one 32-bit word per ``tx_clk`` cycle on
    ``cxp_if_data_o`` / ``cxp_if_kmask_o`` (combinational from the
    arbiter's mux).  We read it now (stable for the rest of the cycle)
    and then ``await RisingEdge`` to move on.

    Reading FIRST and advancing AFTER matters when a test sets up a
    stimulus (e.g. ``link_reset_req``) on the rising edge that starts
    the SOP cycle — the conventional ``await RisingEdge; read`` pattern
    would skip the SOP cycle entirely and land on word 1 of the packet.
    """
    await ReadOnly()
    data  = int(dut.cxp_if_data_o.value)
    kmask = int(dut.cxp_if_kmask_o.value)
    await RisingEdge(dut.clk)
    return data, kmask


def is_idle_word(data: int, kmask: int) -> bool:
    """True for the IDLE word K28.5 K28.1 K28.1 D21.5, kmask 0111."""
    return (kmask == 0b0111
            and (data & 0xFF)        == K28_5
            and ((data >> 8)  & 0xFF) == K28_1
            and ((data >> 16) & 0xFF) == K28_1
            and ((data >> 24) & 0xFF) == D21_5)


def is_sop(data: int, kmask: int) -> bool:
    """True for a packet SOP word (4×K27.7, kmask 1111)."""
    return kmask == 0b1111 and all(((data >> (8 * i)) & 0xFF) == K27_7
                                    for i in range(4))


def is_eop(data: int, kmask: int) -> bool:
    """True for a packet EOP word (4×K29.7, kmask 1111)."""
    return kmask == 0b1111 and all(((data >> (8 * i)) & 0xFF) == K29_7
                                    for i in range(4))


async def collect_packet(dut, max_idle=2000, max_data=1024):
    """Wait for a SOP K27.7 word on the wire and return the in-packet
    word list, including the SOP word itself but not the trailing K29.7.

    A timeout (max_idle) protects against runaway runs when no packet
    ever fires.
    """
    # Skip leading IDLE words.
    seen_sop = False
    for _ in range(max_idle):
        data, kmask = await sample_wire(dut)
        if is_sop(data, kmask):
            words = [(data, kmask)]
            seen_sop = True
            break
    assert seen_sop, "no SOP K27.7 word observed within max_idle cycles"

    # Body until EOP.
    for _ in range(max_data):
        data, kmask = await sample_wire(dut)
        if is_eop(data, kmask):
            return words
        words.append((data, kmask))
    raise AssertionError("collect_packet: EOP K29.7 never arrived")


# -----------------------------------------------------------------------------
# Trigger packet helpers
# -----------------------------------------------------------------------------
# A HS trigger packet (§6.3.2.2 table 16 as cited; v1.1.1 §8.3.2 Table 16)
# is a 2-word frame:
#   HDR  : data = 4×K28.4 (rising) or 4×K28.2 (falling), kmask = 1111
#   DLY  : data = 4×Delay (0 in this build),             kmask = 0000
# Because collect_packet() targets K27.7-framed packets only, we sample
# the wire directly here.
def is_trig_rising_hdr(data: int, kmask: int) -> bool:
    """True for a rising-edge trigger HDR word (4×K28.4, kmask 1111)."""
    return kmask == 0b1111 and all(((data >> (8 * i)) & 0xFF) == K28_4
                                    for i in range(4))


def is_trig_falling_hdr(data: int, kmask: int) -> bool:
    """True for a falling-edge trigger HDR word (4×K28.2, kmask 1111)."""
    return kmask == 0b1111 and all(((data >> (8 * i)) & 0xFF) == K28_2
                                    for i in range(4))


async def wait_for_trigger_packet(dut, max_idle=2000):
    """Sample the wire until a trigger HDR (4×K28.4 or 4×K28.2) word
    appears, then verify the next-cycle DLY word.  Returns the K-code
    byte (0x9C for rising, 0x5C for falling).  Raises AssertionError on
    timeout."""
    for _ in range(max_idle):
        data, kmask = await sample_wire(dut)
        if is_trig_rising_hdr(data, kmask) or is_trig_falling_hdr(data, kmask):
            kcode = data & 0xFF
            dly_data, dly_km = await sample_wire(dut)
            assert dly_km == 0b0000, (
                f"trigger DLY word kmask=0x{dly_km:x} expected 0"
            )
            assert dly_data == 0x0000_0000, (
                f"trigger DLY word data=0x{dly_data:08x} expected 0"
            )
            return kcode
    raise AssertionError(
        f"no trigger HDR word seen within {max_idle} wire cycles"
    )


# -----------------------------------------------------------------------------
# TC 1 — Post-Reset IDLE
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_idle_after_reset(dut):
    """With no source active after reset, the wire carries only IDLE words.

    No packet source may be valid out of reset, so the arbiter must fill
    every cycle with the §8.2.5 IDLE word (Table 14).

    Stimulus: reset with every input at its default (TPG off, trigger 0,
              `rx_serial` 1); 32 wire samples starting 2 cycles after
              reset release.
    Checks:   every sample is kmask 0111 with bytes K28.5, K28.1, K28.1,
              D21.5 (P0 → P3).
    """
    dut.TESTCASE.value = 1
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)

    # Sample 32 words; every one should be the IDLE word.
    for i in range(32):
        data, kmask = await sample_wire(dut)
        assert is_idle_word(data, kmask), (
            f"word {i}: expected IDLE, got data=0x{data:08x} kmask=0x{kmask:x}"
        )


# -----------------------------------------------------------------------------
# TC 2 — Stream Packet From Internal TPG
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_stream_from_tpg(dut):
    """TPG pixels reach the wire as a type-0x01 stream packet.

    Smoke test of the whole video path: TPG → pixel mux → packer →
    `cxp_stream_top` → arbiter stream slot → `cxp_if_data_o`.

    Stimulus: `cfg_use_tpg` = `cfg_run` = 1, `cfg_arbitrary` = 0,
              `cfg_dsizeP` = 8; `collect_packet` for the first packet
              (SOP within 4000 samples).
    Checks:   a SOP (4×K27.7) is seen and an EOP (4×K29.7) follows within
              1024 words; the TYPE word (word 1) has kmask 0 and all four
              lanes equal 0x01.
    Note:     only the TYPE word is checked — header fields, K28.3 image
              header marker, payload and CRC are not decoded here.
    """
    dut.TESTCASE.value = 2
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)

    dut.cfg_use_tpg.value   = 1
    dut.cfg_run.value       = 1
    dut.cfg_arbitrary.value = 0
    dut.cfg_dsizeP.value    = 8

    # Collect the first stream packet from the wire.
    words = await collect_packet(dut, max_idle=4000)

    # Word 0: SOP K27.7 (already asserted by collect_packet).
    # Word 1: 4×PacketType.  Type 0x01 = stream data.
    type_data, type_km = words[1]
    assert type_km == 0b0000, f"type word kmask=0x{type_km:x}"
    type_byte = type_data & 0xFF
    assert type_byte == 0x01, f"expected stream type 0x01, got 0x{type_byte:02x}"

    # All four lanes of the type word should carry 0x01.
    for i in range(4):
        b = (type_data >> (8 * i)) & 0xFF
        assert b == 0x01, f"type word lane {i}: 0x{b:02x}"


# -----------------------------------------------------------------------------
# TC 3 — LinkReset Produces a Type-0x03 Ack
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_link_reset_no_ack(dut):
    """A LinkReset puts no acknowledgment on the wire.

    §8.6.1.1: an acknowledgment answers a control command.  The
    ConnectionReset write is acknowledged by the control path; the reset
    itself must not add a second, unsolicited one.

    Stimulus: after one edge, `link_reset_req` = 1 for one cycle.
    Checks:   400 wire words after the pulse are all IDLE.
    """
    dut.TESTCASE.value = 3
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)

    await RisingEdge(dut.clk)
    dut.link_reset_req.value = 1
    await RisingEdge(dut.clk)
    dut.link_reset_req.value = 0

    for n in range(400):
        data, kmask = await sample_wire(dut)
        assert is_idle_word(data, kmask), (
            f"word {n} after LinkReset is not IDLE: 0x{data:08x} k={kmask:04b}")


# -----------------------------------------------------------------------------
# TC 4 — TestMode Emits Link-Test Packets, Stream Suppressed
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_linktest_packets_under_testmode(dut):
    """Under TestMode the device sends link-test packets and no new stream.

    §10.3.35 TestMode drives `cxp_tx_linktest` (type-0x04 packets) and
    `linktest_suppress_traffic`, which stops new stream packets from
    starting (one already on the wire completes). TestMode is
    backdoor-poked (same trick as the cxp_rx_linktest TB) instead of
    driving an APB write through the serial uplink.

    Stimulus: TPG on (`cfg_dsizeP` = 8) so the stream source is trying to
              send; on the next edge `cxp_ctrl_bootstrap_regs_i.reg_q[row_test_mode]`
              = 1 (→ `cfg_test_mode`); then `collect_packet` five times
              (SOP within 8000 samples, up to 2400 words per packet to fit
              a 1027-word link-test packet plus forced IDLEs).
    Checks:   packet 0 may be type 0x01; every other packet has TYPE byte
              0x03 or 0x04; at least one 0x04 packet appears.
    Note:     only TYPE byte 0 is checked; TestMode landing inside a
              stream packet is `device_top` 28.
    """
    dut.TESTCASE.value = 4
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)

    # Have the TPG running so the stream source is *trying* to send —
    # we then verify TestMode keeps it off the wire.
    dut.cfg_use_tpg.value   = 1
    dut.cfg_run.value       = 1
    dut.cfg_arbitrary.value = 0
    dut.cfg_dsizeP.value    = 8

    # Backdoor-poke TestMode.
    await RisingEdge(dut.clk)
    dut.cxp_ctrl_bootstrap_regs_i.reg_q[int(dut.row_test_mode.value)].value = 1
    await RisingEdge(dut.clk)

    # Packet 0 may be a type-0x01 stream packet that started before
    # suppression took effect — tolerate it, but every packet started
    # AFTER must be linktest/ack.
    # Use max_data=2400 to accommodate a 1027-word linktest packet plus
    # up to ~12 mid-packet IDLE inserts forced by the 100-word cadence.
    saw_linktest = False
    for pkt_idx in range(5):
        words = await collect_packet(dut, max_idle=8000, max_data=2400)
        tb = words[1][0] & 0xFF
        if pkt_idx == 0 and tb == 0x01:
            # In-flight stream packet — tolerated (see comment above).
            continue
        assert tb in (0x03, 0x04), (
            f"under TestMode packet #{pkt_idx} had forbidden type 0x{tb:02x}"
        )
        if tb == 0x04:
            saw_linktest = True
    assert saw_linktest, "TestMode was on but no type-0x04 packet appeared"


# -----------------------------------------------------------------------------
# TC 8 — Trigger Rising Edge
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_trigger_rising_edge(dut):
    """A rising `trigger_in_app` edge sends exactly one K28.4 trigger packet.

    Device → host HS trigger (§8.3.2 Table 16): 4×K28.4 HDR then 4×Delay.
    No packet may fire at reset release or while the level is steady.

    Stimulus: polarity 0 (active-high); `trigger_in_app` = 0 for 20
              samples, then 1 and held; `wait_for_trigger_packet` (HDR
              within 200 samples), then 40 more samples.
    Checks:   no K28.4 / K28.2 HDR in the first 20 samples; then an HDR
              of 4×K28.4 followed by a Delay word with kmask 0 and data 0;
              no HDR in the following 40 samples.
    """
    dut.TESTCASE.value = 8
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)
    await uplink(dut)
    # Default polarity = 0 (active-high).  trigger_in_app starts 0.

    # No edge yet — no trigger packet should fire.
    for _ in range(20):
        data, kmask = await sample_wire(dut)
        assert not is_trig_rising_hdr(data, kmask), (
            "trigger packet fired before any edge"
        )
        assert not is_trig_falling_hdr(data, kmask)

    # Drive the rising edge.
    dut.trigger_in_app.value = 1
    kcode = await wait_for_trigger_packet(dut, max_idle=200)
    assert kcode == K28_4, (
        f"trigger packet K-code 0x{kcode:02x} != K28.4 (0x{K28_4:02x})"
    )

    # No further trigger packet while trigger_in stays high.
    for _ in range(40):
        data, kmask = await sample_wire(dut)
        assert not is_trig_rising_hdr(data, kmask), (
            "spurious trigger packet while trigger_in held steady"
        )
        assert not is_trig_falling_hdr(data, kmask)


# -----------------------------------------------------------------------------
# TC 9 — Trigger Edge Pair
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_trigger_edge_pair(dut):
    """Rising then falling edges send a K28.4 packet then a K28.2 packet.

    The trigger K-code must follow the physical edge direction, and two
    trigger packets must be able to go out back to back.

    Stimulus: `trigger_in_app` 0 → 1 right after reset; after the first
              packet's Delay word is sampled, 1 → 0.
    Checks:   first HDR is 4×K28.4, second is 4×K28.2, each within 200
              samples and each followed by a Delay word of kmask 0,
              data 0.
    """
    dut.TESTCASE.value = 9
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)
    await uplink(dut)

    # Rising edge.
    dut.trigger_in_app.value = 1
    kc1 = await wait_for_trigger_packet(dut, max_idle=200)
    assert kc1 == K28_4, f"first packet K-code 0x{kc1:02x} != K28.4"

    # Falling edge.
    dut.trigger_in_app.value = 0
    kc2 = await wait_for_trigger_packet(dut, max_idle=200)
    assert kc2 == K28_2, f"second packet K-code 0x{kc2:02x} != K28.2"


# -----------------------------------------------------------------------------
# TC 10 — Trigger Preempts Stream at a Word Boundary
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_trigger_preempts_stream(dut):
    """A trigger edge mid-stream-packet is spliced in, then streaming resumes.

    §8.2.4: a trigger packet is inserted at the next word boundary instead
    of waiting for the in-flight stream packet's EOP; the inserter holds
    the stream for its two words and the stream resumes after.

    Stimulus: `uplink`; TPG on, `cfg_dsizeP` = 8; 200 cycles; wait for a
              stream SOP (≤ 2000 samples), sample 2 more words, then
              `trigger_in_app` = 1.
    Checks:   an HDR of 4×K28.4 with a valid Delay word within 8 samples
              of the edge (two synchroniser flops, the packet start, the
              output register) — 13 words of the stream packet remain, so
              waiting for its EOP would miss the bound; then another
              stream SOP within 2000 samples.
    Note:     resume is inferred from the next SOP and the teardown's
              framing check, not from the packet's CRC.
    """
    dut.TESTCASE.value = 10
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)
    await uplink(dut)

    # Have the TPG running.
    dut.cfg_use_tpg.value   = 1
    dut.cfg_run.value       = 1
    dut.cfg_arbitrary.value = 0
    dut.cfg_dsizeP.value    = 8

    # Let it generate at least one stream packet so the arbiter is
    # busy.  Spin until the wire is firmly in the middle of a stream
    # packet (SOP seen, no EOP yet).
    for _ in range(200):
        await RisingEdge(dut.clk)

    # Park us mid-stream: wait for a stream SOP, then advance two
    # cycles into the packet body before firing the trigger.
    saw_sop = False
    for _ in range(2000):
        data, kmask = await sample_wire(dut)
        if not saw_sop and is_sop(data, kmask):
            saw_sop = True
            # Advance two wire cycles into the packet body.
            await sample_wire(dut)
            await sample_wire(dut)
            break
    assert saw_sop, "did not catch a stream SOP within 2000 wire cycles"

    dut.trigger_in_app.value = 1

    # Inserted, the trigger HDR is on the wire 4 cycles after the edge
    # (2 synchroniser flops, the packet start, the output register); 8
    # leaves room for an IDLE that is due, and is well short of the 13
    # stream words still to go.
    kcode = await wait_for_trigger_packet(dut, max_idle=8)
    assert kcode == K28_4, (
        f"trigger packet K-code 0x{kcode:02x} != K28.4 (0x{K28_4:02x})"
    )

    # The preempted stream packet must resume — wait for its EOP
    # (K29.7-pair, but easiest: look for the next stream SOP, which
    # implies the previous packet finished).  Bound it generously since
    # the stream packet's tail can be long.
    next_sop = False
    for _ in range(2000):
        data, kmask = await sample_wire(dut)
        if is_sop(data, kmask):
            next_sop = True
            break
    assert next_sop, "stream did not resume / produce another SOP after trigger"


# -----------------------------------------------------------------------------
# TC 11 — Trigger In TestMode
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_trigger_in_testmode(dut):
    """A device trigger in TestMode is inserted into the test packet.

    §8.3.2 / §8.2.4: a trigger has the highest priority and is inserted
    into any lower-priority packet; §8.7.4 restricts data packets in
    TestMode, not the trigger (decision D2).

    Stimulus: backdoor `reg_q[row_test_mode]` = 1; wait for a test
              packet's SOP and 100 more words; `trigger_in_app` = 1.
    Checks:   an HDR of 4×K28.4 with a valid Delay word within 20 words;
              the test packet around it keeps its framing (teardown).
    """
    dut.TESTCASE.value = 11
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)
    await uplink(dut)

    await RisingEdge(dut.clk)
    dut.cxp_ctrl_bootstrap_regs_i.reg_q[int(dut.row_test_mode.value)].value = 1
    await RisingEdge(dut.clk)
    for _ in range(4000):
        data, kmask = await sample_wire(dut)
        if is_sop(data, kmask):
            break
    else:
        raise AssertionError("no test packet under TestMode")
    for _ in range(100):
        await sample_wire(dut)
    dut.trigger_in_app.value = 1
    kcode = await wait_for_trigger_packet(dut, max_idle=20)
    assert kcode == K28_4, f"trigger K-code 0x{kcode:02x} != K28.4"


# -----------------------------------------------------------------------------
# TC 12 — Link-Reset Active Window
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_link_reset_active_during_window(dut):
    """`link_reset_active` opens on a request and closes after `done`.

    The register file holds the ConnectionReset bit for at least 8 cycles
    (the wrapper's LINK_RESET_CLEAR_CYCLES) and until the DUT's tx domain
    has echoed it; `done` pulses when it clears (§10.3.28 window).

    Stimulus: after one edge, `link_reset_req` = 1 for one cycle.
    Checks:   `link_reset_active` = 1 at the first ReadOnly after the
              pulse; `link_reset_done` = 1 within 64 cycles;
              `link_reset_active` = 0 one cycle after `done`.
    Note:     any window width ≤ 64 passes; the exact width is not
              asserted.
    """
    dut.TESTCASE.value = 12
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)

    await RisingEdge(dut.clk)
    dut.link_reset_req.value = 1
    await RisingEdge(dut.clk)
    dut.link_reset_req.value = 0

    # Cycle 0 inside the active window — sample at ReadOnly.
    await ReadOnly()
    assert int(dut.link_reset_active.value) == 1, (
        "link_reset_active did not assert after link_reset_req"
    )
    await RisingEdge(dut.clk)

    # Wait for done — within a generous bound.
    saw_done = False
    for _ in range(64):
        await ReadOnly()
        if int(dut.link_reset_done.value):
            saw_done = True
            break
        await RisingEdge(dut.clk)
    assert saw_done, "link_reset_done never asserted within 64 cycles"

    # After done, active must be back at 0.
    await RisingEdge(dut.clk)
    await ReadOnly()
    assert int(dut.link_reset_active.value) == 0


# -----------------------------------------------------------------------------
# TC 13 — LinkReset Clears Bootstrap Registers
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_link_reset_clears_bootstrap_regs(dut):
    """A LinkReset clears the four §10.3.28 registers in the regfile.

    v1.1.1 §10.3.28 (v1.0 §8.3.15) ConnectionReset side-effects:
    MasterHostConnectionID, StreamPacketSizeMax, TestMode and
    TestErrorCountSelector return to 0, via `lrst_clr_*` looped back into
    `cxp_ctrl_bootstrap_regs` by the wrapper.

    Stimulus: backdoor preload of the MasterHostConnectionID,
              StreamPacketSizeMax, TestMode and TestErrorCountSelector
              rows of `reg_q` (0xA5A5A5A5, 0x80, 1, 1); one edge; 1-cycle
              `link_reset_req`; wait for `link_reset_done` (≤ 64 cycles),
              then one more edge.
    Checks:   before the reset the snoops show 0xA5A5A5A5, 0x80 and 1;
              afterwards `bs_master_host_link_id`, `bs_stream_pkt_dsize`,
              `bs_test_mode` and the TestErrorCountSelector row all read 0.
    Note:     TestErrorCountSelector is not read back before the reset.
    """
    dut.TESTCASE.value = 13
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)

    # Backdoor pre-load of the reg_q rows (indices published by the wrapper).
    await RisingEdge(dut.clk)
    regs = dut.cxp_ctrl_bootstrap_regs_i.reg_q
    regs[int(dut.row_master_host_conn_id.value)].value = 0xA5A5_A5A5
    regs[int(dut.row_stream_pkt_dsize.value)].value    = 0x0000_0080
    regs[int(dut.row_test_mode.value)].value           = 1
    regs[int(dut.row_test_err_cnt_sel.value)].value    = 0x0000_0001
    await RisingEdge(dut.clk)

    # Sanity-check the snoops see the pre-loaded values.
    await ReadOnly()
    assert int(dut.bs_master_host_link_id.value) == 0xA5A5_A5A5
    assert int(dut.bs_stream_pkt_dsize.value)    == 0x0000_0080
    assert int(dut.bs_test_mode.value)           == 1
    await RisingEdge(dut.clk)

    # Fire the reset.
    dut.link_reset_req.value = 1
    await RisingEdge(dut.clk)
    dut.link_reset_req.value = 0

    # Wait for the controller to finish.
    for _ in range(64):
        await ReadOnly()
        if int(dut.link_reset_done.value):
            break
        await RisingEdge(dut.clk)
    else:
        raise AssertionError("link_reset_done never fired")

    # Sample one cycle after done — every cleared register must read 0.
    await RisingEdge(dut.clk)
    await ReadOnly()
    assert int(dut.bs_master_host_link_id.value)    == 0, (
        "MasterHostLinkId not cleared by §8.3.15 fan-out"
    )
    assert int(dut.bs_stream_pkt_dsize.value)       == 0, (
        "StreamPacketDataSize not cleared by §8.3.15 fan-out"
    )
    assert int(dut.bs_test_mode.value)              == 0, (
        "TestMode not cleared by §8.3.15 fan-out"
    )
    assert int(regs[int(dut.row_test_err_cnt_sel.value)].value) == 0, (
        "TestErrorCounterSelector not cleared by §8.3.15 fan-out"
    )


# -----------------------------------------------------------------------------
# TC 15 — LinkReset De-asserts the Trigger Output
# -----------------------------------------------------------------------------
@cxp_test()
async def test_15_link_reset_clears_trigger_output(dut):
    """LinkReset with the trigger held high sends a falling-edge packet.

    §10.3.28 / §8.3.2: the device shall de-assert its trigger as part of
    link discovery. During the ConnectionReset window `cxp_tx_trigger_hs`
    sends the de-asserted level, so a host left asserted gets a K28.2.

    Stimulus: `trigger_in_app` 0 → 1 and held; K28.4 packet awaited
              (≤ 200 samples); then after one edge a 1-cycle
              `link_reset_req`.
    Checks:   first packet K28.4; then a trigger packet within 400
              samples whose HDR is 4×K28.2; both Delay words kmask 0,
              data 0.
    Note:     polarity 0 only; that no K28.4 follows the window with the
              pin still held is `test_20`.
    """
    dut.TESTCASE.value = 15
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)
    await uplink(dut)

    # Rising edge first so the device is in the "trigger asserted" state.
    dut.trigger_in_app.value = 1
    kcode = await wait_for_trigger_packet(dut, max_idle=200)
    assert kcode == K28_4

    # Fire LinkReset.  trigger_in_app stays at 1; the trigger source sends
    # the de-asserted level during the window → K28.2 packet.
    await RisingEdge(dut.clk)
    dut.link_reset_req.value = 1
    await RisingEdge(dut.clk)
    dut.link_reset_req.value = 0

    kcode = await wait_for_trigger_packet(dut, max_idle=400)
    assert kcode == K28_2, (
        f"post-LinkReset trigger K-code 0x{kcode:02x} != K28.2 (falling)"
    )


# -----------------------------------------------------------------------------
# TC 16 — Back-to-Back LinkResets
# -----------------------------------------------------------------------------
@cxp_test()
async def test_16_link_reset_back_to_back(dut):
    """Two successive LinkReset requests both complete.

    Verification plan §5.5 #4 asks for two LinkResets 50 ms apart with no
    stuck state; compressed here to a few cycles, since the controller
    has no notion of wall time.

    Stimulus: twice: after one edge a 1-cycle `link_reset_req`, wait for
              `link_reset_done` (≤ 64 cycles), then 8 idle cycles — the
              second request lands ~9 cycles after the first `done`.
    Checks:   `link_reset_done` fires within 64 cycles in both
              iterations.
    Note:     the requests never overlap a window, so the timer reload
              path is not exercised; acks and `link_reset_active` are not
              checked.
    """
    dut.TESTCASE.value = 16
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)

    for i in range(2):
        await RisingEdge(dut.clk)
        dut.link_reset_req.value = 1
        await RisingEdge(dut.clk)
        dut.link_reset_req.value = 0

        # Wait for done — within a generous bound.
        for _ in range(64):
            await ReadOnly()
            if int(dut.link_reset_done.value):
                break
            await RisingEdge(dut.clk)
        else:
            raise AssertionError(f"reset #{i}: done never fired")

        # Drain a few cycles between resets.
        for _ in range(8):
            await RisingEdge(dut.clk)


# -----------------------------------------------------------------------------
# TC 17 — LinkReset Done Drives the Ack
# -----------------------------------------------------------------------------
@cxp_test()
async def test_17_link_reset_done_no_ack(dut):
    """The end of the LinkReset window sends nothing either.

    Stimulus: after one edge, `link_reset_req` = 1 for one cycle.
    Checks:   `link_reset_done` fires within 64 cycles; the wire is IDLE
              from the pulse through 200 words after `done`.
    """
    dut.TESTCASE.value = 17
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)

    await RisingEdge(dut.clk)
    dut.link_reset_req.value = 1
    await RisingEdge(dut.clk)
    dut.link_reset_req.value = 0

    done_at = None
    for n in range(400):
        await ReadOnly()
        if done_at is None and int(dut.link_reset_done.value):
            done_at = n
        data = int(dut.cxp_if_data_o.value)
        kmask = int(dut.cxp_if_kmask_o.value)
        await RisingEdge(dut.clk)
        assert is_idle_word(data, kmask), (
            f"word {n} after LinkReset is not IDLE: 0x{data:08x} k={kmask:04b}")
    assert done_at is not None and done_at < 64, "link_reset_done never fired"


# -----------------------------------------------------------------------------
# TC 18 — ConnectionConfig Write Restarts PacketTag
# -----------------------------------------------------------------------------
@cxp_test()
async def test_18_connection_config_write_resets_tag(dut):
    """A ConnectionConfig write restarts the stream PacketTag at 0.

    §8.5.3 / §10.3.33: a write to ConnectionConfig resets the stream
    control, even when the value does not change; the next stream packet
    carries PacketTag 0.  The regfile's write strobe reaches
    `cxp_interface_top.conn_cfg_wr` through the wrapper.

    Stimulus: TPG streaming with `cfg_dsizeP` = 8; three packets; then a
              3-cycle `conn_cfg_wr_inject` pulse (the wrapper ORs it into
              the regfile's ConnectionConfig write strobe, which a host
              write raises); then three more packets.
    Checks:   the first three tags run on by one; among the two packets
              after the pulse one has tag 0 (a packet whose header was
              already latched keeps its tag), and the packet after it
              has tag 1.
    """
    dut.TESTCASE.value = 18
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)

    dut.cfg_use_tpg.value   = 1
    dut.cfg_run.value       = 1
    dut.cfg_arbitrary.value = 0
    dut.cfg_dsizeP.value    = 8

    def tag_of(words):
        data, kmask = words[3]                  # 4 x PacketTag
        assert kmask == 0, f"tag word kmask=0x{kmask:x}"
        return data & 0xFF

    before = [tag_of(await collect_packet(dut, max_idle=4000)) for _ in range(3)]
    assert all((b - a) & 0xFF == 1 for a, b in zip(before, before[1:])), (
        f"tags before the write do not run on: {before}")
    assert before[-1] + 1 < 0xFF, f"tags too close to the wrap: {before}"

    # Held for 3 edges: a cocotb write after RisingEdge lands one edge
    # late, and a 1-then-0 pair on consecutive edges can collapse.  The
    # tag reset is a level, so the length does not matter.
    dut.conn_cfg_wr_inject.value = 1
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.conn_cfg_wr_inject.value = 0

    after = [tag_of(await collect_packet(dut, max_idle=4000)) for _ in range(3)]
    dut.cfg_run.value = 0
    assert 0 in after[:2], f"no tag 0 after the ConnectionConfig write: {before} -> {after}"
    i = after.index(0)
    assert after[i + 1] == 1, f"tag after 0 is {after[i + 1]}: {after}"


# -----------------------------------------------------------------------------
# TC 19 — Trigger Phase Sweep Over The IDLE Cadence
# -----------------------------------------------------------------------------
async def drive_sensor_frames(dut, width: int, height: int, frames: int):
    """Push `frames` Mono8 frames through the sensor port, one pixel per
    accepted cycle (s_pix_ready honoured), with SOF / EOL / EOF flags."""
    dut.ext_meta_xsize.value = width
    dut.ext_meta_ysize.value = height
    dut.ext_meta_pixfmt.value = 0x0101
    for _ in range(frames):
        for y in range(height):
            for x in range(width):
                dut.s_pix_data.value = ((x + y) & 0xFF) << 8  # 16-bit sample, Mono8 = [15:8]
                dut.s_pix_valid.value = 1
                dut.s_pix_sof.value = int(x == 0 and y == 0)
                dut.s_pix_eol.value = int(x == width - 1)
                dut.s_pix_eof.value = int(x == width - 1 and y == height - 1)
                while True:
                    await RisingEdge(dut.clk)
                    if int(dut.s_pix_ready.value):
                        break
    dut.s_pix_valid.value = 0
    dut.s_pix_sof.value = dut.s_pix_eol.value = dut.s_pix_eof.value = 0


@cxp_test()
async def test_19_trig_phase_sweep_100(dut):
    """No trigger packet is split by an IDLE, at any phase of the cadence.

    §8.2.5.1, §8.2.5.2, Table 16, §8.3.3.

    Stimulus: `uplink` with a host that never acknowledges (the device
              paces its triggers by the 64-cycle timeout); sensor path
              (cfg_use_tpg = 0, cfg_run = 1), `cfg_dsizeP` = 200, six
              128 x 32 Mono8 frames at one pixel per cycle, sent back to
              back from the store-and-forward FIFO; then TestMode for
              20000 cycles (test packets back to back fill every phase of
              the cadence); `trigger_in_app` toggles every 3..11 cycles
              (seeded) for the whole run, then rests.
    Checks:   every trigger leader is followed on the next word by its
              Delay word; the leaders alternate K28.4 / K28.2 and the
              last one is the pin's final level; consecutive leaders are
              at least 64 words apart; the leaders went out at 80 or more
              positions of the IDLE cadence; the stream reassembles.
    """
    dut.TESTCASE.value = 19
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)
    host = await uplink(dut, trig_ack="drop")
    host.raw = []
    dut.cfg_use_tpg.value = 0
    dut.cfg_run.value = 1
    dut.cfg_dsizeP.value = 200
    done = [False]

    rng = random.Random(19)

    async def sweep():
        while not done[0]:
            for _ in range(rng.randint(3, 11)):
                await RisingEdge(dut.clk)
            dut.trigger_in_app.value = 1 - int(dut.trigger_in_app.value)

    sw = cocotb.start_soon(sweep())
    await drive_sensor_frames(dut, 128, 32, 6)
    for _ in range(3000):
        await RisingEdge(dut.clk)
    dut.cfg_run.value = 0
    row = int(dut.row_test_mode.value)
    dut.cxp_ctrl_bootstrap_regs_i.reg_q[row].value = 1
    for _ in range(20000):
        await RisingEdge(dut.clk)
    dut.cxp_ctrl_bootstrap_regs_i.reg_q[row].value = 0
    for _ in range(2000):
        await RisingEdge(dut.clk)
    done[0] = True
    await sw
    for _ in range(300):
        await RisingEdge(dut.clk)
    words = host.raw
    torn = split_short_packets(words)
    assert not torn, f"{len(torn)} torn trigger packets, first: {torn[:3]}"
    leaders, phases, run = [], set(), 0
    for i, (w, k) in enumerate(words):
        if is_idle_word(w, k):
            run = 0
            continue
        if k == 0xF and w in (0x9C9C9C9C, 0x5C5C5C5C):
            leaders.append((i, w & 0xFF))
            phases.add(run)
        run += 1
    codes = [c for _, c in leaders]
    want = [K28_4 if n % 2 == 0 else K28_2 for n in range(len(codes))]
    assert codes == want, f"leaders do not alternate: {[hex(c) for c in codes[:12]]}"
    assert codes[-1] == (K28_4 if int(dut.trigger_in_app.value) else K28_2)
    gaps = [b - a for (a, _), (b, _) in zip(leaders, leaders[1:])]
    assert min(gaps) >= 64, f"leaders {min(gaps)} words apart"
    assert len(phases) >= 80, f"leaders at {len(phases)} cadence positions"
    assert not host.reasm.errors, host.reasm.errors[:4]


# -----------------------------------------------------------------------------
# TC 20 — No Trigger Packet When The Reset Window Ends
# -----------------------------------------------------------------------------
async def count_trigger_packets(dut, n: int) -> list[int]:
    """Sample `n` words; return the K-codes of the trigger leaders seen."""
    seen = []
    for _ in range(n):
        data, kmask = await sample_wire(dut)
        if is_trig_rising_hdr(data, kmask):
            seen.append(K28_4)
        elif is_trig_falling_hdr(data, kmask):
            seen.append(K28_2)
    return seen


@cxp_test()
async def test_20_trigger_held_across_reset(dut):
    """A held trigger pin is not re-sent as a new edge after a reset.

    §10.3.28: the device trigger is set to 0 by a connection reset (and
    by power-up); a pin still asserted afterwards is not a new event.

    Stimulus: `uplink`; (a) `trigger_in_app` = 1 through a reset of the
              device, the link back up, 300 words; (b) the pin to 0 and
              back to 1 (a real edge); (c) with the pin held, a
              ConnectionReset request; 600 words; (d) pin 0, 50 words,
              pin 1.
    Checks:   (a) no trigger packet; (b) one K28.4; (c) one K28.2 (the
              reset de-asserts the trigger) and no K28.4 after it; (d)
              no packet for the pin going 0 (already de-asserted), one
              K28.4 for the real edge.
    """
    dut.TESTCASE.value = 20
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)
    host = await uplink(dut)
    dut.trigger_in_app.value = 1
    await RisingEdge(dut.clk)
    dut.rst_n.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    for _ in range(8):
        await RisingEdge(dut.clk)
    await host.link_up(timeout=40000)
    got = await count_trigger_packets(dut, 300)
    assert got == [], f"after reset with the pin held: {got}"

    dut.trigger_in_app.value = 0
    for _ in range(20):
        await RisingEdge(dut.clk)
    dut.trigger_in_app.value = 1
    got = await count_trigger_packets(dut, 200)
    assert got == [K28_4], f"real rising edge: {got}"

    dut.link_reset_req.value = 1
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.link_reset_req.value = 0
    got = await count_trigger_packets(dut, 600)
    assert got == [K28_2], f"ConnectionReset with the pin held: {got}"

    dut.trigger_in_app.value = 0
    got = await count_trigger_packets(dut, 50)
    dut.trigger_in_app.value = 1
    got += await count_trigger_packets(dut, 200)
    assert got == [K28_4], f"pin 0 then 1 after the reset: {got}"
