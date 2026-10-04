"""Cocotb TB for `cxp_rx_packet_parser`.

Uplink (host→camera) packet demultiplexer, CoaXPress 1.1.1 (CXP-001-2015
§8.2 K-code map Table 11, §8.3.3 I/O acknowledgment, §8.5 long packets).
The TB drives the 32-bit word / 4-bit kmask interface directly (`rx_clk`
10 ns) and checks the `ioack` / `long_*` outputs with inline asserts; there
is no reference model. FSM coverage is collected for `state_q` (3 states,
5 designed arcs). The low-speed trigger (Table 15) never reaches the
parser: the sampler takes it out of the character stream.

Words arrive through `common/cxp_gapped.GappedDriver`, not back to back:
the real uplink delivers one word per 40 x `p_OS_RATIO` clocks, so `d_valid`
drops for a random 0-4 cycles before each word and the bus carries the stale
word or random junk (data *and* kmask) meanwhile. A parser that sampled
`d_in` without `d_valid` would show it as a wrong or extra event.

The outputs are combinational on (`state_q`, `d_in`), so an event fires
DURING the cycle whose `d_in` is the relevant payload word — the cycle after
the leader for an I/O acknowledgment, the same cycle as the word for
long-packet words.
`drive_seq` samples inside the driver's per-word yield, so capture i is the
parser's response to `seq[i]` (post-transition `state_q` + the word
currently on `d_in`) however long the gap before it was. Test 03 (the v1.0
GPIO packet) was removed with CXP 1.1 and tests 04–06 (the Table 16 word-form
trigger) with the word-form parser path; the remaining tests keep their
numbers.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Reset → no `long_valid`.
  2  IDLE words discarded — no output, no `pkt_err`.
  7  Long packet SOP / TYPE / 3 body words / EOP → 5 `long_*` events.
  8  Second SOP inside a body (lost trailer) → first packet aborted
     (`long_eop` + `long_err`), second packet framed normally.
  9  SOP and EOP with one corrupted lane (3 of 4) still frame a packet;
     a single K27.7 lane in hunt does not start one.
 10  IDLE inside a body → packet aborted, back to hunt.
 11  `flush` mid-body → abort word; `flush` in hunt → nothing.
 12  Body longer than RX_MAX_BODY_WORDS (2048) → packet aborted.
 13  A body word with an 8B/10B error → forwarded with `long_err`, no
     `long_eop`; the packet continues.
 14  A K-character where the TYPE word belongs → no SOP, `pkt_err`, back
     to hunt; the next packet still frames.
 15  Table 17 I/O acknowledgment (4×K28.6 + 4×0x01) between packets and
     inside a command body (also with its leader doubled) → one `ioack`
     each, its words taken out, the command goes on; a wrong code or a
     missing code word → no `ioack`.
 16  A 4×K28.4 word right before a command (a trigger leader cut short)
     → the command still frames: TYPE with `long_sop`, body, EOP.
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import NextTimeStep, ReadOnly, RisingEdge

from cxp_gapped import GappedDriver
from cxp_testcase import cxp_test
from fsm_coverage import register_fsm


CLK   = 10
K28_0 = 0x1C
K28_2 = 0x5C
K28_4 = 0x9C
K28_5 = 0xBC
K28_6 = 0xDC
K27_7 = 0xFB
K29_7 = 0xFD
K28_1 = 0x3C
D21_5 = 0xB5


# -----------------------------------------------------------------------------
# FSM coverage
# -----------------------------------------------------------------------------
register_fsm(
    name="rx_packet_parser",
    states=["ST_IDLE_HUNT", "ST_LONG_TYPE", "ST_LONG_BODY"],
    state_path="cxp_rx_packet_parser_i.state_q",
    clk_path="rx_clk",
    arcs=[
        ("ST_IDLE_HUNT", "ST_LONG_TYPE"), ("ST_LONG_TYPE", "ST_LONG_BODY"),
        ("ST_LONG_BODY", "ST_IDLE_HUNT"), ("ST_LONG_BODY", "ST_LONG_TYPE"),
        ("ST_LONG_TYPE", "ST_IDLE_HUNT"),
    ],
)


# -----------------------------------------------------------------------------
# Word builders
# -----------------------------------------------------------------------------
def rep4(b: int) -> int:
    """Byte `b` replicated on all four lanes P0..P3."""
    return ((b & 0xFF) << 24) | ((b & 0xFF) << 16) | ((b & 0xFF) << 8) | (b & 0xFF)


def w(b0: int, b1: int, b2: int, b3: int) -> int:
    """Pack four lane bytes into a word, P0 in bits [7:0]."""
    return ((b3 & 0xFF) << 24) | ((b2 & 0xFF) << 16) | ((b1 & 0xFF) << 8) | (b0 & 0xFF)


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def reset(dut):
    """Start the clock, zero the inputs, hold `rx_rst_n` low 4 cycles."""
    cocotb.start_soon(Clock(dut.rx_clk, CLK, unit="ns").start())
    dut.rx_rst_n.value = 0
    dut.d_in.value     = 0
    dut.d_kmask.value  = 0
    dut.d_valid.value  = 0
    dut.flush.value    = 0
    dut.d_err.value    = 0
    for _ in range(4):
        await RisingEdge(dut.rx_clk)
    dut.rx_rst_n.value = 1
    await RisingEdge(dut.rx_clk)


# -----------------------------------------------------------------------------
# Driver / monitor
# -----------------------------------------------------------------------------
async def drive_seq(dut, seq: list[tuple[int, int]], max_gap: int = 4):
    """Drive a list of (data, kmask) words with gaps, and capture outputs.

    Returns a list of sampled output events; index i corresponds to the
    cycle where `seq[i]` is on `d_in` with `d_valid` high, whatever the
    gaps in between did. `max_gap = 0` drives the words back to back.
    """
    captured: list[dict] = []

    def quiet():
        """No event may fire on a cycle the bus is not valid."""
        assert int(dut.long_valid.value) == 0, "long_valid while d_valid low"
        assert int(dut.pkt_err_pulse.value) == 0, "pkt_err while d_valid low"
        assert int(dut.ioack.value) == 0, "ioack while d_valid low"

    drv = GappedDriver(dut.rx_clk, dut.d_valid, dut.d_in, dut.d_kmask,
                       max_gap=max_gap, idle_check=quiet)
    async for _beat in drv.words(seq):
        await ReadOnly()
        captured.append({
            "long_valid": int(dut.long_valid.value),
            "long_data":  int(dut.long_data.value),
            "long_kmask": int(dut.long_kmask.value),
            "long_sop":   int(dut.long_sop.value),
            "long_eop":   int(dut.long_eop.value),
            "long_err":   int(dut.long_err.value),
            "long_type":  int(dut.long_type.value),
            "pkt_err":    int(dut.pkt_err_pulse.value),
            "ioack":      int(dut.ioack.value),
        })
    return captured


# -----------------------------------------------------------------------------
# TC 1 — Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_reset(dut):
    """After reset with `d_valid = 0` the parser emits no events.

    Baseline for every other test: the output mux defaults hold while no
    word is presented.

    Stimulus: reset sequence only (4 cycles low, 1 cycle after release);
              no words, `d_valid = 0`.
    Checks:   `long_valid == 0` and `ioack == 0` right after the
              post-release edge.
    Note:     only the output defaults are observed; `state_q`, the TYPE
              latch and `pkt_err_pulse` are not checked.
    """
    dut.TESTCASE.value = 1
    await reset(dut)
    assert int(dut.long_valid.value) == 0
    assert int(dut.ioack.value) == 0


# -----------------------------------------------------------------------------
# TC 2 — IDLE Words Discarded
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_idle(dut):
    """IDLE words are dropped in hunt with every output low.

    The low-speed uplink is IDLE-filled between packets (§8.2.5); the parser
    must stay in ST_IDLE_HUNT and neither emit nor flag an error.

    Stimulus: 5 IDLE words (K28.5, K28.1, K28.1, D21.5), kmask 0111.
    Checks:   in every one of the 5 captures `ioack == 0`,
              `long_valid == 0` and `pkt_err == 0`.
    """
    dut.TESTCASE.value = 2
    await reset(dut)
    cap = await drive_seq(dut, [(w(K28_5, K28_1, K28_1, D21_5), 0b0111)] * 5)
    for c in cap:
        assert c["ioack"] == 0
        assert c["long_valid"] == 0
        assert c["pkt_err"] == 0


# -----------------------------------------------------------------------------
# TC 7 — Long Packet
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_long_packet(dut):
    """A framed long packet is streamed on `long_*` with sop, type and eop.

    Covers ST_IDLE_HUNT → ST_LONG_TYPE → ST_LONG_BODY → ST_IDLE_HUNT: the
    TYPE word carries `long_sop` + `long_type`, body words pass verbatim and
    the 4×K29.7 trailer carries `long_eop`.

    Stimulus: 4×K27.7 SOP (kmask 1111), 4×0x02 TYPE, body words 0x11111111,
              0x22222222, 0x33333333 (kmask 0000), 4×K29.7 EOP (kmask 1111).
    Checks:   exactly 5 captures have `long_valid` (TYPE, 3 body, EOP); the
              first has `long_sop == 1` and `long_type == 0x02`; captures
              2..4 carry the body words in order; the fifth has
              `long_eop == 1`.
    Note:     `long_kmask` and the absence of `pkt_err` are not checked.
    """
    dut.TESTCASE.value = 7
    await reset(dut)
    body = [0x11111111, 0x22222222, 0x33333333]
    cap = await drive_seq(dut, [
        (rep4(K27_7), 0b1111),       # SOP
        (rep4(0x02),  0b0000),       # TYPE — long_sop+long_valid here
        (body[0],     0b0000),
        (body[1],     0b0000),
        (body[2],     0b0000),
        (rep4(K29_7), 0b1111),       # EOP
    ])
    longs = [c for c in cap if c["long_valid"]]
    # Expect 5 events: TYPE, body[0..2], K29.7
    assert len(longs) == 5, f"long events: {len(longs)} -> {longs}"
    # First event = TYPE word, sop=1, long_type=0x02
    assert longs[0]["long_sop"] == 1
    assert longs[0]["long_type"] == 0x02
    # body[0..2] match
    assert longs[1]["long_data"] == body[0]
    assert longs[2]["long_data"] == body[1]
    assert longs[3]["long_data"] == body[2]
    # Last event = K29.7, eop=1
    assert longs[4]["long_eop"] == 1


# -----------------------------------------------------------------------------
# TC 8 — Long Packet Truncated by a Second SOP
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_truncated(dut):
    """A second SOP inside a body aborts the first packet and starts anew.

    §8.2.2.1 / lost trailer: without the resync, the next command's words
    would be read as the tail of the broken one.

    Stimulus: 4×K27.7, 4×0x02, 0xCAFEBABE, then a premature second 4×K27.7
              (no EOP in between), 4×0x02, 0xDEADBEEF, 4×K29.7.
    Checks:   two `long_sop` events; two `long_eop` events — the first on
              the premature SOP with `long_err` = 1 and `pkt_err`, the
              second on K29.7 with `long_err` = 0; 0xDEADBEEF belongs to
              the second packet.
    """
    dut.TESTCASE.value = 8
    await reset(dut)
    cap = await drive_seq(dut, [
        (rep4(K27_7), 0b1111),
        (rep4(0x02),  0b0000),
        (0xCAFEBABE,  0b0000),
        (rep4(K27_7), 0b1111),    # premature 2nd SOP
        (rep4(0x02),  0b0000),
        (0xDEADBEEF,  0b0000),
        (rep4(K29_7), 0b1111),
    ])
    long_evts = [c for c in cap if c["long_valid"]]
    type_events = [c for c in long_evts if c["long_sop"]]
    eop_events  = [c for c in long_evts if c["long_eop"]]
    assert len(type_events) == 2, f"expected 2 SOP, got {len(type_events)}"
    assert len(eop_events)  == 2, f"expected 2 EOP, got {len(eop_events)}"
    assert cap[3]["long_eop"] and cap[3]["long_err"] and cap[3]["pkt_err"]
    assert cap[6]["long_eop"] and not cap[6]["long_err"]
    assert cap[4]["long_sop"] and cap[5]["long_data"] == 0xDEADBEEF


# -----------------------------------------------------------------------------
# TC 9 — Three Of Four Lanes
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_three_of_four(dut):
    """SOP and EOP survive one bad lane; one K27.7 lane is not an SOP.

    §8.2.2.1: replicated characters are voted.

    Stimulus: K27.7 in P0 only (kmask 0x1), 4×0x02, data, 4×K29.7 —
              then K27.7 ×3 with P3 = 0x00 (kmask 0x7), 4×0x02, data,
              K29.7 ×3 with P1 = 0x00 (kmask 0xD).
    Checks:   the first sequence makes no packet; the second makes one
              packet with `long_sop` on its TYPE and a clean `long_eop`.
    """
    dut.TESTCASE.value = 9
    await reset(dut)
    cap = await drive_seq(dut, [
        (w(K27_7, 0, 0, 0), 0b0001),
        (rep4(0x02), 0), (0x1234_5678, 0),
        (rep4(K29_7), 0b1111),
        (w(K27_7, K27_7, K27_7, 0), 0b0111),
        (rep4(0x02), 0), (0x1234_5678, 0),
        (w(K29_7, 0, K29_7, K29_7), 0b1101),
    ])
    assert not any(c["long_valid"] for c in cap[:4]), "one K27.7 lane started a packet"
    assert cap[5]["long_sop"] and cap[6]["long_valid"]
    assert cap[7]["long_eop"] and not cap[7]["long_err"]


# -----------------------------------------------------------------------------
# TC 10 — IDLE Inside A Body
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_idle_in_body(dut):
    """An IDLE inside a low-speed body means the trailer was lost (§8.2.5.2).

    Stimulus: 4×K27.7, 4×0x04, data, IDLE, data, 4×K29.7.
    Checks:   the IDLE cycle is an abort word (`long_eop`, `long_err`,
              `pkt_err`); nothing after it is forwarded as body.
    """
    dut.TESTCASE.value = 10
    await reset(dut)
    idle = (w(K28_5, K28_1, K28_1, D21_5), 0b0111)
    cap = await drive_seq(dut, [
        (rep4(K27_7), 0b1111), (rep4(0x04), 0), (0x0302_0100, 0),
        idle, (0x0706_0504, 0), (rep4(K29_7), 0b1111),
    ])
    assert cap[3]["long_eop"] and cap[3]["long_err"] and cap[3]["pkt_err"]
    assert not any(c["long_valid"] for c in cap[4:]), "words forwarded after the abort"


# -----------------------------------------------------------------------------
# TC 11 — Flush
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_flush(dut):
    """`flush` (link lost) ends a packet in flight with an abort word.

    Stimulus: 4×K27.7, 4×0x02, one data word; `flush` for one cycle with
              `d_valid` = 0; then `flush` again while hunting.
    Checks:   the first flush cycle shows `long_valid`, `long_eop`,
              `long_err`; the second shows nothing.
    """
    dut.TESTCASE.value = 11
    await reset(dut)
    await drive_seq(dut, [(rep4(K27_7), 0b1111), (rep4(0x02), 0), (0xAAAA_5555, 0)])
    dut.flush.value = 1
    await ReadOnly()
    got = (int(dut.long_valid.value), int(dut.long_eop.value), int(dut.long_err.value))
    await NextTimeStep()
    await RisingEdge(dut.rx_clk)
    dut.flush.value = 0
    assert got == (1, 1, 1), f"flush mid-packet gave valid/eop/err {got}"
    dut.flush.value = 1
    await ReadOnly()
    got = int(dut.long_valid.value)
    await NextTimeStep()
    await RisingEdge(dut.rx_clk)
    dut.flush.value = 0
    assert got == 0, "flush while hunting produced a word"


# -----------------------------------------------------------------------------
# TC 12 — Over-Long Body
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_too_long(dut):
    """A body longer than RX_MAX_BODY_WORDS is a lost trailer.

    Stimulus: 4×K27.7, 4×0x04, 2100 data words.
    Checks:   one abort word (`long_eop` + `long_err`) once the body holds
              2048 words (TYPE included), then no more `long_valid`.
    """
    dut.TESTCASE.value = 12
    await reset(dut)
    seq = [(rep4(K27_7), 0b1111), (rep4(0x04), 0)] + [(i, 0) for i in range(2100)]
    cap = await drive_seq(dut, seq)
    aborts = [i for i, c in enumerate(cap) if c["long_eop"]]
    assert aborts == [2049], f"abort at {aborts}"
    assert cap[2049]["long_err"]
    assert not any(c["long_valid"] for c in cap[2050:])


# -----------------------------------------------------------------------------
# TC 13 — Decode Error In The Body
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_decode_error_word(dut):
    """A body word with an 8B/10B error is marked, not dropped or merged.

    Stimulus: 4×K27.7, 4×0x02, data, data with `d_err` = 1, data, 4×K29.7.
    Checks:   the marked word is forwarded with `long_err` = 1 and
              `long_eop` = 0; the packet still ends on K29.7 without
              `long_err`.
    """
    dut.TESTCASE.value = 13
    await reset(dut)
    seq = [(rep4(K27_7), 0b1111), (rep4(0x02), 0), (0x1111_1111, 0),
           (0x2222_2222, 0), (0x3333_3333, 0), (rep4(K29_7), 0b1111)]
    captured = []
    dut.d_valid.value = 1
    for i, (d, k) in enumerate(seq):
        dut.d_in.value, dut.d_kmask.value = d, k
        dut.d_err.value = 1 if i == 3 else 0
        await ReadOnly()
        captured.append((int(dut.long_valid.value), int(dut.long_eop.value),
                         int(dut.long_err.value)))
        await NextTimeStep()
        await RisingEdge(dut.rx_clk)
    dut.d_valid.value = 0
    dut.d_err.value = 0
    assert captured[3] == (1, 0, 1), f"marked word {captured[3]}"
    assert captured[5] == (1, 1, 0), f"trailer {captured[5]}"


# -----------------------------------------------------------------------------
# TC 14 — K-character In The TYPE Position
# -----------------------------------------------------------------------------
@cxp_test()
async def test_14_kchar_in_type(dut):
    """A K-character where the 4×TYPE word belongs starts no packet.

    The word after a K27.7 leader is the packet type (§8.5, Table 17), a
    data character; a K-character there means the leader was spurious or
    the word was lost.  Nothing may be presented as SOP — a long_sop with
    a K-code TYPE would open a packet that cxp_ctrl_cmd_parser then frames on
    garbage — and the parser goes back to hunting.

    Stimulus: 4×K27.7 then an IDLE word; 4×K27.7 then a 4×K29.7 trailer;
              finally a well-formed 3-word packet.
    Checks:   neither K-character TYPE word is forwarded (`long_valid` 0)
              and each raises `pkt_err`; the packet that follows is framed
              normally (SOP, body, EOP).
    """
    dut.TESTCASE.value = 14
    await reset(dut)
    idle = (w(K28_5, K28_1, K28_1, D21_5), 0b0111)
    cap = await drive_seq(dut, [
        (rep4(K27_7), 0b1111), idle,
        (rep4(K27_7), 0b1111), (rep4(K29_7), 0b1111),
        (rep4(K27_7), 0b1111), (rep4(0x02), 0), (0xDEAD_BEEF, 0),
        (rep4(K29_7), 0b1111),
    ])
    for i in (1, 3):
        assert cap[i]["long_valid"] == 0, f"K-code TYPE at {i} presented as SOP"
        assert cap[i]["pkt_err"] == 1, f"K-code TYPE at {i} not flagged"
    assert cap[5]["long_sop"] and cap[5]["long_type"] == 0x02
    assert cap[6]["long_valid"] and not cap[6]["long_eop"]
    assert cap[7]["long_eop"] and not cap[7]["long_err"]


# -----------------------------------------------------------------------------
# TC 15 — I/O Acknowledgment From The Host
# -----------------------------------------------------------------------------
@cxp_test()
async def test_15_ioack_extracted(dut):
    """A Table 17 I/O acknowledgment is taken out of the word stream.

    §8.3.3: the host acknowledges each device trigger with 4×K28.6 +
    4×Code (0x01); §8.2.4: it is inserted at a word boundary, also inside
    a packet the host is sending.

    Stimulus: an acknowledgment between packets (P3 of the leader
              corrupted to K28.5: 3 of 4 lanes); a command SOP, TYPE,
              body word, acknowledgment, body word, EOP; a leader with
              code 0x02; a leader followed by an IDLE word; then a packet
              with a doubled leader and a 0x01 code inside its body.
    Checks:   `ioack` on the three 0x01 code words only; the commands'
              `long_*` events are their TYPE, body words and EOP, with no
              `long_err`; no acknowledgment word reaches `long_*`; no
              `pkt_err` anywhere.
    """
    dut.TESTCASE.value = 15
    await reset(dut)
    lead = (rep4(K28_6), 0b1111)
    lead3 = (w(K28_6, K28_6, K28_6, K28_5), 0b1111)
    idle = (w(K28_5, K28_1, K28_1, D21_5), 0b0111)
    cap = await drive_seq(dut, [
        lead3, (rep4(0x01), 0),                                 # 0, 1
        (rep4(K27_7), 0b1111), (rep4(0x02), 0), (0x1111_1111, 0),  # 2, 3, 4
        lead, (rep4(0x01), 0),                                  # 5, 6
        (0x2222_2222, 0), (rep4(K29_7), 0b1111),                # 7, 8
        lead, (rep4(0x02), 0),                                  # 9, 10
        lead, idle,                                             # 11, 12
        (rep4(K27_7), 0b1111), (rep4(0x02), 0), (0x3333_3333, 0),  # 13, 14, 15
        lead, lead, (rep4(0x01), 0),                            # 16, 17, 18
        (0x4444_4444, 0), (rep4(K29_7), 0b1111),                # 19, 20
    ])
    acks = [i for i, c in enumerate(cap) if c["ioack"]]
    assert acks == [1, 6, 18], f"ioack at {acks}"
    longs = [(i, c["long_data"]) for i, c in enumerate(cap) if c["long_valid"]]
    assert [i for i, _ in longs] == [3, 4, 7, 8, 14, 15, 19, 20], f"long events at {longs}"
    assert not any(c["long_err"] for c in cap), "a packet was aborted"
    assert not any(c["pkt_err"] for c in cap), "pkt_err raised"
    assert cap[3]["long_sop"] and cap[8]["long_eop"] and cap[20]["long_eop"]


# -----------------------------------------------------------------------------
# TC 16 — SOP After A Trigger Leader
# -----------------------------------------------------------------------------
@cxp_test()
async def test_16_sop_after_trig_leader(dut):
    """A stray trigger leader word does not eat the packet after it.

    The low-speed trigger is the six-character Table 15 packet, taken out
    by the sampler; a K28.4 word reaching the parser is a damaged leader
    and carries no Delay word to wait for (§8.2.2, §8.3.2.1).

    Stimulus: 4×K28.4; SOP, TYPE 0x02, body 0x1111_1111, EOP.
    Checks:   `long_*` events on TYPE (with `long_sop`, type 0x02), the
              body word and EOP (with `long_eop`); no `long_err`.
    """
    dut.TESTCASE.value = 16
    await reset(dut)
    cap = await drive_seq(dut, [
        (rep4(K28_4), 0b1111),
        (rep4(K27_7), 0b1111), (rep4(0x02), 0), (0x1111_1111, 0),
        (rep4(K29_7), 0b1111),
    ])
    longs = [i for i, c in enumerate(cap) if c["long_valid"]]
    assert longs == [2, 3, 4], f"long events at {longs}"
    assert cap[2]["long_sop"] and cap[2]["long_type"] == 0x02
    assert cap[4]["long_eop"] and not any(c["long_err"] for c in cap)
