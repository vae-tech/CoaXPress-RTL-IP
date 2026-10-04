"""Cocotb TB for `cxp_device_top` with three unrelated clocks.

The whole device between the serial uplink and the downlink word bus,
built with `p_ASYNC_CLOCKS = 1` and run with rx_clk 10 ns, tx_clk 8 ns and
app_clk 12 ns, so every clock crossing (configuration, ConnectionReset,
control responses and the read buffer, trigger acknowledgment, link-reset
clears, test counters, stream FIFO) carries real traffic.  The host model
(`common/cxp_host.py`) drives the uplink and splits the downlink with the
golden `cxp_protocol` codecs in the device's wire format
(`cxp_protocol.DEVICE`); every acknowledgment CRC and every stream packet
(CRC, tag, DsizeP) is checked by the golden decoders.

`setup` restarts the host and pulses the three resets, released at
unrelated times; the TPG is the pixel source and `cfg_run` gates it.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  After reset, with the TPG stopped, the downlink carries only IDLE.
  2  Discovery reads: Standard, Revision, ControlPacketSizeMax,
     ConnectionConfigDefault and DeviceVendorName.
  3  Register writes round-trip: Width at its feature address, its 0x3000
     slot reads that address, Height, TestPattern.
  4  The TPG streams images of the programmed Width x Height; packets
     reassemble without CRC, tag or DsizeP errors.
  5  ConnectionReset: the write is acknowledged once, nothing follows when
     the link-reset window ends, and StreamPacketSizeMax and TestMode read
     0 afterwards.
  6  TestMode: connection-test packets of 1024 counting words appear and
     TestPacketCountTx counts them.
  7  A host trigger pulses trig_out and is acknowledged with K28.6 / 0x01.
  8  Invalid commands are answered at once: undefined opcode 0x42, Size 0
     0x46, a read over ControlPacketSizeMax 0x45; the largest read fits.
  9  Register accesses the register file refuses: 0x40, 0x43, 0x41; a
     write of 1 to 0x0001_4000 is not a ConnectionReset.
 10  Power-up is a connection reset (§10.3.28): the §10.3.28 registers
     read their connection-reset values, and AcquisitionStart before any
     StreamPacketSizeMax write sends no stream packet.
 11  A reset of the rx domain alone, after one acknowledged read and one
     acknowledged host trigger, puts nothing on the downlink.
 12  A device trigger edge swept across every phase of the IDLE cadence
     during connection-test packets and during control acknowledgments:
     each leader is followed by its Delay word, never by an IDLE.
 13  A read sent right behind another read (a host re-sending after its
     timeout, §8.6.1.1) never gets an acknowledgment carrying the other
     read's data.
 14  A reset of the tx domain alone and of the app domain alone while
     streaming: nothing stray on the wire, the device comes back as after
     power-up, the stream restarts at tag 0 with a whole image.
 15  ConnectionReset post-conditions (§10.3.28, every bullet this device
     has) after priming each register, counter, the trigger and the
     stream.
 16  ConnectionReset read back while the reset is in progress: one ack
     for the write, every read answered once, no ack after the window.
 17  ConnectionReset during a stream packet, a host test packet and a
     held trigger, then five more back to back: no torn packet, the
     post-conditions hold, rediscovery streams again from tag 0.
 18  After a ConnectionReset mid-image, the first stream packet once the
     host writes StreamPacketSizeMax again starts a whole image.
 19  The test pattern stopped (cfg_run 1 -> 0) at every point of a packet
     and restarted: every packet closes, every image is whole.
 20  Sensor path: no image before AcquisitionStart; AcquisitionStop
     completes the image in progress, then IDLE only.
 21  StreamPacketSizeMax below one minimal packet (36 bytes) holds the
     stream; at 36 every packet is 36 bytes.
 22  TestMode while streaming: after it the stream resumes with a whole
     image, nothing held from before.
 23  StreamPacketSizeMax changed between images and mid-image: every
     packet fits the value in force, every image is whole.
 24  A slow user-window slave (the wrapper's APB slave; the 100 ms Wait
     and 900 ms timeout are 1000 / 9000 rx cycles): under the Wait time
     no Wait; over it one 0x04 within 200 ms, then the final; never
     answering: 0x04, then 0x40 before the announced time ends; a
     bootstrap register never gets a Wait.
 25  0xFF while the user slave hangs, right after a Wait, while a read
     acknowledgment waits to be framed, and twice back to back: one 0x03
     per 0xFF, no stray Wait or final afterwards, and the next read gets
     its own data.
 26  Small sensor frames of changing size back to back, the metadata
     moving on as soon as a frame's last pixel is taken, the stream slowed
     by short packets: every image header carries its own frame's size.
 27  Host triggers swept across 200-word stream packets: every I/O
     acknowledgment leaves within a few words of its request, inserted
     into the stream packet.
 28  TestMode written at several points of a stream packet and cleared at
     several points of a test packet: no packet is cut, each write is
     acknowledged once, the stream resumes with whole images.
 29  Host triggers during TestMode are acknowledged at once, and nothing
     follows when TestMode ends.
 30  Device trigger pin against a host that acknowledges, drops every
     acknowledgment, or acknowledges late: no trigger packet before the
     previous one is acknowledged or timed out; the host's trigger level
     ends equal to the pin.
 31  A host trigger and a device trigger edge within a few cycles of each
     other, in both orders, while streaming: both short packets are whole,
     the stream packet around them too.
 32  A device trigger edge while the link is down sends nothing; the
     level is sent once the host has brought the link up.
 33  Host connection-test packets with a Table 15 trigger inside at words
     1, 512 and 1023, character phases 1 to 3, TestMode 0 and 1: no test
     error, every packet counted, every trigger fired and acknowledged.
 34  The uplink goes quiet in the middle of a write, at each of its
     words, and comes back one bit off: at most one acknowledgment for
     the cut write, never a success; the next read is answered 0x00 and
     the write had no effect.
 35  A Table 15 trigger inside a write at each character phase of its
     data word: the trigger fires and is acknowledged once, the write is
     acknowledged 0x01 and took effect.
 36  Sensor images of one to eight pixels back to back, the metadata moving
     on in the cycle each image's last pixel is taken: every image header
     carries its own image's size and SourceTag.
 37  All three resets pulsed together while a stream packet is on the wire
     and a read is pending, at several points of the read: the device
     comes back on the host's IDLE alone, sends nothing stale, answers
     the next reads and streams again from tag 0 with whole images.
 38  The TPG image header takes StreamID, TapG and Flags from the
     Image1StreamID, TapGeometry and StreamFlags registers.
 39  Writes of 1, 2 and 3 bytes into the APB user window change only those
     bytes (PSTRB), and PixelFormat takes PFNC values (§11.2.1.6) while
     the image header carries the Table 25 PixelF code.
"""

from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Combine, ReadOnly, RisingEdge, Timer

from cxp_protocol import DEVICE
from cxp_protocol import packets as gp
from cxp_protocol import stream as gs
from cxp_protocol import regmap as grm
from cxp_host import Host
from cxp_link_check import check_downlink, split_short_packets
from cxp_reset import pulse_reset
from cxp_testcase import at_teardown, cxp_test

RX_NS, TX_NS, APP_NS = 10, 8, 12
OS_RATIO = 4
RX_CLK_KHZ = 10                 # wrapper parameter: 1 ms = 10 rx cycles
USER = 0x0002_0000              # wrapper's user window (64 words)

# Register addresses (the generated map, src/regmap/cxp_regmap.yaml)
A_STANDARD      = grm.STANDARD
A_REVISION      = grm.REVISION
A_VENDOR_NAME   = grm.DEVICE_VENDOR_NAME
A_CONN_RESET    = grm.CONNECTION_RESET
A_MASTER_HOST   = grm.MASTER_HOST_CONNECTION_ID
A_CONN_CFG      = grm.CONNECTION_CONFIG
A_TEST_ERR_SEL  = grm.TEST_ERROR_COUNT_SELECTOR
A_TEST_ERR_CNT  = grm.TEST_ERROR_COUNT
A_TEST_PKT_RX   = grm.TEST_PACKET_COUNT_RX
A_ELEC_TEST     = grm.ELECTRICAL_COMPLIANCE_TEST
A_XML_SEL       = grm.XML_MANIFEST_SELECTOR
A_ACQ_START     = grm.ACQUISITION_START_ALIAS
A_ACQ_STOP      = grm.ACQUISITION_STOP_ALIAS
A_CTRL_PKT_MAX  = grm.CONTROL_PACKET_SIZE_MAX
A_STR_PKT_MAX   = grm.STREAM_PACKET_SIZE_MAX
A_CONN_CFG_DEF  = grm.CONNECTION_CONFIG_DEFAULT
A_TEST_MODE     = grm.TEST_MODE
A_TEST_PKT_TX   = grm.TEST_PACKET_COUNT_TX
A_WIDTH_SLOT    = grm.WIDTH_SLOT
A_WIDTH         = grm.WIDTH_ALIAS
A_HEIGHT        = grm.HEIGHT_ALIAS
A_TEST_PATTERN  = grm.TEST_PATTERN
A_TAP_GEOMETRY  = grm.TAP_GEOMETRY_ALIAS
A_STREAM_ID     = grm.IMAGE1_STREAM_ID_ALIAS
A_STREAM_FLAGS  = grm.STREAM_FLAGS

async def setup(dut, run: bool = False, rx_ns: int = RX_NS,
                start_host: bool = True) -> Host:
    """Start the clocks, reset all three domains, start a host.

    cocotb ends every task a test started when the test finishes, so each
    test gets its own clocks and host.  The host's golden deframer is
    checked for framing errors and the §8.2.5.1 IDLE rule at teardown.
    With `start_host` false the host is returned unstarted: the uplink
    stays low and the link down."""
    cocotb.start_soon(Clock(dut.rx_clk, rx_ns, unit="ns").start())
    cocotb.start_soon(Clock(dut.tx_clk, TX_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.app_clk, APP_NS, unit="ns").start())
    dut.cfg_run.value = 0
    dut.cfg_use_tpg.value = 1
    for n in ("s_pix_data", "s_pix_valid", "s_pix_sof", "s_pix_eol", "s_pix_eof"):
        getattr(dut, n).value = 0
    dut.s_meta_xsize.value = 16
    dut.s_meta_ysize.value = 8
    dut.s_meta_pixfmt.value = 0x0101
    dut.s_meta_sourcetag.value = 0
    dut.trig_in.value = 0
    dut.rx_serial.value = 0
    dut.apb_latency.value = 1
    dut.apb_slverr.value = 0
    dut.rx_rst_n.value = 0
    dut.tx_rst_n.value = 0
    dut.app_rst_n.value = 0
    await Timer(100, unit="ns")
    dut.rx_rst_n.value = 1
    await Timer(7, unit="ns")
    dut.tx_rst_n.value = 1
    await Timer(5, unit="ns")
    dut.app_rst_n.value = 1
    host = Host(dut, q=DEVICE, os_ratio=OS_RATIO)
    if not start_host:
        return host
    host.start()
    at_teardown(lambda: check_downlink(host))
    await host.link_up()
    dut.cfg_run.value = int(run)
    return host


async def tx_cycles(dut, n: int):
    for _ in range(n):
        await RisingEdge(dut.tx_clk)


# -----------------------------------------------------------------------------
# TC 1 — Idle After Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_idle(dut):
    """With the TPG stopped the device sends nothing but IDLE words.

    Stimulus: `setup` (cfg_run = 0); 3000 tx_clk cycles.
    Checks:   no acknowledgment, stream, trigger, I/O-ack, test or unknown
              packet on the downlink; at least 3000 words observed.
    """
    dut.TESTCASE.value = 1
    host = await setup(dut)
    await tx_cycles(dut, 3000)
    assert host.words >= 3000
    assert not host.acks and not host.other
    assert host.stream_packets == 0 and not host.triggers and not host.ioacks
    assert not host.linktests


# -----------------------------------------------------------------------------
# TC 2 — Discovery Reads
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_discovery(dut):
    """The Table 45 discovery registers read back across the clock domains.

    Stimulus: `setup`; reads of Standard, Revision, ControlPacketSizeMax,
              ConnectionConfigDefault and the 8 words of DeviceVendorName.
    Checks:   0xC0A79AE5, 0x00010001, and the register map's packet size,
              ConnectionConfigDefault and DeviceVendorName (NULL-padded).
    """
    dut.TESTCASE.value = 2
    host = await setup(dut)
    assert await host.read1(A_STANDARD) == 0xC0A79AE5
    assert await host.read1(A_REVISION) == 0x00010001
    assert await host.read1(A_CTRL_PKT_MAX) == grm.CONTROL_PACKET_SIZE_MAX_VALUE
    assert await host.read1(A_CONN_CFG_DEF) == grm.CONNECTION_CONFIG_DEFAULT_VALUE
    code, words = await host.read(A_VENDOR_NAME, 8)
    assert code == gp.ACK_OK_DATA
    name = b"".join(w.to_bytes(4, "big") for w in words).rstrip(b"\0")
    assert name == grm.DEVICE_VENDOR_NAME_STR.encode(), name


# -----------------------------------------------------------------------------
# TC 3 — Register Round Trip
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_registers(dut):
    """Writes land in the register file and read back through both maps.

    Stimulus: `setup`; write Width = 12 at its feature address, Height = 6,
              TestPattern = 2; read the Width slot (0x3000).
    Checks:   every write acked 0x01; the slot reads the Width feature
              address; Width reads 12, Height 6, TestPattern 2.
    """
    dut.TESTCASE.value = 3
    host = await setup(dut)
    await host.write_ok(A_WIDTH, 12)
    await host.write_ok(A_HEIGHT, 6)
    await host.write_ok(A_TEST_PATTERN, 2)
    assert await host.read1(A_WIDTH_SLOT) == A_WIDTH
    assert await host.read1(A_WIDTH) == 12
    assert await host.read1(A_HEIGHT) == 6
    assert await host.read1(A_TEST_PATTERN) == 2


# -----------------------------------------------------------------------------
# TC 4 — Test-Pattern Stream
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_stream(dut):
    """The TPG image crosses app -> tx intact at the programmed geometry.

    Stimulus: `setup`; StreamPacketSizeMax = 288 bytes (64-word payload,
              the host's discovery write); Width = 12, Height = 6 (Mono8);
              cfg_run = 1; wait for three image headers.
    Checks:   the second image (the first may have started before the
              geometry write) is 12 x 6 with 6 lines of 3 words; the golden
              reassembler reports no CRC, tag or DsizeP error.
    """
    dut.TESTCASE.value = 4
    host = await setup(dut)
    await host.write_ok(A_STR_PKT_MAX, 4 * (64 + 8))
    await host.write_ok(A_WIDTH, 12)
    await host.write_ok(A_HEIGHT, 6)
    dut.cfg_run.value = 1
    for _ in range(200000):
        await RisingEdge(dut.tx_clk)
        if len(host.images) >= 3:
            break
    assert len(host.images) >= 3, f"only {len(host.images)} images"
    img = host.images[1]
    assert (img.meta.xsize, img.meta.ysize) == (12, 6), img.meta
    assert len(img.lines) == 6 and all(len(ln) == 3 for ln in img.lines), \
        [len(ln) for ln in img.lines]
    assert not host.reasm.errors, host.reasm.errors
    assert host.reasm.crc_errors == 0


# -----------------------------------------------------------------------------
# TC 5 — ConnectionReset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_connection_reset(dut):
    """A ConnectionReset crosses rx -> tx and its clears come back to rx.

    Stimulus: `setup`; read StreamPacketSizeMax; ConnectionReset = 1; 400
              tx_clk cycles for the link-reset window.
    Checks:   StreamPacketSizeMax reads 0 at power-up (§10.3.28: power-up
              executes a connection reset); the reset write is
              acked 0x01 and no further acknowledgment follows when the
              window closes (§8.6.1.1: one per command); StreamPacketSizeMax
              and TestMode then read 0.
    """
    dut.TESTCASE.value = 5
    host = await setup(dut)
    spsm = await host.read1(A_STR_PKT_MAX)
    assert spsm == 0, f"StreamPacketSizeMax {spsm} at power-up"
    await host.write_ok(A_CONN_RESET, 1)
    await tx_cycles(dut, 400)
    extra = [a.code for a in host.acks]
    assert extra == [], extra
    host.acks.clear()
    assert await host.read1(A_STR_PKT_MAX) == 0
    assert await host.read1(A_TEST_MODE) == 0


# -----------------------------------------------------------------------------
# TC 6 — TestMode
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_test_mode(dut):
    """TestMode reaches the tx-side test generator; its count returns to rx.

    Stimulus: `setup`; TestMode = 1; wait for two connection-test packets;
              TestMode = 0; read TestPacketCountTx (high, low word).
    Checks:   each packet carries 1024 words counting up by one byte per
              lane from 0 (P0..P3 = n, n+1, n+2, n+3); the count is >= 2.
    """
    dut.TESTCASE.value = 6
    host = await setup(dut)
    await host.write_ok(A_TEST_MODE, 1)
    for _ in range(100000):
        await RisingEdge(dut.tx_clk)
        if len(host.linktests) >= 2:
            break
    assert len(host.linktests) >= 2, f"{len(host.linktests)} test packets"
    for body in host.linktests[:2]:
        assert len(body) == 1024, len(body)
        for i, w in enumerate(body):
            s = (4 * i) & 0xFF
            exp = (((s + 3) & 0xFF) << 24) | (((s + 2) & 0xFF) << 16) \
                | (((s + 1) & 0xFF) << 8) | s
            assert w == exp, f"word {i}: 0x{w:08x} != 0x{exp:08x}"
    await host.write_ok(A_TEST_MODE, 0)
    code, words = await host.read(A_TEST_PKT_TX, 2)
    assert code == gp.ACK_OK_DATA
    count = (words[0] << 32) | words[1]
    assert count >= 2, count


# -----------------------------------------------------------------------------
# TC 7 — Host Trigger
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_host_trigger(dut):
    """A host trigger is delivered on rx and acknowledged on tx.

    Stimulus: `setup`; one rising-edge trigger packet on the uplink; watch
              3000 tx_clk cycles.
    Checks:   trig_out goes high at least once; exactly one I/O
              acknowledgment with code 0x01.
    """
    dut.TESTCASE.value = 7
    host = await setup(dut)
    seen = []

    async def watch():
        while True:
            await RisingEdge(dut.rx_clk)
            if int(dut.trig_out.value):
                seen.append(True)

    w = cocotb.start_soon(watch())
    await host.trigger(rising=True, delay=0)
    await tx_cycles(dut, 3000)
    w.cancel()
    assert seen, "trig_out never pulsed"
    assert host.ioacks == [gp.IOACK_OK], host.ioacks


# -----------------------------------------------------------------------------
# TC 8 — Invalid Commands
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_invalid_commands(dut):
    """Each invalid command gets one final code and nothing is executed.

    §8.6.1.1; Table 22 codes; §8.6.4 packet size limit.

    Stimulus: `setup`; SPEC-built commands: opcode 0x02; a read of Size 0;
              a read of N words with N + 6 words just over
              ControlPacketSizeMax; a read of the largest N that fits (all
              from the XML ROM, which is long enough).
    Checks:   acks 0x42, 0x46, 0x45 (each short form); the largest read
              is answered 0x00 with N words.
    """
    dut.TESTCASE.value = 8
    host = await setup(dut)
    n_max = grm.CONTROL_PACKET_SIZE_MAX_VALUE // 4 - 6
    ack = await host.command(gp.ctrl_cmd(0x02, 0x0000, 4))
    assert ack.code == gp.ACK_BAD_OP, hex(ack.code)
    ack = await host.command(gp.ctrl_cmd(gp.OP_READ, 0x0000, 0))
    assert ack.code == gp.ACK_SIZE_MISMATCH, hex(ack.code)
    code, _ = await host.read(grm.XML_BLOB_ADDR, n_max + 1)
    assert code == gp.ACK_OVERSIZE, hex(code)
    code, data = await host.read(grm.XML_BLOB_ADDR, n_max)
    assert code == gp.ACK_OK_DATA and len(data) == n_max, (hex(code), len(data))


# -----------------------------------------------------------------------------
# TC 9 — Register Access Codes
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_access_codes(dut):
    """The register file's Table 22 code reaches the acknowledgment.

    Stimulus: `setup`; read 0x0020 (nothing there); write Standard
              (0x0000); write PixelFormat (its alias) = 0x0199; write 1 to
              0x0001_4000; read StreamPacketSizeMax.
    Checks:   acks 0x40, 0x43, 0x41, 0x40; StreamPacketSizeMax still reads
              its power-on value (no ConnectionReset happened).
    """
    dut.TESTCASE.value = 9
    host = await setup(dut)
    spsm = await host.read1(A_STR_PKT_MAX)
    code, _ = await host.read(0x0020)
    assert code == gp.ACK_BAD_ADDR, hex(code)
    assert await host.write(0x0000, 1) == gp.ACK_RO_WRITE
    assert await host.write(grm.PIXEL_FORMAT_ALIAS, 0x0199) == gp.ACK_BAD_DATA
    assert await host.write(0x0001_4000, 1) == gp.ACK_BAD_ADDR
    assert await host.read1(A_STR_PKT_MAX) == spsm



# -----------------------------------------------------------------------------
# TC 10 — Power-Up Is A Connection Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_powerup_is_connection_reset(dut):
    """§10.3.28: the device executes a connection reset at power-up.

    Stimulus: `setup` (cfg_run = 0), the host sends IDLE only; read
              StreamPacketSizeMax, MasterHostConnectionID, TestMode,
              TestErrorCountSelector, ConnectionConfig; write
              AcquisitionStart without writing StreamPacketSizeMax; record
              2000 tx_clk words.
    Checks:   StreamPacketSizeMax 0, MasterHostConnectionID 0, TestMode 0,
              TestErrorCountSelector 0, ConnectionConfig its default; no
              stream packet on the downlink (Table 44: none while
              StreamPacketSizeMax = 0).
    """
    dut.TESTCASE.value = 10
    host = await setup(dut)
    got = {
        "StreamPacketSizeMax": await host.read1(A_STR_PKT_MAX),
        "MasterHostConnectionID": await host.read1(A_MASTER_HOST),
        "TestMode": await host.read1(A_TEST_MODE),
        "TestErrorCountSelector": await host.read1(A_TEST_ERR_SEL),
        "ConnectionConfig": await host.read1(A_CONN_CFG),
    }
    want = {
        "StreamPacketSizeMax": 0, "MasterHostConnectionID": 0, "TestMode": 0,
        "TestErrorCountSelector": 0,
        "ConnectionConfig": grm.CONNECTION_CONFIG_DEFAULT_VALUE,
    }
    await host.write_ok(A_ACQ_START, 1)
    await tx_cycles(dut, 2000)
    wrong = {k: hex(v) for k, v in got.items() if v != want[k]}
    assert not wrong and host.stream_packets == 0, \
        f"after power-up: {wrong}; {host.stream_packets} stream packets"


# -----------------------------------------------------------------------------
# TC 11 — Single-Domain Reset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_single_domain_reset(dut):
    """A reset of one clock domain alone emits nothing on the downlink.

    Stimulus: `setup`; one read (acknowledged) and one host trigger
              (I/O-acknowledged), so the rx -> tx response and trigger
              crossings each hold one completed transfer; rx_rst_n alone
              for 20 rx_clk cycles while tx and app run; 3000 tx_clk
              cycles; the link comes back on the host's IDLE.
    Checks:   no acknowledgment, I/O acknowledgment, trigger or other
              packet after the reset; a read afterwards is answered once.
    """
    dut.TESTCASE.value = 11
    host = await setup(dut)
    assert await host.read1(A_STANDARD) == 0xC0A79AE5
    await host.trigger(rising=True, delay=0)
    await tx_cycles(dut, 2000)
    assert host.ioacks == [gp.IOACK_OK], host.ioacks
    host.ioacks.clear()
    await pulse_reset(dut.rx_rst_n, dut.rx_clk, cycles=20)
    await tx_cycles(dut, 3000)
    phantom = [f"ack 0x{a.code:02x} size {a.size}" for a in host.acks]
    phantom += [f"I/O ack 0x{c:02x}" for c in host.ioacks]
    phantom += [f"trigger {t}" for t in host.triggers]
    assert not phantom, f"after an rx-only reset: {phantom}"
    await host.link_up()
    assert await host.read1(A_STANDARD) == 0xC0A79AE5
    assert not host.acks, [a.code for a in host.acks]


# -----------------------------------------------------------------------------
# TC 12 — IDLE Cadence Against The Device Trigger
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_idle_cadence_per_packet_type(dut):
    """A device trigger never straddles an IDLE, whatever the cadence phase.

    §8.2.5.1 / §8.2.5.2, Table 16, §8.3.3.

    Stimulus: `setup` (the host acknowledges every trigger); TestMode = 1
              (connection-test packets back to back: every phase of the
              IDLE cadence is on the wire); 250 pin toggles, each 0..120
              tx_clk cycles after the previous trigger was acknowledged;
              TestMode = 0; StreamPacketSizeMax = 832 and cfg_run = 1
              with 60 more toggles; cfg_run = 0 and 20 back-to-back
              largest reads with 30 more.
    Checks:   every trigger leader is followed on the next word by its
              Delay word; one trigger packet per toggle (each waits for
              the previous acknowledgment); the triggers inside test
              packets went out at 80 or more distinct positions of the
              IDLE cadence; no run of more than 99 non-IDLE words.
    """
    dut.TESTCASE.value = 12
    host = await setup(dut)
    host.raw = []
    n_max = grm.CONTROL_PACKET_SIZE_MAX_VALUE // 4 - 6
    rng = random.Random(12)
    toggles = 0

    async def sweep(n):
        nonlocal toggles
        for _ in range(n):
            before, acked = len(host.triggers), len(host.trig_ack_words)
            dut.trig_in.value = 1 - int(dut.trig_in.value)
            toggles += 1
            assert await wait_count(dut, lambda: len(host.trig_ack_words), acked + 1, 5000), \
                f"toggle {toggles}: no trigger / acknowledgment"
            assert len(host.triggers) == before + 1
            await tx_cycles(dut, rng.randint(0, 120))

    await host.write_ok(A_TEST_MODE, 1)
    assert await wait_count(dut, lambda: len(host.linktests), 1, 20000), "no test packet"
    lt_from = len(host.raw)
    await sweep(250)
    lt_to = len(host.raw)
    await host.write_ok(A_TEST_MODE, 0)
    await host.write_ok(A_STR_PKT_MAX, 4 * (200 + 8))
    dut.cfg_run.value = 1
    await sweep(60)
    dut.cfg_run.value = 0
    sw = cocotb.start_soon(sweep(30))
    for _ in range(20):
        code, _ = await host.read(grm.XML_BLOB_ADDR, n_max)
        assert code == gp.ACK_OK_DATA
    await sw
    await tx_cycles(dut, 3000)
    torn = split_short_packets(host.raw)
    assert not torn, f"{len(torn)} torn trigger packets, first: {torn[:3]}"
    assert len(host.triggers) == toggles, f"{len(host.triggers)} triggers for {toggles} toggles"
    phases, run = set(), 0
    for i, (w, k) in enumerate(host.raw):
        if k == gp.IDLE_KMASK and w == gp.IDLE_WORD:
            run = 0
            continue
        if lt_from <= i < lt_to and k == 0xF and w in (gp.rep4(gp.K28_2), gp.rep4(gp.K28_4)):
            phases.add(run)
        run += 1
    assert len(phases) >= 80, f"triggers at {len(phases)} cadence positions: {sorted(phases)}"
    assert host.max_run <= 99, host.max_run


# -----------------------------------------------------------------------------
# TC 13 — Pipelined Commands
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_pipelined_cmds(dut):
    """A second read sent behind the first never corrupts the first's ack.

    §8.6.1.1 (a host re-sends after its timeout), Table 22.

    Stimulus: `setup` with rx_clk at 2 ns, so a six-word command crosses
              the uplink in about 2 us, well inside the ~8 us an
              acknowledgment can wait behind one connection-test packet;
              golden: two largest reads A and B of different
              parts of the XML ROM, one at a time.  TestMode = 1 so every
              acknowledgment waits behind a connection-test packet; then A
              and B sent back to back without waiting, three times.
    Checks:   every acknowledgment carries exactly A's or B's golden data
              with a good CRC (or B is not answered at all); A's ack never
              carries B's words.
    """
    dut.TESTCASE.value = 13
    host = await setup(dut, rx_ns=2)
    n = grm.CONTROL_PACKET_SIZE_MAX_VALUE // 4 - 6
    a_addr, b_addr = grm.XML_BLOB_ADDR, grm.XML_BLOB_ADDR + 4 * n
    code_a, gold_a = await host.read(a_addr, n)
    code_b, gold_b = await host.read(b_addr, n)
    assert code_a == code_b == gp.ACK_OK_DATA and gold_a != gold_b
    await host.write_ok(A_TEST_MODE, 1)
    host.acks.clear()
    bad = []
    for rnd in range(3):
        await host.send(gp.ctrl_cmd(gp.OP_READ, a_addr, 4 * n, q=host.q)
                        + gp.ctrl_cmd(gp.OP_READ, b_addr, 4 * n, q=host.q))
        first = await host.wait_ack(timeout_words=40000)
        if not (first.code == gp.ACK_OK_DATA and first.crc_ok and first.data == gold_a):
            bad.append(f"round {rnd}: first ack 0x{first.code:02x} crc_ok={first.crc_ok} "
                       f"{'= B' if first.data == gold_b else 'data mismatch'}")
        try:
            second = await host.wait_ack(timeout_words=40000)
        except AssertionError:
            continue                          # B ignored: allowed
        if not (second.code == gp.ACK_OK_DATA and second.crc_ok and second.data == gold_b):
            bad.append(f"round {rnd}: second ack 0x{second.code:02x} crc_ok={second.crc_ok} "
                       f"{'= A' if second.data == gold_a else 'data mismatch'}")
    await host.write_ok(A_TEST_MODE, 0)
    assert not bad, bad


# -----------------------------------------------------------------------------
# Helpers for the reset tests
# -----------------------------------------------------------------------------
SPSM_64 = 4 * (64 + 8)          # StreamPacketSizeMax for a 64-word payload


async def wait_images(dut, host: Host, n: int, limit: int = 300000):
    for _ in range(limit):
        await RisingEdge(dut.tx_clk)
        if len(host.images) >= n:
            return
    raise AssertionError(f"only {len(host.images)} images of {n}")


def fresh_stream(host: Host):
    """Forget stream history: tags and images restart after a reset."""
    host.reasm = gs.StreamReassembler(host.q)
    host.stream_tags.clear()
    host.stream_packets = 0


async def rediscover_and_stream(dut, host: Host, images: int = 2,
                                whole: bool = False):
    """What a host does after a reset: StreamPacketSizeMax, then images.
    The first packet must carry tag 0; with `whole` it must also start a
    whole image (nothing of an image begun before the reset)."""
    fresh_stream(host)
    await host.write_ok(A_STR_PKT_MAX, SPSM_64)
    await wait_images(dut, host, images)
    assert host.stream_tags[0] == 0, f"first tag after reset {host.stream_tags[0]}"
    if whole:
        assert not host.reasm.errors, host.reasm.errors[:3]


# -----------------------------------------------------------------------------
# TC 14 — Single-Domain Reset While Streaming
# -----------------------------------------------------------------------------
@cxp_test()
async def test_14_single_domain_reset_streaming(dut):
    """A reset of one domain alone is a clean reset of the device.

    cxp_cdc_reset turns any one reset input into a reset of all three
    domains, released rx, tx, app.

    Stimulus: `setup`; StreamPacketSizeMax for 64-word packets, 16 x 8
              images, cfg_run = 1, two images; then for tx_rst_n and
              app_rst_n in turn: that reset alone for 20 of its cycles
              mid-stream; 3000 tx_clk cycles; relink.
    Checks:   after each reset no acknowledgment, I/O ack or trigger
              appears and no stream packet leaves while
              StreamPacketSizeMax reads 0 (power-up value); after the host
              writes it again the first packet carries tag 0.
    Note:     a hard reset cuts the packet on the wire at that moment; the
              host's deframer restarts at each reset, so the teardown
              framing check covers what the device sends afterwards.
    """
    dut.TESTCASE.value = 14
    host = await setup(dut)
    await host.write_ok(A_WIDTH, 16)
    await host.write_ok(A_HEIGHT, 8)
    await host.write_ok(A_STR_PKT_MAX, SPSM_64)
    dut.cfg_run.value = 1
    await wait_images(dut, host, 2)
    for rst, clk in ((dut.tx_rst_n, dut.tx_clk), (dut.app_rst_n, dut.app_clk)):
        host.acks.clear()
        host.ioacks.clear()
        host.triggers.clear()
        await pulse_reset(rst, clk, cycles=20)
        host.deframer = gp.Deframer()
        fresh_stream(host)
        await tx_cycles(dut, 3000)
        stray = [f"ack 0x{a.code:02x}" for a in host.acks] + \
                [f"I/O ack {c}" for c in host.ioacks] + [f"trigger {t}" for t in host.triggers]
        assert not stray, f"after {rst._name} alone: {stray}"
        assert host.stream_packets == 0, f"{host.stream_packets} packets with SPSM at 0"
        await host.link_up()
        assert await host.read1(A_STR_PKT_MAX) == 0
        await host.write_ok(A_WIDTH, 16)
        await host.write_ok(A_HEIGHT, 8)
        await rediscover_and_stream(dut, host)


# -----------------------------------------------------------------------------
# TC 15 — ConnectionReset Post-Conditions
# -----------------------------------------------------------------------------
@cxp_test()
async def test_15_conn_reset_postconditions(dut):
    """Every §10.3.28 post-condition this device has, in one test.

    Stimulus: `setup` with trig_in held asserted from the start (no
              trigger packet: it was never de-asserted); prime:
              MasterHostConnectionID, StreamPacketSizeMax, two images
              streamed and the stream stopped (TestMode must not start
              mid-packet, a separate finding), ElectricalComplianceTest =
              1, one host test packet with a corrupted word
              (TestErrorCount 1, TestPacketCountRx 1), TestMode = 1 until
              TestPacketCountTx > 0; write ConnectionReset = 1; 3000
              tx_clk cycles.
    Checks:   ConnectionReset, MasterHostConnectionID, StreamPacketSizeMax,
              TestMode, TestErrorCountSelector, XmlManifestSelector,
              ElectricalComplianceTest, TestErrorCount, TestPacketCountTx
              and TestPacketCountRx read 0, ConnectionConfig its default;
              exactly one trigger packet, falling (the trigger set to 0),
              and none after; no stream or test packet until
              StreamPacketSizeMax is written; then the first stream packet
              has tag 0.
    """
    dut.TESTCASE.value = 15
    dut.trig_in.value = 1
    host = await setup(dut)
    dut.trig_in.value = 1
    await host.write_ok(A_MASTER_HOST, 0x1234_5678)
    await host.write_ok(A_WIDTH, 16)
    await host.write_ok(A_HEIGHT, 8)
    await host.write_ok(A_STR_PKT_MAX, SPSM_64)
    await host.write_ok(A_ELEC_TEST, 1)
    dut.cfg_run.value = 1
    await wait_images(dut, host, 2)
    dut.cfg_run.value = 0
    await tx_cycles(dut, 4000)
    bad = gp.linktest_packet()
    bad[5] = (bad[5][0] ^ 1, bad[5][1])
    await host.send(bad)
    await host.send([gp.IDLE] * 4)
    assert await host.read1(A_TEST_ERR_CNT) == 1
    await host.write_ok(A_TEST_MODE, 1)
    for _ in range(100000):
        await RisingEdge(dut.tx_clk)
        if host.linktests:
            break
    code, words = await host.read(A_TEST_PKT_TX, 2)
    assert code == gp.ACK_OK_DATA and (words[0] << 32 | words[1]) > 0
    host.triggers.clear()
    await host.write_ok(A_CONN_RESET, 1)
    fresh_stream(host)
    n_lt = len(host.linktests)
    await tx_cycles(dut, 3000)
    want = {A_CONN_RESET: 0, A_MASTER_HOST: 0, A_STR_PKT_MAX: 0, A_TEST_MODE: 0,
            A_TEST_ERR_SEL: 0, A_XML_SEL: 0, A_ELEC_TEST: 0, A_TEST_ERR_CNT: 0,
            A_CONN_CFG: grm.CONNECTION_CONFIG_DEFAULT_VALUE}
    got = {a: await host.read1(a) for a in want}
    wrong = {hex(a): hex(v) for a, v in got.items() if v != want[a]}
    for a in (A_TEST_PKT_TX, A_TEST_PKT_RX):
        code, words = await host.read(a, 2)
        if (words[0] << 32 | words[1]) != 0:
            wrong[hex(a)] = words
    assert not wrong, f"after ConnectionReset: {wrong}"
    assert host.triggers == [(False, host.triggers[0][1])] if host.triggers else False, \
        f"trigger packets at the reset: {host.triggers}"
    assert host.stream_packets == 0, f"{host.stream_packets} stream packets with SPSM at 0"
    # One test packet may complete after the write (TestMode ends after the
    # current packet); none may start later.
    assert len(host.linktests) <= n_lt + 1, (n_lt, len(host.linktests))
    dut.cfg_run.value = 1
    await rediscover_and_stream(dut, host)


# -----------------------------------------------------------------------------
# TC 16 — Poll During The Reset Window
# -----------------------------------------------------------------------------
@cxp_test()
async def test_16_poll_during_reset_window(dut):
    """ConnectionReset read back at once, as a GenTL producer polls it.

    §10.3.28: the device clears the register when the reset is done; a
    host may read it meanwhile.

    Stimulus: `setup`; write ConnectionReset = 1; then 5 reads of it back
              to back, each after the previous acknowledgment; 3000 tx_clk
              cycles.
    Checks:   one acknowledgment (0x01) for the write; each read answered
              once, the values never going from 0 back to 1; the last
              read 0; no acknowledgment during the 3000 cycles.
    Note:     at this bench's clocks the reset lasts ~20 rx_clk cycles and
              a command takes ~10 us, so every read sees 0.
    """
    dut.TESTCASE.value = 16
    host = await setup(dut)
    assert await host.write(A_CONN_RESET, 1) == gp.ACK_OK_WRITE
    vals = [await host.read1(A_CONN_RESET) for _ in range(5)]
    assert vals == sorted(vals, reverse=True) and vals[-1] == 0, vals
    await tx_cycles(dut, 3000)
    assert not host.acks, [a.code for a in host.acks]


# -----------------------------------------------------------------------------
# TC 17 — ConnectionReset Under Everything
# -----------------------------------------------------------------------------
@cxp_test()
async def test_17_conn_reset_everything(dut):
    """A ConnectionReset in the middle of all traffic, then a storm.

    Stimulus: `setup`; 16 x 8 images streaming with 64-word packets;
              trig_in toggling every 97 tx_clk cycles; a host test packet
              being sent when the ConnectionReset write goes out behind
              it; then five more ConnectionReset writes back to back.
    Checks:   every write acknowledged once (0x01); no torn packet and the
              IDLE rule held (teardown); StreamPacketSizeMax reads 0; the
              trigger packets alternate rising / falling; rediscovery
              streams again from tag 0.
    """
    dut.TESTCASE.value = 17
    host = await setup(dut)
    await host.write_ok(A_WIDTH, 16)
    await host.write_ok(A_HEIGHT, 8)
    await host.write_ok(A_STR_PKT_MAX, SPSM_64)
    dut.cfg_run.value = 1
    await wait_images(dut, host, 1)
    stop = [False]

    async def toggle():
        while not stop[0]:
            await tx_cycles(dut, 97)
            dut.trig_in.value = 1 - int(dut.trig_in.value)

    tg = cocotb.start_soon(toggle())
    await host.send(gp.linktest_packet() + [gp.IDLE])
    codes = [await host.write(A_CONN_RESET, 1)]
    fresh_stream(host)
    for _ in range(5):
        codes.append(await host.write(A_CONN_RESET, 1))
    stop[0] = True
    await tg
    assert codes == [gp.ACK_OK_WRITE] * 6, codes
    assert await host.read1(A_STR_PKT_MAX) == 0
    kinds = [r for r, _ in host.triggers]
    assert all(a != b for a, b in zip(kinds, kinds[1:])), f"trigger edges {kinds}"
    await rediscover_and_stream(dut, host)


# -----------------------------------------------------------------------------
# TC 18 — Whole Image After A ConnectionReset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_18_whole_image_after_conn_reset(dut):
    """Nothing of an image begun before a ConnectionReset goes out after it.

    §10.3.28 resets the stream control; §9.4: every image starts with its
    header.

    Stimulus: `setup`; 16 x 8 images, 64-word packets, cfg_run = 1; one
              image; ConnectionReset = 1 mid-image; StreamPacketSizeMax
              written again; two images.
    Checks:   the first packet carries tag 0 and the reassembler sees an
              image header first (no "pixel data outside a line").
    """
    dut.TESTCASE.value = 18
    host = await setup(dut)
    await host.write_ok(A_WIDTH, 16)
    await host.write_ok(A_HEIGHT, 8)
    await host.write_ok(A_STR_PKT_MAX, SPSM_64)
    dut.cfg_run.value = 1
    await wait_images(dut, host, 1)
    await tx_cycles(dut, 37)
    await host.write_ok(A_CONN_RESET, 1)
    await rediscover_and_stream(dut, host, whole=True)


# -----------------------------------------------------------------------------
# Sensor driver
# -----------------------------------------------------------------------------
class Sensor:
    """A free-running sensor on s_pix_*: `w` x `h` Mono8 frames back to back
    (one pixel per app_clk cycle while ready), `gap` cycles between frames.
    Pixel value = (frame + x + 3 y) & 0xFF.  It cannot be stopped: pixels
    the device does not want must be taken and dropped."""

    def __init__(self, dut, w: int = 16, h: int = 8, gap: int = 20):
        self.dut, self.w, self.h, self.gap = dut, w, h, gap
        self.frames = 0
        self.task = cocotb.start_soon(self._run())

    async def _run(self):
        d = self.dut
        while True:
            for y in range(self.h):
                for x in range(self.w):
                    d.s_pix_data.value = ((self.frames + x + 3 * y) & 0xFF) << 8  # 16-bit sample, Mono8 = [15:8]
                    d.s_pix_sof.value = int(x == 0 and y == 0)
                    d.s_pix_eol.value = int(x == self.w - 1)
                    d.s_pix_eof.value = int(x == self.w - 1 and y == self.h - 1)
                    d.s_pix_valid.value = 1
                    while True:
                        await RisingEdge(d.app_clk)
                        if int(d.s_pix_ready.value):
                            break
            d.s_pix_valid.value = 0
            self.frames += 1
            for _ in range(self.gap):
                await RisingEdge(d.app_clk)

    def stop(self):
        self.task.cancel()
        self.dut.s_pix_valid.value = 0


# -----------------------------------------------------------------------------
# TC 19 — Test Pattern Stopped Mid-Packet
# -----------------------------------------------------------------------------
@cxp_test()
async def test_19_stop_tpg_mid_packet(dut):
    """Stopping the generator anywhere in a packet leaves nothing open.

    §8.5.2: a packet may be shorter than StreamPacketSizeMax; the last one
    of an image closes short.

    Stimulus: `setup`; 16 x 8 images (32 words + header and markers),
              64-word packets; cfg_run = 1; for delays 0 .. 70 tx_clk
              cycles in steps of 7 after an image header: cfg_run = 0,
              then restart after 0, 1, 600 or 2000 cycles (cycled).
    Checks:   every image reassembles whole with no CRC, tag or DsizeP
              error; no framing error (teardown).
    """
    dut.TESTCASE.value = 19
    host = await setup(dut)
    await host.write_ok(A_WIDTH, 16)
    await host.write_ok(A_HEIGHT, 8)
    await host.write_ok(A_STR_PKT_MAX, SPSM_64)
    restarts = [0, 1, 600, 2000]
    for i, delay in enumerate(range(0, 71, 7)):
        n = len(host.images)
        dut.cfg_run.value = 1
        await wait_images(dut, host, n + 1)
        await tx_cycles(dut, delay)
        dut.cfg_run.value = 0
        await tx_cycles(dut, restarts[i % 4])
    dut.cfg_run.value = 1
    n = len(host.images)
    await wait_images(dut, host, n + 2)
    assert not host.reasm.errors, host.reasm.errors[:3]
    assert host.reasm.crc_errors == 0


# -----------------------------------------------------------------------------
# TC 20 — Acquisition Start / Stop On The Sensor Path
# -----------------------------------------------------------------------------
@cxp_test()
async def test_20_acq_start_stop_sensor(dut):
    """The sensor's images enter only while the acquisition runs.

    §11.2.1.4 / §11.2.1.5: AcquisitionStart starts the acquisition,
    AcquisitionStop ends it after the image in progress.

    Stimulus: `setup` with cfg_use_tpg = 0 and a free-running 16 x 8
              sensor; StreamPacketSizeMax for 64-word packets; 4 sensor
              frames; AcquisitionStart; 3 images; AcquisitionStop in the
              middle of a frame; 6 more sensor frames.
    Checks:   no stream packet before AcquisitionStart; after it whole
              images, the first starting with its header; after the stop
              at most the image in progress completes, then no packet.
    """
    dut.TESTCASE.value = 20
    host = await setup(dut)
    dut.cfg_use_tpg.value = 0
    await host.write_ok(A_STR_PKT_MAX, SPSM_64)
    sensor = Sensor(dut)
    while sensor.frames < 4:
        await RisingEdge(dut.app_clk)
    before = host.stream_packets
    await host.write_ok(A_ACQ_START, 1)
    await wait_images(dut, host, 3)
    while True:                        # into the middle of a sensor frame
        await RisingEdge(dut.app_clk)
        if int(dut.s_pix_valid.value) and int(dut.s_pix_eol.value):
            break
    await host.write_ok(A_ACQ_STOP, 1)
    at_stop = len(host.images)
    f0 = sensor.frames
    while sensor.frames < f0 + 6:
        await RisingEdge(dut.app_clk)
    sensor.stop()
    assert before == 0, f"{before} stream packets before AcquisitionStart"
    assert not host.reasm.errors, host.reasm.errors[:3]
    assert len(host.images) <= at_stop + 1, \
        f"{len(host.images) - at_stop} images after AcquisitionStop"


# -----------------------------------------------------------------------------
# TC 21 — StreamPacketSizeMax Below One Packet
# -----------------------------------------------------------------------------
@cxp_test()
async def test_21_spsm_below_minimal_packet(dut):
    """A StreamPacketSizeMax no packet fits into holds the stream.

    §10.3.32: the device uses any packet size up to StreamPacketSizeMax;
    the smallest stream packet (Table 19, one payload word) is 36 bytes.

    Stimulus: `setup`; cfg_run = 1 with 16 x 8 images;
              StreamPacketSizeMax = 32; 20 000 tx_clk cycles;
              StreamPacketSizeMax = 36; two images.
    Checks:   the 32 is accepted (0x01) and no stream packet leaves; at 36
              every packet is 9 words (36 bytes) and the images are whole.
    """
    dut.TESTCASE.value = 21
    host = await setup(dut)
    await host.write_ok(A_WIDTH, 16)
    await host.write_ok(A_HEIGHT, 8)
    dut.cfg_run.value = 1
    await host.write_ok(A_STR_PKT_MAX, 32)
    await tx_cycles(dut, 20000)
    held = host.stream_packets
    fresh_stream(host)
    host.stream_lens.clear()
    await host.write_ok(A_STR_PKT_MAX, 36)
    await wait_images(dut, host, 2)
    assert held == 0, f"{held} stream packets at StreamPacketSizeMax 32"
    assert max(host.stream_lens) <= 9, max(host.stream_lens)


# -----------------------------------------------------------------------------
# TC 22 — Stream After TestMode
# -----------------------------------------------------------------------------
@cxp_test()
async def test_22_testmode_exit_whole_image(dut):
    """After TestMode the stream starts again with a whole image.

    §8.7.4: in TestMode only test and control packets are sent; what the
    pixel source produced meanwhile must not come out afterwards.

    Stimulus: `setup`; 16 x 8 images, 64-word packets, cfg_run = 1; one
              image; TestMode = 1 for 30 000 tx_clk cycles (the FIFO would
              fill); TestMode = 0; two images.
    Checks:   the first stream packet after TestMode starts with an image
              header and the images reassemble whole.
    Note:     TestMode landing inside a stream packet is swept in
              test 28.
    """
    dut.TESTCASE.value = 22
    host = await setup(dut)
    await host.write_ok(A_WIDTH, 16)
    await host.write_ok(A_HEIGHT, 8)
    await host.write_ok(A_STR_PKT_MAX, SPSM_64)
    dut.cfg_run.value = 1
    await wait_images(dut, host, 1)
    await host.write_ok(A_TEST_MODE, 1)
    for _ in range(100000):
        await RisingEdge(dut.tx_clk)
        if host.linktests:
            break
    await tx_cycles(dut, 30000)
    fresh_stream(host)
    await host.write_ok(A_TEST_MODE, 0)
    await wait_images(dut, host, 2)
    assert not host.reasm.errors, host.reasm.errors[:3]


# -----------------------------------------------------------------------------
# TC 23 — StreamPacketSizeMax Negotiation
# -----------------------------------------------------------------------------
@cxp_test()
async def test_23_spsm_negotiation(dut):
    """Every packet fits the StreamPacketSizeMax in force.

    Table 44, §10.3.32 (the whole packet, SOP to EOP, in bytes).

    Stimulus: `setup`; 16 x 8 images, cfg_run = 1; StreamPacketSizeMax
              128, 1024, 4096, 64 and 40 bytes, each for two images,
              written between images; then 1024 -> 128 mid-image; a
              value that is not a multiple of 4.
    Checks:   each packet sent after a value is in force is at most that
              many bytes; images whole; 130 refused with 0x41.
    """
    dut.TESTCASE.value = 23
    host = await setup(dut)
    await host.write_ok(A_WIDTH, 16)
    await host.write_ok(A_HEIGHT, 8)
    dut.cfg_run.value = 1
    bad = []
    for spsm in (128, 1024, 4096, 64, 40):
        await host.write_ok(A_STR_PKT_MAX, spsm)
        await tx_cycles(dut, 500)
        host.stream_lens.clear()
        n = len(host.images)
        await wait_images(dut, host, n + 2)
        if max(host.stream_lens) * 4 > spsm:
            bad.append((spsm, 4 * max(host.stream_lens)))
    await host.write_ok(A_STR_PKT_MAX, 1024)
    n = len(host.images)
    await wait_images(dut, host, n + 1)
    await tx_cycles(dut, 60)
    await host.write_ok(A_STR_PKT_MAX, 128)
    await tx_cycles(dut, 500)
    host.stream_lens.clear()
    n = len(host.images)
    await wait_images(dut, host, n + 2)
    if max(host.stream_lens) * 4 > 128:
        bad.append((128, 4 * max(host.stream_lens)))
    assert await host.write(A_STR_PKT_MAX, 130) == gp.ACK_BAD_DATA
    assert not bad, f"(StreamPacketSizeMax, biggest packet bytes): {bad}"
    assert not host.reasm.errors, host.reasm.errors[:3]


# -----------------------------------------------------------------------------
# Helpers for the slow-slave tests
# -----------------------------------------------------------------------------
def ms_ns(ms: float, rx_ns: int = RX_NS) -> float:
    """Device milliseconds (p_RX_CLK_KHZ cycles) in simulation ns."""
    return ms * RX_CLK_KHZ * rx_ns


async def acks_for(dut, host: Host, ns: float) -> list:
    """Every acknowledgment within `ns` of now, with its arrival time
    (ns after now)."""
    from cocotb.utils import get_sim_time
    t0 = get_sim_time("ns")
    out = []
    while get_sim_time("ns") - t0 < ns:
        await RisingEdge(dut.tx_clk)
        while host.acks:
            out.append((host.acks.popleft(), get_sim_time("ns") - t0))
    return out


# -----------------------------------------------------------------------------
# TC 24 — Wait Acknowledgment From A Slow Slave
# -----------------------------------------------------------------------------
@cxp_test()
async def test_24_ctrl_wait_ack(dut):
    """§8.6.1.1: a Device that needs more time sends one Wait in time, then
    the final acknowledgment within the time the Wait announced.

    Stimulus: user-window reads with the slave answering after 600 rx
              cycles (60 ms, under the 100 ms Wait), 1500 cycles (150 ms,
              over it) and never; then the slave answering after 12000
              cycles (1200 ms, after the 900 ms timeout): read USER, and
              read USER + 8 as soon as the 0x40 arrives; a read of
              Standard (register file).
    Checks:   under: only 0x00; over: 0x04 (long form, one word, 100 ..
              10 000 ms) within 200 ms of the command, then 0x00 with the
              slave's word; never: 0x04, then 0x40 before the Wait's time
              ends; late: both reads end 0x40 (the late answer to the first
              completes nothing, the second's own access times out);
              Standard: only 0x00.
    """
    dut.TESTCASE.value = 24
    host = await setup(dut)
    rd = lambda a: gp.ctrl_cmd(gp.OP_READ, a, 4, q=host.q)   # noqa: E731
    bad = []

    dut.apb_latency.value = 600
    await host.send(rd(USER))
    got = await acks_for(dut, host, ms_ns(300))
    if [a.code for a, _ in got] != [gp.ACK_OK_DATA]:
        bad.append(f"under: {[hex(a.code) for a, _ in got]}")

    dut.apb_latency.value = 1500
    await host.send(rd(USER + 4))
    got = await acks_for(dut, host, ms_ns(400))
    codes = [a.code for a, _ in got]
    if codes != [gp.ACK_WAIT, gp.ACK_OK_DATA]:
        bad.append(f"over: {[hex(c) for c in codes]}")
    else:
        w, t_w = got[0]
        if not (w.crc_ok and len(w.data) == 1 and 100 <= w.data[0] <= 10_000):
            bad.append(f"over: Wait {w}")
        if t_w > ms_ns(200):
            bad.append(f"over: Wait after {t_w / ms_ns(1):.0f} ms")
        if got[1][0].data != [0xA500_0001]:
            bad.append(f"over: data {got[1][0].data}")

    dut.apb_latency.value = 0xFFFF
    await host.send(rd(USER))
    got = await acks_for(dut, host, ms_ns(1200))
    codes = [a.code for a, _ in got]
    if codes != [gp.ACK_WAIT, gp.ACK_BAD_ADDR]:
        bad.append(f"never: {[hex(c) for c in codes]}")
    elif got[1][1] > got[0][1] + ms_ns(got[0][0].data[0]):
        bad.append("never: 0x40 after the time the Wait announced")

    dut.apb_latency.value = 12000
    await host.send(rd(USER))
    first = [a.code for a, _ in await acks_for(dut, host, ms_ns(1000))]
    await host.send(rd(USER + 8))
    second = [(a.code, a.data) for a, _ in await acks_for(dut, host, ms_ns(1300))]
    if first != [gp.ACK_WAIT, gp.ACK_BAD_ADDR]:
        bad.append(f"late, first: {[hex(c) for c in first]}")
    if [c for c, _ in second] != [gp.ACK_WAIT, gp.ACK_BAD_ADDR]:
        bad.append(f"late, second: {[(hex(c), d) for c, d in second]}")

    dut.apb_latency.value = 1
    await host.send(rd(A_STANDARD))
    got = await acks_for(dut, host, ms_ns(300))
    if [a.code for a, _ in got] != [gp.ACK_OK_DATA]:
        bad.append(f"bootstrap: {[hex(a.code) for a, _ in got]}")
    assert not bad, bad


# -----------------------------------------------------------------------------
# TC 25 — Control Channel Reset During Execution
# -----------------------------------------------------------------------------
@cxp_test()
async def test_25_ctrl_reset_during_exec(dut):
    """§8.6.1.2: a 0xFF gets exactly one 0x03; the abandoned command leaves
    nothing behind.

    Stimulus: (a) slave never answers; read USER, then 0xFF at once;
              (b) the same, 0xFF sent after the Wait arrived; (c) TestMode
              = 1 so acknowledgments wait behind connection-test packets;
              read of 4 words, then 0xFF right behind it; (d) two 0xFF
              back to back.  Between rounds the slave answers in 5 cycles
              and USER + 8 is read.
    Checks:   (a) [0x03] or [0x04, 0x03]; (b) [0x04, 0x03]; (c) [0x00 with
              the golden words, 0x03]; (d) [0x03, 0x03]; nothing more for
              1000 ms after each; every check read returns 0xA5000002.
    """
    dut.TESTCASE.value = 25
    host = await setup(dut)
    rst = gp.ctrl_cmd(gp.OP_RESET, q=host.q)
    rd = lambda a, n=1: gp.ctrl_cmd(gp.OP_READ, a, 4 * n, q=host.q)   # noqa: E731
    bad = []

    async def check_read(tag):
        dut.apb_latency.value = 5
        code, data = await host.read(USER + 8)
        if (code, data) != (gp.ACK_OK_DATA, [0xA500_0002]):
            bad.append(f"{tag}: next read 0x{code:02x} {data}")

    dut.apb_latency.value = 0xFFFF
    await host.send(rd(USER) + rst)
    codes = [a.code for a, _ in await acks_for(dut, host, ms_ns(1100))]
    if codes not in ([gp.ACK_OK_RESET], [gp.ACK_WAIT, gp.ACK_OK_RESET]):
        bad.append(f"(a) {[hex(c) for c in codes]}")
    await check_read("(a)")

    dut.apb_latency.value = 0xFFFF
    await host.send(rd(USER))
    w = await host.wait_ack(timeout_words=40000)
    await host.send(rst)
    codes = [w.code] + [a.code for a, _ in await acks_for(dut, host, ms_ns(1100))]
    if codes != [gp.ACK_WAIT, gp.ACK_OK_RESET]:
        bad.append(f"(b) {[hex(c) for c in codes]}")
    await check_read("(b)")

    code, gold = await host.read(A_VENDOR_NAME, 4)
    await host.write_ok(A_TEST_MODE, 1)
    host.acks.clear()
    await host.send(rd(A_VENDOR_NAME, 4) + rst)
    got = [a for a, _ in await acks_for(dut, host, ms_ns(300))]
    await host.write_ok(A_TEST_MODE, 0)
    if [a.code for a in got] != [gp.ACK_OK_DATA, gp.ACK_OK_RESET] or got[0].data != gold:
        bad.append(f"(c) {[(hex(a.code), a.data) for a in got]}")
    await check_read("(c)")

    await host.send(rst + rst)
    codes = [a.code for a, _ in await acks_for(dut, host, ms_ns(300))]
    if codes != [gp.ACK_OK_RESET, gp.ACK_OK_RESET]:
        bad.append(f"(d) {[hex(c) for c in codes]}")
    await check_read("(d)")
    assert not bad, bad


# -----------------------------------------------------------------------------
# TC 26 — Sensor Metadata Per Image
# -----------------------------------------------------------------------------
@cxp_test()
async def test_26_sensor_meta_per_image(dut):
    """An image header carries the metadata of its own frame.

    Table 38 / §10.4.2: Xsize and Ysize describe the image the header
    opens.  A sensor may present the next frame's metadata as soon as the
    previous frame's last pixel is taken (the emulator bench does).

    Stimulus: `setup` with cfg_use_tpg = 0; StreamPacketSizeMax 40 (two
              payload words, so the stream path is the bottleneck);
              AcquisitionStart; frames 4x4, 5x4, 29x4, 49x7, 57x6, 21x1,
              25x1, 13x1 back to back, s_meta_xsize/ysize switched to the
              next frame's size in the cycle the last pixel is taken.
    Checks:   8 images; each header's Xsize x Ysize is its frame's; every
              line holds the frame's pixels.
    """
    dut.TESTCASE.value = 26
    host = await setup(dut)
    dut.cfg_use_tpg.value = 0
    await host.write_ok(A_STR_PKT_MAX, 40)
    await host.write_ok(A_ACQ_START, 1)
    sizes = [(4, 4), (5, 4), (29, 4), (49, 7), (57, 6), (21, 1), (25, 1), (13, 1)]
    d = dut
    d.s_meta_xsize.value, d.s_meta_ysize.value = sizes[0]
    for n, (w, h) in enumerate(sizes):
        for y in range(h):
            for x in range(w):
                d.s_pix_data.value = ((n + x + 3 * y) & 0xFF) << 8  # 16-bit sample, Mono8 = [15:8]
                d.s_pix_sof.value = int(x == 0 and y == 0)
                d.s_pix_eol.value = int(x == w - 1)
                d.s_pix_eof.value = int(x == w - 1 and y == h - 1)
                d.s_pix_valid.value = 1
                while True:
                    await RisingEdge(d.app_clk)
                    if int(d.s_pix_ready.value):
                        break
        if n + 1 < len(sizes):
            d.s_meta_xsize.value, d.s_meta_ysize.value = sizes[n + 1]
    d.s_pix_valid.value = 0
    await wait_images(dut, host, len(sizes))
    await tx_cycles(dut, 3000)                 # the last image's tail
    bad = []
    for n, ((w, h), im) in enumerate(zip(sizes, host.images)):
        if (im.meta.xsize, im.meta.ysize) != (w, h):
            bad.append(f"frame {n}: header {im.meta.xsize}x{im.meta.ysize}, sent {w}x{h}")
            continue
        want = [[(n + x + 3 * y) & 0xFF for x in range(w)] for y in range(h)]
        if im.pixels(8) != want:
            bad.append(f"frame {n}: pixels {im.pixels(8)} sent {want}")
    assert not bad, bad



# -----------------------------------------------------------------------------
# Downlink helpers for the transmit-scheduler tests
# -----------------------------------------------------------------------------
TRIG_ACK_TIMEOUT = 800          # wrapper parameter: tx_clk cycles without an I/O ack
K28_6_WORD = gp.rep4(gp.K28_6)
K27_7_WORD = gp.rep4(gp.K27_7)
K29_7_WORD = gp.rep4(gp.K29_7)


def _itop(dut):
    return dut.cxp_device_top_i.cxp_interface_top_i


async def wait_packet_start(dut, ptype: int, limit: int = 100000):
    """Return at the tx_clk cycle after a SOP followed by a TYPE word of
    `ptype` has been on the wire."""
    prev_sop = False
    for _ in range(limit):
        await RisingEdge(dut.tx_clk)
        await ReadOnly()
        w, k = int(dut.cxp_if_data.value), int(dut.cxp_if_kmask.value)
        if prev_sop and k == 0 and w == gp.rep4(ptype):
            await RisingEdge(dut.tx_clk)
            return
        prev_sop = (k == 0xF and w == K27_7_WORD)
    raise AssertionError(f"no packet of type 0x{ptype:02x} in {limit} cycles")


async def wait_count(dut, get, n: int, limit: int):
    for _ in range(limit):
        if get() >= n:
            return True
        await RisingEdge(dut.tx_clk)
    return get() >= n


def packet_type_at(raw, idx: int):
    """TYPE of the long packet word `idx` of a raw capture lies in, or None."""
    for j in range(min(idx, len(raw)) - 1, -1, -1):
        w, k = raw[j]
        if k == 0xF and w == K29_7_WORD:
            return None
        if k == 0xF and w == K27_7_WORD:
            return raw[j + 1][0] & 0xFF if j + 1 < len(raw) else None
    return None


def in_long_packet(raw, idx: int) -> bool:
    """True when word `idx` of a raw capture lies inside a long packet."""
    return packet_type_at(raw, idx) is not None


# -----------------------------------------------------------------------------
# TC 27 — I/O Acknowledgment Latency Under Stream
# -----------------------------------------------------------------------------
@cxp_test()
async def test_27_ioack_latency_under_stream(dut):
    """An I/O acknowledgment is inserted into a stream packet, not queued.

    §8.2.4, Table 13 (the I/O acknowledgment has priority 1 and is
    inserted at a word boundary), §8.3.3 (the host's timeout is one
    low-speed character).

    Stimulus: `setup`; StreamPacketSizeMax = 832 (200-word packets, back
              to back); cfg_run = 1; 200 host triggers, rising and
              falling in turn, each 0..250 tx_clk cycles after the
              previous one has left the uplink.
    Checks:   one I/O acknowledgment (code 0x01) per trigger; each
              K28.6 leader on the wire at most 6 words after the tx-side
              request; at least 20 requests fell inside a stream packet
              (the test pattern fills about one word in five); the stream
              reassembles without error.
    """
    dut.TESTCASE.value = 27
    host = await setup(dut)
    host.raw = []
    req_at = []

    async def watch_req():
        prev = 0
        while True:
            await RisingEdge(dut.tx_clk)
            await ReadOnly()
            v = int(_itop(dut).trig_pkt_rcvd_tx.value)
            if v and not prev:
                req_at.append(len(host.raw))
            prev = v

    await host.write_ok(A_STR_PKT_MAX, 4 * (200 + 8))
    dut.cfg_run.value = 1
    await wait_images(dut, host, 1)
    w = cocotb.start_soon(watch_req())
    rng = random.Random(27)
    n = 200
    for i in range(n):
        await host.trigger(rising=(i % 2 == 0))
        await tx_cycles(dut, rng.randint(0, 250))
    await tx_cycles(dut, 3000)
    w.cancel()
    leaders = [i for i, (d, k) in enumerate(host.raw) if k == 0xF and d == K28_6_WORD]
    assert len(req_at) == n, f"{len(req_at)} I/O-ack requests"
    assert host.ioacks == [gp.IOACK_OK] * n, host.ioacks
    late = [(r, l) for r, l in zip(req_at, leaders) if l - r > 6]
    assert not late, f"{len(late)} of {n} acknowledgments late, (request, leader) {late[:4]}"
    inside = sum(in_long_packet(host.raw, r) for r in req_at)
    assert inside >= 20, f"only {inside} requests inside a packet"
    assert not host.reasm.errors, host.reasm.errors[:4]


# -----------------------------------------------------------------------------
# TC 28 — TestMode Against The Stream
# -----------------------------------------------------------------------------
@cxp_test()
async def test_28_testmode_vs_stream(dut):
    """TestMode entry and exit never cut a packet.

    §8.7.4: in TestMode the device sends only connection-test and control
    packets; what is already on the wire completes.

    Stimulus: `setup`; StreamPacketSizeMax = 832; cfg_run = 1; rounds of:
              0..1200 cycles after a stream packet's TYPE word, write
              TestMode = 1; 0..1040 cycles after a test packet's TYPE
              word, write TestMode = 0; wait for one more image — until
              five rounds had TestMode take effect inside a stream packet
              (at most 40 rounds).
    Checks:   five such rounds; no framing error on the downlink; each
              write acknowledged once with 0x01 and nothing more; every
              stream packet reassembles (CRC, tag, DsizeP); a test packet
              in every round.
    """
    dut.TESTCASE.value = 28
    host = await setup(dut)
    host.raw = []
    await host.write_ok(A_STR_PKT_MAX, 4 * (200 + 8))
    dut.cfg_run.value = 1
    await wait_images(dut, host, 1)
    sup = _itop(dut).cxp_tx_domain_i.linktest_suppress_traffic
    rng = random.Random(28)
    inside = rounds = 0
    while inside < 5 and rounds < 40:
        rounds += 1
        await wait_packet_start(dut, gp.TYPE_STREAM)
        await tx_cycles(dut, rng.randint(0, 1200))
        lt0 = len(host.linktests)
        wr = cocotb.start_soon(host.write_ok(A_TEST_MODE, 1))
        for _ in range(20000):
            await RisingEdge(dut.tx_clk)
            await ReadOnly()
            if int(sup.value):
                break
        inside += packet_type_at(host.raw, len(host.raw)) == gp.TYPE_STREAM
        await wr
        assert await wait_count(dut, lambda: len(host.linktests), lt0 + 1, 20000), \
            f"round {rounds}: no test packet"
        await wait_packet_start(dut, gp.TYPE_LINKTEST)
        await tx_cycles(dut, rng.randint(0, 1040))
        await host.write_ok(A_TEST_MODE, 0)
        await wait_images(dut, host, len(host.images) + 1)
        assert not host.acks, f"round {rounds}: extra acknowledgment {host.acks}"
    assert inside >= 5, f"TestMode landed inside a stream packet {inside} times in {rounds} rounds"
    assert not host.deframer.errors, host.deframer.errors[:4]
    assert not host.reasm.errors, host.reasm.errors[:4]


# -----------------------------------------------------------------------------
# TC 29 — I/O Acknowledgment In TestMode
# -----------------------------------------------------------------------------
@cxp_test()
async def test_29_ioack_in_testmode(dut):
    """Host triggers in TestMode are acknowledged at once, none on exit.

    §8.3.3 (every trigger packet is acknowledged; no TestMode exemption),
    §8.7.4 (TestMode restricts data packets).

    Stimulus: `setup`; TestMode = 1; after the first test packet, four
              host triggers, each once the previous one is acknowledged;
              TestMode = 0; 3000 tx_clk cycles.
    Checks:   each trigger acknowledged (0x01) within 3000 tx_clk cycles
              while TestMode is 1; exactly four acknowledgments in all.
    """
    dut.TESTCASE.value = 29
    host = await setup(dut)
    await host.write_ok(A_TEST_MODE, 1)
    assert await wait_count(dut, lambda: len(host.linktests), 1, 20000), "no test packet"
    for i in range(4):
        await host.trigger(rising=(i % 2 == 0))
        assert await wait_count(dut, lambda: len(host.ioacks), i + 1, 3000), \
            f"trigger {i} not acknowledged in TestMode"
    await host.write_ok(A_TEST_MODE, 0)
    await tx_cycles(dut, 3000)
    assert host.ioacks == [gp.IOACK_OK] * 4, host.ioacks


# -----------------------------------------------------------------------------
# TC 30 — Device Trigger Acknowledgment Rules
# -----------------------------------------------------------------------------
# An ack leaving the uplink pin reaches the tx side within this many
# downlink words: one uplink character is 50 tx words here (OS 4, 10 ns
# rx_clk, 8 ns tx_clk) and the word is decoded a character or two after
# its last bit.
ACK_RX_WORDS = 200


def check_trigger_pacing(host: Host, first: int, timeout: int) -> list:
    """Every device trigger packet after the first waits for an
    acknowledgment or for the timeout (§8.3.3).  An acknowledgment counts
    if it reached the device after the previous packet: it left the
    uplink pin at most ACK_RX_WORDS before it (Table 17 names no packet,
    so a late one releases the next packet)."""
    bad = []
    t = host.trig_times
    for k in range(first, len(t) - 1):
        acked = any(t[k] - ACK_RX_WORDS < a <= t[k + 1] for a in host.trig_ack_words)
        if not acked and t[k + 1] - t[k] < timeout - 2:
            bad.append(f"packets {k}, {k + 1} at words {t[k]}, {t[k + 1]}")
    return bad


@cxp_test()
async def test_30_tx_trigger_ack_rules(dut):
    """Device triggers follow the §8.3.3 transmitter rules.

    §8.3.3: after a trigger packet the device sends no new one until the
    host has acknowledged it or a timeout has passed; §8.3.2 the host's
    trigger level follows the device's.

    Stimulus: `setup`; the pin toggled every 5 tx_clk cycles for 3000
              cycles against a host that (a) acknowledges each trigger
              packet, (b) acknowledges none, (c) acknowledges each
              TRIG_ACK_TIMEOUT + 30 % later; after each phase the pin
              is held and the wire runs 3 x TRIG_ACK_TIMEOUT cycles.
    Checks:   no trigger packet follows another before an acknowledgment
              has reached the device between them (left the uplink at
              most ACK_RX_WORDS before the first) or TRIG_ACK_TIMEOUT
              cycles have passed; after each phase the host's trigger
              level equals the pin; every leader is followed by its Delay
              word.
    """
    dut.TESTCASE.value = 30
    host = await setup(dut)
    host.raw = []
    late = (TRIG_ACK_TIMEOUT * TX_NS * 13) // (10 * RX_NS)
    for mode, delay in (("ack", 0), ("drop", 0), ("ack", late)):
        host.trig_ack, host.trig_ack_delay = mode, delay
        first = len(host.trig_times)
        for _ in range(600):
            await tx_cycles(dut, 5)
            dut.trig_in.value = 1 - int(dut.trig_in.value)
        await tx_cycles(dut, 3 * TRIG_ACK_TIMEOUT + 2 * late)
        bad = check_trigger_pacing(host, first, TRIG_ACK_TIMEOUT)
        assert not bad, f"{mode}/{delay}: {len(bad)} unpaced, first {bad[:3]}"
        assert host.trig_level == int(dut.trig_in.value), \
            f"{mode}/{delay}: host level {host.trig_level}, pin {int(dut.trig_in.value)}"
    assert not split_short_packets(host.raw)


# -----------------------------------------------------------------------------
# TC 31 — Nested Insertion
# -----------------------------------------------------------------------------
@cxp_test()
async def test_31_nested_preempt(dut):
    """A trigger and an I/O acknowledgment close together are both whole.

    §8.2.4, Table 13: both are inserted into the stream packet at word
    boundaries, neither splits the other, the stream packet resumes.

    Stimulus: `setup` with the host acknowledging device triggers;
              StreamPacketSizeMax = 832; cfg_run = 1; the tx_clk latency L
              from a host trigger leaving the uplink to its tx-side
              request is measured once; then for d = -6 .. +6: after a
              stream TYPE word plus 20 cycles, a host trigger, and the
              device pin toggled L + d cycles after it left the uplink.
    Checks:   14 I/O acknowledgments (one for the calibration trigger)
              and 13 device trigger packets; no torn short packet;
              the stream reassembles; the leader orders seen include
              both "trigger first" and "acknowledgment first".
    """
    dut.TESTCASE.value = 31
    host = await setup(dut)
    host.trig_ack = "ack"
    host.raw = []
    await host.write_ok(A_STR_PKT_MAX, 4 * (200 + 8))
    dut.cfg_run.value = 1
    await wait_images(dut, host, 1)
    req = _itop(dut).trig_pkt_rcvd_tx

    async def cycles_to_req(limit=5000):
        for n in range(limit):
            await RisingEdge(dut.tx_clk)
            await ReadOnly()
            if int(req.value):
                return n
        raise AssertionError("no I/O-ack request")

    await host.trigger(rising=True)
    lat = await cycles_to_req()
    await tx_cycles(dut, 500)
    orders = set()
    for i, d in enumerate(range(-6, 7)):
        await wait_packet_start(dut, gp.TYPE_STREAM)
        await tx_cycles(dut, 20)
        n_trig, n_ack = len(host.triggers), len(host.ioacks)
        await host.trigger(rising=(i % 2 == 1))
        await tx_cycles(dut, max(0, lat + d))
        dut.trig_in.value = 1 - int(dut.trig_in.value)
        assert await wait_count(dut, lambda: len(host.ioacks), n_ack + 1, 3000), f"d={d}: no I/O ack"
        assert await wait_count(dut, lambda: len(host.triggers), n_trig + 1, 3000), f"d={d}: no trigger"
        ti = max(i for i, (w, k) in enumerate(host.raw) if k == 0xF and w in (gp.rep4(gp.K28_2), gp.rep4(gp.K28_4)))
        ai = max(i for i, (w, k) in enumerate(host.raw) if k == 0xF and w == K28_6_WORD)
        orders.add("trigger first" if ti < ai else "ack first")
        await tx_cycles(dut, 2 * TRIG_ACK_TIMEOUT)
    await tx_cycles(dut, 3000)
    torn = split_short_packets(host.raw)
    assert not torn, torn[:3]
    assert len(host.ioacks) == 14, host.ioacks
    assert len(host.triggers) == 13, host.triggers
    assert not host.reasm.errors, host.reasm.errors[:4]
    assert orders == {"trigger first", "ack first"}, orders


# -----------------------------------------------------------------------------
# TC 32 — Device Trigger Waits For The Link
# -----------------------------------------------------------------------------
@cxp_test()
async def test_32_trigger_waits_for_link(dut):
    """No device trigger packet while no host is connected.

    §8.3.2: both sides de-assert the trigger as part of link discovery;
    before the link is up there is nobody to acknowledge a trigger.

    Stimulus: `setup` with the host not started (uplink held low); the
              pin toggled 0 -> 1 -> 0 -> 1, 400 tx_clk cycles apart;
              3000 cycles; then the host starts and brings the link up;
              3000 cycles.
    Checks:   no trigger leader on the wire while the link is down; one
              K28.4 packet after it came up.
    """
    dut.TESTCASE.value = 32
    host = await setup(dut, start_host=False)
    seen = []

    async def watch():
        while True:
            await RisingEdge(dut.tx_clk)
            await ReadOnly()
            w, k = int(dut.cxp_if_data.value), int(dut.cxp_if_kmask.value)
            if k == 0xF and w in (gp.rep4(gp.K28_2), gp.rep4(gp.K28_4)):
                seen.append((w & 0xFF, int(dut.sb_link_detected.value)))

    wt = cocotb.start_soon(watch())
    for v in (1, 0, 1):
        await tx_cycles(dut, 400)
        dut.trig_in.value = v
    await tx_cycles(dut, 3000)
    assert not seen, f"trigger packets with the link down: {seen}"
    wt.cancel()
    host.trig_ack = "ack"
    host.start()
    at_teardown(lambda: check_downlink(host))
    await host.link_up()
    await tx_cycles(dut, 3000)
    assert [r for r, _ in host.triggers] == [True], host.triggers


async def count_rises(dut, sig, box):
    prev = 0
    while True:
        await RisingEdge(dut.rx_clk)
        v = int(sig.value)
        box["n"] += int(v and not prev)
        prev = v


# -----------------------------------------------------------------------------
# TC 33 — Trigger Inside A Host Test Packet
# -----------------------------------------------------------------------------
@cxp_test()
async def test_33_trigger_in_linktest_packet(dut):
    """A Table 15 trigger inside a host test packet leaves the test clean.

    §8.2.4: the low-speed trigger may be inserted at any character
    boundary, also inside a packet; §8.7.3: the device counts the host's
    test packets and their word errors.

    Stimulus: `setup`; for TestMode 0 then 1: read TestErrorCount and
              TestPacketCountRx; for phase p = 1..3 one host test packet
              (Table 23) with a Table 15 trigger (Delay 60) inserted p
              characters into payload words 1, 512 and 1023, the nine
              alternating rising / falling from a rising one (a repeated
              edge is a resend, §8.3.3); a falling trigger in the IDLE
              after; 40 IDLE; read both counters again.
    Checks:   per mode: TestErrorCount unchanged, TestPacketCountRx + 3,
              5 rises of `trig_out`, 10 I/O acknowledgments 0x01.
    """
    dut.TESTCASE.value = 33
    host = await setup(dut)
    rises = {"n": 0}
    cocotb.start_soon(count_rises(dut, dut.trig_out, rises))

    async def counters():
        err = await host.read1(A_TEST_ERR_CNT)
        code, words = await host.read(A_TEST_PKT_RX, 2)
        assert code == gp.ACK_OK_DATA
        return err, (words[0] << 32) | words[1]

    for mode in (0, 1):
        await host.write_ok(A_TEST_MODE, mode)
        err0, pkt0 = await counters()
        rises["n"], host.ioacks = 0, []
        for p in (1, 2, 3):
            chars = gp.beats_to_chars(gp.linktest_packet())
            for i, w in ((2, 1023), (1, 512), (0, 1)):
                rise = (3 * (p - 1) + i) % 2 == 0
                chars = gp.insert_chars(chars, 4 * (2 + w) + p, gp.trigger_ls_chars(rise, 60))
            await host.send(chars)
        host.up.insert(gp.trigger_ls_chars(False, 60))
        await host.up.idle(40)
        n_rise, n_ack = rises["n"], list(host.ioacks)
        err1, pkt1 = await counters()
        assert err1 == err0, f"TestMode {mode}: TestErrorCount {err0} -> {err1}"
        assert pkt1 == pkt0 + 3, f"TestMode {mode}: TestPacketCountRx {pkt0} -> {pkt1}"
        assert n_rise == 5, f"TestMode {mode}: trig_out rose {n_rise} times for 5 rising triggers"
        assert n_ack == [gp.IOACK_OK] * 10, f"TestMode {mode}: I/O acks {n_ack}"
    await host.write_ok(A_TEST_MODE, 0)


# -----------------------------------------------------------------------------
# TC 34 — Uplink Lost In The Middle Of A Command
# -----------------------------------------------------------------------------
@cxp_test()
async def test_34_lock_loss_mid_cmd(dut):
    """A command cut by a line drop is never executed, and the link comes
    back for the next one.

    §10.2: after loss of lock the receiver re-establishes character and
    word alignment; §8.6.1: a command that did not arrive whole is not
    executed.

    Stimulus: `setup`; read Width; a write of Width ^ 8 cut after k of
              its words (k = 1 .. all but the trailer): the host stops,
              the line is held low for 200 words plus 3 bit times, the
              host starts again; 40 IDLE.
    Checks:   per k: at most one acknowledgment for the cut write and
              none of them 0x01; a read of Width is answered 0x00 with
              the original value.
    """
    dut.TESTCASE.value = 34
    host = await setup(dut)
    orig = await host.read1(A_WIDTH)
    cmd = gp.ctrl_cmd(gp.OP_WRITE, A_WIDTH, data=[orig ^ 8], q=DEVICE)
    for k in range(1, len(cmd)):
        host.acks.clear()
        await host.send(cmd[:k])
        host.up.stop()
        dut.rx_serial.value = 0
        for _ in range(200 * 40 * OS_RATIO + 3 * OS_RATIO):
            await RisingEdge(dut.rx_clk)
        host.up.start()
        await host.link_up()
        await host.up.idle(40)
        cut = [a.code for a in host.acks]
        host.acks.clear()
        assert len(cut) <= 1 and gp.ACK_OK_WRITE not in cut, f"k={k}: acks {cut}"
        code, data = await host.read(A_WIDTH)
        assert code == gp.ACK_OK_DATA and data == [orig], (
            f"k={k}: read Width 0x{code:02x} {data}, expected {orig}")


# -----------------------------------------------------------------------------
# TC 35 — Trigger Inside A Control Write
# -----------------------------------------------------------------------------
@cxp_test()
async def test_35_trigger_in_ctrl_write(dut):
    """A low-speed trigger inside a write leaves the write intact.

    §8.2.4 / §8.3.2.1: the host may start a Table 15 trigger at any
    character boundary, also inside a command it is sending; §8.3.3: the
    device acknowledges it.

    Stimulus: `setup`; for p = 0..3: a write of Width = 16 + 4 p with a
              trigger (Delay 30), rising for even p and falling for odd,
              inserted p characters into the data word; read Width back.
    Checks:   each write acknowledged 0x01; each read returns the value
              written; 2 rises of `trig_out`; 4 I/O acknowledgments 0x01.
    """
    dut.TESTCASE.value = 35
    host = await setup(dut)
    rises = {"n": 0}
    cocotb.start_soon(count_rises(dut, dut.trig_out, rises))
    host.ioacks = []
    for p in range(4):
        v = 16 + 4 * p
        cmd = gp.beats_to_chars(gp.ctrl_cmd(gp.OP_WRITE, A_WIDTH, data=[v], q=DEVICE))
        cmd = gp.insert_chars(cmd, 4 * 4 + p, gp.trigger_ls_chars(p % 2 == 0, 30))
        ack = await host.command(cmd)
        assert ack.code == gp.ACK_OK_WRITE, f"p={p}: write ack 0x{ack.code:02x}"
        assert await host.read1(A_WIDTH) == v, f"p={p}: Width not written"
    await host.up.idle(10)
    assert rises["n"] == 2, f"trig_out rose {rises['n']} times for 2 rising triggers"
    assert host.ioacks == [gp.IOACK_OK] * 4, f"I/O acks {host.ioacks}"



# -----------------------------------------------------------------------------
# TC 36 — Sensor Metadata Of Tiny Images
# -----------------------------------------------------------------------------
@cxp_test()
async def test_36_sensor_meta_tiny_images(dut):
    """An image header carries its own image's metadata, however short the
    image.

    Table 38: the header describes the image it opens.  A sensor presents
    the next image's metadata once the last pixel of this one is taken;
    an image of a few pixels is still in the pixel pipeline then, so the
    metadata has to be taken where the image's first pixel is.

    Stimulus: `setup` with cfg_use_tpg = 0; StreamPacketSizeMax 40;
              AcquisitionStart; images 1x1, 2x1, 4x1, 3x2, 1x1, 8x1, 2x2,
              5x1 back to back with no gap, each with its own SourceTag;
              the metadata switched to the next image's in the cycle its
              last pixel is taken (the handshake read settled, before the
              edge that takes it).
    Checks:   8 images; each header's Xsize x Ysize and SourceTag are its
              image's; every line holds its pixels.
    """
    dut.TESTCASE.value = 36
    host = await setup(dut)
    dut.cfg_use_tpg.value = 0
    await host.write_ok(A_STR_PKT_MAX, 40)
    await host.write_ok(A_ACQ_START, 1)
    sizes = [(1, 1), (2, 1), (4, 1), (3, 2), (1, 1), (8, 1), (2, 2), (5, 1)]
    d = dut

    def meta(n):
        w, h = sizes[n]
        d.s_meta_xsize.value, d.s_meta_ysize.value = w, h
        d.s_meta_sourcetag.value = 0x100 + n

    meta(0)
    for n, (w, h) in enumerate(sizes):
        for y in range(h):
            for x in range(w):
                d.s_pix_data.value = ((7 * n + x + 3 * y) & 0xFF) << 8  # 16-bit sample, Mono8 = [15:8]
                d.s_pix_sof.value = int(x == 0 and y == 0)
                d.s_pix_eol.value = int(x == w - 1)
                d.s_pix_eof.value = int(x == w - 1 and y == h - 1)
                d.s_pix_valid.value = 1
                while True:
                    await ReadOnly()
                    taken = int(d.s_pix_ready.value)
                    await RisingEdge(d.app_clk)
                    if taken:
                        break
        if n + 1 < len(sizes):
            meta(n + 1)
    d.s_pix_valid.value = 0
    await wait_images(dut, host, len(sizes))
    await tx_cycles(dut, 3000)
    bad = []
    for n, ((w, h), im) in enumerate(zip(sizes, host.images)):
        if (im.meta.xsize, im.meta.ysize, im.meta.sourcetag) != (w, h, 0x100 + n):
            bad.append(f"image {n}: header {im.meta.xsize}x{im.meta.ysize} "
                       f"tag 0x{im.meta.sourcetag:x}, sent {w}x{h} tag 0x{0x100 + n:x}")
            continue
        want = [[(7 * n + x + 3 * y) & 0xFF for x in range(w)] for y in range(h)]
        if im.pixels(8) != want:
            bad.append(f"image {n}: pixels {im.pixels(8)} sent {want}")
    assert not bad, bad


# -----------------------------------------------------------------------------
# TC 37 — All Three Resets At Once Under A Live Host
# -----------------------------------------------------------------------------
@cxp_test()
async def test_37_all_resets_at_once(dut):
    """A full device reset mid-stream with a read pending.

    §10.2 / §10.3.28: after a reset the device is as after power-up; the
    host is not restarted and keeps sending IDLE, so the uplink has to
    re-align on it alone.  §8.5.3: the stream restarts at tag 0.

    Stimulus: `setup`; 16 x 8 images, 64-word packets, cfg_run = 1; per
              point: a read of Standard sent without waiting; rx_rst_n,
              tx_rst_n and app_rst_n pulsed together for 20 of their
              cycles, 40 to 1100 rx_clk cycles into the command, 0 to
              400 after its last word (executing, acknowledgment framed,
              sent), or 0 to 6 tx_clk cycles into the acknowledgment on
              the wire; 3000 tx_clk
              cycles with only IDLE from the host.
    Checks:   at the reset a stream packet was on the wire at least once;
              afterwards at most one acknowledgment, and only the read's
              own (0x00, Standard); no I/O ack, trigger, test or stream
              packet while StreamPacketSizeMax reads 0; the link comes up
              without host action; Standard and StreamPacketSizeMax read
              back; after StreamPacketSizeMax is written the first packet
              has tag 0 and every image is whole (no word from before the
              reset replayed).
    """
    dut.TESTCASE.value = 37
    host = await setup(dut)
    host.raw = []
    cut_packet = 0
    points = [("in", 40), ("in", 250), ("in", 600), ("in", 1100),
              ("after", 0), ("after", 20), ("after", 60), ("after", 150),
              ("after", 400), ("ack", 0), ("ack", 3), ("ack", 6)]
    for where, lag in points:
        await host.write_ok(A_WIDTH, 16)
        await host.write_ok(A_HEIGHT, 8)
        await host.write_ok(A_STR_PKT_MAX, SPSM_64)
        dut.cfg_run.value = 1
        await wait_images(dut, host, 1)
        await wait_packet_start(dut, gp.TYPE_STREAM)
        host.acks.clear()
        rd = cocotb.start_soon(host.send(gp.ctrl_cmd(gp.OP_READ, A_STANDARD, 4, q=host.q)))
        if where != "in":
            await rd
        if where == "ack":
            await wait_packet_start(dut, gp.TYPE_CTRL_ACK)
            await tx_cycles(dut, lag)
        else:
            for _ in range(lag):
                await RisingEdge(dut.rx_clk)
        cut_packet += in_long_packet(host.raw, len(host.raw) - 1)
        await Combine(
            cocotb.start_soon(pulse_reset(dut.rx_rst_n, dut.rx_clk, cycles=20)),
            cocotb.start_soon(pulse_reset(dut.tx_rst_n, dut.tx_clk, cycles=20)),
            cocotb.start_soon(pulse_reset(dut.app_rst_n, dut.app_clk, cycles=20)))
        host.deframer = gp.Deframer()
        fresh_stream(host)
        host.ioacks.clear()
        host.triggers.clear()
        n_lt = len(host.linktests)
        await rd
        await tx_cycles(dut, 3000)
        acks = list(host.acks)
        dut._log.info("%s %d: packet cut %d, acks %s", where, lag, cut_packet, [hex(a.code) for a in acks])
        host.acks.clear()
        stray = [f"ack 0x{a.code:02x} {a.data}" for a in acks
                 if a.code != gp.ACK_OK_DATA or a.data != [0xC0A79AE5]]
        stray += [f"{len(acks)} acks"] if len(acks) > 1 else []
        stray += [f"I/O ack {c}" for c in host.ioacks] + [f"trigger {t}" for t in host.triggers]
        stray += [f"{len(host.linktests) - n_lt} test packets"] if len(host.linktests) > n_lt else []
        stray += [f"{host.stream_packets} stream packets"] if host.stream_packets else []
        assert not stray, f"{where} {lag}: after the reset: {stray}"
        await host.link_up()
        assert await host.read1(A_STANDARD) == 0xC0A79AE5
        assert await host.read1(A_STR_PKT_MAX) == 0
        await host.write_ok(A_WIDTH, 16)
        await host.write_ok(A_HEIGHT, 8)
        await rediscover_and_stream(dut, host, whole=True)
        dut.cfg_run.value = 0
        await tx_cycles(dut, 2000)
        host.raw = []
    assert cut_packet, "no reset point fell inside a stream packet"


# -----------------------------------------------------------------------------
# TC 38 — TPG Header Fields From the Registers
# -----------------------------------------------------------------------------
@cxp_test()
async def test_38_tpg_header_from_registers(dut):
    """The TPG image header carries the host's StreamID, TapG and Flags.

    Table 38: StreamID, TapG and Flags describe the image.  The XML offers
    them as Image1StreamID, TapGeometry (Geometry_1X_1Y only) and
    StreamFlags ("stream header flag byte").

    Stimulus: `setup`; StreamPacketSizeMax 288 bytes; Width 12, Height 6;
              Image1StreamID 0x23, StreamFlags 0x02, TapGeometry 1 (refused)
              and 0; cfg_run = 1; four images; StreamFlags 0; two more.
    Checks:   TapGeometry 1 is answered 0x41; images 1 to 3 carry StreamID
              0x23, TapG 0, Flags 0x02; the last image Flags 0; the golden
              reassembler reports no CRC, tag or DsizeP error.
    """
    dut.TESTCASE.value = 38
    host = await setup(dut)
    await host.write_ok(A_STR_PKT_MAX, 4 * (64 + 8))
    await host.write_ok(A_WIDTH, 12)
    await host.write_ok(A_HEIGHT, 6)
    await host.write_ok(A_STREAM_ID, 0x23)
    await host.write_ok(A_STREAM_FLAGS, 0x02)
    assert await host.write(A_TAP_GEOMETRY, 1) == 0x41
    await host.write_ok(A_TAP_GEOMETRY, 0)
    dut.cfg_run.value = 1
    await wait_images(dut, host, 4)
    fields = [(im.meta.streamid, im.meta.tapg, im.meta.flags) for im in host.images[1:4]]
    assert fields == [(0x23, 0, 0x02)] * 3, fields
    await host.write_ok(A_STREAM_FLAGS, 0)
    n = len(host.images)
    await wait_images(dut, host, n + 2)
    assert host.images[-1].meta.flags == 0, host.images[-1].meta
    assert not host.reasm.errors, host.reasm.errors[:3]
    assert host.reasm.crc_errors == 0


# -----------------------------------------------------------------------------
# TC 39 — Partial User-Window Writes And PFNC PixelFormat
# -----------------------------------------------------------------------------
@cxp_test()
async def test_39_pstrb_and_pfnc(dut):
    """A write of B bytes writes B bytes; PixelFormat speaks PFNC.

    Table 21: Size is the number of bytes written; §10.3 the space is
    byte-addressed.  APB3 has no byte enables, so the IP drives PSTRB
    (APB4) and the bench slave honours it.  §11.2.1.6: the PixelFormat
    feature's value is the GenICam PFNC one; the device maps it to the
    PixelF code it sends.

    Stimulus: `setup`; writes of Size 1 (0x5A) at USER + 4, Size 2
              (0x1122) at USER + 8 and Size 3 (0x334455) at USER + 12;
              reads of the three words.  PixelFormat 0x0102 (a PixelF
              code), then 0x01100003 (PFNC Mono10); StreamPacketSizeMax
              288; Width 8, Height 2; cfg_run = 1; two images.
    Checks:   the words read 0x5A000001, 0x11220002 and 0x33445503 (the
              bench slave powers up at 0xA50000nn); the PixelF code is
              refused 0x41 and PixelFormat still reads PFNC Mono8; PFNC
              Mono10 is taken and reads back; every image header carries
              PixelF 0x0102.
    """
    dut.TESTCASE.value = 39
    host = await setup(dut)
    for off, size, value in ((4, 1, 0x5A00_0000), (8, 2, 0x1122_0000), (12, 3, 0x3344_5500)):
        await host.send(gp.ctrl_cmd(gp.OP_WRITE, USER + off, size, [value], q=host.q))
        a = await host.wait_ack(timeout_words=4000)
        assert a.code == gp.ACK_OK_WRITE, f"Size {size} write: 0x{a.code:02x}"
    words = [await host.read1(USER + off) for off in (4, 8, 12)]
    assert words == [0x5A00_0001, 0x1122_0002, 0x3344_5503], [hex(w) for w in words]

    assert await host.write(grm.PIXEL_FORMAT_ALIAS, 0x0102) == gp.ACK_BAD_DATA
    assert await host.read1(grm.PIXEL_FORMAT_ALIAS) == 0x0108_0001
    await host.write_ok(grm.PIXEL_FORMAT_ALIAS, 0x0110_0003)
    assert await host.read1(grm.PIXEL_FORMAT_ALIAS) == 0x0110_0003
    await host.write_ok(A_STR_PKT_MAX, 4 * (64 + 8))
    await host.write_ok(A_WIDTH, 8)
    await host.write_ok(A_HEIGHT, 2)
    dut.cfg_run.value = 1
    await wait_images(dut, host, 2)
    dut.cfg_run.value = 0
    assert all(im.meta.pixfmt == 0x0102 for im in host.images), (
        [hex(im.meta.pixfmt) for im in host.images])
    assert not host.reasm.errors, host.reasm.errors[:3]
