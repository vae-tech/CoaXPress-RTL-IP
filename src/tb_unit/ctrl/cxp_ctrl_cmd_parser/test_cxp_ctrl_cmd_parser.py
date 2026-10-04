"""Cocotb TB for `cxp_ctrl_cmd_parser`.

The DUT parses a host → device control command (§8.6.2, Table 21) out of
the long-packet body stream of `cxp_rx_packet_parser` (gated by
`long_type == 0x02`), buffers the write data, checks the CRC, and only
then emits one command record per packet for the executor.  The wrapper
decodes the record into the outputs the tests read: a one-cycle
`cmd_valid` with op / size / address / word count for a command to
execute, `cmd_crc_err_pulse` (0x80) or `cmd_logical_err_pulse` with its
Table 22 code for one to refuse.

Single 10 ns `rx_clk`; the wrapper sets BUF_DEPTH = 16 (RTL default 64)
and a packet size limit of 88 bytes, i.e. N <= 16 words.
`make_packet` builds the packet with the golden `cxp_protocol` codec in
its specification profile (`SPEC`, Table 21) — TYPE word (SOP), word 0 =
Cmd in P0 + Size in P1..P3, word 1 = Addr (big-endian), N data words,
CRC word, K29.7 word (EOP, kmask 0xF) — and `drive_packet` presents it
one word per cycle. `monitor_cmd` samples `cmd_valid` (first one:
op/addr/size/word count), both error pulses and the logical error code
after every edge.

Write data are register values; the codec puts them on the wire
big-endian (§8.2.1) and the RTL byte-swaps them back, so wbuf holds the
register values.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Read command (op=0x00, size=4, addr=X) with valid CRC → cmd_valid,
     fields propagated correctly.
  2  Write command (op=0x01, 4 data words) with valid CRC → cmd_valid,
     wbuf populated with the byte-swapped data.
  3  Control reset (op=0xFF) → cmd_valid with op=0xFF.
  4  CRC error → cmd_crc_err_pulse, no cmd_valid.
  5  N = 16 (the limit) is accepted; N = 17 → cmd_logical_err_pulse
     code 0x45, no cmd_valid.
  6  Read of multi-word range (N=4) → cmd_word_count=4, op=0x00.
  7  Read of B = 3 bytes → Size 3, N = 1.
  8  Write of B = 8 bytes → Size 8, N = 2, wbuf holds both values.
  9  The §8.2.2.2 worked example (read of address 0) is accepted.
 10  A packet the parser aborts (lost trailer) → logical error 0x47; the
     next command executes.
 11  A word marked with an 8B/10B error → CRC error, even with a good CRC.
 12  Undefined opcodes → 0x42, with or without trailing words.
 13  A read or write with Size 0 → 0x46.
 14  A packet shorter or longer than its Size → 0x46, including one cut
     off where the address word belongs.
 15  A trailer right after the TYPE word → 0x47.
 16  A reset with a non-zero Size or Addr is still a reset.
 17  A read whose Size is within 3 of the 24-bit maximum → 0x45.
 18  Extension link: a write → 0x43; a write of ConnectionReset or
     MasterHostConnectionID → 0x01 without execution; a read executes.
 19  Executor full at the start of a packet: a write is dropped with its
     data (the other bank intact), a read is dropped, a 0xFF still
     comes out.
 20  Two writes back to back land in different banks; each command
     names its own.
"""

from __future__ import annotations


from cxp_protocol import SPEC
from cxp_protocol import packets as gp
from cxp_protocol import regmap as rm

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly, NextTimeStep

from cxp_testcase import cxp_test
from fsm_coverage import register_fsm


CLK       = 10
BUF_DEPTH = 16


# -----------------------------------------------------------------------------
# FSM coverage
# -----------------------------------------------------------------------------
register_fsm(
    name="rx_ctrl_cmd",
    states=["ST_IDLE", "ST_CMDSZ", "ST_ADDR", "ST_DATA", "ST_CRC",
            "ST_EOP"],
    state_path="cxp_ctrl_cmd_parser_i.state_q",
    clk_path="rx_clk",
    arcs=[
        ("ST_IDLE", "ST_CMDSZ"), ("ST_CMDSZ", "ST_ADDR"),
        ("ST_ADDR", "ST_DATA"), ("ST_ADDR", "ST_CRC"), ("ST_DATA", "ST_CRC"),
        ("ST_CRC", "ST_EOP"), ("ST_EOP", "ST_IDLE"), ("ST_CRC", "ST_IDLE"), ("ST_DATA", "ST_IDLE"),
        ("ST_ADDR", "ST_IDLE"), ("ST_CMDSZ", "ST_IDLE"),
    ],
)


# -----------------------------------------------------------------------------
# Packet model
# -----------------------------------------------------------------------------
def make_packet(op: int, addr: int, size: int,
                data_words: list[int] | None = None,
                crc_corrupt: bool = False) -> list[tuple[int, int, int, int]]:
    """(data, kmask, sop, eop) per body word of a Table 21 command.

    Built by the golden codec (`SPEC`): the SOP word is dropped, the TYPE
    word carries sop and the K29.7 word carries eop.  `data_words` are
    register values.
    """
    beats = gp.ctrl_cmd(op, addr, size, data_words or [], q=SPEC,
                        corrupt_crc=crc_corrupt)[1:]
    out = [(w, k, 0, 0) for w, k in beats]
    out[0] = (out[0][0], out[0][1], 1, 0)
    out[-1] = (out[-1][0], out[-1][1], 0, 1)
    return out


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def reset(dut):
    """Start `rx_clk`, zero all inputs, hold reset 4 cycles, release + 1."""
    cocotb.start_soon(Clock(dut.rx_clk, CLK, unit="ns").start())
    dut.rx_rst_n.value   = 0
    dut.long_data.value  = 0
    dut.long_kmask.value = 0
    dut.long_valid.value = 0
    dut.long_sop.value   = 0
    dut.long_eop.value   = 0
    dut.long_err.value   = 0
    dut.long_type.value  = 0
    dut.wbuf_addr.value  = 0
    dut.from_extension_link.value = 0
    dut.cmd_full.value   = 0
    dut.wbuf_raw.value   = 0
    dut.wbuf_bank.value  = 0
    for _ in range(4):
        await RisingEdge(dut.rx_clk)
    dut.rx_rst_n.value = 1
    await RisingEdge(dut.rx_clk)


# -----------------------------------------------------------------------------
# Driver / monitor
# -----------------------------------------------------------------------------
async def drive_packet(dut, beats: list[tuple[int, int, int, int]]):
    """Drive one beat per cycle with `long_type = 0x02`, then idle 1 cycle."""
    for d, km, sop, eop in beats:
        dut.long_data.value  = d
        dut.long_kmask.value = km
        dut.long_sop.value   = sop
        dut.long_eop.value   = eop
        dut.long_valid.value = 1
        dut.long_type.value  = 0x02
        await RisingEdge(dut.rx_clk)
    dut.long_valid.value = 0
    dut.long_sop.value   = 0
    dut.long_eop.value   = 0
    await RisingEdge(dut.rx_clk)


class CmdMonitor:
    """What `monitor_cmd` saw: first command's fields and error pulses."""

    def __init__(self):
        self.fired    = False
        self.op       = None
        self.addr     = None
        self.size     = None
        self.wc       = None
        self.crc_err  = False
        self.log_err  = False
        self.log_code = None


async def monitor_cmd(dut, mon: "CmdMonitor", n_cycles: int):
    """Sample outputs after each of `n_cycles` edges into `mon`."""
    for _ in range(n_cycles):
        await RisingEdge(dut.rx_clk)
        if int(dut.cmd_valid.value) and not mon.fired:
            mon.fired = True
            mon.op   = int(dut.cmd_op.value)
            mon.addr = int(dut.cmd_addr.value)
            mon.size = int(dut.cmd_size.value)
            mon.wc   = int(dut.cmd_word_count.value)
        if int(dut.cmd_crc_err_pulse.value):
            mon.crc_err = True
        if int(dut.cmd_logical_err_pulse.value):
            mon.log_err = True
            mon.log_code = int(dut.cmd_logical_err_code.value)


async def run(dut, beats: list[tuple[int, int, int, int]]) -> CmdMonitor:
    """Drive `beats` as one packet and return what the monitor saw."""
    mon = CmdMonitor()
    mtask = cocotb.start_soon(monitor_cmd(dut, mon, len(beats) + 8))
    await drive_packet(dut, beats)
    await mtask
    return mon


def rejected(mon: CmdMonitor, code: int) -> bool:
    """Exactly the logical error `code`: no command, no CRC pulse."""
    return mon.log_err and mon.log_code == code and not mon.fired and not mon.crc_err


# -----------------------------------------------------------------------------
# TC 1 — Read Command
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_read(dut):
    """A read command with a valid CRC fires `cmd_valid` with its fields.

    Basic parse path: ST_IDLE → ST_CMDSZ → ST_ADDR → ST_CRC → ST_EOP →
    ST_IDLE with the command out, Cmd/Size from word 0, the big-endian address from word 1,
    N = ceil(Size / 4), §8.2.2.2 CRC pass.

    Stimulus: `make_packet(op=0x00, addr=0x12345678, size=4)` — 5 words
              back-to-back (TYPE, Cmd/Size, Addr, CRC, K29.7); monitor
              window 15 cycles.
    Checks:   `cmd_valid` seen; at the first `cmd_valid`: `cmd_op ==
              0x00`, `cmd_addr == 0x12345678`, `cmd_size == 4`,
              `cmd_word_count == 1`.
    Note:     the absence of both error pulses is not checked.
    """
    dut.TESTCASE.value = 1
    await reset(dut)
    pkt = make_packet(op=0x00, addr=0x12345678, size=4)
    mon = CmdMonitor()
    mtask = cocotb.start_soon(monitor_cmd(dut, mon, len(pkt) + 10))
    await drive_packet(dut, pkt)
    for _ in range(10):
        await RisingEdge(dut.rx_clk)
    await mtask
    assert mon.fired, "cmd_valid did not fire"
    assert mon.op   == 0x00
    assert mon.addr == 0x12345678
    assert mon.size == 4
    assert mon.wc   == 1


# -----------------------------------------------------------------------------
# TC 2 — Write Command
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_write(dut):
    """A write command fires `cmd_valid` and buffers the register values.

    Covers ST_ADDR → ST_DATA → ST_CRC, the big-endian wire → native byte
    swap of write data (§8.2.1) into `wbuf`, and its 1-cycle read port
    used by the APB master.

    Stimulus: `make_packet(op=0x01, addr=0x100, size=16)` with register
              values 0x11223344, 0x55667788, 0x99AABBCC, 0xDDEEFF00
              (non-palindromic so a missing swap is visible) — 9 words;
              then `wbuf_addr = 0..3`, each read one edge later in
              ReadOnly.
    Checks:   `cmd_valid` seen with `cmd_op == 0x01`, `cmd_addr ==
              0x100`, `cmd_word_count == 4`; `wbuf_data` for index i ==
              `data[i]`.
    Note:     `cmd_size` and the absence of error pulses are not checked.
    """
    dut.TESTCASE.value = 2
    await reset(dut)
    # Non-palindrome words so the wire->native byte-swap is exercised.
    data = [0x11223344, 0x55667788, 0x99AABBCC, 0xDDEEFF00]
    pkt = make_packet(op=0x01, addr=0x100, size=16, data_words=data)
    mon = CmdMonitor()
    mtask = cocotb.start_soon(monitor_cmd(dut, mon, len(pkt) + 10))
    await drive_packet(dut, pkt)
    for _ in range(10):
        await RisingEdge(dut.rx_clk)
    await mtask
    assert mon.fired
    assert mon.op == 0x01
    assert mon.addr == 0x100
    assert mon.wc == 4
    # wbuf holds the register value of each big-endian wire word.
    for i, expected in enumerate(data):
        dut.wbuf_addr.value = i
        await RisingEdge(dut.rx_clk)
        await ReadOnly()
        got = int(dut.wbuf_data.value)
        await NextTimeStep()
        assert got == expected, (
            f"wbuf[{i}] = 0x{got:08x}, expected 0x{expected:08x}")


# -----------------------------------------------------------------------------
# TC 3 — Control Reset Opcode
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_reset_op(dut):
    """A control reset (op 0xFF) is forwarded as a normal `cmd_valid`.

    The parser does not act on 0xFF itself; it must pass it to the APB
    master, which performs the control channel reset. Covers the
    ST_ADDR → ST_CRC arc for N = 0.

    Stimulus: `make_packet(op=0xFF, addr=0, size=0)` — 5 words; monitor
              window 15 cycles.
    Checks:   `cmd_valid` seen with `cmd_op == 0xFF`.
    Note:     `cmd_word_count == 0`, address/size and the absence of error
              pulses are not checked.
    """
    dut.TESTCASE.value = 3
    await reset(dut)
    pkt = make_packet(op=0xFF, addr=0, size=0)
    mon = CmdMonitor()
    mtask = cocotb.start_soon(monitor_cmd(dut, mon, len(pkt) + 10))
    await drive_packet(dut, pkt)
    for _ in range(10):
        await RisingEdge(dut.rx_clk)
    await mtask
    assert mon.fired
    assert mon.op == 0xFF


# -----------------------------------------------------------------------------
# TC 4 — CRC Error
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_crc_error(dut):
    """A bad CRC raises `cmd_crc_err_pulse` and suppresses `cmd_valid`.

    §8.6.3: on a CRC failure the device answers 0x80 and must not perform
    the access, so the command must never reach the APB master.

    Stimulus: `make_packet(op=0x00, addr=0x42, size=4, crc_corrupt=True)`
              — the CRC word inverted; 5 words, monitor window 15 cycles.
    Checks:   `cmd_crc_err_pulse` seen; `cmd_valid` never seen.
    Note:     the absence of `cmd_logical_err_pulse` is not checked.
    """
    dut.TESTCASE.value = 4
    await reset(dut)
    pkt = make_packet(op=0x00, addr=0x42, size=4, crc_corrupt=True)
    mon = CmdMonitor()
    mtask = cocotb.start_soon(monitor_cmd(dut, mon, len(pkt) + 10))
    await drive_packet(dut, pkt)
    for _ in range(10):
        await RisingEdge(dut.rx_clk)
    await mtask
    assert mon.crc_err, "cmd_crc_err_pulse did not fire"
    assert not mon.fired, "cmd_valid must NOT fire on CRC error"


# -----------------------------------------------------------------------------
# TC 5 — Oversize Size Field
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_oversize(dut):
    """N over the packet size limit is refused with 0x45 (§8.6.4).

    Table 21: a packet is N + 6 words; with the 88-byte limit N <= 16.

    Stimulus: a write of 16 words (64 bytes); then a write of 17 words
              (68 bytes), valid CRCs.
    Checks:   the first fires `cmd_valid` with N = 16; the second gives
              `cmd_logical_err_pulse` code 0x45, no CRC pulse, no command.
    """
    dut.TESTCASE.value = 5
    await reset(dut)
    words = [0xDEADBEEF + i for i in range(BUF_DEPTH + 1)]
    mon = await run(dut, make_packet(op=0x01, addr=0x1000, size=4 * BUF_DEPTH,
                                     data_words=words[:BUF_DEPTH]))
    assert mon.fired and mon.wc == BUF_DEPTH
    mon = await run(dut, make_packet(op=0x01, addr=0x1000, size=4 * (BUF_DEPTH + 1),
                                     data_words=words))
    assert rejected(mon, gp.ACK_OVERSIZE), vars(mon)


# -----------------------------------------------------------------------------
# TC 6 — Multi-Word Read
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_burst_read(dut):
    """A multi-word read reports N = Size/4 without expecting data words.

    Reads carry no data on the uplink, so ST_ADDR must go straight to
    ST_CRC whatever N is, while `cmd_word_count` still tells the APB
    master how many dwords to read.

    Stimulus: `make_packet(op=0x00, addr=0x80, size=16)` — 5 words (no
              data words); monitor window 15 cycles.
    Checks:   `cmd_valid` seen with `cmd_word_count == 4` and
              `cmd_op == 0x00`.
    Note:     address, size and the absence of error pulses are not
              checked.
    """
    dut.TESTCASE.value = 6
    await reset(dut)
    pkt = make_packet(op=0x00, addr=0x80, size=16)   # 4 32-bit words
    mon = CmdMonitor()
    mtask = cocotb.start_soon(monitor_cmd(dut, mon, len(pkt) + 10))
    await drive_packet(dut, pkt)
    for _ in range(10):
        await RisingEdge(dut.rx_clk)
    await mtask
    assert mon.fired
    assert mon.wc == 4
    assert mon.op == 0x00


# -----------------------------------------------------------------------------
# TC 7 — Read of Three Bytes
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_read_b3(dut):
    """A read of B = 3 bytes reports Size 3 and N = 1 (Table 21).

    Stimulus: SPEC-built read, addr 0x0000_2004, B = 3 — 5 words.
    Checks:   `cmd_valid` with op 0x00, addr 0x2004, size 3, word count 1;
              no error pulse.
    """
    dut.TESTCASE.value = 7
    await reset(dut)
    pkt = make_packet(op=0x00, addr=rm.DEVICE_VENDOR_NAME + 4, size=3)
    assert len(pkt) == 5, "Table 21 read is N + 6 words with N = 0"
    mon = CmdMonitor()
    mtask = cocotb.start_soon(monitor_cmd(dut, mon, len(pkt) + 10))
    await drive_packet(dut, pkt)
    await mtask
    assert mon.fired and not mon.crc_err and not mon.log_err
    assert (mon.op, mon.addr, mon.size, mon.wc) == (0x00, rm.DEVICE_VENDOR_NAME + 4, 3, 1)


# -----------------------------------------------------------------------------
# TC 8 — Write of Eight Bytes
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_write_b8(dut):
    """A write of B = 8 bytes reports Size 8, N = 2 and buffers both values.

    Stimulus: SPEC-built write to 0x0000_4014 of 0x00010028, 0xCAFEBABE —
              7 words (N + 6 with N = 2, less the SOP word).
    Checks:   `cmd_valid` with op 0x01, addr 0x4014, size 8, word count 2;
              wbuf[0..1] = the two register values; no error pulse.
    """
    dut.TESTCASE.value = 8
    await reset(dut)
    data = [rm.CONNECTION_CONFIG_DEFAULT_VALUE, 0xCAFE_BABE]
    pkt = make_packet(op=0x01, addr=rm.CONNECTION_CONFIG, size=8, data_words=data)
    assert len(pkt) == 7
    mon = CmdMonitor()
    mtask = cocotb.start_soon(monitor_cmd(dut, mon, len(pkt) + 10))
    await drive_packet(dut, pkt)
    await mtask
    assert mon.fired and not mon.crc_err and not mon.log_err
    assert (mon.op, mon.addr, mon.size, mon.wc) == (0x01, rm.CONNECTION_CONFIG, 8, 2)
    for i, expected in enumerate(data):
        dut.wbuf_addr.value = i
        await RisingEdge(dut.rx_clk)
        await ReadOnly()
        got = int(dut.wbuf_data.value)
        await NextTimeStep()
        assert got == expected, f"wbuf[{i}] = 0x{got:08x}, expected 0x{expected:08x}"


# -----------------------------------------------------------------------------
# TC 9 — §8.2.2.2 Worked Example
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_spec_worked_example(dut):
    """The §8.2.2.2 example packet (read of address 0) is accepted.

    Stimulus: the literal words 4×0x02, 00 00 00 04, 00 00 00 00,
              56 86 5D 6F, 4×K29.7 (P0 first).
    Checks:   `cmd_valid` with op 0x00, addr 0, size 4; no CRC error.
    """
    dut.TESTCASE.value = 9
    await reset(dut)
    lanes = lambda *b: b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)
    pkt = [(0x0202_0202, 0, 1, 0),
           (lanes(0x00, 0x00, 0x00, 0x04), 0, 0, 0),
           (lanes(0x00, 0x00, 0x00, 0x00), 0, 0, 0),
           (lanes(0x56, 0x86, 0x5D, 0x6F), 0, 0, 0),
           (0xFDFD_FDFD, 0xF, 0, 1)]
    mon = CmdMonitor()
    mtask = cocotb.start_soon(monitor_cmd(dut, mon, len(pkt) + 10))
    await drive_packet(dut, pkt)
    await mtask
    assert mon.fired, "the spec example was not accepted"
    assert not mon.crc_err
    assert (mon.op, mon.addr, mon.size) == (0x00, 0x0, 4)


# -----------------------------------------------------------------------------
# TC 10 — Aborted Packet
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_aborted_packet(dut):
    """A packet ended by a parser abort is acked 0x47 and not executed.

    §8.6.1.1: an invalid command is acknowledged at once and discarded;
    Table 22 0x47 malformed packet.  The parser aborts a packet whose
    trailer was lost (`long_eop` with `long_err`).

    Stimulus: TYPE, Cmd/Size and Addr words of a write, then an abort
              word (`long_eop` = `long_err` = 1); then a complete read.
    Checks:   the abort gives `cmd_logical_err_pulse` with code 0x47 and no
              `cmd_valid`; the read then fires `cmd_valid` with its fields.
    """
    dut.TESTCASE.value = 10
    await reset(dut)
    part = make_packet(op=0x01, addr=rm.CONNECTION_CONFIG, size=4, data_words=[0x1])[:3]
    mon = CmdMonitor()
    mtask = cocotb.start_soon(monitor_cmd(dut, mon, len(part) + 6))
    for d, km, sop, eop in part:
        dut.long_data.value, dut.long_kmask.value = d, km
        dut.long_sop.value, dut.long_eop.value = sop, eop
        dut.long_valid.value, dut.long_type.value = 1, 0x02
        await RisingEdge(dut.rx_clk)
    dut.long_sop.value = 0
    dut.long_eop.value = 1
    dut.long_err.value = 1
    await RisingEdge(dut.rx_clk)
    dut.long_valid.value = 0
    dut.long_eop.value = 0
    dut.long_err.value = 0
    await mtask
    assert mon.log_err and mon.log_code == 0x47, f"abort code {mon.log_code}"
    assert not mon.fired and not mon.crc_err

    pkt = make_packet(op=0x00, addr=rm.DEVICE_VENDOR_NAME + 8, size=4)
    mon = CmdMonitor()
    mtask = cocotb.start_soon(monitor_cmd(dut, mon, len(pkt) + 6))
    await drive_packet(dut, pkt)
    await mtask
    assert mon.fired and (mon.op, mon.addr) == (0x00, rm.DEVICE_VENDOR_NAME + 8)


# -----------------------------------------------------------------------------
# TC 11 — Decode Error Mark
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_decode_error_word(dut):
    """A word the receiver marks as badly decoded fails the command.

    The decoder outputs 0x00 for a code error, which may still match the
    CRC by chance; the mark makes the failure certain (0x80).

    Stimulus: a SPEC-built read with a valid CRC; `long_err` = 1 (no eop)
              on the Addr word.
    Checks:   `cmd_crc_err_pulse` seen; `cmd_valid` never seen.
    """
    dut.TESTCASE.value = 11
    await reset(dut)
    pkt = make_packet(op=0x00, addr=rm.DEVICE_VENDOR_NAME, size=4)
    mon = CmdMonitor()
    mtask = cocotb.start_soon(monitor_cmd(dut, mon, len(pkt) + 6))
    for i, (d, km, sop, eop) in enumerate(pkt):
        dut.long_data.value, dut.long_kmask.value = d, km
        dut.long_sop.value, dut.long_eop.value = sop, eop
        dut.long_err.value = 1 if i == 2 else 0
        dut.long_valid.value, dut.long_type.value = 1, 0x02
        await RisingEdge(dut.rx_clk)
    dut.long_valid.value = 0
    dut.long_sop.value = 0
    dut.long_eop.value = 0
    dut.long_err.value = 0
    await mtask
    assert mon.crc_err and not mon.fired


# -----------------------------------------------------------------------------
# TC 12 — Undefined Opcode
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_undefined_opcode(dut):
    """An undefined opcode is refused with 0x42 (Table 21: reserved).

    Stimulus: SPEC-built packets with Cmd 0x02 and 0x80 (read form); Cmd
              0x7F followed by three extra words; then a valid read.
    Checks:   each gives code 0x42 and no command; the read then fires.
    """
    dut.TESTCASE.value = 12
    await reset(dut)
    for op in (0x02, 0x80):
        mon = await run(dut, make_packet(op=op, addr=rm.DEVICE_VENDOR_NAME, size=4))
        assert rejected(mon, gp.ACK_BAD_OP), (op, vars(mon))
    pkt = make_packet(op=0x7F, addr=rm.DEVICE_VENDOR_NAME, size=12)
    pkt = pkt[:-1] + [(0x11111111 * k, 0, 0, 0) for k in (1, 2, 3)] + pkt[-1:]
    mon = await run(dut, pkt)
    assert rejected(mon, gp.ACK_BAD_OP), vars(mon)
    mon = await run(dut, make_packet(op=0x00, addr=rm.DEVICE_VENDOR_NAME + 4, size=4))
    assert mon.fired and mon.addr == rm.DEVICE_VENDOR_NAME + 4


# -----------------------------------------------------------------------------
# TC 13 — Size Zero
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_size_zero(dut):
    """A read or write of zero bytes is refused with 0x46.

    Table 21: Size shall be >= 1 except for a control channel reset.

    Stimulus: SPEC-built read with Size 0; write with Size 0, no data.
    Checks:   both give code 0x46 and no command.
    """
    dut.TESTCASE.value = 13
    await reset(dut)
    for op in (0x00, 0x01):
        mon = await run(dut, make_packet(op=op, addr=rm.DEVICE_VENDOR_NAME, size=0))
        assert rejected(mon, gp.ACK_SIZE_MISMATCH), (op, vars(mon))


# -----------------------------------------------------------------------------
# TC 14 — Length Mismatch
# -----------------------------------------------------------------------------
@cxp_test()
async def test_14_length_mismatch(dut):
    """A packet shorter or longer than its Size is refused with 0x46.

    Table 22 0x46: message size inconsistent with the size indication.

    Stimulus: a write of Size 8 with one of its two data words removed;
              a read with one extra word before the trailer; a read with
              its CRC word removed; a read cut off right after its
              Cmd/Size word, so the trailer lands where the address
              belongs; then a valid write.
    Checks:   each of the four gives code 0x46 and no command; the write
              then fires.
    """
    dut.TESTCASE.value = 14
    await reset(dut)
    pkt = make_packet(op=0x01, addr=rm.CONNECTION_CONFIG, size=8, data_words=[1, 2])
    mon = await run(dut, pkt[:3] + pkt[4:])
    assert rejected(mon, gp.ACK_SIZE_MISMATCH), vars(mon)
    pkt = make_packet(op=0x00, addr=rm.DEVICE_VENDOR_NAME, size=4)
    mon = await run(dut, pkt[:-1] + [(0, 0, 0, 0)] + pkt[-1:])
    assert rejected(mon, gp.ACK_SIZE_MISMATCH), vars(mon)
    mon = await run(dut, pkt[:-2] + pkt[-1:])
    assert rejected(mon, gp.ACK_SIZE_MISMATCH), vars(mon)
    mon = await run(dut, pkt[:2] + pkt[-1:])
    assert rejected(mon, gp.ACK_SIZE_MISMATCH), vars(mon)
    mon = await run(dut, make_packet(op=0x01, addr=rm.CONNECTION_CONFIG, size=4, data_words=[7]))
    assert mon.fired and mon.op == 0x01


# -----------------------------------------------------------------------------
# TC 15 — Empty Packet
# -----------------------------------------------------------------------------
@cxp_test()
async def test_15_empty_packet(dut):
    """A trailer right after the TYPE word is refused with 0x47.

    Stimulus: TYPE word (sop), then the trailer (eop).
    Checks:   code 0x47, no command.
    """
    dut.TESTCASE.value = 15
    await reset(dut)
    pkt = make_packet(op=0x00, addr=rm.DEVICE_VENDOR_NAME, size=4)
    mon = await run(dut, [pkt[0], pkt[-1]])
    assert rejected(mon, gp.ACK_MALFORMED), vars(mon)


# -----------------------------------------------------------------------------
# TC 16 — Reset With Fields
# -----------------------------------------------------------------------------
@cxp_test()
async def test_16_reset_with_fields(dut):
    """A control channel reset gets through whatever its Size and Addr.

    §8.6.1.2 makes the reset the host's way out of any state, so the
    parser does not refuse it over fields it does not use.

    Stimulus: SPEC-built reset with Size 4 and Addr 0x1234.
    Checks:   `cmd_valid` with op 0xFF and word count 0.
    """
    dut.TESTCASE.value = 16
    await reset(dut)
    mon = await run(dut, make_packet(op=0xFF, addr=0x1234, size=4))
    assert mon.fired and mon.op == 0xFF and mon.wc == 0, vars(mon)


# -----------------------------------------------------------------------------
# TC 17 — Size Field Wrap
# -----------------------------------------------------------------------------
@cxp_test()
async def test_17_size_field_wrap(dut):
    """The largest Size values are refused as oversize, not wrapped.

    Table 21 Size is 24 bits; N = ceil(Size / 4) must be computed without
    wrapping so the §8.6.4 limit check sees it.

    Stimulus: reads with Size 0xFFFFFD, 0xFFFFFE, 0xFFFFFF, valid CRC.
    Checks:   each gives `cmd_logical_err_pulse` code 0x45, no command,
              no CRC pulse.
    """
    dut.TESTCASE.value = 17
    await reset(dut)
    bad = []
    for size in (0xFFFFFD, 0xFFFFFE, 0xFFFFFF):
        mon = await run(dut, make_packet(op=0x00, addr=0x0, size=size))
        if not rejected(mon, gp.ACK_OVERSIZE):
            bad.append(f"Size {size:#x}: {vars(mon)}")
    assert not bad, bad


# -----------------------------------------------------------------------------
# Helpers for the executor-side tests
# -----------------------------------------------------------------------------
async def run_raw(dut, beats) -> list[tuple[int, int, int]]:
    """Drive one packet; return every record the parser emitted as
    (op, err, wbank)."""
    out: list[tuple[int, int, int]] = []

    async def mon():
        for _ in range(len(beats) + 8):
            await RisingEdge(dut.rx_clk)
            if int(dut.cmd_any.value):
                out.append((int(dut.cmd_op.value), int(dut.cmd_err.value),
                            int(dut.cmd_wbank.value)))

    t = cocotb.start_soon(mon())
    await drive_packet(dut, beats)
    await t
    return out


async def read_wbuf(dut, bank: int, n: int) -> list[int]:
    """Read n words of one bank through the parser's buffer port (the
    wrapper's raw bank select)."""
    words = []
    dut.wbuf_raw.value, dut.wbuf_bank.value = 1, bank
    for i in range(n):
        dut.wbuf_addr.value = i
        await RisingEdge(dut.rx_clk)
        await RisingEdge(dut.rx_clk)
        await ReadOnly()
        words.append(int(dut.wbuf_data.value))
        await NextTimeStep()
    dut.wbuf_raw.value = 0
    return words


# -----------------------------------------------------------------------------
# TC 18 — Extension Link
# -----------------------------------------------------------------------------
@cxp_test()
async def test_18_extension_link(dut):
    """On an extension link the parser answers writes itself.

    §5.1: a Device refuses writes that arrive on an extension link;
    §10.3.28 / §10.3.30 notes: a ConnectionReset or MasterHostConnectionID
    write there is ignored, and §8.6.1.1 still wants one acknowledgment.

    Stimulus: from_extension_link = 1; write 0x10000; write 0x4000; write
              0x4008; read 0x10000; then from_extension_link = 0, write
              0x10000.
    Checks:   records (0x01, 0x43), (0x01, 0x01), (0x01, 0x01),
              (0x00, 0x00), (0x01, 0x00): one per packet, only the last
              two execute.
    """
    dut.TESTCASE.value = 18
    await reset(dut)
    dut.from_extension_link.value = 1
    got = []
    for op, addr in ((0x01, rm.MFR_BASE), (0x01, rm.CONNECTION_RESET),
                     (0x01, rm.MASTER_HOST_CONNECTION_ID), (0x00, rm.MFR_BASE)):
        data = [0x1] if op == 0x01 else None
        got += [(o, e) for o, e, _ in await run_raw(dut, make_packet(op, addr, 4, data))]
    dut.from_extension_link.value = 0
    got += [(o, e) for o, e, _ in await run_raw(dut, make_packet(0x01, rm.MFR_BASE, 4, [0x2]))]
    assert got == [(0x01, 0x43), (0x01, 0x01), (0x01, 0x01), (0x00, 0x00), (0x01, 0x00)], got


# -----------------------------------------------------------------------------
# TC 19 — Executor Full
# -----------------------------------------------------------------------------
@cxp_test()
async def test_19_executor_full(dut):
    """A packet that starts while the executor is full leaves nothing but
    a 0xFF.

    The executor holds one command running and one waiting; a third has
    nowhere to go, and its data must not overwrite either bank.

    Stimulus: write A (4 words) with cmd_full = 0; then cmd_full = 1 and
              write B (4 other words), read, 0xFF; then cmd_full = 0.
    Checks:   A comes out in bank 0; B and the read produce no record; the
              0xFF does (err 0); bank 0 still holds A and bank 1 is not
              written with B.
    """
    dut.TESTCASE.value = 19
    await reset(dut)
    a = [0xA0A0_0000 + i for i in range(4)]
    b = [0xB0B0_0000 + i for i in range(4)]
    first = await run_raw(dut, make_packet(0x01, rm.MFR_BASE, 16, a))
    assert first == [(0x01, 0x00, 0)], first
    dut.cmd_full.value = 1
    rest = []
    rest += await run_raw(dut, make_packet(0x01, rm.MFR_BASE, 16, b))
    rest += await run_raw(dut, make_packet(0x00, rm.MFR_BASE, 4))
    rest += await run_raw(dut, make_packet(0xFF, 0x0, 0))
    dut.cmd_full.value = 0
    assert rest == [(0xFF, 0x00, 1)], rest
    assert await read_wbuf(dut, 0, 4) == a
    assert await read_wbuf(dut, 1, 4) != b, "dropped write stored its data"


# -----------------------------------------------------------------------------
# TC 20 — Write Banks
# -----------------------------------------------------------------------------
@cxp_test()
async def test_20_write_banks(dut):
    """Consecutive writes use alternate banks, so the second arrives while
    the first may still execute.

    Stimulus: write A (3 words) then write B (3 words), cmd_full = 0.
    Checks:   A names bank 0, B bank 1; the buffer read with the bank of
              the last write (the wrapper's) returns B; A's words are
              still in bank 0 (read after a third, 1-word write C that
              lands in bank 0 again and changes only its word 0).
    """
    dut.TESTCASE.value = 20
    await reset(dut)
    a = [0x1111_0000 + i for i in range(3)]
    b = [0x2222_0000 + i for i in range(3)]
    ra = await run_raw(dut, make_packet(0x01, rm.MFR_BASE, 12, a))
    rb = await run_raw(dut, make_packet(0x01, rm.MFR_BASE, 12, b))
    assert [w for _, _, w in ra + rb] == [0, 1], (ra, rb)
    assert await read_wbuf(dut, 1, 3) == b
    rc = await run_raw(dut, make_packet(0x01, rm.MFR_BASE, 4, [0x3333_0000]))
    assert [w for _, _, w in rc] == [0], rc
    assert await read_wbuf(dut, 0, 3) == [0x3333_0000, a[1], a[2]]

