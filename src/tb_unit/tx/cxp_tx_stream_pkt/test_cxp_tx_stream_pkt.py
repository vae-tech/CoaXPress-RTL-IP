"""Cocotb TB for `cxp_tx_stream_pkt`.

The DUT frames a flat 32-bit payload stream into CoaXPress type-0x01
stream data packets (CXP 1.1.1 §8.5.1 Table 19): SOP, replicated header,
N = `s_len` data words, CRC word, EOP; one PacketTag counter per
StreamID. The wrapper only renames ports (no `_i`/`_o`) and adds TESTCASE.

Single 8 ns `tx_clk`; reset held 8 cycles, then 4 idle cycles.
`PktTxDriver` presents queued packets back to back, holds each beat until
`s_valid & s_ready`, drives `s_len` with the head packet's length (as
`cxp_cdc_stream_fifo` does) and always drives `s_kmask` = 0. `run()` steps the
driver and captures every `m_valid & m_ready` beat (`m_ready` = 1 unless a
test passes a pattern). `split_packets` asserts SOP/EOP framing and
`check_packet` compares a packet bit-exactly against a reference header,
the data words, the golden §8.2.2.2 CRC word and the 4×K29.7 trailer. FSM
state/arc coverage of `state_q` is collected via `fsm_coverage`.

TC 1–6 come from the verification plan in `docs/design/cxp_camera_ip_modules.md`
§2.7; the rest are derived. Wire conventions (cited as v1.0 §6.2.1,
§6.2.2.2): `m_data[7:0]` = P0 (sent first) … `m_data[31:24]` = P3, bit 0
first within a character; 32-bit values such as the CRC are sent
big-endian, so P0 carries the CRC MSB byte. The CRC reference mirrors the
RTL convention (coverage and byte order), not a spec-derived model — see
`docs/design/modules/lib/cxp_lib_crc32.md`.

Not under test: a packet with `s_sop` and `s_eop` on the same beat with
`s_len` > 1 (covered only at DsizeP = 1, TC 9).

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Header words match Table 19 (SOP / type / SID / Tag / DsizeP).
  2  CRC word of 5 random packets matches the golden CRC.
  3  PacketTag wraps 0xFF → 0x00 over 257 packets on one StreamID.
  4  The 4×K29.7 trailer is the only `m_eop` beat of every packet.
  6  Random `m_ready` back-pressure corrupts neither data nor CRC.
  7  PacketTag counters are independent per StreamID.
  8  `m_valid` drops to 0 after the EOP when upstream is empty.
  9  Single-data-word packets (DsizeP = 1) framed correctly.
 10  `stream_ctrl_reset` restarts the PacketTag at 0 (§8.5.3).
 11  Early `s_eop` — closes a packet shorter than its `s_len`.
 12  `suppress_stream` (TestMode) mid-data: the packet completes, the next
     one waits for the release.
 13  `suppress_stream` mid-header: the packet completes, the next waits.
 14  `suppress_stream` while idle leaves the tag sequence intact.
 15  DsizeP = the packet's length (`s_len`); start on `s_pkt_avail`.
 16  `stream_en` low: the packet in flight completes, later ones wait and
     go out, in order with the next tags, once it is high again
     whole, and the first packet after it continues the tag sequence.
 17  `stream_ctrl_reset` mid-packet: the next packet still carries tag 0.
 18  `stream_ctrl_reset` at every cycle of a packet sequence, including
     the cycle a packet starts: the packet after it carries tag 0.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from cxp_protocol import crc as gcrc

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from cxp_testcase import cxp_test
from fsm_coverage import register_fsm


TX_PERIOD_NS = 8

# 8b/10b control character byte values (Kx.y = y*32 + x).
K27_7 = 0xFB
K29_7 = 0xFD


# -----------------------------------------------------------------------------
# FSM coverage
# -----------------------------------------------------------------------------
# The packet FSM lives in the shared cxp_tx_pkt_framer instance; this module
# builds the Table 19 header and owns the PacketTag counter around it.
register_fsm(
    name="stream_pkt_tx",
    states=["ST_IDLE", "ST_HDR", "ST_DATA", "ST_CRC", "ST_EOP"],
    state_path="cxp_tx_stream_pkt_i.cxp_tx_pkt_framer_i.state_q",
    clk_path="tx_clk",
    # Designed transition graph (next-state case statement).
    arcs=[
        ("ST_IDLE", "ST_HDR"), ("ST_HDR", "ST_DATA"),
        ("ST_DATA", "ST_CRC"), ("ST_CRC", "ST_EOP"),
        ("ST_EOP", "ST_IDLE"),
    ],
)


# -----------------------------------------------------------------------------
# Wire-beat datatype
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class WireBeat:
    """One accepted cycle of the `m_*` outputs the monitor cares about."""
    data:  int
    kmask: int
    sop:   int
    eop:   int


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
async def bringup(dut):
    """Start `tx_clk`, idle all inputs, hold reset 8 cycles, run 4 more."""
    cocotb.start_soon(Clock(dut.tx_clk, TX_PERIOD_NS, unit="ns").start(start_high=False))
    dut.tx_rst_n.value          = 0
    dut.s_data.value            = 0
    dut.s_kmask.value           = 0
    dut.s_valid.value           = 0
    dut.s_sop.value             = 0
    dut.s_eop.value             = 0
    dut.s_streamid.value        = 0
    dut.s_len.value             = 0
    dut.s_pkt_avail.value       = 1        # every queued packet is all in
    dut.stream_ctrl_reset.value = 0
    dut.stream_en.value         = 1
    dut.suppress_stream.value   = 0
    dut.m_ready.value           = 0

    for _ in range(8):
        await RisingEdge(dut.tx_clk)
    dut.tx_rst_n.value = 1
    for _ in range(4):
        await RisingEdge(dut.tx_clk)


# -----------------------------------------------------------------------------
# Upstream driver and wire monitor
# -----------------------------------------------------------------------------
class PktTxDriver:
    """Upstream packet driver.

    Holds a queue of (stream_id, [data_words], s_len) and presents the
    head-of-queue beat each cycle until `s_valid & s_ready` handshakes.
    `s_len` is the packet's length unless the test gives another.
    """

    def __init__(self, dut):
        self.dut = dut
        self.queue: list[tuple[int, list[int], int]] = []
        self.cur:   tuple[int, list[int], int] | None = None
        self.cur_pos: int = 0

    def push(self, stream_id: int, data_words: list[int], s_len: int | None = None):
        """Queue one packet for StreamID `stream_id`."""
        n = len(data_words) if s_len is None else s_len
        self.queue.append((stream_id, list(data_words), n))

    @property
    def has_data(self) -> bool:
        """True while a packet is in progress or queued."""
        return self.cur is not None or bool(self.queue)

    def _advance(self):
        """Pop the next queued packet (or go empty)."""
        if self.queue:
            self.cur = self.queue.pop(0)
            self.cur_pos = 0
        else:
            self.cur = None
            self.cur_pos = 0

    def drive(self):
        """Present the current beat (or idle the bus) before the next edge."""
        if self.cur is None:
            self._advance()
        if self.cur is None:
            self.dut.s_data.value     = 0
            self.dut.s_kmask.value    = 0
            self.dut.s_valid.value    = 0
            self.dut.s_sop.value      = 0
            self.dut.s_eop.value      = 0
            self.dut.s_streamid.value = 0
            self.dut.s_len.value      = 0
            return
        sid, data, n = self.cur
        i = self.cur_pos
        self.dut.s_len.value      = n
        self.dut.s_data.value     = data[i]
        self.dut.s_kmask.value    = 0
        self.dut.s_valid.value    = 1
        self.dut.s_sop.value      = 1 if i == 0 else 0
        self.dut.s_eop.value      = 1 if i == len(data) - 1 else 0
        self.dut.s_streamid.value = sid

    def observe(self):
        """After a tx_clk rising edge, advance if the beat handshook."""
        if self.cur is None:
            return
        if int(self.dut.s_valid.value) != 1 or int(self.dut.s_ready.value) != 1:
            return
        self.cur_pos += 1
        if self.cur_pos >= len(self.cur[1]):
            self._advance()


async def run(dut, drv: PktTxDriver, cycles: int,
              m_ready_pattern=None) -> list[WireBeat]:
    """Run driver + monitor for up to `cycles` clocks; return accepted beats.

    Stops early once the driver is empty and more than 4 idle cycles
    follow an EOP beat. `m_ready_pattern(cycle) -> 0|1` injects
    back-pressure; the default holds `m_ready` = 1.
    """
    captured: list[WireBeat] = []
    idle_count = 0

    for c in range(cycles):
        drv.drive()
        if m_ready_pattern is not None:
            dut.m_ready.value = m_ready_pattern(c)
        else:
            dut.m_ready.value = 1

        await RisingEdge(dut.tx_clk)

        drv.observe()

        if int(dut.m_valid.value) and int(dut.m_ready.value):
            captured.append(WireBeat(
                data  = int(dut.m_data.value),
                kmask = int(dut.m_kmask.value),
                sop   = int(dut.m_sop.value),
                eop   = int(dut.m_eop.value),
            ))
            idle_count = 0
        else:
            idle_count += 1

        # Once the driver is empty AND we've seen a few idle bubbles
        # after the last beat, we're done.
        if not drv.has_data and idle_count > 4 and captured and captured[-1].eop:
            break

    return captured


async def _capture_step(dut, drv: PktTxDriver, captured: list[WireBeat],
                        m_ready: int = 1):
    """One cycle of cocotb-driven stim/observe (does what `run()` does
    inside, but lets the test body control suppress_stream and stopping
    conditions cycle-by-cycle)."""
    drv.drive()
    dut.m_ready.value = m_ready
    await RisingEdge(dut.tx_clk)
    drv.observe()
    if int(dut.m_valid.value) and int(dut.m_ready.value):
        captured.append(WireBeat(
            data  = int(dut.m_data.value),
            kmask = int(dut.m_kmask.value),
            sop   = int(dut.m_sop.value),
            eop   = int(dut.m_eop.value),
        ))


# -----------------------------------------------------------------------------
# Reference model and checkers
# -----------------------------------------------------------------------------
def split_packets(beats: list[WireBeat]) -> list[list[WireBeat]]:
    """Cut a captured beat stream into packets at SOP/EOP boundaries.

    Asserts no SOP inside an open packet, no beat outside a packet, and
    no packet left open at the end.
    """
    pkts: list[list[WireBeat]] = []
    cur:  list[WireBeat] = []
    in_pkt = False
    for b in beats:
        if b.sop:
            assert not in_pkt, f"SOP inside an open packet: {b}"
            cur = [b]
            in_pkt = True
        else:
            assert in_pkt, f"non-SOP beat outside a packet: {b}"
            cur.append(b)
        if b.eop:
            assert in_pkt, f"EOP outside a packet: {b}"
            pkts.append(cur)
            cur = []
            in_pkt = False
    assert not in_pkt, "captured stream ended with an open packet"
    return pkts


def crc32_bytes(stream_id: int, tag: int, dsizeP: int,
                data_words: list[int]) -> bytes:
    """The bytes the DUT folds into its CRC: the data words only (Table 19,
    "stream data 4 to (N+3)"), P0 first."""
    raw = bytearray()
    for w in data_words:
        raw += w.to_bytes(4, "little")
    return bytes(raw)


def expected_crc_word(stream_id: int, tag: int, dsizeP: int,
                      data_words: list[int]) -> int:
    """The CRC word on m_data: the §8.2.2.2 register over the covered words,
    LSByte in P0 (golden `cxp_protocol.crc`)."""
    raw = crc32_bytes(stream_id, tag, dsizeP, data_words)
    words = [int.from_bytes(raw[i:i + 4], "little") for i in range(0, len(raw), 4)]
    return gcrc.crc_word(words)


def expected_header(stream_id: int, tag: int, dsizeP: int) -> list[WireBeat]:
    """The six leading header beats for a packet, in transmission order."""
    def rep4(b: int) -> int:
        return (b << 24) | (b << 16) | (b << 8) | b
    return [
        WireBeat(rep4(K27_7),               0xF, 1, 0),
        WireBeat(rep4(0x01),                0x0, 0, 0),
        WireBeat(rep4(stream_id & 0xFF),    0x0, 0, 0),
        WireBeat(rep4(tag & 0xFF),          0x0, 0, 0),
        WireBeat(rep4((dsizeP >> 8) & 0xFF),0x0, 0, 0),
        WireBeat(rep4(dsizeP & 0xFF),       0x0, 0, 0),
    ]


def check_packet(pkt: list[WireBeat], stream_id: int, tag: int,
                 dsizeP: int, data_words: list[int]):
    """Bit-exact verification of a captured packet against the spec."""
    expected_len = 6 + len(data_words) + 1 + 1   # hdr + data + crc + eop
    assert len(pkt) == expected_len, (
        f"packet length {len(pkt)} != expected {expected_len}"
    )

    # Header
    exp_hdr = expected_header(stream_id, tag, dsizeP)
    for i, (got, exp) in enumerate(zip(pkt[:6], exp_hdr)):
        assert got == exp, f"header word {i}: got {got}, expected {exp}"

    # Data words (kmask must be 0, no sop/eop except trailing).
    for i, w in enumerate(data_words):
        beat = pkt[6 + i]
        assert beat.data  == w,    f"data word {i}: got {beat.data:#010x}, expected {w:#010x}"
        assert beat.kmask == 0,    f"data word {i}: kmask={beat.kmask:#x}, expected 0"
        assert beat.sop   == 0,    f"data word {i}: SOP set"
        assert beat.eop   == 0,    f"data word {i}: EOP set"

    # CRC word
    crc_beat = pkt[6 + len(data_words)]
    exp_crc  = expected_crc_word(stream_id, tag, dsizeP, data_words)
    assert crc_beat.data == exp_crc, (
        f"CRC word: got {crc_beat.data:#010x}, expected {exp_crc:#010x}"
    )
    assert crc_beat.kmask == 0,  f"CRC word: kmask={crc_beat.kmask:#x}"
    assert crc_beat.sop   == 0,  "CRC word: SOP set"
    assert crc_beat.eop   == 0,  "CRC word: EOP set"

    # Trailer K29.7
    eop_beat = pkt[-1]
    assert eop_beat.data  == ((K29_7 << 24) | (K29_7 << 16) | (K29_7 << 8) | K29_7), (
        f"trailer word: got {eop_beat.data:#010x}, expected 4×K29.7"
    )
    assert eop_beat.kmask == 0xF, f"trailer kmask={eop_beat.kmask:#x}, expected 0xF"
    assert eop_beat.sop   == 0,   "trailer: SOP set"
    assert eop_beat.eop   == 1,   "trailer: EOP not set"


# -----------------------------------------------------------------------------
# TC 1 — Header Field Layout
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_header_field_layout(dut):
    """The six header words of a packet match Table 19 bit-exactly.

    Wire words 0–5 must be 4×K27.7 (kmask 0xF, `m_sop`), 4×0x01, 4×SID,
    4×PacketTag, 4×DsizeP[15:8], 4×DsizeP[7:0] (§8.5.1 Table 19; the
    plan cites v1.0 Table 18). Single point of authority for the header.

    Stimulus: DsizeP = 4; one packet on StreamID 0x55 with data
              0xDEADBEEF, 0xCAFEF00D, 0x0BADC0DE, 0x12345678; `m_ready`
              held 1; up to 80 cycles.
    Checks:   exactly one packet; `check_packet` with tag 0, DsizeP 4 —
              length N + 8, header beats exact (data, kmask, sop, eop),
              data words with kmask 0, CRC word against the golden
              CRC, 4×K29.7 trailer with kmask 0xF and `m_eop`.
    """
    dut.TESTCASE.value = 1
    DSIZE = 4
    await bringup(dut)
    drv = PktTxDriver(dut)
    drv.push(stream_id=0x55,
             data_words=[0xDEAD_BEEF, 0xCAFE_F00D, 0x0BAD_C0DE, 0x1234_5678])

    beats = await run(dut, drv, cycles=80)
    pkts  = split_packets(beats)
    assert len(pkts) == 1, f"expected 1 packet, got {len(pkts)}"

    # The full check is part of every other test too, but do it
    # explicitly here so this test is the single point of authority for
    # the header layout.
    check_packet(pkts[0],
                 stream_id=0x55,
                 tag=0,                # first packet on this stream
                 dsizeP=DSIZE,
                 data_words=[0xDEAD_BEEF, 0xCAFE_F00D, 0x0BAD_C0DE, 0x1234_5678])


# -----------------------------------------------------------------------------
# TC 2 — CRC32 Against the Golden Reference
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_crc32_random(dut):
    """The CRC word of every packet matches the golden §8.2.2.2 CRC.

    Proves the CRC is reseeded per packet and folds the header and data
    words in a stable order across back-to-back packets.

    Stimulus: DsizeP = 8; 5 packets of 8 random 32-bit words
              (`random.Random(0xC0FFEE)`) on StreamID 0x10, back to back;
              `m_ready` held 1; up to 600 cycles.
    Checks:   5 packets; `check_packet` on each with tags 0..4 — the CRC
              word equals the golden CRC over the data words only
              (Table 19), register LSByte in P0.
    """
    dut.TESTCASE.value = 2
    DSIZE = 8
    await bringup(dut)
    drv = PktTxDriver(dut)

    rng = random.Random(0xC0FFEE)
    payloads: list[list[int]] = []
    for _ in range(5):
        words = [rng.randrange(0, 1 << 32) for _ in range(DSIZE)]
        payloads.append(words)
        drv.push(stream_id=0x10, data_words=words)

    beats = await run(dut, drv, cycles=600)
    pkts  = split_packets(beats)
    assert len(pkts) == len(payloads)
    for tag, (pkt, words) in enumerate(zip(pkts, payloads)):
        check_packet(pkt, stream_id=0x10, tag=tag, dsizeP=DSIZE, data_words=words)


# -----------------------------------------------------------------------------
# TC 3 — PacketTag Wrap
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_packet_tag_wrap(dut):
    """The per-StreamID PacketTag walks 0, 1, …, 0xFF and wraps to 0x00.

    The 8-bit `tag_table_q` entry is bumped only when the trailer is
    accepted; the 257th packet must carry tag 0 again.

    Stimulus: DsizeP = 1; 257 one-word packets with random data
              (`random.Random(0xBEEF)`) on StreamID 0xA5; up to
              257 × 16 + 200 cycles.
    Checks:   257 packets; for packet i the tag word (wire word 3) has
              byte 0 == i & 0xFF and is 4× replicated; `check_packet` on
              every packet, so the CRC is also checked across the wrap.
    """
    dut.TESTCASE.value = 3
    DSIZE = 1
    await bringup(dut)
    drv = PktTxDriver(dut)

    N = 257
    rng = random.Random(0xBEEF)
    payloads = [[rng.randrange(0, 1 << 32)] for _ in range(N)]
    for words in payloads:
        drv.push(stream_id=0xA5, data_words=words)

    # Long simulation — wrap takes 257 packets × ~9 wire-clocks each.
    beats = await run(dut, drv, cycles=N * 16 + 200)
    pkts  = split_packets(beats)
    assert len(pkts) == N, f"got {len(pkts)} packets, expected {N}"

    for i, (pkt, words) in enumerate(zip(pkts, payloads)):
        tag = i & 0xFF
        # Tag is in word index 3 of the packet (4×PacketTag).
        tag_byte = pkt[3].data & 0xFF
        assert tag_byte == tag, (
            f"packet {i}: tag byte {tag_byte:#x}, expected {tag:#x}"
        )
        # 4× replication intact.
        assert pkt[3].data == ((tag << 24) | (tag << 16) | (tag << 8) | tag), (
            f"packet {i}: tag word not 4× replicated: {pkt[3].data:#010x}"
        )
        # And while we're at it, the CRC still matches at every step.
        check_packet(pkt, stream_id=0xA5, tag=tag, dsizeP=DSIZE, data_words=words)


# -----------------------------------------------------------------------------
# TC 4 — Trailer K29.7 Aligned With m_eop
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_trailer_k29_7(dut):
    """`m_eop` fires exactly once per packet, on the 4×K29.7 trailer beat.

    Guards the ST_EOP outputs and the EOP alignment the arbiter relies on
    to release the stream slot.

    Stimulus: DsizeP = 3; one packet of 3 random words
              (`random.Random(0xD00D)`) on each StreamID 1, 2, 3, 0x80,
              0xFF; up to 400 cycles.
    Checks:   5 packets (framing via `split_packets`); per packet exactly
              one beat with `m_eop`, it is the last beat, its data is
              0xFDFDFDFD (4×K29.7) and its kmask is 0xF.
    Note:     `check_packet` is not called — header, data, CRC and tags
              are not checked here.
    """
    dut.TESTCASE.value = 4
    DSIZE = 3
    await bringup(dut)
    drv = PktTxDriver(dut)

    rng = random.Random(0xD00D)
    for sid in (1, 2, 3, 0x80, 0xFF):
        drv.push(stream_id=sid,
                 data_words=[rng.randrange(0, 1 << 32) for _ in range(DSIZE)])

    beats = await run(dut, drv, cycles=400)
    pkts  = split_packets(beats)
    assert len(pkts) == 5

    expected_trailer = (K29_7 << 24) | (K29_7 << 16) | (K29_7 << 8) | K29_7
    for i, pkt in enumerate(pkts):
        # Exactly one EOP per packet, on the trailer beat.
        eop_count = sum(1 for b in pkt if b.eop)
        assert eop_count == 1, f"packet {i}: {eop_count} EOP beats, expected 1"
        trailer = pkt[-1]
        assert trailer.eop == 1, f"packet {i}: trailer beat has eop=0"
        assert trailer.data == expected_trailer, (
            f"packet {i}: trailer word {trailer.data:#010x} != 4×K29.7"
        )
        assert trailer.kmask == 0xF, (
            f"packet {i}: trailer kmask={trailer.kmask:#x}, expected 0xF"
        )


# -----------------------------------------------------------------------------
# TC 6 — Backpressure
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_backpressure(dut):
    """Random `m_ready` stalls corrupt neither data nor CRC.

    Header index, data counter, CRC and tag must advance only on accepted
    beats (`out_fire` / `data_in_fire`).

    Stimulus: DsizeP = 6; 3 packets of random words
              (`random.Random(0x5A5A)`) on StreamID 0x77; `m_ready` = 0
              on ~30 % of cycles (`random.Random(0xDEADBEEF)`); up to
              1500 cycles.
    Checks:   3 packets; `check_packet` on each with tags 0..2.
    Note:     only accepted beats are captured, so output stability
              during a stall is not checked directly, and which FSM
              states were stalled is not recorded.
    """
    dut.TESTCASE.value = 6
    DSIZE = 6
    await bringup(dut)
    drv = PktTxDriver(dut)

    rng = random.Random(0x5A5A)
    payloads = [[rng.randrange(0, 1 << 32) for _ in range(DSIZE)]
                for _ in range(3)]
    for words in payloads:
        drv.push(stream_id=0x77, data_words=words)

    # m_ready is 0 ~30% of cycles — exercises stalls in HDR, DATA, CRC,
    # and EOP states.
    stall_rng = random.Random(0xDEAD_BEEF)
    def ready_pat(_c: int) -> int:
        return 0 if stall_rng.random() < 0.30 else 1

    beats = await run(dut, drv, cycles=1500, m_ready_pattern=ready_pat)
    pkts  = split_packets(beats)
    assert len(pkts) == len(payloads), (
        f"got {len(pkts)} packets, expected {len(payloads)}"
    )
    for tag, (pkt, words) in enumerate(zip(pkts, payloads)):
        check_packet(pkt, stream_id=0x77, tag=tag, dsizeP=DSIZE, data_words=words)


# -----------------------------------------------------------------------------
# TC 7 — Per-StreamID Tag Independence
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_per_stream_tag_independent(dut):
    """Each StreamID keeps its own PacketTag counter.

    `tag_table_q` must be indexed by the packet's StreamID both for the
    read at SOP and for the bump at EOP.

    Stimulus: DsizeP = 2; 6 packets alternating StreamID 0x21 and
              0x42 (A B A B A B), random words (`random.Random(0x123)`);
              up to 400 cycles.
    Checks:   6 packets in order; `check_packet` on each with a separate
              expected tag per StreamID, each walking 0, 1, 2.
    """
    dut.TESTCASE.value = 7
    DSIZE = 2
    await bringup(dut)
    drv = PktTxDriver(dut)

    # Sequence: A, B, A, B, A, B  (3 packets each)
    rng = random.Random(0x123)
    plan: list[tuple[int, list[int]]] = []
    for _ in range(3):
        plan.append((0x21, [rng.randrange(0, 1 << 32) for _ in range(DSIZE)]))
        plan.append((0x42, [rng.randrange(0, 1 << 32) for _ in range(DSIZE)]))
    for sid, words in plan:
        drv.push(stream_id=sid, data_words=words)

    beats = await run(dut, drv, cycles=400)
    pkts  = split_packets(beats)
    assert len(pkts) == len(plan)

    tag_a = 0
    tag_b = 0
    for pkt, (sid, words) in zip(pkts, plan):
        if sid == 0x21:
            check_packet(pkt, stream_id=sid, tag=tag_a, dsizeP=DSIZE, data_words=words)
            tag_a += 1
        else:
            check_packet(pkt, stream_id=sid, tag=tag_b, dsizeP=DSIZE, data_words=words)
            tag_b += 1


# -----------------------------------------------------------------------------
# TC 8 — Idle Between Packets
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_idle_between_packets(dut):
    """`m_valid` drops to 0 after the trailer when upstream has no data.

    Guards ST_EOP → ST_IDLE and a muted output in ST_IDLE, so the arbiter
    can hand the wire to IDLE or another source.

    Stimulus: DsizeP = 2; one packet (0xA1A1A1A1, 0xB2B2B2B2) on
              StreamID 0x09; inline drive/capture loop with `m_ready` = 1,
              up to 120 cycles, stopping 11 cycles after the EOP beat.
    Checks:   one packet, `check_packet` with tag 0; at least one cycle
              with `m_valid` = 0 after the EOP (any further beat in the
              window would also fail `split_packets`).
    Note:     upstream is empty after the packet, so the 1-cycle ST_IDLE
              gap between back-to-back packets is not shown here.
    """
    dut.TESTCASE.value = 8
    DSIZE = 2
    await bringup(dut)
    drv = PktTxDriver(dut)
    drv.push(stream_id=0x09, data_words=[0xA1A1_A1A1, 0xB2B2_B2B2])

    # Run once with the upstream offering one packet, then stall: drv
    # has no data, so the DUT should idle.
    beats: list[WireBeat] = []
    valid_after_eop_seen = False
    eop_seen_at: int | None = None

    for c in range(120):
        drv.drive()
        dut.m_ready.value = 1
        await RisingEdge(dut.tx_clk)
        drv.observe()
        if int(dut.m_valid.value) and int(dut.m_ready.value):
            beats.append(WireBeat(
                data  = int(dut.m_data.value),
                kmask = int(dut.m_kmask.value),
                sop   = int(dut.m_sop.value),
                eop   = int(dut.m_eop.value),
            ))
            if beats[-1].eop:
                eop_seen_at = c
        elif eop_seen_at is not None and int(dut.m_valid.value) == 0:
            # Confirmed at least one idle cycle after EOP — that's the
            # property we wanted to assert.
            valid_after_eop_seen = True
        if eop_seen_at is not None and c - eop_seen_at > 10:
            break

    pkts = split_packets(beats)
    assert len(pkts) == 1
    check_packet(pkts[0], stream_id=0x09, tag=0, dsizeP=DSIZE,
                 data_words=[0xA1A1_A1A1, 0xB2B2_B2B2])
    assert valid_after_eop_seen, "m_valid stayed high after EOP with no upstream data"


# -----------------------------------------------------------------------------
# TC 9 — Single Data Word Packet
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_single_data_word(dut):
    """The smallest packet (DsizeP = 1) is framed and CRC'd correctly.

    `last_data_word` (`data_left_q == 1`) is true on the first data beat,
    so ST_DATA → ST_CRC is taken immediately; the SOP beat is also the
    EOP beat.

    Stimulus: DsizeP = 1; two one-word packets (0xF00DBABE,
              0x01234567) on StreamID 0x33, each with `s_sop` and `s_eop`
              on the same beat; up to 120 cycles.
    Checks:   2 packets; `check_packet` with tags 0 and 1.
    """
    dut.TESTCASE.value = 9
    DSIZE = 1
    await bringup(dut)
    drv = PktTxDriver(dut)
    drv.push(stream_id=0x33, data_words=[0xF00D_BABE])
    drv.push(stream_id=0x33, data_words=[0x0123_4567])

    beats = await run(dut, drv, cycles=120)
    pkts  = split_packets(beats)
    assert len(pkts) == 2
    check_packet(pkts[0], stream_id=0x33, tag=0, dsizeP=DSIZE,
                 data_words=[0xF00D_BABE])
    check_packet(pkts[1], stream_id=0x33, tag=1, dsizeP=DSIZE,
                 data_words=[0x0123_4567])


# -----------------------------------------------------------------------------
# TC 10 — Stream-Control Reset Restarts PacketTag
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_stream_ctrl_reset_restarts_tag(dut):
    """A `stream_ctrl_reset` pulse restarts the PacketTag at 0.

    CXP 1.1.1 (CXP-001-2015 §8.5.3, §10.3.33): a ConnectionReset or a
    ConnectionConfig write resets stream control, so PacketTag restarts
    from 0. The v1.0 RTL cleared the tag table only on hard reset.

    Stimulus: DsizeP = 2; 3 packets (0xAA, 0xBB) on StreamID 0x10;
              then one idle edge, `stream_ctrl_reset` = 1 for one cycle,
              one more edge; then 2 packets (0xCC, 0xDD) on the same
              StreamID with a fresh driver; up to 400 cycles per phase.
    Checks:   tag bytes (wire word 3, lane 0) are [0, 1, 2] before the
              pulse and [0, 1] after.
    Note:     `check_packet` is not called (no header/CRC check); the
              pulse is applied only while idle — a mid-packet clear is
              not covered.
    """
    dut.TESTCASE.value = 10
    DSIZE = 2
    await bringup(dut)

    # Phase 1: three packets on stream 0x10 -> tags 0,1,2.
    drv = PktTxDriver(dut)
    for _ in range(3):
        drv.push(stream_id=0x10, data_words=[0xAA, 0xBB])
    pkts = split_packets(await run(dut, drv, cycles=400))
    assert [p[3].data & 0xFF for p in pkts] == [0, 1, 2], \
        f"pre-reset tags {[p[3].data & 0xFF for p in pkts]} != [0,1,2]"

    # Pulse the §8.5.3 stream-control reset for one cycle while idle.
    await RisingEdge(dut.tx_clk)
    dut.stream_ctrl_reset.value = 1
    await RisingEdge(dut.tx_clk)
    dut.stream_ctrl_reset.value = 0
    await RisingEdge(dut.tx_clk)

    # Phase 2: two more packets on the SAME stream -> tags restart 0,1.
    drv2 = PktTxDriver(dut)
    for _ in range(2):
        drv2.push(stream_id=0x10, data_words=[0xCC, 0xDD])
    pkts2 = split_packets(await run(dut, drv2, cycles=400))
    assert [p[3].data & 0xFF for p in pkts2] == [0, 1], (
        f"post-reset tags {[p[3].data & 0xFF for p in pkts2]} != [0,1] "
        f"- stream_ctrl_reset did not restart the PacketTag"
    )


# -----------------------------------------------------------------------------
# TC 11 — DsizeP Under-Supply
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_dsizeP_under_supply(dut):
    """An early `s_eop` closes a well-framed short packet; framer recovers.

    Models an upstream that sends fewer words than the `s_len` it
    announced — `cxp_cdc_stream_fifo` never does — the framer must still
    produce a well-framed packet and not read into the next one.

    Stimulus: a 3-word packet (0x11111111, 0x22222222, 0x33333333,
              `s_eop` on the third) with `s_len` = 8, then an 8-word
              packet (0xA0 … 0xA7), both on StreamID 0x55; up to 300
              cycles.
    Checks:   2 packets; packet 0 via `check_packet` with DsizeP 8, the 3
              words actually sent, a CRC over those 3 words (not padded)
              and tag 0; packet 1 with the 8 follow-up words and tag 1.
    Note:     the header's DsizeP (8) over-states the payload (3): the
              header has left before the short end is seen.
    """
    dut.TESTCASE.value = 11
    DSIZE = 8                                # announced s_len
    SHORT_N = 3                              # actual upstream words sent
    await bringup(dut)
    drv = PktTxDriver(dut)

    short_words = [0x1111_1111, 0x2222_2222, 0x3333_3333]
    assert len(short_words) == SHORT_N < DSIZE
    drv.push(stream_id=0x55, data_words=short_words, s_len=DSIZE)

    follow = [0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7]  # full DSIZE
    drv.push(stream_id=0x55, data_words=follow)

    beats = await run(dut, drv, cycles=300)
    pkts  = split_packets(beats)
    assert len(pkts) == 2, (
        f"expected 2 packets (short + recovery), got {len(pkts)} "
        f"-- framer did not close the under-supplied packet"
    )

    # Short packet: framing is the documented graceful-close shape —
    # header carries the announced s_len, data section is SHORT_N words, CRC32
    # was computed over those SHORT_N words (matching check_packet's
    # `data_words=short_words` reference).
    check_packet(pkts[0],
                 stream_id=0x55, tag=0, dsizeP=DSIZE,
                 data_words=short_words)

    # Follow-up packet: tag walked to 1 (the under-supplied packet was
    # accepted as one packet by the per-StreamID tag counter), framing
    # is back to a normal DSIZE-word packet.
    check_packet(pkts[1],
                 stream_id=0x55, tag=1, dsizeP=DSIZE,
                 data_words=follow)


# -----------------------------------------------------------------------------
# TC 12 — TestMode Mid-Data: The Packet Completes, The Next Waits
# -----------------------------------------------------------------------------
async def _suppress_after(dut, n_beats: int, dsize: int, hold: int, sid: int,
                          pkt_a: list[int], pkt_b: list[int]) -> list[WireBeat]:
    """Send A and B; raise `suppress_stream` once `n_beats` of A are out,
    hold it `hold` cycles, release; return every beat captured."""
    drv = PktTxDriver(dut)
    drv.push(stream_id=sid, data_words=pkt_a)
    drv.push(stream_id=sid, data_words=pkt_b)
    beats: list[WireBeat] = []
    for _ in range(40):
        await _capture_step(dut, drv, beats)
        if len(beats) >= n_beats:
            break
    assert beats and beats[0].sop and not any(b.eop for b in beats), \
        f"setup: {len(beats)} beats before TestMode"
    dut.suppress_stream.value = 1
    held: list[WireBeat] = []
    for _ in range(hold):
        await _capture_step(dut, drv, held)
    a_len = 6 + dsize + 2
    assert len(beats) + len(held) == a_len and held[-1].eop, (
        f"packet A not completed under TestMode: {len(beats)} + {len(held)} "
        f"beats, expected {a_len}")
    dut.suppress_stream.value = 0
    rest: list[WireBeat] = []
    for _ in range(120):
        await _capture_step(dut, drv, rest)
        if rest and rest[-1].eop:
            break
    return beats + held + rest


@cxp_test()
async def test_12_suppress_mid_data_completes_packet(dut):
    """TestMode rising mid-data: the packet completes, the next one waits.

    §8.7.4: in TestMode no stream data goes out; a packet already on the
    wire is not cut (a cut packet is a framing error at the host).

    Stimulus: DsizeP = 8; packets A (0xA0000000 | i) and B (0xB0000000 |
              i) on StreamID 0x55; `suppress_stream` = 1 once 9 beats of
              A are out (6 header + 3 data), held 30 cycles, then 0.
    Checks:   A completes while `suppress_stream` is high (16 beats in
              all) and nothing of B goes out then; after the release B
              is sent whole; A tag 0, B tag 1, both bit-exact.
    """
    dut.TESTCASE.value = 12
    DSIZE = 8
    await bringup(dut)
    pkt_a = [0xA000_0000 | i for i in range(DSIZE)]
    pkt_b = [0xB000_0000 | i for i in range(DSIZE)]
    pkts = split_packets(await _suppress_after(dut, 9, DSIZE, 30, 0x55, pkt_a, pkt_b))
    assert len(pkts) == 2, f"expected 2 packets, got {len(pkts)}"
    check_packet(pkts[0], stream_id=0x55, tag=0, dsizeP=DSIZE, data_words=pkt_a)
    check_packet(pkts[1], stream_id=0x55, tag=1, dsizeP=DSIZE, data_words=pkt_b)


# -----------------------------------------------------------------------------
# TC 13 — TestMode Mid-Header: The Packet Completes, The Next Waits
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_suppress_mid_header_completes_packet(dut):
    """TestMode rising mid-header: the packet completes, the next one waits.

    Stimulus: DsizeP = 4; packets A (0xC0 … 0xC3) and B (0xD0 … 0xD3) on
              StreamID 0x77; `suppress_stream` = 1 once 3 header beats
              are out, held 20 cycles, then 0.
    Checks:   A completes under TestMode (12 beats) and B waits; then B;
              A tag 0, B tag 1, both bit-exact.
    """
    dut.TESTCASE.value = 13
    DSIZE = 4
    await bringup(dut)
    pkt_a = [0xC0, 0xC1, 0xC2, 0xC3]
    pkt_b = [0xD0, 0xD1, 0xD2, 0xD3]
    pkts = split_packets(await _suppress_after(dut, 3, DSIZE, 20, 0x77, pkt_a, pkt_b))
    assert len(pkts) == 2, f"expected 2 packets, got {len(pkts)}"
    check_packet(pkts[0], stream_id=0x77, tag=0, dsizeP=DSIZE, data_words=pkt_a)
    check_packet(pkts[1], stream_id=0x77, tag=1, dsizeP=DSIZE, data_words=pkt_b)


# -----------------------------------------------------------------------------
# TC 14 — Suppress While Idle Preserves Tag Sequence
# -----------------------------------------------------------------------------
@cxp_test()
async def test_14_suppress_in_idle_preserves_tag_sequence(dut):
    """`suppress_stream` while idle leaves the PacketTag sequence intact.

    Guards against an over-eager abandon that would clear `tag_table_q`
    or `curr_tag_q` on every suppress-raise.

    Stimulus: DsizeP = 2; one packet (0xE0, 0xE1) on StreamID 0x33
              (≤ 80 cycles); `suppress_stream` = 1 for 15 cycles while
              idle, then 0 and 4 idle cycles; one more packet (0xF0,
              0xF1) on 0x33 with a fresh driver (≤ 80 cycles).
    Checks:   each phase yields exactly one packet; `check_packet` with
              tag 0 and then tag 1 (normal increment, not 0).
    Note:     suppress together with `stream_ctrl_reset` is not covered.
    """
    dut.TESTCASE.value = 14
    DSIZE = 2
    await bringup(dut)

    drv = PktTxDriver(dut)
    drv.push(stream_id=0x33, data_words=[0xE0, 0xE1])
    beats = await run(dut, drv, cycles=80)
    pkts = split_packets(beats)
    assert len(pkts) == 1
    check_packet(pkts[0], stream_id=0x33, tag=0, dsizeP=DSIZE,
                 data_words=[0xE0, 0xE1])

    # Framer is idle now.  Pulse suppress for a stretch.
    dut.suppress_stream.value = 1
    for _ in range(15):
        await RisingEdge(dut.tx_clk)
    dut.suppress_stream.value = 0
    for _ in range(4):
        await RisingEdge(dut.tx_clk)

    # Next packet on the same stream — tag must be 1 (normal increment).
    drv2 = PktTxDriver(dut)
    drv2.push(stream_id=0x33, data_words=[0xF0, 0xF1])
    beats2 = await run(dut, drv2, cycles=80)
    pkts2 = split_packets(beats2)
    assert len(pkts2) == 1
    check_packet(pkts2[0], stream_id=0x33, tag=1, dsizeP=DSIZE,
                 data_words=[0xF0, 0xF1])


# -----------------------------------------------------------------------------
# TC 15 — DsizeP from the packet length, whole packet before start
# -----------------------------------------------------------------------------
@cxp_test()
async def test_15_len_and_pkt_avail(dut):
    """DsizeP is the packet's own length and a packet waits for all of it.

    cxp_cdc_stream_fifo gives the head packet's length (`s_len`) and says when
    all of it is in (`s_pkt_avail`); the header's DsizeP (§8.5.2, Table 19)
    is that length (the chopper sized the packet), so an image's short
    last packet announces what it carries.

    Stimulus: `s_pkt_avail` = 0 for 40 cycles with a 3-word packet
              presented, then 1; then a 12-word packet.
    Checks:   nothing on the wire while `s_pkt_avail` = 0; then packets
              with DsizeP 3 and 12.
    """
    dut.TESTCASE.value = 15
    await bringup(dut)
    drv = PktTxDriver(dut)
    drv.push(stream_id=0x01, data_words=[0x11, 0x22, 0x33])
    dut.s_pkt_avail.value = 0
    held: list[WireBeat] = []
    for _ in range(40):
        await _capture_step(dut, drv, held)
    assert not held, f"{len(held)} beats sent before the packet was all in"

    dut.s_pkt_avail.value = 1
    beats = await run(dut, drv, cycles=100)
    drv.push(stream_id=0x01, data_words=list(range(0x40, 0x4C)))
    beats += await run(dut, drv, cycles=100)
    pkts = split_packets(beats)
    assert len(pkts) == 2, f"expected 2 packets, got {len(pkts)}"
    check_packet(pkts[0], stream_id=0x01, tag=0, dsizeP=3, data_words=[0x11, 0x22, 0x33])
    check_packet(pkts[1], stream_id=0x01, tag=1, dsizeP=12, data_words=list(range(0x40, 0x4C)))


async def _until_sop(dut, drv: PktTxDriver, captured: list[WireBeat], limit: int = 100):
    """Step until the next packet's SOP beat is on the wire."""
    n = sum(b.sop for b in captured)
    for _ in range(limit):
        await _capture_step(dut, drv, captured)
        if sum(b.sop for b in captured) > n:
            return
    raise AssertionError("no SOP on the wire")


# -----------------------------------------------------------------------------
# TC 16 — Stream Disabled (StreamPacketSizeMax = 0)
# -----------------------------------------------------------------------------
@cxp_test()
async def test_16_stream_disabled_holds_packets(dut):
    """While `stream_en` is low no packet starts; queued ones wait.

    CXP 1.1.1 Table 44: while StreamPacketSizeMax is 0 the device shall not
    transmit stream packets.  An image already begun on the wire must not
    be torn, so the packets behind the one in flight wait in the FIFO and
    go out once the size allows a packet again; without a ConnectionReset
    the tags go on (§8.5.3).

    Stimulus: DsizeP = 4; packet A; packet B with `stream_en` = 0
              from the cycle its SOP is out; C, D (a 1-word SOP+EOP beat)
              and E queued while low; 150 cycles; `stream_en` = 1.
    Checks:   while low only A (tag 0) and B (tag 1) on the wire, bit-exact,
              and C is not read; after the rise C, D, E with tags 2, 3, 4.
    """
    dut.TESTCASE.value = 16
    await bringup(dut)
    drv = PktTxDriver(dut)
    a, b = [0xA0, 0xA1, 0xA2, 0xA3], [0xB0, 0xB1, 0xB2, 0xB3]
    c, d, e = [0xC0, 0xC1, 0xC2, 0xC3], [0xD0], [0xE0, 0xE1, 0xE2]
    drv.push(stream_id=0x01, data_words=a)
    drv.push(stream_id=0x01, data_words=b)
    beats: list[WireBeat] = []
    await _until_sop(dut, drv, beats)
    await _until_sop(dut, drv, beats)
    dut.stream_en.value = 0
    for w in (c, d, e):
        drv.push(stream_id=0x01, data_words=w)
    for _ in range(150):
        await _capture_step(dut, drv, beats)
    assert drv.has_data, "packets queued while the stream is disabled were read (dropped)"
    pkts = split_packets(beats)
    assert len(pkts) == 2, f"{len(pkts)} packets on the wire, expected A and B only"
    check_packet(pkts[0], stream_id=0x01, tag=0, dsizeP=4, data_words=a)
    check_packet(pkts[1], stream_id=0x01, tag=1, dsizeP=4, data_words=b)

    dut.stream_en.value = 1
    pkts = split_packets(await run(dut, drv, cycles=200))
    assert len(pkts) == 3, f"expected C, D and E after re-enable, got {len(pkts)}"
    for p, (tag, w) in zip(pkts, ((2, c), (3, d), (4, e))):
        check_packet(p, stream_id=0x01, tag=tag, dsizeP=len(w), data_words=w)


# -----------------------------------------------------------------------------
# TC 17 — Tag Clear While a Packet Is in Flight
# -----------------------------------------------------------------------------
@cxp_test()
async def test_17_ctrl_reset_mid_packet(dut):
    """A `stream_ctrl_reset` inside a packet: the next packet carries tag 0.

    CXP 1.1.1 §8.5.3: the tag restarts at 0 on a ConnectionReset /
    ConnectionConfig write.  The packet on the wire finishes with the tag
    it started with; its end must not bump the cleared tag to 1.

    Stimulus: DsizeP = 4; packet A; packet B with a one-cycle
              `stream_ctrl_reset` two edges after its SOP; packet C.
    Checks:   tags 0, 1, 0; every packet bit-exact.
    """
    dut.TESTCASE.value = 17
    await bringup(dut)
    drv = PktTxDriver(dut)
    words = [[0x10 * k + i for i in range(4)] for k in (1, 2, 3)]
    for w in words:
        drv.push(stream_id=0x02, data_words=w)
    beats: list[WireBeat] = []
    await _until_sop(dut, drv, beats)
    await _until_sop(dut, drv, beats)
    await _capture_step(dut, drv, beats)
    dut.stream_ctrl_reset.value = 1
    await _capture_step(dut, drv, beats)
    dut.stream_ctrl_reset.value = 0
    beats += await run(dut, drv, cycles=200)
    pkts = split_packets(beats)
    assert len(pkts) == 3, f"expected 3 packets, got {len(pkts)}"
    for pkt, tag, w in zip(pkts, (0, 1, 0), words):
        check_packet(pkt, stream_id=0x02, tag=tag, dsizeP=4, data_words=w)


# -----------------------------------------------------------------------------
# TC 18 — Tag Clear On Every Cycle, Including A Packet's Start
# -----------------------------------------------------------------------------
@cxp_test()
async def test_18_ctrl_reset_on_start_edge(dut):
    """A one-cycle `stream_ctrl_reset` restarts the tags wherever it lands.

    §8.5.3 / §10.3.33: a ConnectionConfig write (a one-cycle pulse here)
    restarts the PacketTag at 0.  The packet whose header is already
    latched keeps its tag; the next one carries 0.

    Stimulus: for d = 0 .. 29, from reset: packets A, B, C, D (DsizeP 2)
              on StreamID 0x03, back to back; `stream_ctrl_reset` for the
              one cycle d cycles after the driver starts.
    Checks:   among the packets whose SOP is on the wire after the pulse,
              the tags are 0, 1, 2 … or the first one is any tag and the
              rest are 0, 1, …; every packet bit-exact otherwise.
    """
    dut.TESTCASE.value = 18
    await bringup(dut)
    bad = []
    for d in range(30):
        dut.tx_rst_n.value = 0
        for _ in range(2):
            await RisingEdge(dut.tx_clk)
        dut.tx_rst_n.value = 1
        await RisingEdge(dut.tx_clk)
        drv = PktTxDriver(dut)
        words = [[0x10 * k + i for i in range(2)] for k in range(4)]
        for w in words:
            drv.push(stream_id=0x03, data_words=w)
        beats: list[WireBeat] = []
        pulse_at = None
        for c in range(80):
            dut.stream_ctrl_reset.value = int(c == d)
            if c == d:
                pulse_at = len(beats)
            await _capture_step(dut, drv, beats)
        dut.stream_ctrl_reset.value = 0
        pkts = split_packets(beats)
        starts, n = [], 0
        for pkt in pkts:
            starts.append(n)
            n += len(pkt)
        after = [pkt[3].data & 0xFF for pkt, st in zip(pkts, starts) if st >= pulse_at]
        ok = after == list(range(len(after))) or after[1:] == list(range(len(after) - 1))
        if len(pkts) != 4 or not ok:
            bad.append(f"d={d}: tags after the pulse {after}")
    assert not bad, f"{len(bad)} positions: {bad[:4]}"
