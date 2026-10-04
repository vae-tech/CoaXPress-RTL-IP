"""Cocotb TB for `cxp_ctrl_bus_master`, the control executor.

Executes control commands (CXP-001-2015 §8.6.1) one at a time as
single-word register-bus accesses: words in the user window (0x20000 ..
0x20FFF in the wrapper) go to the APB user port, all others to the
register file port.  Read data land in the read buffer, read back through
the `rbuf_*` port; every response (Wait, final, 0x03) is held on `rsp_*`
until `rsp_ready`.  The wrapper shows the APB user port as the register
bus it was before (`usr_req` = SETUP, `usr_ack` = PREADY) and sends a
0xFF command for `abort`.

The wrapper sets a 16-word buffer and a 1 kHz clock parameter, so the
Wait / timeout limits of 20 / 50 ms are 20 / 50 cycles.  Python models: the parser's write buffer (`wbuf_data` one
cycle after `wbuf_addr`), a register file answering every request the
next cycle from a dict (with per-address error codes), and a user slave
answering after a chosen latency (or never).  Both slaves record every
access.  `run()` issues a command and returns its held response, pulsing
`rsp_ready`.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Read of 4 words: 4 accesses in order, data in the read buffer,
     response 0x00 with N and Size.
  2  Write of 3 words: write data from the buffer, response 0x01.
  3  An error on the second word ends the command with that code.
  4  User-window words go to the user port, others to the register file.
  5  A slow command gets one Wait (0x04, 1234 ms) before the final, the
     time counted per command.
  6  A user slave that never answers times out with 0x40 and the
     timeout flag.
  7  abort (0xFF) drops the command and its response, answering 0x03
     only; an outstanding user access is abandoned promptly.
  8  The response and busy hold until rsp_ready.
  9  abort lands in ST_FETCH, ST_REQ and ST_WAIT-with-ack in turn: each
     drops back to idle with only 0x03.
 10  abort while the user access is being requested: the access is abandoned,
     and the next read of another address returns its own data.
 11  A user slave answering after the timeout: 0x40, and the next read
     returns its own data, not the late answer.
 12  A 0xFF while a response is held: that response, then 0x03, nothing
     else; with a Wait not yet sent, the Wait is dropped.
 13  Commands answered by the parser (err != 0) go out in order with
     their code and a nack pulse, without an access.
 14  A command arriving while one executes waits (cmd_full), starts once
     the response is taken; reads alternate read-buffer banks, so the
     first read's data survive the second.
 15  abort while a register-file access is unanswered drains it
     (ST_DRAIN): the next command waits for the late answer, or for the
     timeout of a register file that never answers; a second 0xFF during
     the drain is answered and keeps draining.
 16  abort while a slow user slave holds PREADY low: PSEL, PENABLE,
     PADDR, PWRITE and PWDATA hold until PREADY (APB has no abort), the
     late answer completes nothing, the next user access follows it.
 17  PSTRB: a user-window write of B bytes enables only those bytes in
     its last word (B mod 4 = 1, 2, 3); full words 1111; reads 0000.
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import NextTimeStep, ReadOnly, RisingEdge

from cxp_protocol import packets as gp
from cxp_protocol import regmap as rm
from cxp_testcase import cxp_test
from fsm_coverage import register_fsm

CLK_NS = 10
USER = 0x0002_0000


# -----------------------------------------------------------------------------
# FSM coverage
# -----------------------------------------------------------------------------
# The command FSM walks IDLE -> (FETCH for a write) -> REQ -> WAIT per word,
# loops back to FETCH/REQ for the next word, and returns to IDLE with the
# final response.  The per-command timeout (WAIT -> IDLE with 0x40) and the
# 0xFF override are written after the case statement; their arcs are listed
# too.  The 0xFF sends FETCH and REQ to IDLE (the request is gated off in
# that cycle, so nothing is outstanding), WAIT to DRAIN only while a
# register-file access is unanswered and to IDLE otherwise; it leaves DRAIN
# alone.  REQ -> DRAIN does not exist.
register_fsm(
    name="ctrl_bus_master",
    states=["ST_IDLE", "ST_FETCH", "ST_REQ", "ST_WAIT", "ST_DRAIN"],
    state_path="cxp_ctrl_bus_master_i.state_q",
    clk_path="clk",
    arcs=[
        ("ST_IDLE", "ST_FETCH"), ("ST_IDLE", "ST_REQ"),
        ("ST_FETCH", "ST_REQ"), ("ST_REQ", "ST_WAIT"),
        ("ST_WAIT", "ST_FETCH"), ("ST_WAIT", "ST_REQ"),
        ("ST_WAIT", "ST_IDLE"), ("ST_WAIT", "ST_DRAIN"),
        ("ST_DRAIN", "ST_IDLE"), ("ST_FETCH", "ST_IDLE"),
        ("ST_REQ", "ST_IDLE"),
    ],
)


class Slave:
    """A register-bus slave answering `latency` cycles after the request
    (None = never)."""

    def __init__(self, dut, prefix: str, latency=1):
        self.dut, self.p, self.latency = dut, prefix, latency
        self.mem: dict[int, int] = {}
        self.err: dict[int, int] = {}
        self.log: list[tuple] = []
        getattr(dut, f"{prefix}_ack").value = 0
        getattr(dut, f"{prefix}_rdata").value = 0
        getattr(dut, f"{prefix}_err").value = 0
        cocotb.start_soon(self._run())

    def sig(self, n):
        return getattr(self.dut, f"{self.p}_{n}")

    async def _run(self):
        pending = None           # (addr, cycles left)
        while True:
            await ReadOnly()
            if self.p == "usr" and not int(self.dut.usr_open.value):
                pending = None   # transfer given up by the master
            if int(self.sig("req").value):
                a = int(self.sig("addr").value)
                we = int(self.sig("we").value)
                self.log.append((we, a, int(self.sig("wdata").value)))
                if we:
                    self.mem[a] = int(self.sig("wdata").value)
                pending = (a, self.latency)
            await RisingEdge(self.dut.clk)
            self.sig("ack").value = 0
            if pending is not None and pending[1] is not None:
                a, left = pending
                if left <= 1:
                    self.sig("ack").value = 1
                    self.sig("rdata").value = self.mem.get(a, 0)
                    self.sig("err").value = self.err.get(a, 0)
                    pending = None
                else:
                    pending = (a, left - 1)


async def wbuf_model(dut, wbuf):
    while True:
        await RisingEdge(dut.clk)
        a = int(dut.wbuf_addr.value)
        dut.wbuf_data.value = wbuf[a] if a < len(wbuf) else 0


async def reset(dut, wbuf=None, usr_latency=1):
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    for n in ("cmd_valid", "cmd_op", "cmd_size", "cmd_addr", "cmd_nwords", "cmd_err",
              "cmd_wbank", "abort", "rsp_ready", "wbuf_data", "rbuf_addr", "rbuf_bank"):
        getattr(dut, n).value = 0
    dut.rst_n.value = 0
    reg = Slave(dut, "reg")
    usr = Slave(dut, "usr", usr_latency)
    cocotb.start_soon(wbuf_model(dut, wbuf if wbuf is not None else []))
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    return reg, usr


async def issue(dut, op, addr, nwords, err=0):
    dut.cmd_err.value = err
    dut.cmd_op.value = op
    dut.cmd_addr.value = addr
    dut.cmd_size.value = 4 * nwords
    dut.cmd_nwords.value = nwords
    dut.cmd_valid.value = 1
    await RisingEdge(dut.clk)
    dut.cmd_valid.value = 0


LAST = {"timeout": 0, "rbank": 0}


async def take(dut, in_ro: bool = False, timeout: int = 100) -> tuple[int, int, int, int]:
    """Wait for a held response and take it: (code, nwords, size, timeout).
    `in_ro`: the caller is already in the ReadOnly phase."""
    if not in_ro:
        await ReadOnly()
    for _ in range(timeout):
        if int(dut.rsp_valid.value):
            break
        await RisingEdge(dut.clk)
        await ReadOnly()
    else:
        raise AssertionError("no response to take")
    r = (int(dut.rsp_code.value), int(dut.rsp_nwords.value), int(dut.rsp_size.value),
         int(dut.rsp_timeout.value))
    await RisingEdge(dut.clk)
    dut.rsp_ready.value = 1
    await RisingEdge(dut.clk)
    dut.rsp_ready.value = 0
    return r


async def responses(dut, cycles: int) -> list[int]:
    """Take every response for `cycles` cycles; return their codes."""
    codes = []
    for _ in range(cycles):
        await ReadOnly()
        if int(dut.rsp_valid.value):
            codes.append((await take(dut, in_ro=True))[0])
            continue
        await RisingEdge(dut.clk)
    return codes


async def response(dut, timeout=400, hold=0):
    """Wait for the final response, taking and counting Wait responses on
    the way; keep the final one `hold` cycles, then pulse rsp_ready.
    Returns (code, nwords, size, waits); its timeout flag and read bank
    are left in LAST."""
    waits = 0
    for _ in range(timeout):
        await ReadOnly()
        if int(dut.rsp_valid.value) and int(dut.rsp_code.value) == gp.ACK_WAIT:
            await take(dut, in_ro=True)
            waits += 1
            continue
        if int(dut.rsp_valid.value):
            r = (int(dut.rsp_code.value), int(dut.rsp_nwords.value), int(dut.rsp_size.value))
            LAST["timeout"] = int(dut.rsp_timeout.value)
            LAST["rbank"] = int(dut.rsp_rbank.value)
            for _ in range(hold):
                await RisingEdge(dut.clk)
                await ReadOnly()
                assert int(dut.rsp_valid.value) == 1 and int(dut.busy.value) == 1
            await RisingEdge(dut.clk)
            dut.rsp_ready.value = 1
            await RisingEdge(dut.clk)
            dut.rsp_ready.value = 0
            return (*r, waits)
        await RisingEdge(dut.clk)
    raise AssertionError("no response")


async def read_rbuf(dut, n, bank=None):
    """Read n words of the read buffer, from the bank of the last read
    response unless `bank` is given."""
    dut.rbuf_bank.value = LAST["rbank"] if bank is None else bank
    out = []
    for i in range(n):
        dut.rbuf_addr.value = i
        await RisingEdge(dut.clk)
        await RisingEdge(dut.clk)
        await ReadOnly()
        out.append(int(dut.rbuf_data.value))
        await NextTimeStep()
    return out


# -----------------------------------------------------------------------------
# TC 1 — Read
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_read(dut):
    """A 4-word read: four accesses at addr, +4, +8, +12; data in rbuf.

    Stimulus: register file holds 0x11..0x44 at 0x4000..0x400C; read N 4.
    Checks:   the access log; response (0x00, 4, 16); rbuf[0..3].
    """
    dut.TESTCASE.value = 1
    reg, _ = await reset(dut)
    reg.mem.update({rm.CONNECTION_RESET + 4 * i: 0x11 * (i + 1) for i in range(4)})
    await issue(dut, gp.OP_READ, rm.CONNECTION_RESET, 4)
    assert (await response(dut))[:3] == (gp.ACK_OK_DATA, 4, 16)
    assert [a for _, a, _ in reg.log] == [rm.CONNECTION_RESET, rm.DEVICE_CONNECTION_ID,
                                         rm.MASTER_HOST_CONNECTION_ID, rm.CONTROL_PACKET_SIZE_MAX]
    assert all(we == 0 for we, _, _ in reg.log)
    assert await read_rbuf(dut, 4) == [0x11, 0x22, 0x33, 0x44]


# -----------------------------------------------------------------------------
# TC 2 — Write
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_write(dut):
    """A 3-word write takes its data from the parser's write buffer.

    Stimulus: wbuf = [A, B, C]; write N 3 at 0x10000.
    Checks:   the log holds 3 writes with A, B, C; response 0x01, N 0.
    """
    dut.TESTCASE.value = 2
    wbuf = [0xAAAA_0001, 0xBBBB_0002, 0xCCCC_0003]
    reg, _ = await reset(dut, wbuf=wbuf)
    await issue(dut, gp.OP_WRITE, rm.MFR_BASE, 3)
    assert (await response(dut))[:3] == (gp.ACK_OK_WRITE, 0, 0)
    assert reg.log == [(1, rm.MFR_BASE, wbuf[0]), (1, rm.MFR_BASE + 4, wbuf[1]), (1, rm.MFR_BASE + 8, wbuf[2])]


# -----------------------------------------------------------------------------
# TC 3 — Error Ends The Command
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_error_stops(dut):
    """The first access answering an error code ends the command with it.

    Table 22 codes travel unchanged (here 0x43 from a read-only register).
    Stimulus: write N 4 at 0x4000; 0x4004 answers 0x43.
    Checks:   two accesses only; response (0x43, 0, 0).
    """
    dut.TESTCASE.value = 3
    reg, _ = await reset(dut, wbuf=[1, 2, 3, 4])
    reg.err[rm.DEVICE_CONNECTION_ID] = gp.ACK_RO_WRITE
    await issue(dut, gp.OP_WRITE, rm.CONNECTION_RESET, 4)
    assert (await response(dut))[:3] == (gp.ACK_RO_WRITE, 0, 0)
    assert len(reg.log) == 2


# -----------------------------------------------------------------------------
# TC 4 — User Window
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_user_window(dut):
    """Words inside the user window go to the user port, word by word.

    Stimulus: read N 4 starting 8 bytes below the window end (0x20FF8):
              two words in the window, two above it.
    Checks:   the user log has 0x20FF8, 0x20FFC; the register log 0x21000,
              0x21004; response 0x00.
    """
    dut.TESTCASE.value = 4
    reg, usr = await reset(dut)
    await issue(dut, gp.OP_READ, USER + 0xFF8, 4)
    assert (await response(dut))[0] == gp.ACK_OK_DATA
    assert [a for _, a, _ in usr.log] == [USER + 0xFF8, USER + 0xFFC]
    assert [a for _, a, _ in reg.log] == [USER + 0x1000, USER + 0x1004]


# -----------------------------------------------------------------------------
# TC 5 — Slow Slave Gets A Wait
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_wait(dut):
    """A command outstanding for p_WAIT_CYCLES raises one Wait.

    §8.6.1.1: a Device needing more time sends one Wait acknowledgment;
    the time counts from the command's first access, not per access.
    Stimulus: (a) user slave latency 30 cycles (> 20); read N 1 in the
              window; (b) latency 8, read N 4 in the window (each access
              under 20 cycles, the command over 20).
    Checks:   (a) exactly one Wait (wait_ms 1234) before the final 0x00
              (1 word, 4 bytes); (b) one Wait, then 0x00 with 4 words.
    """
    dut.TESTCASE.value = 5
    _, usr = await reset(dut, usr_latency=30)
    await issue(dut, gp.OP_READ, USER, 1)
    code, n, size, waits = await response(dut)
    assert (code, n, size, waits) == (gp.ACK_OK_DATA, 1, 4, 1)
    assert int(dut.wait_ms.value) == 1234
    usr.latency = 8
    await issue(dut, gp.OP_READ, USER, 4)
    assert await response(dut) == (gp.ACK_OK_DATA, 4, 16, 1)


# -----------------------------------------------------------------------------
# TC 6 — Timeout
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_timeout(dut):
    """A user access unanswered for p_TIMEOUT_MS ends with 0x40, flagged.

    APB has no abort: a transfer in ACCESS keeps PSEL, PENABLE, PADDR and
    PWRITE until PREADY, so the command ends but the transfer stays open.

    Stimulus: user slave that never answers; read N 1 in the window; then
              a read of the register file, and a second read in the window.
    Checks:   one Wait, then response (0x40, 0, 0) with `rsp_timeout` = 1;
              the APB transfer is still open with the same PADDR and
              PENABLE = 1; the register-file read answers 0x00 at once;
              the second user read times out too (0x40, one Wait) without
              a second SETUP.
    """
    dut.TESTCASE.value = 6
    reg, usr = await reset(dut, usr_latency=None)
    await issue(dut, gp.OP_READ, USER, 1)
    assert await response(dut) == (gp.ACK_BAD_ADDR, 0, 0, 1)
    assert LAST["timeout"] == 1, "timeout not flagged"
    await ReadOnly()
    assert int(dut.usr_open.value) == 1, "APB transfer withdrawn without PREADY"
    assert int(dut.usr_addr.value) == USER
    assert int(dut.cxp_ctrl_bus_master_i.apb_penable_o.value) == 1
    await NextTimeStep()
    reg.mem[rm.CONNECTION_RESET] = 0x5A
    await issue(dut, gp.OP_READ, rm.CONNECTION_RESET, 1)
    code, *_ = await response(dut, timeout=20)
    assert code == gp.ACK_OK_DATA, "register file blocked by an open APB transfer"
    n_setup = len(usr.log)
    await issue(dut, gp.OP_READ, USER + 4, 1)
    assert await response(dut) == (gp.ACK_BAD_ADDR, 0, 0, 1)
    assert len(usr.log) == n_setup, "a second SETUP while the first transfer was open"


# -----------------------------------------------------------------------------
# TC 7 — Abort
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_abort(dut):
    """abort (opcode 0xFF) drops the command and its response.

    Stimulus: (a) read N 8 at 0x4000, abort after 3 cycles; (b) read in
              the user window with a 15-cycle slave, abort while pending.
    Checks:   only 0x03 each time; after (b) the executor abandons the
              user access promptly; a following read works.
    """
    dut.TESTCASE.value = 7
    reg, usr = await reset(dut, usr_latency=15)
    await issue(dut, gp.OP_READ, rm.CONNECTION_RESET, 8)
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.abort.value = 1
    await RisingEdge(dut.clk)
    dut.abort.value = 0
    assert await responses(dut, 10) == [gp.ACK_OK_RESET]
    await issue(dut, gp.OP_READ, USER, 1)
    await RisingEdge(dut.clk)
    dut.abort.value = 1
    await RisingEdge(dut.clk)
    dut.abort.value = 0
    assert (await take(dut))[0] == gp.ACK_OK_RESET
    await ReadOnly()
    for _ in range(4):
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert int(dut.rsp_valid.value) == 0
        if not int(dut.busy.value):
            break
    assert int(dut.busy.value) == 0
    await NextTimeStep()
    await issue(dut, gp.OP_READ, rm.CONNECTION_RESET, 1)
    assert (await response(dut))[0] == gp.ACK_OK_DATA


# -----------------------------------------------------------------------------
# TC 8 — Response Held
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_response_held(dut):
    """The response and busy stay up until rsp_ready.

    Stimulus: read N 1; hold rsp_ready low 25 cycles.
    Checks:   rsp_valid and busy stay 1 for those cycles; both fall after
              rsp_ready.
    """
    dut.TESTCASE.value = 8
    await reset(dut)
    await issue(dut, gp.OP_READ, rm.CONNECTION_RESET, 1)
    assert (await response(dut, hold=25))[0] == gp.ACK_OK_DATA
    await ReadOnly()
    assert int(dut.rsp_valid.value) == 0 and int(dut.busy.value) == 0


# -----------------------------------------------------------------------------
# TC 9 — Abort In Every State Of A Word
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_abort_each_state(dut):
    """abort at each cycle of a word's fetch/request/answer walks every
    abort exit of the FSM.

    A write word takes one cycle per state — ST_FETCH, ST_REQ, then
    ST_WAIT, which the 1-cycle register file answers at once — so
    sweeping the abort one cycle later each time lands it in each of
    them in turn.  With the access answered in the same cycle there is
    nothing outstanding, so none of these goes through ST_DRAIN (TC 15
    covers that exit, the one a slow register file forces).
    Stimulus: write N 4 at 0x4000, abort pulsed d = 0..5 cycles after the
              command, once per d.
    Checks:   only 0x03 is ever raised; busy falls within a few cycles
              once it is taken; a plain read still works after each abort.
    """
    dut.TESTCASE.value = 9
    reg, _ = await reset(dut, wbuf=[1, 2, 3, 4])
    for d in range(6):
        await issue(dut, gp.OP_WRITE, rm.CONNECTION_RESET, 4)
        for _ in range(d):
            await RisingEdge(dut.clk)
        dut.abort.value = 1
        await RisingEdge(dut.clk)
        dut.abort.value = 0
        codes = await responses(dut, 8)
        assert codes == [gp.ACK_OK_RESET], f"abort d={d}: responses {codes}"
        for _ in range(8):
            await ReadOnly()
            if not int(dut.busy.value):
                break
            await RisingEdge(dut.clk)
        assert int(dut.busy.value) == 0, f"abort d={d} left the FSM busy"
        await NextTimeStep()
        reg.mem[rm.CONNECTION_RESET] = 0x5A5A_0000 + d
        await issue(dut, gp.OP_READ, rm.CONNECTION_RESET, 1)
        assert (await response(dut))[0] == gp.ACK_OK_DATA, f"stuck after d={d}"
        assert (await read_rbuf(dut, 1))[0] == 0x5A5A_0000 + d


# -----------------------------------------------------------------------------
# TC 10 — Abort While The User Access Is Requested
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_abort_in_req(dut):
    """A 0xFF that lands while the user access is being issued leaves no
    orphan: the access is abandoned before anything else runs.

    §8.6.1.2: the reset abandons the command, not the bus.

    Stimulus: user slave latency 15; USER = 0x1111, USER + 4 = 0x2222; read
              USER, abort d = 0..3 cycles after the command (one lands in
              ST_REQ); take the 0x03; read USER + 4 at once.
    Checks:   only 0x03 for the aborted read; the second read answers
              0x00 with 0x2222.
    """
    dut.TESTCASE.value = 10
    _, usr = await reset(dut, usr_latency=15)
    usr.mem.update({USER: 0x1111, USER + 4: 0x2222})
    for d in range(4):
        await issue(dut, gp.OP_READ, USER, 1)
        for _ in range(d):
            await RisingEdge(dut.clk)
        dut.abort.value = 1
        await RisingEdge(dut.clk)
        dut.abort.value = 0
        assert (await take(dut))[0] == gp.ACK_OK_RESET, f"d={d}"
        await issue(dut, gp.OP_READ, USER + 4, 1)
        code, *_ = await response(dut)
        assert code == gp.ACK_OK_DATA, f"d={d}: code {code:#x}"
        assert await read_rbuf(dut, 1) == [0x2222], f"d={d}: orphaned access answered"


# -----------------------------------------------------------------------------
# TC 11 — Late Answer After The Timeout
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_late_ack_after_timeout(dut):
    """An answer that comes after the command timed out completes nothing.

    Stimulus: user slave latency 60 (> timeout 50); read USER (0x1111);
              take the Wait and the 0x40; slave latency 1; read USER + 4
              (0x2222) at once.
    Checks:   the first read ends 0x40 with the timeout flag; the second
              answers 0x00 with 0x2222.
    """
    dut.TESTCASE.value = 11
    _, usr = await reset(dut, usr_latency=60)
    usr.mem.update({USER: 0x1111, USER + 4: 0x2222})
    await issue(dut, gp.OP_READ, USER, 1)
    assert (await response(dut))[0] == gp.ACK_BAD_ADDR
    assert LAST["timeout"] == 1
    usr.latency = 1
    await issue(dut, gp.OP_READ, USER + 4, 1)
    code, *_ = await response(dut, timeout=200)
    assert code == gp.ACK_OK_DATA, f"code {code:#x}"
    assert await read_rbuf(dut, 1) == [0x2222], "late answer of the timed-out read"


# -----------------------------------------------------------------------------
# TC 12 — Reset While A Response Is Held
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_reset_while_rsp_held(dut):
    """§8.6.1.2: after a 0xFF only its 0x03 follows what was handed over.

    The held response may already be crossing to the framer, so it goes
    out; nothing of the abandoned command follows the 0x03.
    Stimulus: (a) read 0x4000, response held (rsp_ready 0), 0xFF;
              (b) read USER with a slave of latency 30 (> Wait 20), the
              Wait held unread, a second command queued behind, then
              0xFF; (c) read USER latency 30, 0xFF on the cycle the Wait
              would be produced.
    Checks:   (a) [0x00, 0x03]; (b) [0x04, 0x03], no final, the queued
              command never answered; (c) [0x03] or [0x04, 0x03], never a
              0x04 after the 0x03.
    """
    dut.TESTCASE.value = 12
    reg, usr = await reset(dut, usr_latency=30)
    await issue(dut, gp.OP_READ, rm.CONNECTION_RESET, 1)
    for _ in range(6):
        await RisingEdge(dut.clk)
    dut.abort.value = 1
    await RisingEdge(dut.clk)
    dut.abort.value = 0
    assert await responses(dut, 60) == [gp.ACK_OK_DATA, gp.ACK_OK_RESET]
    await issue(dut, gp.OP_READ, USER, 1)
    for _ in range(30):
        await RisingEdge(dut.clk)
    await issue(dut, gp.OP_READ, rm.CONNECTION_RESET, 1)
    await RisingEdge(dut.clk)
    dut.abort.value = 1
    await RisingEdge(dut.clk)
    dut.abort.value = 0
    assert await responses(dut, 120) == [gp.ACK_WAIT, gp.ACK_OK_RESET]
    for d in range(17, 24):
        await issue(dut, gp.OP_READ, USER, 1)
        for _ in range(d):
            await RisingEdge(dut.clk)
        dut.abort.value = 1
        await RisingEdge(dut.clk)
        dut.abort.value = 0
        codes = await responses(dut, 120)
        assert codes in ([gp.ACK_OK_RESET], [gp.ACK_WAIT, gp.ACK_OK_RESET]), f"d={d}: {codes}"


# -----------------------------------------------------------------------------
# TC 13 — Commands Answered By The Parser
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_parser_codes(dut):
    """A command carrying a code is answered with it, in order, without an
    access; the nack pulse names it (0x01 for an ignored write does not
    pulse).

    Stimulus: rsp_ready held off; commands with err 0x80, 0x45, then (the
              slot being full) 0x47; then take everything; then err 0x01.
    Checks:   responses [0x80, 0x45] (the third dropped: one command
              waits, D7), then [0x01]; nack pulses 0x80, 0x45; no access.
    """
    dut.TESTCASE.value = 13
    reg, _ = await reset(dut)
    nacks = []

    async def mon():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if int(dut.nack_pulse.value):
                nacks.append(int(dut.nack_code.value))

    cocotb.start_soon(mon())
    for e in (gp.ACK_CRC, gp.ACK_OVERSIZE, gp.ACK_MALFORMED):
        await issue(dut, gp.OP_READ, rm.CONNECTION_RESET, 1, err=e)
        for _ in range(3):
            await RisingEdge(dut.clk)
    assert await responses(dut, 20) == [gp.ACK_CRC, gp.ACK_OVERSIZE]
    await issue(dut, gp.OP_WRITE, rm.CONNECTION_RESET, 1, err=gp.ACK_OK_WRITE)
    assert await responses(dut, 20) == [gp.ACK_OK_WRITE]
    assert nacks == [gp.ACK_CRC, gp.ACK_OVERSIZE], nacks
    assert reg.log == []


# -----------------------------------------------------------------------------
# TC 14 — One Command Waits; Read Banks Alternate
# -----------------------------------------------------------------------------
@cxp_test()
async def test_14_queue_and_banks(dut):
    """A second command waits for the first's response to be taken; two
    reads land in different read-buffer banks.

    Stimulus: 0x4000 = 0xAAAA, 0x4004 = 0xBBBB; read 0x4000, then read
              0x4004 while the first response is held 20 cycles.
    Checks:   cmd_full while the second waits; no access for the second
              before the first response is taken; the two responses name
              different banks and each bank holds its own word.
    """
    dut.TESTCASE.value = 14
    reg, _ = await reset(dut)
    reg.mem.update({rm.CONNECTION_RESET: 0xAAAA, rm.DEVICE_CONNECTION_ID: 0xBBBB})
    await issue(dut, gp.OP_READ, rm.CONNECTION_RESET, 1)
    await issue(dut, gp.OP_READ, rm.DEVICE_CONNECTION_ID, 1)
    await ReadOnly()
    assert int(dut.cmd_full.value) == 1
    for _ in range(20):
        await RisingEdge(dut.clk)
    assert [a for _, a, _ in reg.log] == [rm.CONNECTION_RESET], reg.log
    assert (await response(dut))[0] == gp.ACK_OK_DATA
    bank_a = LAST["rbank"]
    assert (await response(dut))[0] == gp.ACK_OK_DATA
    bank_b = LAST["rbank"]
    assert bank_a != bank_b
    assert await read_rbuf(dut, 1, bank_a) == [0xAAAA]
    assert await read_rbuf(dut, 1, bank_b) == [0xBBBB]


# -----------------------------------------------------------------------------
# TC 15 — Abort While A Register-File Access Is Outstanding
# -----------------------------------------------------------------------------
ST_DRAIN = 4


@cxp_test()
async def test_15_drain_register_access(dut):
    """A 0xFF cannot abandon a register-file access (that port has no
    abort), so the executor drains it: no next command starts until the
    access answers, or until the command's timeout for one that never does.

    §8.6.1.2: the reset abandons the command, not the bus.  The other
    benches' register file answers the next cycle, so only a slow one
    reaches ST_DRAIN.
    Stimulus: 0x4000 = 0x1111, 0x4004 = 0x2222.
              (a) register latency 8: read 0x4000, 0xFF while unanswered,
                  read 0x4004 at once (latency 1);
              (b) register file that never answers: the same;
              (c) register latency 12: read 0x4000, 0xFF while unanswered,
                  a second 0xFF 3 cycles later, then read 0x4004.
    Checks:   ST_DRAIN entered each time; never two register accesses
              outstanding — the second request comes after the late
              answer in (a) and no earlier than the 50-cycle timeout in
              (b); responses (a)/(b) [0x03, 0x00], no Wait, (c) [0x03,
              0x03, 0x00], the state still ST_DRAIN after the second 0xFF;
              every second read returns 0x2222, never the late 0x1111.
    """
    dut.TESTCASE.value = 15
    reg, _ = await reset(dut)
    reg.mem.update({rm.CONNECTION_RESET: 0x1111, rm.DEVICE_CONNECTION_ID: 0x2222})
    ev = {"cyc": 0, "req": [], "ack": [], "st": []}

    async def mon():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            ev["cyc"] += 1
            ev["st"].append(int(dut.cxp_ctrl_bus_master_i.state_q.value))
            if int(dut.reg_ack.value):
                ev["ack"].append(ev["cyc"])
            if int(dut.reg_req.value):
                ev["req"].append(ev["cyc"])

    cocotb.start_soon(mon())

    async def pulse_abort(after: int):
        for _ in range(after):
            await RisingEdge(dut.clk)
        dut.abort.value = 1
        await RisingEdge(dut.clk)
        dut.abort.value = 0

    async def abort_outstanding(latency, second_abort=False):
        """Read 0x4000 with the given latency, 0xFF in ST_WAIT, then read
        0x4004; return the two request cycles."""
        for k in ("req", "ack", "st"):
            ev[k].clear()
        reg.latency = latency
        await issue(dut, gp.OP_READ, rm.CONNECTION_RESET, 1)
        await pulse_abort(3)
        await ReadOnly()
        assert int(dut.cxp_ctrl_bus_master_i.state_q.value) == ST_DRAIN, \
            f"latency {latency}: 0xFF did not drain the register access"
        await NextTimeStep()
        reg.latency = 1
        codes = [(await take(dut))[0]]
        if second_abort:
            await pulse_abort(0)
            await ReadOnly()
            assert int(dut.cxp_ctrl_bus_master_i.state_q.value) == ST_DRAIN, \
                "a 0xFF during the drain left ST_DRAIN"
            await NextTimeStep()
            codes.append((await take(dut))[0])
        await issue(dut, gp.OP_READ, rm.DEVICE_CONNECTION_ID, 1)
        code, _, _, waits = await response(dut)
        codes.append(code)
        assert waits == 0, f"latency {latency}: Wait sent for an abandoned command"
        assert await read_rbuf(dut, 1) == [0x2222], \
            f"latency {latency}: the drained answer completed the next read"
        assert len(ev["req"]) == 2, f"latency {latency}: requests {ev['req']}"
        return codes, ev["req"][0], ev["req"][1]

    # (a) A late answer ends the drain.
    codes, r0, r1 = await abort_outstanding(8)
    assert codes == [gp.ACK_OK_RESET, gp.ACK_OK_DATA], codes
    assert ev["ack"][0] == r0 + 8 and r1 > ev["ack"][0], \
        f"(a) second request at {r1} before the late answer at {ev['ack'][0]}"

    # (b) An access that never answers is given up at the command timeout.
    codes, r0, r1 = await abort_outstanding(None)
    assert codes == [gp.ACK_OK_RESET, gp.ACK_OK_DATA], codes
    assert r1 - r0 >= 48, f"(b) drain ended {r1 - r0} cycles after the request"

    # (c) A second 0xFF during the drain is answered and keeps draining.
    codes, r0, r1 = await abort_outstanding(12, second_abort=True)
    assert codes == [gp.ACK_OK_RESET, gp.ACK_OK_RESET, gp.ACK_OK_DATA], codes
    assert r1 > r0 + 12, f"(c) second request at {r1}, drained access answered at {r0 + 12}"


# -----------------------------------------------------------------------------
# TC 16 — An Abandoned APB Transfer Runs To PREADY
# -----------------------------------------------------------------------------
@cxp_test()
async def test_16_apb_abort_holds_until_pready(dut):
    """A given-up APB transfer is not withdrawn: only PREADY ends it.

    AMBA APB: once in ACCESS, PSEL, PENABLE, PADDR, PWRITE and PWDATA stay
    stable until PREADY = 1.  Dropping them earlier is an illegal cycle; a
    slave that completes later (or an APB-to-AXI bridge) would see the
    next transfer's address phase instead.

    Stimulus: user slave latency 30; write N 1 at USER (0xCAFE); a 0xFF 4
              cycles later; take the 0x03; then a read of USER + 4
              (0x2222) at once.
    Checks:   from the 0xFF until PREADY, PSEL = PENABLE = 1, PWRITE = 1,
              PADDR = USER and PWDATA = 0xCAFE every cycle; exactly two
              SETUPs in all (the write, then the read, the read's after the
              write's PREADY); the read answers 0x00 with 0x2222.
    """
    dut.TESTCASE.value = 16
    _, usr = await reset(dut, wbuf=[0xCAFE], usr_latency=30)
    usr.mem[USER + 4] = 0x2222
    bm = dut.cxp_ctrl_bus_master_i
    await issue(dut, gp.OP_WRITE, USER, 1)
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.abort.value = 1
    await RisingEdge(dut.clk)
    dut.abort.value = 0
    held = 0
    while True:
        await ReadOnly()
        if int(dut.usr_ack.value):
            break
        assert (int(bm.apb_psel_o.value), int(bm.apb_penable_o.value), int(bm.apb_pwrite_o.value),
                int(bm.apb_paddr_o.value), int(bm.apb_pwdata_o.value)) == (1, 1, 1, USER, 0xCAFE), \
            f"APB transfer changed {held} cycles after the 0xFF, before PREADY"
        held += 1
        if int(dut.rsp_valid.value):
            assert (await take(dut, in_ro=True))[0] == gp.ACK_OK_RESET
            continue
        await RisingEdge(dut.clk)
    assert held > 10, f"transfer held only {held} cycles"
    await NextTimeStep()
    await issue(dut, gp.OP_READ, USER + 4, 1)
    usr.latency = 1
    code, *_ = await response(dut, timeout=200)
    assert code == gp.ACK_OK_DATA, f"code {code:#x}"
    assert await read_rbuf(dut, 1) == [0x2222]
    assert [(we, a) for we, a, _ in usr.log] == [(1, USER), (0, USER + 4)], usr.log


# -----------------------------------------------------------------------------
# TC 17 — PSTRB On Partial Writes
# -----------------------------------------------------------------------------
@cxp_test()
async def test_17_pstrb_partial_write(dut):
    """Only the bytes a write carries are enabled on APB (PSTRB).

    Table 21: a write of B bytes writes B bytes; §10.3 the space is
    byte-addressed.  Without byte enables the last word of a write that
    ends inside it clobbers the bytes after it.

    Stimulus: user slave latency 1; writes of Size 1, 2, 3, 4 and 5 bytes
              at USER; a read N 1 at USER.
    Checks:   PSTRB at each SETUP: 1000, 1100, 1110, 1111, then 1111 +
              1000; the read's SETUP has PSTRB 0000; every command answers
              0x01 / 0x00.
    """
    dut.TESTCASE.value = 17
    _, usr = await reset(dut, wbuf=[0x11223344, 0x55667788])
    strobes = []

    async def watch():
        while True:
            await ReadOnly()
            if int(dut.usr_req.value):
                strobes.append((int(dut.usr_we.value), int(dut.usr_wstrb.value)))
            await RisingEdge(dut.clk)

    w = cocotb.start_soon(watch())
    for size in (1, 2, 3, 4, 5):
        dut.cmd_err.value = 0
        dut.cmd_op.value = gp.OP_WRITE
        dut.cmd_addr.value = USER
        dut.cmd_size.value = size
        dut.cmd_nwords.value = (size + 3) // 4
        dut.cmd_valid.value = 1
        await RisingEdge(dut.clk)
        dut.cmd_valid.value = 0
        code, *_ = await response(dut)
        assert code == gp.ACK_OK_WRITE, f"size {size}: code {code:#x}"
    await issue(dut, gp.OP_READ, USER, 1)
    code, *_ = await response(dut)
    assert code == gp.ACK_OK_DATA
    w.cancel()
    assert strobes == [(1, 0b1000), (1, 0b1100), (1, 0b1110), (1, 0b1111),
                       (1, 0b1111), (1, 0b1000), (0, 0b0000)], strobes
