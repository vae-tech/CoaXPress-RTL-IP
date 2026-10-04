"""Cocotb TB for `cxp_tx_ctrl_ack`.

The DUT frames one type-0x03 control acknowledgment per `ack_req` pulse
and streams it as 32-bit beats (P0 = `m_data[7:0]`) under valid/ready.
Codes 0x00 (read-OK) and 0x04 (Wait) give a data ack; every other code
gives an immediate ack. CoaXPress 1.1.1 (JIIA CXP-001-2015).

The wrapper pins `p_BUF_DEPTH` = 16. `bringup` starts an 8 ns `tx_clk`
(low first), holds `tx_rst_n` low for 6 edges and starts `rbuf_model`, a
1-cycle-latency model of the APB master's read buffer. `issue_ack` pulses
the request for one cycle; `capture_packet` drives `m_ready` and records
accepted beats up to the first EOP; `check_packet` compares every beat's
data/kmask/sop/eop with `expected_packet`. The `ctrl_ack_tx` FSM
(`cxp_tx_ctrl_ack_i.state_q`) is registered for state/arc coverage.

Spec references:

  §8.6.3 / Table 22  Control acknowledge packet (type 0x03) wire format
                     and acknowledgment codes (v1.0: §6.6.3 / Table 21).
  §8.2.1             Byte ordering — P0 in m_data[7:0], LSB transmitted
                     first; multi-byte values (read / Wait reply data)
                     are big-endian, i.e. byte-swapped onto the wire.
  §8.2.2.2           CRC32 polynomial 0xEDB88320 reflected, seed
                     0xFFFFFFFF, no final XOR, register LSByte in P0.

v1.0 → v1.1.1: the v1.0 "tentative" code 0x02 is replaced by 0x04 "Wait",
which carries a 4-byte ms-timeout reply word framed like a 0x00 read ack;
logical-error codes 0x45/0x46/0x47 are new (Table 22).

Packet shapes (Table 22), built by `expected_packet` with the golden
`cxp_protocol` codec in its specification profile:

  (a) Immediate ack — write-OK (0x01), reset-done (0x03), all 0x40-class
      / 0x80 errors.  No Size, no data, no CRC:

        4 × K27.7 (0xFB)        kmask=0xF, sop=1
        4 × 0x03                kmask=0
        4 × AckCode
        4 × K29.7 (0xFD)        kmask=0xF, eop=1

  (b) Data ack — read-OK (0x00, + reply data) and Wait (0x04, + the
      4-byte ms timeout), N + 6 words:

        4 × K27.7 (0xFB)        kmask=0xF, sop=1
        4 × 0x03                kmask=0
        Word 0       : 4 × AckCode
        Word 1       : Size = B, big-endian
        Word 2..N+1  : data words, big-endian, pad bytes 0
        Word N+2     : CRC32 over words 0..N+1 (TYPE excluded)
        4 × K29.7 (0xFD)        kmask=0xF, eop=1

`ack_size` is B in bytes; N = ceil(B / 4); B = 4 for Wait (0x04).

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  AckCode 0x01 → 4-word immediate ack.
  2  AckCode 0x00, N = 4 → data ack with rbuf payload and CRC.
  3  AckCode 0x03 → 4-word immediate ack.
  4  0x40-class logical errors (0x40/43/45/46/47) → immediate acks.
  5  AckCode 0x04 Wait → 9-word data ack carrying the ms timeout.
  6  AckCode 0x80 CRC-error notification → immediate ack.
  7  Size word for N = 0/1/5/8/16, back-to-back.
  8  Random rbuf payloads → CRC matches the golden §8.2.2.2 CRC.
  9  `m_eop` / kmask 0xF only on the K29.7 beat.
 10  `m_sop` / kmask 0xF only on the K27.7 beat.
 11  Random `m_ready` stalls — packet unchanged.
 12  `ack_busy` 0 in idle, 1 from request through EOP, 0 afterwards.
 13  Second request while busy is dropped; a later request works.
 14  Two data acks in a row come out clean and CRC-valid.
 15  Read of B = 3 bytes → Size 3, one data word with its pad byte 0.
 16  The read-buffer bank given with the request addresses every data
     word of that packet, whatever the bank input does meanwhile.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from cxp_protocol import SPEC
from cxp_protocol import packets as gp

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import NextTimeStep, ReadOnly, RisingEdge

from cxp_testcase import cxp_test
from fsm_coverage import register_fsm


TX_PERIOD_NS = 8

K27_7 = 0xFB
K29_7 = 0xFD


# -----------------------------------------------------------------------------
# FSM coverage
# -----------------------------------------------------------------------------
# The packet FSM lives in the shared cxp_tx_pkt_framer instance; this module
# builds the header words and the read buffer around it.
register_fsm(
    name="ctrl_ack_tx",
    states=["ST_IDLE", "ST_HDR", "ST_DATA", "ST_CRC", "ST_EOP"],
    state_path="cxp_tx_ctrl_ack_i.cxp_tx_pkt_framer_i.state_q",
    clk_path="tx_clk",
    arcs=[
        ("ST_IDLE", "ST_HDR"), ("ST_HDR", "ST_DATA"), ("ST_HDR", "ST_CRC"),
        ("ST_HDR", "ST_EOP"), ("ST_DATA", "ST_CRC"), ("ST_CRC", "ST_EOP"),
        ("ST_EOP", "ST_IDLE"),
    ],
)


# -----------------------------------------------------------------------------
# Reference model
# -----------------------------------------------------------------------------
def rep4(b: int) -> int:
    """Replicate one byte into all four lanes of a 32-bit word."""
    return ((b & 0xFF) << 24) | ((b & 0xFF) << 16) | ((b & 0xFF) << 8) | (b & 0xFF)


def byteswap32(w: int) -> int:
    """Swap the four bytes of a 32-bit word (native value <-> wire order)."""
    w &= 0xFFFFFFFF
    return (((w & 0x0000_00FF) << 24) | ((w & 0x0000_FF00) << 8)
            | ((w & 0x00FF_0000) >> 8) | ((w & 0xFF00_0000) >> 24))


def is_data_ack(code: int) -> bool:
    """§8.6.3: only read-OK (0x00) and Wait (0x04) carry Size+Data+CRC;
    every other code is an immediate ack (SOP|TYPE|CODE|EOP)."""
    return code in (0x00, 0x04)


@dataclass(frozen=True)
class ExpBeat:
    """One expected output beat."""
    data:  int
    kmask: int
    sop:   int
    eop:   int


def expected_packet(code: int, nwords: int, data_words: list[int],
                    size: int | None = None) -> list[ExpBeat]:
    """Expected beats for one ack (layout (a) or (b) above), from the golden
    codec (`SPEC`).  `data_words` are the native rbuf / Wait values; `size`
    is B (default 4·nwords)."""
    assert len(data_words) == nwords
    if not is_data_ack(code):
        assert nwords == 0, "immediate ack carries no data"
    beats = gp.ctrl_ack(code, data_words, size=size, q=SPEC)
    return [ExpBeat(w, k, int(i == 0), int(i == len(beats) - 1))
            for i, (w, k) in enumerate(beats)]


# -----------------------------------------------------------------------------
# Read-buffer model
# -----------------------------------------------------------------------------
async def rbuf_model(dut, rbuf: list[int]):
    """Drive `rbuf_data` from `rbuf[rbuf_addr]` with one clock of latency.

    Models the synchronous `rbuf` read port inside the APB master: address
    sampled at edge T, data valid in cycle T+1; 0 at or beyond len(rbuf).
    Holds `rbuf` by reference, so a test may rewrite entries on the fly.
    """
    dut.rbuf_data.value = 0
    while True:
        await RisingEdge(dut.tx_clk)
        addr = int(dut.rbuf_addr.value)
        dut.rbuf_data.value = rbuf[addr] if addr < len(rbuf) else 0


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def bringup(dut, rbuf: list[int] | None = None):
    """Start `tx_clk`, zero inputs (`m_ready` = 0), reset 6 edges, wait 2,
    then start `rbuf_model` on `rbuf`."""
    cocotb.start_soon(Clock(dut.tx_clk, TX_PERIOD_NS, unit="ns").start(start_high=False))
    dut.tx_rst_n.value       = 0
    dut.ack_req.value        = 0
    dut.ack_code.value       = 0
    dut.ack_size.value       = 0
    dut.ack_wait_ms.value    = 0
    dut.ack_rbank.value      = 0
    dut.rbuf_data.value      = 0
    dut.m_ready.value        = 0
    for _ in range(6):
        await RisingEdge(dut.tx_clk)
    dut.tx_rst_n.value = 1
    await RisingEdge(dut.tx_clk)
    await RisingEdge(dut.tx_clk)

    # Spawn the rbuf model.
    rbuf = rbuf if rbuf is not None else []
    cocotb.start_soon(rbuf_model(dut, rbuf))


# -----------------------------------------------------------------------------
# Drivers / monitors
# -----------------------------------------------------------------------------
async def issue_ack(dut, code: int, nwords: int, wait_ms: int = 0,
                    size: int | None = None):
    """Pulse `ack_req` for one cycle with code / Size B (default 4·N) /
    wait, then zero them."""
    dut.ack_code.value       = code
    dut.ack_size.value       = 4 * nwords if size is None else size
    dut.ack_wait_ms.value    = wait_ms
    dut.ack_req.value        = 1
    await RisingEdge(dut.tx_clk)
    dut.ack_req.value        = 0
    dut.ack_code.value       = 0
    dut.ack_size.value       = 0
    dut.ack_wait_ms.value    = 0


@dataclass(frozen=True)
class GotBeat:
    """One captured (accepted) output beat."""
    data:  int
    kmask: int
    sop:   int
    eop:   int


async def capture_packet(dut, m_ready_pat=None,
                         cycles: int = 200) -> list[GotBeat]:
    """Capture one packet: every `m_valid & m_ready` beat up to the first EOP.

    Drives `m_ready` each cycle (1, or `m_ready_pat(cycle)`); after the EOP
    drives `m_ready` = 0 for one more edge. Fails if no EOP in `cycles`.
    """
    got: list[GotBeat] = []
    for c in range(cycles):
        if m_ready_pat is not None:
            dut.m_ready.value = m_ready_pat(c)
        else:
            dut.m_ready.value = 1
        await RisingEdge(dut.tx_clk)
        if int(dut.m_valid.value) and int(dut.m_ready.value):
            got.append(GotBeat(
                data  = int(dut.m_data.value),
                kmask = int(dut.m_kmask.value),
                sop   = int(dut.m_sop.value),
                eop   = int(dut.m_eop.value),
            ))
            if got[-1].eop:
                # Hold m_ready one more cycle so the DUT returns to ST_IDLE
                # cleanly, then exit.
                dut.m_ready.value = 0
                await RisingEdge(dut.tx_clk)
                return got
    raise AssertionError(f"capture_packet: no EOP seen in {cycles} cycles; got={got}")


# -----------------------------------------------------------------------------
# Checkers
# -----------------------------------------------------------------------------
def check_packet(got: list[GotBeat], exp: list[ExpBeat], label: str = ""):
    """Bit-exact compare: length, then data/kmask/sop/eop of every beat."""
    assert len(got) == len(exp), (
        f"{label}: packet length {len(got)} != expected {len(exp)}\n"
        f" got = {got}\n exp = {exp}"
    )
    for i, (g, e) in enumerate(zip(got, exp)):
        assert g.data  == e.data,  f"{label} beat {i} data:  got {g.data:#010x}, expected {e.data:#010x}"
        assert g.kmask == e.kmask, f"{label} beat {i} kmask: got {g.kmask:#x},  expected {e.kmask:#x}"
        assert g.sop   == e.sop,   f"{label} beat {i} sop:   got {g.sop},       expected {e.sop}"
        assert g.eop   == e.eop,   f"{label} beat {i} eop:   got {g.eop},       expected {e.eop}"


# -----------------------------------------------------------------------------
# TC 1 — Write-OK Ack, No Data
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_write_ok_no_data(dut):
    """A write-OK request (0x01) produces a 4-beat immediate ack.

    §8.6.3 / Table 22: immediate acks carry no Size, data or CRC; covers
    the `ST_IDLE → ST_HDR → ST_EOP` path (bare exit at `hdr_idx_q` = 2).

    Stimulus: empty rbuf; 1-cycle request code 0x01, N = 0, with
              `m_ready` = 0; `capture_packet` then holds `m_ready` = 1
              through the EOP.
    Checks:   `check_packet` against the 4-beat model (4×K27.7 SOP,
              4×0x03, 4×0x01, 4×K29.7 EOP with kmask/sop/eop), and
              `len(got) == 4`.
    """
    dut.TESTCASE.value = 1
    await bringup(dut, rbuf=[])
    await issue_ack(dut, code=0x01, nwords=0)
    got = await capture_packet(dut)
    exp = expected_packet(code=0x01, nwords=0, data_words=[])
    check_packet(got, exp, "write-ok")
    assert len(got) == 4, (
        f"write-ok immediate ack should be 4 beats (§8.6.3), got {len(got)}")


# -----------------------------------------------------------------------------
# TC 2 — Read-OK Ack With Data
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_read_ok_with_data(dut):
    """A read-OK request (0x00, N = 4) streams rbuf words with Size and CRC.

    Covers `ST_HDR → ST_DATA → ST_CRC → ST_EOP`, the `rbuf_addr` prefetch
    against a 1-cycle-latency buffer, and the big-endian data swap.

    Stimulus: rbuf = 0xDEADBEEF, 0xCAFEF00D, 0x0BADC0DE, 0x12345678;
              1-cycle request code 0x00, N = 4; `m_ready` = 1 throughout
              the capture.
    Checks:   `check_packet` against the 12-beat model: Size = 0x10 in
              three `rep4` words, `bswap32` of each rbuf word, and the
              model CRC over beats 2–9.
    """
    dut.TESTCASE.value = 2
    rbuf = [0xDEAD_BEEF, 0xCAFE_F00D, 0x0BAD_C0DE, 0x1234_5678]
    await bringup(dut, rbuf=rbuf)
    await issue_ack(dut, code=0x00, nwords=len(rbuf))
    got = await capture_packet(dut)
    exp = expected_packet(code=0x00, nwords=len(rbuf), data_words=rbuf)
    check_packet(got, exp, "read-ok 4 words")


# -----------------------------------------------------------------------------
# TC 3 — Reset-Done Ack
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_reset_done(dut):
    """A reset-done request (0x03) produces a 4-beat immediate ack.

    Table 22: 0x03 takes the immediate shape, like write-OK.

    Stimulus: empty rbuf; 1-cycle request code 0x03, N = 0; `m_ready` = 1
              during the capture.
    Checks:   `check_packet` against the 4-beat immediate model.
    """
    dut.TESTCASE.value = 3
    await bringup(dut, rbuf=[])
    await issue_ack(dut, code=0x03, nwords=0)
    got = await capture_packet(dut)
    exp = expected_packet(code=0x03, nwords=0, data_words=[])
    check_packet(got, exp, "reset-done")


# -----------------------------------------------------------------------------
# TC 4 — Logical-Error Acks
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_logical_err(dut):
    """0x40-class logical-error codes are framed as immediate acks.

    v1.1.1 Table 22 codes, including the new 0x45 (size too large), 0x46
    (inconsistent size) and 0x47 (malformed); the code byte must pass
    through unmodified.

    Stimulus: for each code 0x40, 0x43, 0x45, 0x46, 0x47: a full
              `bringup`, then a 1-cycle request with N = 0 and a capture
              with `m_ready` = 1.
    Checks:   per code, `check_packet` against the 4-beat immediate model
              and beat 2 == 4×code.
    Note:     0x41, 0x42 and 0x44 are not driven. Each loop iteration's
              `bringup` starts an extra `Clock` and `rbuf_model`.
    """
    dut.TESTCASE.value = 4
    # v1.1.1 Table 22 logical-error codes, incl. the new
    # 0x45 (size too large), 0x46 (inconsistent size), 0x47 (malformed).
    for code in (0x40, 0x43, 0x45, 0x46, 0x47):
        await bringup(dut, rbuf=[])
        await issue_ack(dut, code=code, nwords=0)
        got = await capture_packet(dut)
        exp = expected_packet(code=code, nwords=0, data_words=[])
        check_packet(got, exp, f"logical-err 0x{code:02x}")
        assert got[2].data == rep4(code), f"code field should be 4×0x{code:02x}"


# -----------------------------------------------------------------------------
# TC 5 — Wait Ack
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_wait_ack(dut):
    """A Wait request (0x04) sends one reply word holding the ms timeout.

    §8.6.3 Table 22: 0x04 Wait replaces the v1.0 tentative 0x02 and
    carries a single 4-byte ms-timeout reply word (100 ms .. 10 s), framed
    like a 0x00 read ack; the DUT forces B = 4 whatever `ack_size`.

    Stimulus: for `wait_ms` = 100, 5000, 10000: a full `bringup`, then a
              1-cycle request code 0x04 with `ack_size` = 0 and
              `ack_wait_ms` = wait_ms; capture with `m_ready` = 1.
    Checks:   `check_packet` against the 7-beat model (N = 1, data =
              wait_ms); beat 2 == 4×0x04; beat 3 == Size 4 big-endian;
              beat 4 == `bswap32(wait_ms)`.
    Note:     each loop iteration's `bringup` starts an extra `Clock` and
              `rbuf_model`.
    """
    dut.TESTCASE.value = 5
    for wait_ms in (100, 5000, 10000):
        await bringup(dut, rbuf=[])
        # nwords arg is ignored for 0x04 (framer forces N=1).
        await issue_ack(dut, code=0x04, nwords=0, wait_ms=wait_ms)
        got = await capture_packet(dut)
        exp = expected_packet(code=0x04, nwords=1, data_words=[wait_ms])
        check_packet(got, exp, f"wait {wait_ms} ms")
        assert got[2].data == rep4(0x04), "code field should be 4×0x04"
        # One Size word holding B = 4 (one reply word), big-endian.
        assert got[3].data == byteswap32(4), "Size should be 4 bytes"
        assert got[4].data == byteswap32(wait_ms), \
            "reply data word = ms timeout, big-endian on the wire (§8.2.1)"


# -----------------------------------------------------------------------------
# TC 6 — CRC-Error Ack Code
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_crc_err_code(dut):
    """The CRC-error code (0x80) is framed as a 4-beat immediate ack.

    Table 22 physical-error notification — no Size, data or CRC.

    Stimulus: empty rbuf; 1-cycle request code 0x80, N = 0; `m_ready` = 1
              during the capture.
    Checks:   `check_packet` against the 4-beat immediate model.
    """
    dut.TESTCASE.value = 6
    await bringup(dut, rbuf=[])
    await issue_ack(dut, code=0x80, nwords=0)
    got = await capture_packet(dut)
    exp = expected_packet(code=0x80, nwords=0, data_words=[])
    check_packet(got, exp, "crc-err code")


# -----------------------------------------------------------------------------
# TC 7 — Size Field Encoding
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_size_field_encoding(dut):
    """The three Size words carry 4·N bytes for every read length.

    Covers `ST_HDR → ST_CRC` (N = 0: CRC over code/Size only) and
    `ST_HDR → ST_DATA`, the per-packet CRC reseed without reset, and
    N = `p_BUF_DEPTH` (16), where the last prefetch address wraps.

    Stimulus: rbuf = 0xA0000000..0xA000000F (16 words); after one
              `bringup`, requests code 0x00 with N = 0, 1, 5, 8, 16 in
              sequence, `m_ready` = 1, 4 idle cycles after each capture.
    Checks:   per N, `check_packet` against the N+6-beat model, and beats
              3/4/5 == `rep4` of Size[23:16] / [15:8] / [7:0] with
              Size = 4·N.
    """
    dut.TESTCASE.value = 7
    rbuf = [0xA000_0000 | i for i in range(16)]
    await bringup(dut, rbuf=rbuf)
    # AckCode 0x00 (read-OK) keeps the Size+CRC data-ack form for every N,
    # including N=0 — which also exercises the ST_HDR → ST_CRC arc.
    for n in (0, 1, 5, 8, 16):
        await issue_ack(dut, code=0x00, nwords=n)
        got = await capture_packet(dut)
        exp = expected_packet(code=0x00, nwords=n, data_words=rbuf[:n])
        check_packet(got, exp, f"size N={n}")
        # Sanity check the Size word directly: B, big-endian.
        assert got[3].data == byteswap32(n * 4)
        # A few idle cycles between requests.
        for _ in range(4):
            await RisingEdge(dut.tx_clk)


# -----------------------------------------------------------------------------
# TC 8 — CRC32 On Random Payloads
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_crc32_random(dut):
    """The CRC word matches the golden §8.2.2.2 CRC for random read payloads.

    Exercises the CRC fold schedule (code + Size word + N data words,
    folded on each accepted beat) and the per-packet `crc_init`.

    Stimulus: rbuf = 16 random words (`random.Random(0xDEADBEEF)`); after
              one `bringup`, requests code 0x00 with N = 0, 1, 2, 7, 12,
              `m_ready` = 1, 3 idle cycles after each capture.
    Checks:   per trial, `check_packet` against the N+6-beat model,
              including the CRC word (register, LSByte in P0).
    """
    dut.TESTCASE.value = 8
    rng = random.Random(0xDEAD_BEEF)
    rbuf = [rng.randrange(0, 1 << 32) for _ in range(16)]
    await bringup(dut, rbuf=rbuf)
    for trial, n in enumerate([0, 1, 2, 7, 12]):
        # AckCode 0x00 keeps the CRC-bearing data-ack form for all N.
        await issue_ack(dut, code=0x00, nwords=n)
        got = await capture_packet(dut)
        exp = expected_packet(code=0x00, nwords=n, data_words=rbuf[:n])
        check_packet(got, exp, f"crc trial {trial} N={n}")
        for _ in range(3):
            await RisingEdge(dut.tx_clk)


# -----------------------------------------------------------------------------
# TC 9 — EOP / K-Mask Alignment
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_eop_kmask_alignment(dut):
    """`m_eop` and kmask 0xF appear only on the K29.7 trailer beat.

    kmask 0xF is driven only in `ST_EOP` and at `hdr_idx_q` = 0; header,
    data and CRC beats must be flagged as data.

    Stimulus: rbuf of 2 words; 1-cycle request code 0x00, N = 2;
              `m_ready` = 1 during the capture.
    Checks:   exactly one captured beat has `eop` = 1, it is 4×K29.7 with
              kmask 0xF; every beat with `sop` = `eop` = 0 has kmask 0.
    Note:     `capture_packet` stops at the first EOP, so the "exactly
              one" count cannot see a second EOP; data is not compared.
    """
    dut.TESTCASE.value = 9
    rbuf = [0xF00D_F00D, 0xBADD_C0DE]
    await bringup(dut, rbuf=rbuf)
    await issue_ack(dut, code=0x00, nwords=2)
    got = await capture_packet(dut)

    eop_beats   = [b for b in got if b.eop]
    assert len(eop_beats) == 1, f"expected exactly 1 EOP beat, got {len(eop_beats)}"
    assert eop_beats[0].data  == rep4(K29_7), "EOP beat must be 4×K29.7"
    assert eop_beats[0].kmask == 0xF, "EOP beat kmask must be 0xF"

    # All non-EOP, non-SOP beats must have kmask=0.
    for b in got:
        if b.sop == 0 and b.eop == 0:
            assert b.kmask == 0, f"body beat kmask={b.kmask:#x}, expected 0: {b}"


# -----------------------------------------------------------------------------
# TC 10 — SOP / K-Mask Alignment
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_sop_kmask_alignment(dut):
    """`m_sop` and kmask 0xF appear only on the K27.7 leading beat.

    `m_sop` is `ST_HDR & hdr_idx_q == 0`; the arbiter grants the ack slot
    only on an SOP beat.

    Stimulus: rbuf of 1 word; 1-cycle request code 0x00, N = 1;
              `m_ready` = 1 during the capture.
    Checks:   exactly one captured beat has `sop` = 1; it is 4×K27.7 with
              kmask 0xF and `eop` = 0.
    """
    dut.TESTCASE.value = 10
    rbuf = [0x1234_5678]
    await bringup(dut, rbuf=rbuf)
    await issue_ack(dut, code=0x00, nwords=1)
    got = await capture_packet(dut)

    sop_beats   = [b for b in got if b.sop]
    assert len(sop_beats) == 1, f"expected exactly 1 SOP beat, got {len(sop_beats)}"
    assert sop_beats[0].data  == rep4(K27_7), "SOP beat must be 4×K27.7"
    assert sop_beats[0].kmask == 0xF, "SOP beat kmask must be 0xF"
    # SOP and EOP must not be the same beat for any packet length ≥ 2.
    assert sop_beats[0].eop == 0


# -----------------------------------------------------------------------------
# TC 11 — Random Back-Pressure
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_backpressure(dut):
    """Random `m_ready` stalls do not corrupt or reorder the packet.

    Outputs must hold while `m_ready` = 0; in `ST_DATA` the address stays
    on the in-flight word, and index/CRC advance only on accepted beats.

    Stimulus: rbuf = 8 random words (`random.Random(0xC0DEC0DE)`);
              1-cycle request code 0x00, N = 8; `m_ready` = 0 with
              probability 0.4 per cycle (`random.Random(0x55AA)`),
              400-cycle capture window.
    Checks:   `check_packet` against the 16-beat model.
    Note:     with these seeds no stall falls on the SOP beat.
    """
    dut.TESTCASE.value = 11
    rng = random.Random(0xC0DE_C0DE)
    rbuf = [rng.randrange(0, 1 << 32) for _ in range(8)]
    await bringup(dut, rbuf=rbuf)
    await issue_ack(dut, code=0x00, nwords=8)

    stall_rng = random.Random(0x55AA)
    def pat(_c: int) -> int:
        return 0 if stall_rng.random() < 0.40 else 1

    got = await capture_packet(dut, m_ready_pat=pat, cycles=400)
    exp = expected_packet(code=0x00, nwords=8, data_words=rbuf)
    check_packet(got, exp, "backpressure N=8")


# -----------------------------------------------------------------------------
# TC 12 — ack_busy Tracks In-Flight Status
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_ack_busy(dut):
    """`ack_busy` is 0 in idle, 1 from the request through EOP, then 0 again.

    `ack_busy = (state_q != ST_IDLE)` is combinational on `state_q`, so it
    is sampled in the ReadOnly phase after each edge to get the post-NBA
    value deterministically (same convention as cxp_bus.py).

    Stimulus: rbuf of 3 words; request code 0x00, N = 3, driven with
              `m_ready` = 0 so the SOP beat is held in `ST_HDR` for 5 more
              cycles; then `m_ready` = 1 until the EOP beat, then 0.
    Checks:   (ReadOnly) busy = 0 one edge after bring-up; busy = 1 on the
              edge after the request and on each of the 5 parked edges;
              an EOP is captured; busy = 0 one edge after the EOP;
              `check_packet` of the captured 11 beats.
    Note:     busy is not sampled while in `ST_DATA`/`ST_CRC`/`ST_EOP`.
    """
    dut.TESTCASE.value = 12
    rbuf = [0xAAAA_AAAA, 0xBBBB_BBBB, 0xCCCC_CCCC]
    await bringup(dut, rbuf=rbuf)

    # Idle: busy must be 0.
    await RisingEdge(dut.tx_clk)
    await ReadOnly()
    assert int(dut.ack_busy.value) == 0, "ack_busy not 0 in idle"
    await NextTimeStep()

    # Issue request.  Hold m_ready=0 so the FSM parks in ST_HDR for as long
    # as we want — gives us a known state to sample ack_busy against.
    dut.m_ready.value        = 0
    dut.ack_code.value       = 0x00
    dut.ack_size.value       = 12
    dut.ack_req.value        = 1
    await RisingEdge(dut.tx_clk)
    dut.ack_req.value        = 0
    # FSM has now transitioned IDLE → HDR.  ack_busy must be 1.
    await ReadOnly()
    assert int(dut.ack_busy.value) == 1, "ack_busy not asserted right after req"
    await NextTimeStep()

    # Stay parked a few cycles with m_ready=0; ack_busy must remain 1.
    for _ in range(5):
        await RisingEdge(dut.tx_clk)
        await ReadOnly()
        assert int(dut.ack_busy.value) == 1, "ack_busy dropped while parked in ST_HDR"
        await NextTimeStep()

    # Drain the packet (m_ready=1) and watch ack_busy clear after EOP.
    got: list[GotBeat] = []
    for c in range(80):
        dut.m_ready.value = 1
        await RisingEdge(dut.tx_clk)
        if int(dut.m_valid.value) and int(dut.m_ready.value):
            got.append(GotBeat(
                data  = int(dut.m_data.value),
                kmask = int(dut.m_kmask.value),
                sop   = int(dut.m_sop.value),
                eop   = int(dut.m_eop.value),
            ))
        if got and got[-1].eop:
            dut.m_ready.value = 0
            break
    assert got and got[-1].eop, "did not capture EOP"
    # One cycle after the EOP handshake the FSM is back in ST_IDLE.
    await RisingEdge(dut.tx_clk)
    await ReadOnly()
    assert int(dut.ack_busy.value) == 0, "ack_busy did not clear after EOP"
    await NextTimeStep()
    exp = expected_packet(code=0x00, nwords=3, data_words=rbuf)
    check_packet(got, exp, "ack_busy run")


# -----------------------------------------------------------------------------
# TC 13 — Overlapping Request Is Dropped
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_overlapping_req(dut):
    """A request arriving while a packet is in flight is dropped, not queued.

    The latch is gated by `state_q == ST_IDLE && ack_req_i` (per the RTL
    header); an in-flight packet must not be truncated or restarted, and
    a later request must still work.

    Stimulus: rbuf of 4 words; 1-cycle request code 0x00, N = 4,
              `m_ready` = 1; once 4 beats are accepted, `ack_req` = 1 for
              one cycle with code 0x01, N = 0 (inputs left at 0x01/0
              afterwards); after the EOP, 4 idle edges, then a fresh
              1-cycle 0x01 request.
    Checks:   `check_packet` of packet 1 against the 12-beat model; EOP
              count == 1; `ack_busy` = 0 after the 4 idle edges;
              `check_packet` of the fresh 4-beat immediate ack.
    Note:     the EOP count cannot fail — the capture loop stops at the
              first EOP.
    """
    dut.TESTCASE.value = 13
    rbuf = [0x1111_1111, 0x2222_2222, 0x3333_3333, 0x4444_4444]
    await bringup(dut, rbuf=rbuf)

    # Kick off a read-OK ack with N=4.
    await issue_ack(dut, code=0x00, nwords=4)

    # Drain ~4 beats (so the FSM is mid-packet), then pulse ack_req with a
    # different (smaller) request.  The DUT must IGNORE that, finish the
    # first packet, then return to IDLE — *not* truncate or restart.
    rogue_pulsed = False
    got: list[GotBeat] = []
    cycles_run = 0
    while True:
        dut.m_ready.value = 1
        # After 4 fires we pulse a rogue request.
        if not rogue_pulsed and len(got) == 4:
            dut.ack_code.value       = 0x01
            dut.ack_size.value       = 0
            dut.ack_req.value        = 1
            rogue_pulsed = True
        else:
            dut.ack_req.value = 0
        await RisingEdge(dut.tx_clk)
        if int(dut.m_valid.value) and int(dut.m_ready.value):
            got.append(GotBeat(
                data  = int(dut.m_data.value),
                kmask = int(dut.m_kmask.value),
                sop   = int(dut.m_sop.value),
                eop   = int(dut.m_eop.value),
            ))
        cycles_run += 1
        if got and got[-1].eop:
            break
        assert cycles_run < 200, "first packet did not complete"

    dut.ack_req.value = 0
    exp = expected_packet(code=0x00, nwords=4, data_words=rbuf)
    check_packet(got, exp, "overlap-ignored packet 1")

    # Verify only ONE EOP appeared — the rogue request should not have
    # started a second packet on its own.
    eop_count = sum(1 for b in got if b.eop)
    assert eop_count == 1

    # Drain a few cycles, then issue a fresh request — it should now work.
    for _ in range(4):
        await RisingEdge(dut.tx_clk)
    assert int(dut.ack_busy.value) == 0

    await issue_ack(dut, code=0x01, nwords=0)
    got2 = await capture_packet(dut)
    exp2 = expected_packet(code=0x01, nwords=0, data_words=[])
    check_packet(got2, exp2, "post-overlap fresh ack")


# -----------------------------------------------------------------------------
# TC 14 — Back-to-Back Acks
# -----------------------------------------------------------------------------
@cxp_test()
async def test_14_back_to_back(dut):
    """Two consecutive data acks both come out exact and CRC-valid.

    Covers `ST_EOP → ST_IDLE → ST_HDR` with the per-packet clear of the
    header/data indices and the CRC reseed.

    Stimulus: request code 0x00, N = 3 from rbuf[0..2]; after the capture
              plus 3 idle edges, a 1-cycle request code 0x00, N = 2, with
              rbuf[0..1] overwritten to 0xBBBB0001/0xBBBB0002 right after
              the request edge; `m_ready` = 1 up to the second EOP.
    Checks:   `check_packet` #1 against the 11-beat model; `ack_busy` = 0
              before the second request; `check_packet` #2 against the
              10-beat model built from the new words.
    Note:     the 1-cycle minimum gap between packets is not exercised.
    """
    dut.TESTCASE.value = 14
    rbuf = [0xAAAA_0001, 0xAAAA_0002, 0xAAAA_0003,
            0xBBBB_0001, 0xBBBB_0002]
    await bringup(dut, rbuf=rbuf)

    # First ack: read-OK with N=3.
    await issue_ack(dut, code=0x00, nwords=3)
    got1 = await capture_packet(dut)
    exp1 = expected_packet(code=0x00, nwords=3, data_words=rbuf[:3])
    check_packet(got1, exp1, "back-to-back #1")

    # Bridge with a couple of idle cycles, then a second ack.
    for _ in range(3):
        await RisingEdge(dut.tx_clk)
    assert int(dut.ack_busy.value) == 0, "busy did not clear before 2nd ack"

    # Second ack: N=2 with new data.  The rbuf indices restart at 0 for
    # each packet, and `rbuf_model` holds `rbuf` by reference, so the new
    # words are written into rbuf[0..1] right after the request edge
    # (before the FSM reaches ST_DATA).  `expected_packet` is built from
    # `new_rbuf`, so the CRC is checked over the new words.
    new_rbuf = [0xBBBB_0001, 0xBBBB_0002]
    dut.ack_code.value       = 0x00
    dut.ack_size.value       = 8
    dut.ack_req.value        = 1
    await RisingEdge(dut.tx_clk)
    dut.ack_req.value        = 0

    got2: list[GotBeat] = []
    saw_data_beats = 0
    # Rebind rbuf[0..1] in the list shared with the running model.
    rbuf[0] = new_rbuf[0]
    rbuf[1] = new_rbuf[1]
    for c in range(80):
        dut.m_ready.value = 1
        await RisingEdge(dut.tx_clk)
        if int(dut.m_valid.value) and int(dut.m_ready.value):
            got2.append(GotBeat(
                data  = int(dut.m_data.value),
                kmask = int(dut.m_kmask.value),
                sop   = int(dut.m_sop.value),
                eop   = int(dut.m_eop.value),
            ))
            if got2[-1].eop:
                break

    exp2 = expected_packet(code=0x00, nwords=2, data_words=new_rbuf)
    check_packet(got2, exp2, "back-to-back #2")


# -----------------------------------------------------------------------------
# TC 15 — Read of Three Bytes
# -----------------------------------------------------------------------------
@cxp_test()
async def test_15_read_b3(dut):
    """A read of B = 3 bytes acks Size 3 with one data word, pad byte 0.

    Table 22: Size is "the same as the size field in the corresponding
    Control Command packet"; the 4N − B pad bytes are 0.

    Stimulus: rbuf = [0x11223344]; request code 0x00 with `ack_size` = 3.
    Checks:   `check_packet` against the golden 7-beat model; Size word
              lanes 00 00 00 03; data lanes 11 22 33 00.
    """
    dut.TESTCASE.value = 15
    rbuf = [0x1122_3344]
    await bringup(dut, rbuf=rbuf)
    await issue_ack(dut, code=0x00, nwords=1, size=3)
    got = await capture_packet(dut)
    exp = expected_packet(code=0x00, nwords=1, data_words=rbuf, size=3)
    check_packet(got, exp, "read B=3")
    assert got[3].data == 0x0300_0000, f"Size word 0x{got[3].data:08x}"
    assert got[4].data == 0x0033_2211, f"data word 0x{got[4].data:08x}"


# -----------------------------------------------------------------------------
# TC 16 — Read-Buffer Bank
# -----------------------------------------------------------------------------
@cxp_test()
async def test_16_read_bank(dut):
    """The bank travels with the request and is held for the packet.

    The executor fills the other bank with the next read while this
    acknowledgment is framed, so the framer must keep the bank it was
    given at the request.

    Stimulus: rbuf bank 0 = A (4 words), bank 1 = B (4 words); request
              code 0x00, N = 4 with `ack_rbank` = 1, which drops to 0 the
              cycle after the request; then the same with bank 0.
    Checks:   the first packet carries B, the second A (golden model);
              `rbuf_bank` is 1 during the whole first packet.
    """
    dut.TESTCASE.value = 16
    a = [0xA000_0000 + i for i in range(4)]
    b = [0xB000_0000 + i for i in range(4)]
    await bringup(dut, rbuf=[])

    async def banked():
        while True:
            await RisingEdge(dut.tx_clk)
            addr, bank = int(dut.rbuf_addr.value), int(dut.rbuf_bank.value)
            src = b if bank else a
            dut.rbuf_data.value = src[addr] if addr < len(src) else 0

    cocotb.start_soon(banked())
    banks = []

    async def watch():
        while True:
            await RisingEdge(dut.tx_clk)
            if int(dut.ack_busy.value):
                banks.append(int(dut.rbuf_bank.value))

    w = cocotb.start_soon(watch())
    dut.ack_rbank.value = 1
    await issue_ack(dut, code=0x00, nwords=4)
    dut.ack_rbank.value = 0
    check_packet(await capture_packet(dut), expected_packet(0x00, 4, b), "bank 1")
    assert banks and set(banks) == {1}, banks
    w.cancel()
    await issue_ack(dut, code=0x00, nwords=4)
    check_packet(await capture_packet(dut), expected_packet(0x00, 4, a), "bank 0")

