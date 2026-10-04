"""Cocotb TB for `cxp_ctrl_plane`: command parser + executor.

Everything between a received long packet and the register bus
(CXP-001-2015 §8.6): Table 21 commands in as long-packet words, register
accesses out, one held response per command (Wait at most once, then the
final; 0x03 for a control channel reset), read data in a two-bank read
buffer.  The tests of the deleted command router live here, at the
boundary that replaced it.

One 10 ns clock.  The wrapper sets a 16-word buffer (packets up to 88
bytes), a 1 kHz clock parameter (Wait after 20 cycles of a command,
timeout after 50, Wait payload 1234 ms) and a 64-word APB user window at
0x0002_0000 whose slave latency the test sets.  Python models the
register file (answers the next cycle from a dict, per-address codes),
builds commands with the golden `cxp_protocol` codec (`SPEC`, Table 21)
and takes responses: `take` waits for one, reads its data from the read
buffer while it is held, then pulses `rsp_ready`.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Reset: no response, no access.
  2  A read executes once; the response carries its data.
  3  A CRC error answers 0x80, an oversize read 0x45; both pulse nack.
  4  Extension link: a ConnectionReset / MasterHostConnectionID write
     answers 0x01 unexecuted, another write 0x43, a read executes.
  5  0xFF: ctrl_reset_pulse and 0x03.
  6  A slow user slave: one Wait (0x04, 1234 ms) before the final.
  7  A command arriving while one executes waits, then runs.
  8  A response is held until rsp_ready; nothing is lost.
  9  0xFF while a response is held, and while a Wait is held: that
     response, then 0x03, nothing after.
 10  0xFF swept around the moment the Wait is produced (hung slave): at
     most one Wait, then 0x03, nothing after.
 11  Three refused commands while the response is held: two answered,
     the third dropped (one command waits).
 12  The executor frees in the cycle a new command arrives with one
     waiting: the waiting one runs, the new one waits, both answered.
 13  A write arriving while a slow write still executes: both data sets
     intact (two write-buffer banks).
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import NextTimeStep, ReadOnly, RisingEdge

from cxp_protocol import SPEC
from cxp_protocol import packets as gp
from cxp_protocol import regmap as rm
from cxp_testcase import cxp_test

CLK_NS = 10
USER = 0x0002_0000
N_MAX = 16


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class RegFile:
    """Register file: answers every request the next cycle."""

    def __init__(self, dut):
        self.dut = dut
        self.mem: dict[int, int] = {}
        self.err: dict[int, int] = {}
        self.log: list[tuple[int, int, int]] = []
        cocotb.start_soon(self._run())

    async def _run(self):
        d = self.dut
        while True:
            await ReadOnly()
            req = int(d.reg_req.value)
            if req:
                a, we, wd = int(d.reg_addr.value), int(d.reg_we.value), int(d.reg_wdata.value)
                self.log.append((we, a, wd))
            await RisingEdge(d.clk)
            d.reg_ack.value = req
            if req:
                if we and not self.err.get(a):
                    self.mem[a] = wd
                d.reg_rdata.value = self.mem.get(a, 0x5A00_0000 | a)
                d.reg_err.value = self.err.get(a, 0)


def packet(op: int, addr: int, size: int, data=None, corrupt=False):
    """(data, kmask, sop, eop) per long-packet word of a Table 21 command."""
    beats = gp.ctrl_cmd(op, addr, size, data or [], q=SPEC, corrupt_crc=corrupt)[1:]
    out = [(w, k, 0, 0) for w, k in beats]
    out[0] = (out[0][0], out[0][1], 1, 0)
    out[-1] = (out[-1][0], out[-1][1], 0, 1)
    return out


async def send(dut, beats):
    """One word per cycle, type 0x02, then one idle cycle."""
    for w, k, sop, eop in beats:
        dut.long_data.value, dut.long_kmask.value = w, k
        dut.long_sop.value, dut.long_eop.value = sop, eop
        dut.long_valid.value, dut.long_type.value = 1, 0x02
        await RisingEdge(dut.clk)
    dut.long_valid.value = dut.long_sop.value = dut.long_eop.value = 0
    await RisingEdge(dut.clk)


async def reset(dut, apb_latency: int = 1) -> RegFile:
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    for n in ("long_data", "long_kmask", "long_valid", "long_sop", "long_eop", "long_err",
              "long_type", "from_extension_link", "reg_ack", "reg_rdata", "reg_err",
              "rbuf_bank", "rbuf_addr", "rsp_ready"):
        getattr(dut, n).value = 0
    dut.apb_latency.value = apb_latency
    dut.rst_n.value = 0
    reg = RegFile(dut)
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    return reg


async def take(dut, timeout: int = 300, hold: int = 0) -> tuple:
    """Wait for a response, read its data from the read buffer while it is
    held (and `hold` cycles more), then take it.
    Returns (code, size, wait_ms, data)."""
    for _ in range(timeout):
        await ReadOnly()
        if int(dut.rsp_valid.value):
            break
        await RisingEdge(dut.clk)
    else:
        raise AssertionError("no response")
    code, size = int(dut.rsp_code.value), int(dut.rsp_size.value)
    wait_ms, bank = int(dut.rsp_wait_ms.value), int(dut.rsp_rbank.value)
    data = []
    await RisingEdge(dut.clk)
    if code == gp.ACK_OK_DATA:
        dut.rbuf_bank.value = bank
        for i in range((size + 3) // 4):
            dut.rbuf_addr.value = i
            await RisingEdge(dut.clk)
            await RisingEdge(dut.clk)
            await ReadOnly()
            data.append(int(dut.rbuf_data.value))
            await RisingEdge(dut.clk)
    for _ in range(hold):
        await ReadOnly()
        assert int(dut.rsp_valid.value) == 1, "response dropped while held"
        assert int(dut.rsp_code.value) == code, "response changed while held"
        await RisingEdge(dut.clk)
    dut.rsp_ready.value = 1
    await RisingEdge(dut.clk)
    dut.rsp_ready.value = 0
    return code, size, wait_ms, data


async def codes(dut, cycles: int) -> list[int]:
    """Take every response for `cycles` cycles; return their codes."""
    out = []
    for _ in range(cycles):
        await ReadOnly()
        if int(dut.rsp_valid.value):
            await NextTimeStep()
            out.append((await take(dut))[0])
            continue
        await RisingEdge(dut.clk)
    return out


async def final(dut, timeout: int = 300) -> tuple:
    """Take responses up to the first that is not a Wait; return it."""
    while True:
        r = await take(dut, timeout)
        if r[0] != gp.ACK_WAIT:
            return r


class Pulses:
    """Counts ctrl_reset_pulse and records nack codes."""

    def __init__(self, dut):
        self.dut, self.resets, self.nacks = dut, 0, []
        cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await RisingEdge(self.dut.clk)
            await ReadOnly()
            self.resets += int(self.dut.ctrl_reset_pulse.value)
            if int(self.dut.nack_pulse.value):
                self.nacks.append(int(self.dut.nack_code.value))


# -----------------------------------------------------------------------------
# TC 1 — Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_reset(dut):
    """After reset nothing happens without a command.

    Stimulus: reset, 50 idle cycles.
    Checks:   no response, no register access.
    """
    dut.TESTCASE.value = 1
    reg = await reset(dut)
    assert await codes(dut, 50) == []
    assert reg.log == []


# -----------------------------------------------------------------------------
# TC 2 — Read
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_read(dut):
    """A read executes once and its response carries the data.

    Stimulus: 0x4000..0x4008 = 1, 2, 3; read 12 bytes at 0x4000.
    Checks:   three register reads; response 0x00, Size 12, data [1, 2, 3].
    """
    dut.TESTCASE.value = 2
    reg = await reset(dut)
    reg.mem.update({rm.CONNECTION_RESET: 1, rm.DEVICE_CONNECTION_ID: 2, rm.MASTER_HOST_CONNECTION_ID: 3})
    await send(dut, packet(gp.OP_READ, rm.CONNECTION_RESET, 12))
    assert await take(dut) == (gp.ACK_OK_DATA, 12, 0, [1, 2, 3])
    assert [a for _, a, _ in reg.log] == [rm.CONNECTION_RESET, rm.DEVICE_CONNECTION_ID, rm.MASTER_HOST_CONNECTION_ID]


# -----------------------------------------------------------------------------
# TC 3 — Parser Errors
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_parser_errors(dut):
    """Commands the parser refuses are answered with their code.

    Stimulus: a read with a corrupted CRC; a read of N_MAX + 1 words.
    Checks:   0x80 then 0x45; nack pulses 0x80, 0x45; no access.
    """
    dut.TESTCASE.value = 3
    reg = await reset(dut)
    p = Pulses(dut)
    await send(dut, packet(gp.OP_READ, rm.CONNECTION_RESET, 4, corrupt=True))
    assert (await take(dut))[0] == gp.ACK_CRC
    await send(dut, packet(gp.OP_READ, rm.CONNECTION_RESET, 4 * (N_MAX + 1)))
    assert (await take(dut))[0] == gp.ACK_OVERSIZE
    assert p.nacks == [gp.ACK_CRC, gp.ACK_OVERSIZE] and reg.log == []


# -----------------------------------------------------------------------------
# TC 4 — Extension Link
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_extension_link(dut):
    """§5.1: an extension link only reads; §10.3.28 / §10.3.30 notes: two
    registers ignore a write there.

    Stimulus: from_extension_link = 1; write 0x4000, write 0x4008, write
              0x10000, read 0x10000.
    Checks:   0x01, 0x01, 0x43 (nack 0x43), 0x00; the only access is the
              read.
    """
    dut.TESTCASE.value = 4
    reg = await reset(dut)
    p = Pulses(dut)
    dut.from_extension_link.value = 1
    got = []
    for op, addr in ((gp.OP_WRITE, rm.CONNECTION_RESET), (gp.OP_WRITE, rm.MASTER_HOST_CONNECTION_ID),
                     (gp.OP_WRITE, rm.MFR_BASE), (gp.OP_READ, rm.MFR_BASE)):
        await send(dut, packet(op, addr, 4, [1] if op == gp.OP_WRITE else None))
        got.append((await take(dut))[0])
    assert got == [gp.ACK_OK_WRITE, gp.ACK_OK_WRITE, gp.ACK_RO_WRITE, gp.ACK_OK_DATA], got
    assert reg.log == [(0, rm.MFR_BASE, 0)] or [(we, a) for we, a, _ in reg.log] == [(0, rm.MFR_BASE)]
    assert p.nacks == [gp.ACK_RO_WRITE]


# -----------------------------------------------------------------------------
# TC 5 — Control Channel Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_reset_op(dut):
    """§8.6.1.2: 0xFF is answered 0x03 and pulses ctrl_reset_pulse.

    Stimulus: 0xFF; then a read.
    Checks:   0x03, one reset pulse; the read works after it.
    """
    dut.TESTCASE.value = 5
    await reset(dut)
    p = Pulses(dut)
    await send(dut, packet(gp.OP_RESET, 0, 0))
    assert (await take(dut))[0] == gp.ACK_OK_RESET
    assert p.resets == 1
    await send(dut, packet(gp.OP_READ, rm.CONNECTION_RESET, 4))
    assert (await take(dut))[0] == gp.ACK_OK_DATA


# -----------------------------------------------------------------------------
# TC 6 — Wait
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_wait(dut):
    """A slow user slave gets one Wait before the final response.

    Stimulus: APB latency 30 (> 20); read USER.
    Checks:   0x04 with 1234 ms, then 0x00 with the slave's word.
    """
    dut.TESTCASE.value = 6
    await reset(dut, apb_latency=30)
    await send(dut, packet(gp.OP_READ, USER, 4))
    code, _, ms, _ = await take(dut)
    assert (code, ms) == (gp.ACK_WAIT, 1234)
    assert await take(dut) == (gp.ACK_OK_DATA, 4, 0, [0xA500_0000])


# -----------------------------------------------------------------------------
# TC 7 — Command While Busy
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_command_while_busy(dut):
    """A command that arrives while one executes waits, then runs.

    Stimulus: APB latency 12; read USER, then at once read 0x4000.
    Checks:   0x00 with USER's word, then 0x00 with 0x4000's word; the
              register read starts only after the first response.
    """
    dut.TESTCASE.value = 7
    reg = await reset(dut, apb_latency=12)
    reg.mem[rm.CONNECTION_RESET] = 0x1234
    await send(dut, packet(gp.OP_READ, USER, 4))
    await send(dut, packet(gp.OP_READ, rm.CONNECTION_RESET, 4))
    assert reg.log == []
    assert await take(dut) == (gp.ACK_OK_DATA, 4, 0, [0xA500_0000])
    assert await take(dut) == (gp.ACK_OK_DATA, 4, 0, [0x1234])


# -----------------------------------------------------------------------------
# TC 8 — Response Held
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_held(dut):
    """A response stays up, unchanged, until rsp_ready.

    Stimulus: read 0x4000; keep rsp_ready low 40 cycles.
    Checks:   rsp_valid and the code hold for those cycles; one response.
    """
    dut.TESTCASE.value = 8
    await reset(dut)
    await send(dut, packet(gp.OP_READ, rm.CONNECTION_RESET, 4))
    assert (await take(dut, hold=40))[0] == gp.ACK_OK_DATA
    assert await codes(dut, 30) == []


# -----------------------------------------------------------------------------
# TC 9 — Reset While A Response Is Held
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_reset_while_rsp_held(dut):
    """§8.6.1.2: a 0xFF drops what is pending; what is already presented
    goes out, then only the 0x03.

    Stimulus: (a) read 0x4000, response held, 0xFF; (b) APB latency 30,
              read USER, the Wait held unread, 0xFF; (c) the same with a
              read of 0x4000 queued behind the USER read.
    Checks:   (a) [0x00, 0x03]; (b) [0x04, 0x03]; (c) [0x04, 0x03]; one
              reset pulse each; nothing after.
    """
    dut.TESTCASE.value = 9
    await reset(dut, apb_latency=30)
    p = Pulses(dut)
    await send(dut, packet(gp.OP_READ, rm.CONNECTION_RESET, 4))
    await send(dut, packet(gp.OP_RESET, 0, 0))
    assert await codes(dut, 80) == [gp.ACK_OK_DATA, gp.ACK_OK_RESET]
    for queued in (False, True):
        await send(dut, packet(gp.OP_READ, USER, 4))
        for _ in range(30):
            await RisingEdge(dut.clk)
        if queued:
            await send(dut, packet(gp.OP_READ, rm.CONNECTION_RESET, 4))
        await send(dut, packet(gp.OP_RESET, 0, 0))
        got = await codes(dut, 150)
        assert got == [gp.ACK_WAIT, gp.ACK_OK_RESET], f"queued={queued}: {got}"
    assert p.resets == 3


# -----------------------------------------------------------------------------
# TC 10 — Wait Then Reset Race
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_wait_then_reset_race(dut):
    """A 0xFF that lands next to the Wait never leaves a Wait behind it.

    Stimulus: APB slave that never answers; read USER, then 0xFF whose
              last word arrives d = 8..39 cycles after the read (the Wait
              is due 20 cycles into the command); responses taken as they
              come.
    Checks:   each round gives [0x03] or [0x04, 0x03]; nothing after the
              0x03; the next read of 0x4000 answers 0x00.
    """
    dut.TESTCASE.value = 10
    await reset(dut, apb_latency=0xFFFF)
    rst = packet(gp.OP_RESET, 0, 0)
    for d in range(8, 40):
        await send(dut, packet(gp.OP_READ, USER, 4))
        for _ in range(d - len(rst)):
            await RisingEdge(dut.clk)
        collected = cocotb.start_soon(codes(dut, 200))
        await send(dut, rst)
        got = await collected
        assert got in ([gp.ACK_OK_RESET], [gp.ACK_WAIT, gp.ACK_OK_RESET]), f"d={d}: {got}"
    await send(dut, packet(gp.OP_READ, rm.CONNECTION_RESET, 4))
    assert (await take(dut, timeout=2000))[0] == gp.ACK_OK_DATA


# -----------------------------------------------------------------------------
# TC 11 — Refused Commands While The Response Is Held
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_own_rsp_overrun(dut):
    """One command may wait; a third is dropped, not merged (D7).

    §8.6.1.1: the Host sends the next command after the acknowledgment;
    one waiting command covers a Host re-sending after its timeout.
    Stimulus: rsp_ready low; a CRC-corrupted read, an oversize read, a
              read with Size 0; then take everything.
    Checks:   [0x80, 0x45], nothing else.
    """
    dut.TESTCASE.value = 11
    await reset(dut)
    await send(dut, packet(gp.OP_READ, rm.CONNECTION_RESET, 4, corrupt=True))
    await send(dut, packet(gp.OP_READ, rm.CONNECTION_RESET, 4 * (N_MAX + 1)))
    await send(dut, packet(gp.OP_READ, rm.CONNECTION_RESET, 0))
    assert await codes(dut, 100) == [gp.ACK_CRC, gp.ACK_OVERSIZE]


# -----------------------------------------------------------------------------
# TC 12 — Start Coincident With A New Command
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_start_coincident_cmd(dut):
    """The executor frees in the cycle a new command arrives while one
    waits: the waiting one runs, the new one takes its place.

    Stimulus: APB latency 5; read USER (A), read 0x4000 (B) queued, then
              read 0x4004 (C) sent so its last word lands d = 0..8 cycles
              around the moment A's response is taken.
    Checks:   every round answers A then B, and C either as the third
              response or not at all (it found the slot full); never out
              of order, never with another's data.
    """
    dut.TESTCASE.value = 12
    reg = await reset(dut, apb_latency=5)
    reg.mem.update({rm.CONNECTION_RESET: 0xB, rm.DEVICE_CONNECTION_ID: 0xC})
    c_pkt = packet(gp.OP_READ, rm.DEVICE_CONNECTION_ID, 4)
    for d in range(9):
        await send(dut, packet(gp.OP_READ, USER, 4))
        await send(dut, packet(gp.OP_READ, rm.CONNECTION_RESET, 4))
        sender = cocotb.start_soon(send(dut, c_pkt))
        first = await take(dut, hold=d)
        await sender
        rest = []
        for _ in range(2):
            try:
                rest.append(await take(dut, timeout=60))
            except AssertionError:
                break
        assert first[3] == [0xA500_0000], f"d={d}: A {first}"
        assert rest and rest[0][3] == [0xB], f"d={d}: B {rest}"
        assert len(rest) == 1 or rest[1][3] == [0xC], f"d={d}: C {rest}"


# -----------------------------------------------------------------------------
# TC 13 — Write Behind A Slow Write
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_write_behind_slow_write(dut):
    """A write that arrives while another write still executes keeps both
    data sets (two write-buffer banks).

    Stimulus: APB latency 2; write W1 (4 words) to USER, then at once
              write W2 (4 words) to USER + 0x40; responses taken as they
              come (a Wait may precede either); then read both back.
    Checks:   two 0x01; USER holds W1 and USER + 0x40 holds W2.
    """
    dut.TESTCASE.value = 13
    await reset(dut, apb_latency=2)
    w1 = [0x1100 + i for i in range(4)]
    w2 = [0x2200 + i for i in range(4)]
    await send(dut, packet(gp.OP_WRITE, USER, 16, w1))
    await send(dut, packet(gp.OP_WRITE, USER + 0x40, 16, w2))
    finals = [c for c in await codes(dut, 300) if c != gp.ACK_WAIT]
    assert finals == [gp.ACK_OK_WRITE] * 2, finals
    await send(dut, packet(gp.OP_READ, USER, 16))
    assert (await final(dut))[3] == w1, "first write lost its data"
    await send(dut, packet(gp.OP_READ, USER + 0x40, 16))
    assert (await final(dut))[3] == w2
