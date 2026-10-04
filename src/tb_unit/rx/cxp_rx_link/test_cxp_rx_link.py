"""Cocotb integration TB for `cxp_rx_link`.

The DUT is the device-side low-speed receive wrapper: oversampling
sampler → 4× 8b/10b decoder → IDLE link monitor → packet parser, feeding the
trigger receiver, connection-test checker and control-command path
(`cxp_ctrl_cmd_parser` → `cxp_ctrl_bus_master` → register bus + read buffer).
The TB drives the soft-PHY serial input `rx_serial` with encoded
CoaXPress words and checks that each demuxed channel produces the
expected output.

The wrapper overrides `OS_RATIO` = 8, `SAMP_LOCK_HITS` = 2,
`BUF_DEPTH` = 16. Single 10 ns `rx_clk`;
`reset()` holds `rx_rst_n` low for 5 edges with all inputs 0
(`from_extension_link` is never driven — Verilator default 0).
`drive_bits` holds each serial bit for `OS_RATIO` cycles, so one 40-bit
word lasts 320 cycles; words come from `cxp_8b10b.encode_word` with the
running disparity chained across helpers, starting at RD−. No shared
checkers: each test polls outputs after `RisingEdge`.

Host-model notes: every trigger is the Table 15 six-character packet from
the golden codec (`trigger_chars()` inserts it into an IDLE stream);
`ctrl_packet_words()` builds a Table 21 command with the golden codec.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Reset → `rx_lock`, `aligned`, `link_detected` all low.
  2  IDLE stream → `rx_lock`, then `link_detected`.
  4  Rising trigger packet → `trigger_out_app` fires.
  5  Control read command → APB read, `rsp_code` = 0x00, rbuf populated.
  6  Control reset opcode (0xFF) → `ctrl_reset_pulse` + `rsp_code` = 0x03.
  7  Trigger packet → single `trig_pkt_rcvd` strobe (§8.3.3 I/O-ack feed),
     independent of `cfg_trig_polarity`.
  8  Three back-to-back 1027-word test packets with one IDLE between them
     → lock never drops, packet count 3, one error for a corrupted word
     1023.
  9  A write whose trailer is lost, then a read → the write is acked 0x47
     and not executed, the read is executed and acked 0x00.
 10  IDLE from RD+ → lock with no disparity error; a read with one 10b
     symbol replaced by an invalid code → acked 0x80, not executed.
 11  A register access that never completes → exactly one Wait (0x04)
     before 200 ms, then a final 0x40 (timeout).
 12  A register access that never completes, then 0xFF → 0x03, and the
     abandoned read never answers.
 13  Table 15 triggers at the four character phases, in IDLE and inside a
     command → each acked once, the command still executes.
 14  One serial bit lost between sampler lock and link-up, at each of the
     four character phases of a word, then clean IDLE → the link still
     comes up within 200 words.
 15  One extra character after link-up (a slip while locked), then a
     read → the read is answered within 100 words.
 16  Table 15 triggers with Delay 0, 80, 160, 239 → the latency from the
     first leader character to `trigger_out_app` is a constant plus Delay
     units of 1/24 bit (§8.3.2.1, Figure 20), within one rx_clk cycle.
 17  A Table 15 trigger with one leader character hit by a bit error,
     inside a read and between IDLEs, at every character phase → the
     trigger fires and is acked; both reads answer 0x00.
 18  A Table 15 trigger while the sampler is locked but the link is still
     down → nothing fires or is acked; the same trigger once up → fires.
 19  Delay characters with one hit by a bit error → fires, latency as
     the clean Delay; three different Delay characters, or Delay 240 →
     `trigger_glitch_pulse`, no `trig_pkt_rcvd`, no `trigger_out_app`.
 20  Two Table 15 triggers back to back inside a read, at every character
     phase → both acked, the read answers 0x00 (the RD change of the
     first rides over the second).
 21  A falling Table 15 trigger right after a data character that one bit
     error turns into K28.2 → still taken as falling, at its own time;
     the rising trigger after it fires (no false "rising" one character
     early, no lost edge).
"""

from __future__ import annotations

from cxp_protocol import SPEC
from cxp_protocol import packets as gp
from cxp_protocol import regmap as rm

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly, NextTimeStep, Timer

import cxp_8b10b as cxp
from cxp_testcase import cxp_test


CLK      = 10
OS_RATIO = 8

K28_1 = cxp.K28_1
K28_5 = cxp.K28_5
K27_7 = cxp.K27_7
K29_7 = cxp.K29_7
D21_5 = cxp.D21_5


# -----------------------------------------------------------------------------
# Host-side packet encoding
# -----------------------------------------------------------------------------
def rep4(b: int) -> int:
    """Replicate one byte into all four lanes of a 32-bit word."""
    return ((b & 0xFF) << 24) | ((b & 0xFF) << 16) | ((b & 0xFF) << 8) | (b & 0xFF)


def word_bits(w40: int) -> list[int]:
    """Split a 40-bit symbol word into serial bits, bit 0 first."""
    return [(w40 >> i) & 1 for i in range(40)]


def encode_packet(beats: list[tuple[list[int], int]], rd_in: int = 0
                  ) -> tuple[list[int], int]:
    """Encode a list of (byte_lanes, kmask) into 40-bit symbol words."""
    out: list[int] = []
    rd = rd_in
    for lanes, km in beats:
        w, rd = cxp.encode_word(lanes, km, rd)
        out.append(w)
    return out, rd


def idle_words(n: int, rd_in: int = 0) -> tuple[list[int], int]:
    """Encode `n` IDLE words K28.5 K28.1 K28.1 D21.5 (Table 14)."""
    beats = [([K28_5, K28_1, K28_1, D21_5], 0b0111)] * n
    return encode_packet(beats, rd_in)


def trigger_chars(rising: bool, delay: int, n_before: int, n_after: int) -> list[int]:
    """Serial bits: `n_before` IDLE words, a Table 15 trigger after the
    K28.5 of the next IDLE word, `n_after` IDLE words, one RD chain."""
    idle = gp.beats_to_chars([gp.IDLE])
    chars = (idle * n_before + idle[:1] + gp.trigger_ls_chars(rising, delay)
             + idle[1:] + idle * n_after)
    syms, _ = cxp.encode_chars(chars)
    return [(sym >> i) & 1 for sym in syms for i in range(10)]


def ctrl_packet_words(op: int, addr: int, size: int,
                      data_words: list[int] | None = None,
                      rd_in: int = 0) -> tuple[list[int], int]:
    """Encode a Table 21 control command (golden `cxp_protocol`, `SPEC`)
    into 40-bit symbol words."""
    beats = gp.ctrl_cmd(op, addr, size, data_words or [], q=SPEC)
    lanes = [([(w >> (8 * i)) & 0xFF for i in range(4)], k) for w, k in beats]
    return encode_packet(lanes, rd_in)


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def reset(dut, clock: bool = True):
    """Start `rx_clk` (unless `clock` is false: already running), zero
    inputs, hold `rx_rst_n` low 5 edges, release 1."""
    if clock:
        cocotb.start_soon(Clock(dut.rx_clk, CLK, unit="ns").start())
    dut.rx_rst_n.value          = 0
    dut.rx_serial.value         = 0
    dut.cfg_trig_polarity.value = 0
    dut.clr_lt_err.value        = 0
    dut.clr_lt_pkt.value        = 0
    dut.prdata.value            = 0
    dut.pready.value            = 0
    dut.pslverr.value           = 0
    dut.rbuf_addr.value         = 0
    for _ in range(5):
        await RisingEdge(dut.rx_clk)
    dut.rx_rst_n.value = 1
    await RisingEdge(dut.rx_clk)


# -----------------------------------------------------------------------------
# Drivers / monitors
# -----------------------------------------------------------------------------
async def drive_bits(dut, bits: list[int]):
    """Drive `bits` onto `rx_serial`, each held for `OS_RATIO` cycles."""
    for b in bits:
        dut.rx_serial.value = b
        for _ in range(OS_RATIO):
            await RisingEdge(dut.rx_clk)


async def wait_for(dut, signal_name: str, timeout_cycles: int = 50000) -> int:
    """Return the edge count until `signal_name` reads 1, or -1 on timeout."""
    sig = getattr(dut, signal_name)
    for n in range(timeout_cycles):
        await RisingEdge(dut.rx_clk)
        if int(sig.value):
            return n
    return -1


# -----------------------------------------------------------------------------
# TC 1 — Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_reset(dut):
    """Link status outputs are low straight out of reset.

    Reset values of the sampler lock and link-monitor state as seen at the
    wrapper ports.

    Stimulus: reset sequence only; `rx_serial` = 0 throughout.
    Checks:   one edge after reset release `rx_lock` = 0, `aligned` = 0,
              `link_detected` = 0.
    Note:     no symbols are received, so nothing is learnt about the RD
              register or the error pulses.
    """
    dut.TESTCASE.value = 1
    await reset(dut)
    assert int(dut.rx_lock.value) == 0
    assert int(dut.aligned.value) == 0
    assert int(dut.link_detected.value) == 0


# -----------------------------------------------------------------------------
# TC 2 — Link Lock On IDLE
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_link_lock(dut):
    """An IDLE stream brings up sampler lock and then link detection.

    Walks the sampler to lock and the link monitor up after 2 IDLE words;
    `link_detected` = sampler lock and link up.

    Stimulus: 30 IDLE words (Table 14) encoded from RD− — 1200 bits,
              9600 cycles — driven in the background.
    Checks:   `rx_lock` rises within 4000 cycles; `link_detected` then
              rises within a further 2000 cycles; the driver completes.
    Note:     `rx_code_err_pulse` / `rx_disp_err_pulse` are not sampled,
              and an RD+ start is not tried.
    """
    dut.TESTCASE.value = 2
    await reset(dut)
    words, _ = idle_words(30)
    bits = sum((word_bits(w) for w in words), [])
    drv = cocotb.start_soon(drive_bits(dut, bits))
    n = await wait_for(dut, "rx_lock", timeout_cycles=4000)
    assert n >= 0, "rx_lock did not assert"
    # link_detected follows the link monitor.
    n2 = await wait_for(dut, "link_detected", timeout_cycles=2000)
    assert n2 >= 0, "link_detected did not assert"
    await drv


# -----------------------------------------------------------------------------
# TC 4 — Trigger Packet
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_trigger(dut):
    """A rising-edge trigger packet produces an application trigger pulse.

    Covers the sampler's Table 15 extraction and its hookup into
    `cxp_rx_trigger_lspd` with the default polarity.

    Stimulus: `cfg_trig_polarity` = 0; 20 IDLE words, a rising Table 15
              trigger (Delay 2) inside the next IDLE word, 30 IDLE words,
              RD chained through.
    Checks:   `trigger_out_app` reads 1 within 20000 cycles.
    Note:     the delay value, pulse width and `trigger_glitch_pulse` are
              not checked here (test_16, test_19).
    """
    dut.TESTCASE.value = 4
    await reset(dut)
    dut.cfg_trig_polarity.value = 0
    bits = trigger_chars(True, 2, 20, 30)

    cocotb.start_soon(drive_bits(dut, bits))
    n = await wait_for(dut, "trigger_out_app", timeout_cycles=20000)
    assert n >= 0, "trigger_out_app did not fire"


# -----------------------------------------------------------------------------
# TC 5 — Control Read Command
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_ctrl_read(dut):
    """A control read on the serial uplink performs an APB read into rbuf.

    End-to-end control path: parser long packet → `cxp_ctrl_cmd_parser` →
    router → APB master → response and read-buffer port.

    Stimulus: 20 IDLE words, a 6-word Table 21 control packet (SOP,
              TYPE 0x02, Cmd 0x00 + Size 4, Addr 0x42, CRC, EOP), 20 IDLE
              words. A Python APB slave answers
              `psel & penable` by driving `prdata` = 0xDEADBEEF with
              `pready` = 1 for one cycle.
    Checks:   the first `rsp_valid` within 40000 cycles carries
              `rsp_code` = 0x00; then with `rbuf_addr` = 0, `rbuf_data`
              sampled in ReadOnly one edge later equals 0xDEADBEEF.
    Note:     `paddr`/`pwrite` and `rsp_size` are not checked; the
              read address 0x42 is not dword-aligned and nothing rejects
              it.
    """
    dut.TESTCASE.value = 5
    await reset(dut)
    warm, rd = idle_words(20)
    bits = sum((word_bits(w) for w in warm), [])
    ctrl_w, rd = ctrl_packet_words(op=0x00, addr=0x42, size=4, rd_in=rd)
    bits += sum((word_bits(w) for w in ctrl_w), [])
    trail, _ = idle_words(20, rd)
    bits += sum((word_bits(w) for w in trail), [])

    cocotb.start_soon(drive_bits(dut, bits))

    # APB slave model: respond with 0xDEADBEEF on the read.
    async def apb_slave():
        while True:
            await RisingEdge(dut.rx_clk)
            if int(dut.psel.value) and int(dut.penable.value):
                dut.prdata.value = 0xDEADBEEF
                dut.pready.value = 1
                await RisingEdge(dut.rx_clk)
                dut.pready.value = 0
    slave = cocotb.start_soon(apb_slave())

    # Wait for rsp_valid.
    rsp = -1
    for _ in range(40000):
        await RisingEdge(dut.rx_clk)
        if int(dut.rsp_valid.value):
            rsp = int(dut.rsp_code.value)
            break
    slave.kill()
    assert rsp == 0x00, f"rsp_code = 0x{rsp:02x}, expected 0x00"
    # Read rbuf[0].
    dut.rbuf_addr.value = 0
    await RisingEdge(dut.rx_clk)
    await ReadOnly()
    val = int(dut.rbuf_data.value)
    await NextTimeStep()
    assert val == 0xDEADBEEF, f"rbuf[0]=0x{val:08x}"


# -----------------------------------------------------------------------------
# TC 6 — Control Reset Opcode
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_ctrl_reset_op(dut):
    """A control-channel reset command (op 0xFF) pulses reset and acks 0x03.

    Covers the reset branch through `cxp_ctrl_cmd_parser` → router → APB
    master with no bus access, and the `ctrl_reset_pulse` export.

    Stimulus: 20 IDLE words, a control packet with op 0xFF, size 0,
              addr 0, 20 IDLE words; no APB slave (`pready` stays 0).
    Checks:   `ctrl_reset_pulse` is seen on some edge up to and including
              the first `rsp_valid` (within 40000 cycles), and that
              response has `rsp_code` = 0x03.
    Note:     `from_extension_link` is 0, so the §10.3.29 extension-link
              gate is not exercised.
    """
    dut.TESTCASE.value = 6
    await reset(dut)
    warm, rd = idle_words(20)
    bits = sum((word_bits(w) for w in warm), [])
    ctrl_w, rd = ctrl_packet_words(op=0xFF, addr=0, size=0, rd_in=rd)
    bits += sum((word_bits(w) for w in ctrl_w), [])
    trail, _ = idle_words(20, rd)
    bits += sum((word_bits(w) for w in trail), [])

    cocotb.start_soon(drive_bits(dut, bits))
    seen_reset = False
    rsp = -1
    for _ in range(40000):
        await RisingEdge(dut.rx_clk)
        if int(dut.ctrl_reset_pulse.value):
            seen_reset = True
        if int(dut.rsp_valid.value):
            rsp = int(dut.rsp_code.value)
            break
    assert seen_reset, "ctrl_reset_pulse did not fire"
    assert rsp == 0x03, f"rsp_code = 0x{rsp:02x}"


# -----------------------------------------------------------------------------
# TC 7 — Trigger Packet Strobe For I/O Ack
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_trig_pkt_rcvd_for_ioack(dut):
    """Every received trigger packet strobes `trig_pkt_rcvd` once, whatever
    the polarity.

    §8.3.3: every received trigger packet must be acknowledged. The
    `trig_pkt_rcvd` strobe (which feeds `cxp_tx_io_ack`) must fire
    independently of `cfg_trig_polarity` — the polarity filter only gates
    the application trigger `trigger_out_app`, not the I/O-ack obligation.

    Stimulus: `cfg_trig_polarity` = 1 (falling edge selected for the app);
              20 IDLE words, a *rising* Table 15 trigger (Delay 2) inside
              the next IDLE word, 40 IDLE words.
    Checks:   `trig_pkt_rcvd` is seen within 20000 cycles; over the next
              400 cycles it stays 0 (single-cycle strobe, no re-assert);
              `trigger_out_app` is never 1 across both windows.
    Note:     `saw_rcvd == 1` cannot fail once seen — the first loop
              stops at the first strobe. The glitched-leader case (strobe
              suppressed) is not exercised.
    """
    dut.TESTCASE.value = 7
    await reset(dut)
    dut.cfg_trig_polarity.value = 1            # select falling for the app
    bits = trigger_chars(True, 2, 20, 40)      # rising packet

    cocotb.start_soon(drive_bits(dut, bits))

    saw_rcvd = 0
    saw_app  = 0
    for _ in range(20000):
        await RisingEdge(dut.rx_clk)
        if int(dut.trig_pkt_rcvd.value):
            saw_rcvd += 1
        if int(dut.trigger_out_app.value):
            saw_app += 1
        if saw_rcvd:
            # Stop at the first strobe; the drain loop below confirms it
            # is a single 1-cycle strobe.
            break
    assert saw_rcvd == 1, (
        f"trig_pkt_rcvd must strobe once per received trigger packet "
        f"(saw {saw_rcvd})"
    )

    # Drain a little and confirm it does not re-assert and that the
    # polarity filter did indeed suppress the application pulse.
    for _ in range(400):
        await RisingEdge(dut.rx_clk)
        assert int(dut.trig_pkt_rcvd.value) == 0, "spurious trig_pkt_rcvd"
        if int(dut.trigger_out_app.value):
            saw_app += 1
    assert saw_app == 0, (
        "rising trigger should be filtered from trigger_out_app when "
        "cfg_trig_polarity=1 — but the I/O-ack feed must still have fired"
    )


# -----------------------------------------------------------------------------
# TC 8 — Back-To-Back Connection Test Packets
# -----------------------------------------------------------------------------
async def drive_bits_fast(dut, bits: list[int]):
    """`drive_bits` with one timer per bit instead of one edge per cycle."""
    await RisingEdge(dut.rx_clk)
    for b in bits:
        dut.rx_serial.value = b
        await Timer(OS_RATIO * CLK, unit="ns")


@cxp_test()
async def test_08_back_to_back_test_packets(dut):
    """Host test packets with one IDLE between them keep the link (§8.7.3).

    Table 23 packets are 1027 words long and §8.7.3 only asks the host
    for one IDLE between them; §8.2.5.1 allows 10 000 words between IDLEs
    on the low-speed link, so neither lock nor the link may drop.

    Stimulus: 20 IDLE; Table 23 packet; 1 IDLE; Table 23 packet; 1 IDLE;
              Table 23 packet with word 1023 bit 0 flipped; 20 IDLE —
              all from the golden codec, running disparity chained.
    Checks:   once up, `rx_lock` and `link_detected` never fall;
              `lt_pkt_count_rx` = 3 and `lt_err_count` = 1 at the end.
    """
    dut.TESTCASE.value = 8
    await reset(dut)
    lanes = lambda beats: [([(w >> (8 * i)) & 0xFF for i in range(4)], k) for w, k in beats]
    good = gp.linktest_packet()
    bad = list(good)
    w, k = bad[2 + 1023]
    bad[2 + 1023] = (w ^ 1, k)
    idle = [gp.IDLE]
    seq = [gp.IDLE] * 20 + good + idle + good + idle + bad + [gp.IDLE] * 20
    words, _ = encode_packet(lanes(seq))
    bits = sum((word_bits(x) for x in words), [])

    drops = {"n": 0}

    async def watch_lock():
        up = False
        while True:
            await RisingEdge(dut.rx_clk)
            if int(dut.link_detected.value):
                up = True
            elif up:
                drops["n"] += 1
                up = False

    cocotb.start_soon(watch_lock())
    await drive_bits_fast(dut, bits)
    for _ in range(10):
        await RisingEdge(dut.rx_clk)
    assert drops["n"] == 0, f"link dropped {drops['n']} time(s) inside the test packets"
    assert int(dut.lt_pkt_count_rx.value) == 3, f"packets {int(dut.lt_pkt_count_rx.value)}"
    assert int(dut.lt_err_count.value) == 1, f"errors {int(dut.lt_err_count.value)}"


# -----------------------------------------------------------------------------
# TC 9 — Lost Trailer
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_lost_trailer(dut):
    """A lost K29.7 must not merge two commands (§8.2.2.1, §8.2.5.2).

    The host's IDLE after the broken write ends it (no IDLE is allowed
    inside a low-speed packet); the write is acked 0x47 (§8.6.1.1) and the
    next command is executed on its own.

    Stimulus: 20 IDLE; a Table 21 write to 0x4014 with its K29.7 word
              replaced by an IDLE; 10 IDLE; a Table 21 read of 0x2008;
              20 IDLE.  A Python APB slave answers reads with 0xDEADBEEF.
    Checks:   the first response is 0x47; the second is 0x00 with rbuf[0]
              = 0xDEADBEEF; no APB write happens.
    """
    dut.TESTCASE.value = 9
    await reset(dut)
    lanes = lambda beats: [([(w >> (8 * i)) & 0xFF for i in range(4)], k) for w, k in beats]
    wr = gp.ctrl_cmd(gp.OP_WRITE, rm.CONNECTION_CONFIG, data=[0x1], q=SPEC)[:-1]   # no K29.7
    rd = gp.ctrl_cmd(gp.OP_READ, rm.DEVICE_VENDOR_NAME + 8, 4, q=SPEC)
    seq = [gp.IDLE] * 20 + wr + [gp.IDLE] * 10 + rd + [gp.IDLE] * 20
    words, _ = encode_packet(lanes(seq))
    bits = sum((word_bits(x) for x in words), [])
    cocotb.start_soon(drive_bits_fast(dut, bits))

    writes = {"n": 0}

    async def apb_slave():
        while True:
            await RisingEdge(dut.rx_clk)
            if int(dut.psel.value) and int(dut.penable.value):
                writes["n"] += int(dut.pwrite.value)
                dut.prdata.value = 0xDEADBEEF
                dut.pready.value = 1
                await RisingEdge(dut.rx_clk)
                dut.pready.value = 0
    slave = cocotb.start_soon(apb_slave())

    codes = []
    for _ in range(len(bits) * OS_RATIO + 2000):
        await RisingEdge(dut.rx_clk)
        if int(dut.rsp_valid.value):
            codes.append(int(dut.rsp_code.value))
            if len(codes) == 2:
                break
    slave.cancel()
    assert codes == [0x47, 0x00], f"responses {[hex(c) for c in codes]}"
    assert writes["n"] == 0, "the broken write was executed"
    dut.rbuf_addr.value = 0
    await RisingEdge(dut.rx_clk)
    await ReadOnly()
    val = int(dut.rbuf_data.value)
    await NextTimeStep()
    assert val == 0xDEADBEEF, f"rbuf[0]=0x{val:08x}"


# -----------------------------------------------------------------------------
# TC 10 — RD Seed And Decode Errors
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_rd_seed_and_code_error(dut):
    """The disparity chain follows the host's; a bad symbol fails the command.

    The host's running disparity at lock is unknown; the first K28.5 seeds
    the chain, so an RD+ start gives no spurious disparity error.  A word
    with a code error is marked and fails the command (0x80).

    Stimulus: 20 IDLE encoded from RD+; a Table 21 read of 0x2008 whose
              Addr word lane P2 is replaced by the invalid 10b code
              0b0000000000; 20 IDLE.
    Checks:   no `rx_disp_err_pulse` before the read; `rx_code_err_pulse`
              seen; the response is 0x80 and no APB access happens.
    """
    dut.TESTCASE.value = 10
    await reset(dut)
    lanes = lambda beats: [([(w >> (8 * i)) & 0xFF for i in range(4)], k) for w, k in beats]
    warm, rd = encode_packet(lanes([gp.IDLE] * 20), rd_in=1)
    cmd, rd = encode_packet(lanes(gp.ctrl_cmd(gp.OP_READ, rm.DEVICE_VENDOR_NAME + 8, 4, q=SPEC)), rd)
    cmd[3] &= ~(0x3FF << 20)                     # Addr word, lane P2: invalid code
    tail, _ = encode_packet(lanes([gp.IDLE] * 20), rd)
    bits = sum((word_bits(x) for x in warm + cmd + tail), [])
    n_warm = len(warm) * 40 * OS_RATIO
    cocotb.start_soon(drive_bits_fast(dut, bits))

    seen = {"disp_warm": 0, "code": 0, "apb": 0}
    codes = []
    for n in range(len(bits) * OS_RATIO + 2000):
        await RisingEdge(dut.rx_clk)
        if n < n_warm:
            seen["disp_warm"] += int(dut.rx_disp_err_pulse.value)
        seen["code"] += int(dut.rx_code_err_pulse.value)
        seen["apb"] += int(dut.psel.value)
        if int(dut.rsp_valid.value):
            codes.append(int(dut.rsp_code.value))
            break
    assert seen["disp_warm"] == 0, "spurious disparity error on an RD+ start"
    assert seen["code"] >= 1, "the invalid symbol was not flagged"
    assert codes == [0x80], f"responses {[hex(c) for c in codes]}"
    assert seen["apb"] == 0, "the command reached the bus"


# -----------------------------------------------------------------------------
# TC 11 / 12 — Slow Register Bus
# -----------------------------------------------------------------------------
async def _collect_rsp(dut, cycles: int) -> list[tuple[int, int, int]]:
    """(cycle, code, wait_ms) of every response seen within `cycles`."""
    out = []
    for n in range(cycles):
        await RisingEdge(dut.rx_clk)
        if int(dut.rsp_valid.value):
            out.append((n, int(dut.rsp_code.value),
                        int(dut.rsp_wait_ms.value)))
    return out


def _bits_of(beats) -> list[int]:
    lanes = [([(w >> (8 * i)) & 0xFF for i in range(4)], k) for w, k in beats]
    words, _ = encode_packet(lanes)
    return sum((word_bits(x) for x in words), [])


@cxp_test()
async def test_11_wait_then_timeout(dut):
    """A hung register access gets one Wait, then a timeout (§8.6.1.1).

    The wrapper runs the control plane at 50 kHz (50 rx_clk cycles per
    millisecond): Wait after 100 ms, timeout after 900 ms, Wait payload
    1000 ms.  PREADY never rises.

    Stimulus: 20 IDLE, a Table 21 read of 0x2000, IDLE for the rest.
    Checks:   exactly two responses: 0x04 with 1000 ms less than 200 ms
              (10000 cycles) after the command, then 0x40.
    """
    dut.TESTCASE.value = 11
    await reset(dut)
    bits = _bits_of([gp.IDLE] * 20 + gp.ctrl_cmd(gp.OP_READ, rm.DEVICE_VENDOR_NAME, 4, q=SPEC)
                    + [gp.IDLE] * 40)
    cmd_end = (20 + 6) * 40 * OS_RATIO
    cocotb.start_soon(drive_bits_fast(dut, bits))
    rsps = await _collect_rsp(dut, cmd_end + 50 * 950)
    codes = [c for _, c, _ in rsps]
    assert codes == [0x04, 0x40], f"responses {[hex(c) for c in codes]}"
    assert rsps[0][0] - cmd_end < 50 * 200, f"Wait {rsps[0][0] - cmd_end} cycles after the command"
    assert rsps[0][2] == 1000, f"Wait payload {rsps[0][2]} ms"


@cxp_test()
async def test_12_hung_then_reset(dut):
    """0xFF aborts a hung access and answers 0x03 (§8.6.1.2).

    Stimulus: 20 IDLE, a read of 0x2000 (PREADY never rises), 10 IDLE,
              a 0xFF control channel reset, IDLE.
    Checks:   the responses are exactly [0x03] within the first 100 ms (no
              Wait yet, no final for the abandoned read), and
              `ctrl_reset_pulse` fires.
    """
    dut.TESTCASE.value = 12
    await reset(dut)
    bits = _bits_of([gp.IDLE] * 20 + gp.ctrl_cmd(gp.OP_READ, rm.DEVICE_VENDOR_NAME, 4, q=SPEC)
                    + [gp.IDLE] * 2 + gp.ctrl_cmd(gp.OP_RESET, q=SPEC) + [gp.IDLE] * 20)
    cocotb.start_soon(drive_bits_fast(dut, bits))
    seen_reset = {"n": 0}

    async def watch():
        while True:
            await RisingEdge(dut.rx_clk)
            seen_reset["n"] += int(dut.ctrl_reset_pulse.value)
    cocotb.start_soon(watch())
    rsps = await _collect_rsp(dut, len(bits) * OS_RATIO + 2000)
    codes = [c for _, c, _ in rsps]
    assert codes[0] == 0x03, f"responses {[hex(c) for c in codes]}"
    assert 0x40 not in codes and 0x00 not in codes, f"the aborted read answered: {codes}"
    assert seen_reset["n"] == 1


# -----------------------------------------------------------------------------
# TC 13 — Table 15 Trigger At Every Character Phase
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_ls_trigger_char_form(dut):
    """A Table 15 trigger at any character boundary fires and is acked once.

    §8.2.4 / §8.3.2.1: the low-speed trigger is six characters (leader
    K28.2 K28.4 K28.4 rising, K28.4 K28.2 K28.2 falling, then 3×Delay) that
    the host may insert at any character boundary, also inside a packet.
    The receiver must take them out without shifting the word framing.

    Stimulus: 20 IDLE, then for phase p = 0..3: 4 IDLE with a rising
              Table 15 trigger (Delay 51) inserted p characters into the
              second IDLE word; a Table 21 read of 0x2000 with a falling
              trigger (Delay 187) inserted p characters into its Addr
              word; 6 IDLE; 10 IDLE at the end.  One RD chain over every character; APB
              slave answers 0xDEADBEEF.
    Checks:   `trig_pkt_rcvd` strobes 8 times; `trigger_out_app` pulses
              4 times (polarity 0: rising triggers only); 4 responses,
              all 0x00; no code or disparity error; `rx_lock` never
              drops.
    """
    dut.TESTCASE.value = 13
    await reset(dut)
    chars = gp.beats_to_chars([gp.IDLE] * 20)
    for p in range(4):
        idle = gp.beats_to_chars([gp.IDLE] * 4)
        cut = 4 + p
        chars += idle[:cut] + gp.trigger_ls_chars(True, 51) + idle[cut:]
        cmd = gp.beats_to_chars(gp.ctrl_cmd(gp.OP_READ, rm.DEVICE_VENDOR_NAME, 4, q=SPEC))
        cut = 3 * 4 + p                          # SOP, TYPE, Cmd/Size, then Addr
        chars += cmd[:cut] + gp.trigger_ls_chars(False, 187) + cmd[cut:]
        chars += gp.beats_to_chars([gp.IDLE] * 6)
    chars += gp.beats_to_chars([gp.IDLE] * 10)
    syms, _ = cxp.encode_chars(chars)
    bits = [(s >> i) & 1 for s in syms for i in range(10)]
    cocotb.start_soon(drive_bits_fast(dut, bits))

    async def apb_slave():
        while True:
            await RisingEdge(dut.rx_clk)
            if int(dut.psel.value) and int(dut.penable.value):
                dut.prdata.value = 0xDEADBEEF
                dut.pready.value = 1
                await RisingEdge(dut.rx_clk)
                dut.pready.value = 0
    slave = cocotb.start_soon(apb_slave())

    seen = {"rcvd": 0, "rise": 0, "code": 0, "disp": 0, "unlock": 0}
    codes = []
    locked = False
    prev = 0
    # Up to the end of the stimulus: the line then stops toggling.
    for _ in range(len(bits) * OS_RATIO):
        await RisingEdge(dut.rx_clk)
        seen["rcvd"] += int(dut.trig_pkt_rcvd.value)
        seen["code"] += int(dut.rx_code_err_pulse.value)
        seen["disp"] += int(dut.rx_disp_err_pulse.value)
        lock = int(dut.rx_lock.value)
        seen["unlock"] += int(locked and not lock)
        locked |= bool(lock)
        out = int(dut.trigger_out_app.value)
        seen["rise"] += int(out and not prev)
        prev = out
        if int(dut.rsp_valid.value):
            codes.append(int(dut.rsp_code.value))
    slave.cancel()
    assert seen["rcvd"] == 8, f"trig_pkt_rcvd strobed {seen['rcvd']} times for 8 triggers"
    assert codes == [0x00] * 4, f"responses {[hex(c) for c in codes]}"
    assert seen["code"] == 0 and seen["disp"] == 0, f"decode errors {seen}"
    assert seen["unlock"] == 0, "rx_lock dropped"
    assert seen["rise"] == 4, f"trigger_out_app pulses {seen['rise']} for 4 rising triggers"


# -----------------------------------------------------------------------------
# Serial driver with a cycle count
# -----------------------------------------------------------------------------
async def drive_counted(dut, bits: list[int], each_cycle=None, drop_bit=None):
    """Drive `bits` one per OS_RATIO rx_clk cycles and count the cycles.

    `each_cycle(cyc)` runs after every edge.  `drop_bit(i)` is asked before
    bit i; when it returns True that bit is not sent (a lost bit)."""
    cyc = 0
    for i, b in enumerate(bits):
        if drop_bit is not None and drop_bit(i):
            continue
        dut.rx_serial.value = b
        for _ in range(OS_RATIO):
            await RisingEdge(dut.rx_clk)
            cyc += 1
            if each_cycle is not None:
                each_cycle(cyc)
    return cyc


def char_bits(chars) -> list[int]:
    syms, _ = cxp.encode_chars(chars)
    return [(sym >> i) & 1 for sym in syms for i in range(10)]


async def apb_answer(dut, value: int = 0xDEADBEEF):
    while True:
        await RisingEdge(dut.rx_clk)
        if int(dut.psel.value) and int(dut.penable.value):
            dut.prdata.value = value
            dut.pready.value = 1
            await RisingEdge(dut.rx_clk)
            dut.pready.value = 0


# -----------------------------------------------------------------------------
# TC 14 — Glitch Before Link-Up
# -----------------------------------------------------------------------------
@cxp_test()
async def test_14_glitch_before_link_up(dut):
    """A lost bit before link-up does not keep the link down.

    §10.2: the device keeps looking for the IDLE pattern until it finds
    it.  The sampler holds its character phase once locked and the link
    monitor never asks it to hunt again while the link is down.

    Stimulus: for p = 0..3 (reset between): 300 IDLE words; the first bit
              of character p of a word, after `rx_lock` rises and while
              the link is still down, is not sent, so every later
              character is one bit off the locked phase.
    Checks:   `aligned` rises within 200 words of the lost bit, each p.
    """
    dut.TESTCASE.value = 14
    bits = char_bits(gp.beats_to_chars([gp.IDLE] * 300))
    for p in range(4):
        await reset(dut, clock=(p == 0))
        st = {"drop": None, "up": None, "cyc": 0}

        def drop(i):
            if (st["drop"] is None and i % 40 == 10 * p and int(dut.rx_lock.value)
                    and not int(dut.aligned.value)):
                st["drop"] = i
                return True
            return False

        def each(cyc):
            st["cyc"] = cyc
            if st["drop"] is not None and st["up"] is None and int(dut.aligned.value):
                st["up"] = cyc

        await drive_counted(dut, bits, each, drop)
        assert st["drop"] is not None, f"phase {p}: no bit dropped before link-up"
        words_after = (len(bits) - st["drop"]) // 40
        assert st["up"] is not None, f"phase {p}: link still down {words_after} words after the lost bit"
        assert (st["up"] // OS_RATIO - st["drop"]) // 40 < 200, f"phase {p}: link up late"


# -----------------------------------------------------------------------------
# TC 15 — Character Slip While Locked
# -----------------------------------------------------------------------------
@cxp_test()
async def test_15_slip_while_locked(dut):
    """One inserted character after link-up does not silence the link.

    §10.2, Table 14: K28.5 only ever appears in P0, so a K28.5 in another
    lane shows the word framing is wrong.

    Stimulus: 20 IDLE words (link up); one extra K28.5 character; 20 IDLE
              words; a read of 0x2000; 100 IDLE words.
    Checks:   the read gets a response (`rsp_valid`) before the end.
    """
    dut.TESTCASE.value = 15
    await reset(dut)
    chars = gp.beats_to_chars([gp.IDLE] * 20) + [(gp.K28_5, True)]
    chars += gp.beats_to_chars([gp.IDLE] * 20)
    chars += gp.beats_to_chars(gp.ctrl_cmd(gp.OP_READ, rm.DEVICE_VENDOR_NAME, 4, q=SPEC))
    chars += gp.beats_to_chars([gp.IDLE] * 100)
    slave = cocotb.start_soon(apb_answer(dut))
    codes = []
    await drive_counted(dut, char_bits(chars),
                        lambda c: codes.append(int(dut.rsp_code.value))
                        if int(dut.rsp_valid.value) else None)
    slave.cancel()
    assert codes, "read after a one-character slip never answered"
    assert codes[0] == 0x00, f"response 0x{codes[0]:02x}"


# -----------------------------------------------------------------------------
# TC 16 — Trigger Delay In Absolute Time
# -----------------------------------------------------------------------------
@cxp_test()
async def test_16_delay_absolute_time(dut):
    """The recreated trigger has a fixed latency from the event.

    §8.3.2.1 / Figure 20: Delay = 239 minus the event-to-packet time in
    units of 1/24 bit, and the receiver counts Delay down in the same
    unit, so the latency from the first leader character to the output
    grows by exactly Delay / 24 bit intervals.

    Stimulus: (a) for Delay in 0, 80, 160, 239: 10 IDLE, a rising Table
              15 trigger, 10 IDLE, a falling one, 10 IDLE.  (b) Phase
              sweep: for every pre-roll of 0 .. OS_RATIO - 1 rx_clk cycles
              before the line starts (reset between), 10 IDLE, then a
              rising trigger (Delay 120) at character phase p of an IDLE
              word for p = 0..3, each followed 3 IDLE words later by a
              falling one (the next rising trigger must change the level,
              §8.3.3 — a repeated edge is a resend).
    Checks:   (rise of `trigger_out_app` - start of the rising leader)
              - Delay x OS_RATIO / 24 cycles is the same for every Delay
              within one rx_clk cycle (the wait is rounded to a cycle);
              in (b) every trigger of every pre-roll has the same latency
              from its leader to within one rx_clk cycle.
    """
    dut.TESTCASE.value = 16
    await reset(dut)
    delays = [0, 80, 160, 239]
    chars = gp.beats_to_chars([gp.IDLE] * 20)
    starts = {}
    for d in delays:
        chars += gp.beats_to_chars([gp.IDLE] * 10)
        starts[d] = len(chars)
        chars += gp.trigger_ls_chars(True, d) + gp.beats_to_chars([gp.IDLE] * 10)
        chars += gp.trigger_ls_chars(False, d) + gp.beats_to_chars([gp.IDLE] * 10)
    rises, prev = [], [0]

    def each(cyc):
        v = int(dut.trigger_out_app.value)
        if v and not prev[0]:
            rises.append(cyc)
        prev[0] = v

    await drive_counted(dut, char_bits(chars), each)
    assert len(rises) == len(delays), f"{len(rises)} output rises for {len(delays)} triggers"
    resid = {d: r - starts[d] * 10 * OS_RATIO - d * OS_RATIO / 24
             for d, r in zip(delays, rises)}
    spread = max(resid.values()) - min(resid.values())
    assert spread <= 1, (f"latency minus Delay/24 bit varies by {spread:.1f} rx_clk "
                         f"cycles: { {d: round(v, 1) for d, v in resid.items()} }")

    lats = {}
    idle = gp.beats_to_chars([gp.IDLE])
    for pre in range(OS_RATIO):
        await reset(dut, clock=False)
        for _ in range(pre):
            await RisingEdge(dut.rx_clk)
        chars = gp.beats_to_chars([gp.IDLE] * 10)
        starts = []
        for p in range(4):
            chars += gp.beats_to_chars([gp.IDLE] * 3)
            starts.append(len(chars) + p)
            chars += idle[:p] + gp.trigger_ls_chars(True, 120) + idle[p:]
            chars += gp.beats_to_chars([gp.IDLE] * 3) + gp.trigger_ls_chars(False, 120)
        chars += gp.beats_to_chars([gp.IDLE] * 3)
        rises.clear()
        prev[0] = 0
        await drive_counted(dut, char_bits(chars), each)
        assert len(rises) == 4, f"pre-roll {pre}: {len(rises)} rises for 4 triggers"
        for p, (st, r) in enumerate(zip(starts, rises)):
            lats[(pre, p)] = r - st * 10 * OS_RATIO
    spread = max(lats.values()) - min(lats.values())
    assert spread <= 1, f"latency varies by {spread} cycles over phases: {sorted(set(lats.values()))}"


# -----------------------------------------------------------------------------
# Character-level stimulus helpers
# -----------------------------------------------------------------------------
def char_bits_flip(chars, flips: dict) -> list[int]:
    """`char_bits` with bit `flips[i]` of character i's 10-bit symbol
    inverted (a line error; the running disparity goes on as sent)."""
    syms, _ = cxp.encode_chars(chars)
    for i, b in flips.items():
        syms[i] ^= 1 << b
    return [(sym >> i) & 1 for sym in syms for i in range(10)]


class RxWatch:
    """Counts of the rx_top outputs over one `drive_counted` run."""

    def __init__(self, dut):
        self.dut = dut
        self.rcvd = self.rise = self.glitch = 0
        self.codes: list[int] = []
        self.rise_at: list[int] = []
        self.rcvd_down = self.rise_down = 0
        self._prev = 0

    def __call__(self, cyc):
        d = self.dut
        up = int(d.aligned.value)
        r = int(d.trig_pkt_rcvd.value)
        self.rcvd += r
        self.rcvd_down += r and not up
        self.glitch += int(d.trigger_glitch_pulse.value)
        out = int(d.trigger_out_app.value)
        if out and not self._prev:
            self.rise += 1
            self.rise_down += not up
            self.rise_at.append(cyc)
        self._prev = out
        if int(d.rsp_valid.value):
            self.codes.append(int(d.rsp_code.value))


# -----------------------------------------------------------------------------
# TC 17 — Table 15 Leader With A Bad Character
# -----------------------------------------------------------------------------
@cxp_test()
async def test_17_leader_bit_error(dut):
    """One bit error in a trigger leader does not cost the trigger or the
    command around it.

    §8.2.2: the link protocol is immune to single bit errors; the three
    leader characters vote like the Delay.  A leader that is not
    recognised leaves six characters in the word stream, which rotates
    the lanes (Table 14 K28.5 no longer in P0).

    Stimulus: 20 IDLE; for p = 0..3: 4 IDLE with a rising Table 15
              trigger (Delay 51) inserted p characters into the second
              IDLE word, leader character (p % 3) with bit 2 inverted; a
              read of 0x2000 with a falling trigger inserted p characters
              into its Addr word, leader character ((p + 1) % 3) with bit
              7 inverted; 6 IDLE; 10 IDLE at the end.  APB slave answers.
    Checks:   8 `trig_pkt_rcvd`, 4 rises of `trigger_out_app` (polarity
              0: the falling ones are taken and change the level), no
              glitch; 4 responses, all 0x00.
    """
    dut.TESTCASE.value = 17
    await reset(dut)
    chars = gp.beats_to_chars([gp.IDLE] * 20)
    flips = {}
    for p in range(4):
        idle = gp.beats_to_chars([gp.IDLE] * 4)
        cut = 4 + p
        flips[len(chars) + cut + p % 3] = 2
        chars += idle[:cut] + gp.trigger_ls_chars(True, 51) + idle[cut:]
        cmd = gp.beats_to_chars(gp.ctrl_cmd(gp.OP_READ, rm.DEVICE_VENDOR_NAME, 4, q=SPEC))
        cut = 3 * 4 + p
        flips[len(chars) + cut + (p + 1) % 3] = 7
        chars += cmd[:cut] + gp.trigger_ls_chars(False, 51) + cmd[cut:]
        chars += gp.beats_to_chars([gp.IDLE] * 6)
    chars += gp.beats_to_chars([gp.IDLE] * 10)
    slave = cocotb.start_soon(apb_answer(dut))
    w = RxWatch(dut)
    await drive_counted(dut, char_bits_flip(chars, flips), w)
    slave.cancel()
    assert w.codes == [0x00] * 4, f"responses {[hex(c) for c in w.codes]}"
    assert (w.rcvd, w.rise, w.glitch) == (8, 4, 0), (
        f"trig_pkt_rcvd {w.rcvd}, trigger_out_app {w.rise}, glitch {w.glitch} "
        "for 4 rising and 4 falling triggers")


# -----------------------------------------------------------------------------
# TC 18 — Trigger While The Link Is Down
# -----------------------------------------------------------------------------
@cxp_test()
async def test_18_trigger_while_down(dut):
    """Triggers count only once the link is up.

    §10.1.1 / §8.3.2: before the link is detected the character framing
    is not confirmed, so nothing decoded from it may reach the
    application or be acknowledged.

    Stimulus: one IDLE; IDLE words 2, 3 and 4 each with a rising Table
              15 trigger (Delay 0) after its K28.5 (the sampler locks on
              the third K28.5, the link comes up two IDLE words later);
              20 IDLE; the same trigger inside an IDLE word; 20 IDLE.
    Checks:   while `aligned` = 0 no `trig_pkt_rcvd` and no
              `trigger_out_app`; once up, one of each.
    """
    dut.TESTCASE.value = 18
    await reset(dut)
    idle = gp.beats_to_chars([gp.IDLE])
    trig = gp.trigger_ls_chars(True, 0)
    chars = idle + (idle[:1] + trig + idle[1:]) * 3 + gp.beats_to_chars([gp.IDLE] * 20)
    chars += idle[:2] + trig + idle[2:] + gp.beats_to_chars([gp.IDLE] * 20)
    w = RxWatch(dut)
    await drive_counted(dut, char_bits(chars), w)
    assert (w.rcvd_down, w.rise_down) == (0, 0), (
        f"while down: trig_pkt_rcvd {w.rcvd_down}, trigger_out_app {w.rise_down}")
    assert (w.rcvd, w.rise) == (1, 1), f"once up: trig_pkt_rcvd {w.rcvd}, trigger_out_app {w.rise}"


# -----------------------------------------------------------------------------
# TC 19 — Delay Vote
# -----------------------------------------------------------------------------
@cxp_test()
async def test_19_delay_vote(dut):
    """The three Delay characters vote; no majority is a glitch.

    §8.2.2 / Table 15: the Delay is sent three times so one bad copy is
    out-voted; with no two copies alike, or a value above 239, the packet
    cannot be trusted.

    Stimulus: 20 IDLE; rising triggers each followed by 10 IDLE: (a)
              Delay 120 clean, then a clean falling one and 10 IDLE; (b)
              Delay 120 with Delay character 1 hit by a bit error; (c)
              Delay characters 10, 20, 30; (d) Delay 240.
    Checks:   (a) and (b) fire and are acked, (b) at the latency of (a)
              within one rx_clk cycle; (c) and (d) pulse
              `trigger_glitch_pulse`, with no `trig_pkt_rcvd` and no
              `trigger_out_app`.
    """
    dut.TESTCASE.value = 19
    await reset(dut)
    idle10 = gp.beats_to_chars([gp.IDLE] * 10)
    chars = gp.beats_to_chars([gp.IDLE] * 20)
    starts, flips = [], {}
    for case in "abcd":
        starts.append(len(chars))
        t = gp.trigger_ls_chars(True, 240 if case == "d" else 120)
        if case == "b":
            flips[len(chars) + 4] = 3
        if case == "c":
            t = t[:3] + [(10, False), (20, False), (30, False)]
        chars += t + idle10
        if case == "a":
            chars += gp.trigger_ls_chars(False, 120) + idle10
    w = RxWatch(dut)
    marks = []

    def each(cyc):
        g, r = w.glitch, w.rcvd
        w(cyc)
        if w.glitch != g or w.rcvd != r:
            marks.append(("g" if w.glitch != g else "r", cyc))

    await drive_counted(dut, char_bits_flip(chars, flips), each)
    assert [m for m, _ in marks] == ["r", "r", "r", "g", "g"], f"events {marks}"
    assert w.rise == 2, f"trigger_out_app rose {w.rise} times for 2 good triggers"
    lat = [r - starts[i] * 10 * OS_RATIO for i, r in enumerate(w.rise_at)]
    assert abs(lat[0] - lat[1]) <= 1, f"latencies {lat}: the voted Delay differs"


# -----------------------------------------------------------------------------
# TC 20 — Two Triggers Back To Back
# -----------------------------------------------------------------------------
@cxp_test()
async def test_20_back_to_back_triggers(dut):
    """Two adjacent trigger packets keep the command around them intact.

    §8.2.4: a trigger may start at any character boundary, so a second one
    may follow the first at once.  The rising packet with a neutral Delay
    changes the running disparity, the falling one does not; the change
    must still reach the character after both.

    Stimulus: 20 IDLE; for p = 0..3 a read of 0x2000 with a rising
              (Delay 51) and a falling (Delay 51) trigger inserted together
              p characters into its Addr word; 6 IDLE; 10 IDLE at the end.
    Checks:   8 `trig_pkt_rcvd`, 4 rises of `trigger_out_app` (polarity
              0); 4 responses 0x00; no code or disparity error.
    """
    dut.TESTCASE.value = 20
    await reset(dut)
    chars = gp.beats_to_chars([gp.IDLE] * 20)
    two = gp.trigger_ls_chars(True, 51) + gp.trigger_ls_chars(False, 51)
    for p in range(4):
        cmd = gp.beats_to_chars(gp.ctrl_cmd(gp.OP_READ, rm.DEVICE_VENDOR_NAME, 4, q=SPEC))
        cut = 3 * 4 + p
        chars += cmd[:cut] + two + cmd[cut:] + gp.beats_to_chars([gp.IDLE] * 6)
    chars += gp.beats_to_chars([gp.IDLE] * 10)
    slave = cocotb.start_soon(apb_answer(dut))
    w = RxWatch(dut)
    errs = {"n": 0}

    def each(cyc):
        w(cyc)
        errs["n"] += int(dut.rx_code_err_pulse.value) + int(dut.rx_disp_err_pulse.value)

    await drive_counted(dut, char_bits(chars), each)
    slave.cancel()
    assert w.codes == [0x00] * 4, f"responses {[hex(c) for c in w.codes]}"
    assert (w.rcvd, w.rise) == (8, 4), f"trig_pkt_rcvd {w.rcvd}, trigger_out_app {w.rise}"
    assert errs["n"] == 0, f"{errs['n']} decode errors"


# -----------------------------------------------------------------------------
# TC 21 — A Bit Error That Forges A Leader Character
# -----------------------------------------------------------------------------
def _forge_k28_2(chars, idx):
    """Bit index whose inversion turns character `idx` (a data character)
    into K28.2 at the running disparity it is sent with, or None."""
    _, rd = cxp.encode_chars(chars[:idx])
    d, _ = cxp.encode_byte(chars[idx][0], False, rd)
    k, _ = cxp.encode_byte(cxp.K28_2, True, rd)
    diff = d ^ k
    return diff.bit_length() - 1 if bin(diff).count("1") == 1 else None


@cxp_test()
async def test_21_forged_leader_character(dut):
    """One bit error just before a falling leader does not turn it into a
    rising one.

    §8.2.2: a single bit error must not cost a trigger.  D28.2 (0x5C) at
    RD- is one bit from K28.2, so with a falling leader K28.4 K28.2 K28.2
    right behind it the window [K28.2', K28.4, K28.2] holds two of three
    rising-leader characters one character early.  Taken as rising there,
    the trigger is dropped as a resend (level already 1) and the real
    rising trigger after it too.

    Stimulus: 20 IDLE; a rising trigger (Delay 51) in an IDLE word; 6
              IDLE; a write of one word at DeviceUserID whose four data
              characters are a byte one bit from K28.2 at its RD (0x5C at
              RD-, 0xA3 at RD+), a falling trigger (Delay 51) right after
              the fourth and that bit of the fourth inverted; 6 IDLE; a
              rising trigger; 10 IDLE.  Sent twice: `cfg_trig_polarity` 0
              (rising edges reach `trigger_out_app`), then 1 (falling).
    Checks:   3 `trig_pkt_rcvd` each time, no glitch; polarity 0: two
              pulses, for the two rising triggers, at the same latency
              from their first leader character; polarity 1: one pulse, for
              the falling trigger, at that latency too (within one rx_clk
              cycle: not one character early); the write answers 0x80
              both times (its data character is not data).
    """
    dut.TESTCASE.value = 21
    idle = gp.beats_to_chars([gp.IDLE])
    chars = gp.beats_to_chars([gp.IDLE] * 20)
    starts = [len(chars) + 1]
    chars += idle[:1] + gp.trigger_ls_chars(True, 51) + idle[1:] + gp.beats_to_chars([gp.IDLE] * 6)
    flips, at = {}, None
    for b in (0x5C, 0xA3, 0x4C, 0x54, 0xAB, 0xB3):
        cmd = gp.beats_to_chars(gp.ctrl_cmd(gp.OP_WRITE, rm.DEVICE_USER_ID, 4, [b * 0x0101_0101], q=SPEC))
        last = next(i for i in range(3, len(cmd)) if cmd[i - 3:i + 1] == [(b, False)] * 4)
        bit = _forge_k28_2(chars + cmd, len(chars) + last)
        if bit is not None:
            at = len(chars) + last
            flips[at] = bit
            starts.append(at + 1)
            chars += cmd[:last + 1] + gp.trigger_ls_chars(False, 51) + cmd[last + 1:]
            break
    assert at is not None, "no data byte one bit from K28.2 at this RD"
    chars += gp.beats_to_chars([gp.IDLE] * 6)
    starts.append(len(chars) + 1)
    chars += idle[:1] + gp.trigger_ls_chars(True, 51) + idle[1:] + gp.beats_to_chars([gp.IDLE] * 10)
    bits = char_bits_flip(chars, flips)
    lat = lambda cyc, k: cyc - starts[k] * 10 * OS_RATIO   # noqa: E731

    await reset(dut)
    w = RxWatch(dut)
    await drive_counted(dut, bits, w)
    assert (w.rcvd, w.rise, w.glitch) == (3, 2, 0), (
        f"polarity 0: trig_pkt_rcvd {w.rcvd}, pulses {w.rise}, glitch {w.glitch}")
    lat_r1, lat_r2 = lat(w.rise_at[0], 0), lat(w.rise_at[1], 2)
    assert abs(lat_r1 - lat_r2) <= 1, f"rising latencies {lat_r1} / {lat_r2}"
    assert w.codes == [0x80], f"responses {[hex(c) for c in w.codes]}"

    await reset(dut, clock=False)
    dut.cfg_trig_polarity.value = 1
    w = RxWatch(dut)
    await drive_counted(dut, bits, w)
    assert (w.rcvd, w.rise, w.glitch) == (3, 1, 0), (
        f"polarity 1: trig_pkt_rcvd {w.rcvd}, pulses {w.rise}, glitch {w.glitch}")
    lat_f = lat(w.rise_at[0], 1)
    assert abs(lat_f - lat_r1) <= 1, f"falling latency {lat_f} vs rising {lat_r1} (one character = {10 * OS_RATIO})"
    assert w.codes == [0x80], f"responses {[hex(c) for c in w.codes]}"
