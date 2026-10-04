"""Cocotb TB for `cxp_ctrl_bootstrap_regs`.

Device-side bootstrap register file for CoaXPress 1.1.1 (JIIA
CXP-001-2015 §10.3, Table 45): a single-cycle register slave serving the
Table 45 map, the GenICam strings, a GenICam XML ROM and the manufacturer
window, and exporting control state and the §10.3.28 ConnectionReset
side-effects on `ctl_*` ports. The TB runs a 10 ns `sys_clk` and accesses
it through `CxpRegBus` (1-cycle `we`/`re` pulses, `rdata` sampled in
ReadOnly); `ctl_*` outputs and the link-0 counter inputs are checked or
driven directly. `reset()` holds `sys_rst_n` low 4 cycles with the NV
DeviceUserID and counters driven from Python. All checks are inline.

The wrapper flattens the unpacked `test_err_count` / `test_pkt_count_*`
array ports to one link (NUM_LINKS = 1), sets LINK_RESET_CLEAR_CYCLES = 8
so ConnectionReset self-clears in a few cycles (CONN_RESET_TIMEOUT = 64),
and drives the local ConnectionReset request and its tx-domain echo from
Python; `reset()` echoes the active level back three cycles later. The
ROM loads the one generated image, `src/rtl/gen/cxp_camera_xml.mem`, by
absolute path (`p_XML_BLOB_MEM`, `src/regmap/regmap.mk`); addresses,
strings and the blob come from the generated `cxp_protocol.regmap`.

Migrated from the v1.0 layout (old §8.3 / Table 47). Besides the v1.0
coverage (string layout, manifest-selector bound, registered read
latency, reset defaults) it covers the v1.1.1 deltas: Revision
0x00010001, the 0x0018/0x001C low-address move, the 0x3000 device-control
block, the re-laid 0x4000 block (ConnectionConfigDefault @0x4018, shifted
test registers, TestPacketCount*, HsUpconnection) and the §10.3.28
ConnectionReset side-effects. No FSM is registered.

Test cases (`dut.TESTCASE` set per case for wave visibility):

  1  Standard magic and Revision 0x00010001 (§10.3.5/6).
  2  XML manifest block, XmlUrlAddress @0x0018, Iidc2Address @0x001C.
  3  XmlManifestSelector rejects values ≥ XmlManifestSize.
  4  GenICam XML served from the ROM at 0x90000000; ROM read-only.
  5  0x3000 slots read their feature's address and refuse writes; the
     features are R/W at that address and drive their ports.
  6  Use-case features survive ConnectionReset.
  7  DeviceConnectionID reads the strap value.
  8  ConnectionReset reads 1 during the clear window, then self-clears.
  9  ConnectionReset side-effect set (§10.3.28).
 10  MasterHostConnectionID R/W.
 11  StreamPacketSizeMax powers up at 0 (§10.3.28) and is R/W.
 12  ControlPacketSizeMax read-only, multiple of 4, ≥ 128 bytes.
 13  ConnectionConfig takes its one supported value + strobe; Default RO.
 14  Shifted v1.1.1 test block, TestPacketCountTx/Rx, HsUpconnection.
 15  ElectricalComplianceTest R/W stub cleared by ConnectionReset.
 16  GenICam strings big-endian, NULL-padded; 0x2090 removed.
 17  DeviceUserID R/W and reloaded from NV on reset.
 18  Writes to read-only addresses ignored.
 19  `ready` is the registered `we | re`.
 20  Every access returns its Table 22 code on `err`; refused writes do
     nothing.
 21  TestErrorCountSelector refuses a selector with no connection.
 22  The ConnectionReset bit stays 1 until the tx domain has echoed it.
 23  Without the echo the bit still clears after the timeout.
 24  A local ConnectionReset request acts as a host write of 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import NextTimeStep, ReadOnly, RisingEdge

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "verif" / "common"))

from cxp_bus import CxpRegBus  # noqa: E402
from cxp_protocol import regmap as gregmap  # noqa: E402
from cxp_testcase import cxp_test  # noqa: E402


# -----------------------------------------------------------------------------
# Spec / RTL constants — kept in lock-step with the parameter defaults
# -----------------------------------------------------------------------------
STANDARD_MAGIC            = 0xC0A7_9AE5
REVISION                  = 0x0001_0001   # §10.3.6 v1.1.1 (was 0x00010000)
XML_MANIFEST_SIZE         = gregmap.XML_MANIFEST_SIZE_VALUE
XML_VERSION               = gregmap.XML_VERSION_VALUE          # genicam.version
XML_SCHEMA_VERSION        = gregmap.XML_SCHEMA_VERSION_VALUE   # schema 1.1.0
XML_URL_ADDRESS           = gregmap.XML_URL_ADDRESS_VALUE
IIDC2_ADDRESS             = 0x0000_0000   # §10.3.12 no IIDC2
CONNECTION_CONFIG_DEFAULT = gregmap.CONNECTION_CONFIG_DEFAULT_VALUE  # §10.3.34 1 conn @ 1.25 G
DEVICE_CONNECTION_ID      = gregmap.DEVICE_CONNECTION_ID_VALUE
CONTROL_PKT_SIZE_MAX      = gregmap.CONTROL_PACKET_SIZE_MAX_VALUE  # §10.3.31 bytes
HS_UPCONNECTION_SUPPORT   = 0x0000_0000   # §10.3.41 not supported

# GenICam XML blob ROM — XmlUrl advertises "Local:cxp_camera.xml;90000000;<size>".
# The ROM loads src/rtl/gen/cxp_camera_xml.mem, generated with the XML; its
# address, size and bytes come from the generated register map, like the RTL's.
XML_BLOB_ADDR             = gregmap.XML_BLOB_ADDR
XML_BLOB_BYTES            = gregmap.XML_BLOB_BYTES

# §10.3.19-27 use-case features.  Each 0x3000 slot is read-only and holds
# the address of its feature in the manufacturer window (the cxp_camera.xml
# <Address>); the feature is a R/W register there.  `vals` are two values the
# feature takes; reset = power-on default; out = exported port name.
#               name          slot     feature      reset  out
G = gregmap
DEVICE_CTRL = [
    ("Width",            G.WIDTH_SLOT,  G.WIDTH_ALIAS,  G.WIDTH_RESET,  "ctl_tpg_width_o"),
    ("Height",           G.HEIGHT_SLOT, G.HEIGHT_ALIAS, G.HEIGHT_RESET, "ctl_tpg_height_o"),
    ("AcquisitionMode",  G.ACQUISITION_MODE_SLOT, G.ACQUISITION_MODE_ALIAS,
     G.ACQUISITION_MODE_RESET, "ctl_acquisition_mode_o"),
    ("AcquisitionStart", G.ACQUISITION_START_SLOT, G.ACQUISITION_START_ALIAS,
     G.ACQUISITION_START_RESET, "ctl_acquisition_start_o"),
    ("AcquisitionStop",  G.ACQUISITION_STOP_SLOT, G.ACQUISITION_STOP_ALIAS,
     G.ACQUISITION_STOP_RESET, "ctl_acquisition_stop_o"),
    ("PixelFormat",      G.PIXEL_FORMAT_SLOT, G.PIXEL_FORMAT_ALIAS,
     G.PIXEL_FORMAT_RESET, "ctl_pixel_format_o"),                     # Mono8
    ("TapGeometry",      G.TAP_GEOMETRY_SLOT, G.TAP_GEOMETRY_ALIAS,
     G.TAP_GEOMETRY_RESET, "ctl_tap_geometry_o"),
    ("Image1StreamID",   G.IMAGE1_STREAM_ID_SLOT, G.IMAGE1_STREAM_ID_ALIAS,
     G.IMAGE1_STREAM_ID_RESET, "ctl_image1_stream_id_o"),             # = TPG StreamID
]

# Device features whose write also pulses a port.
WR_PULSE = {"AcquisitionStart": "ctl_acquisition_start_wr_o",
            "AcquisitionStop":  "ctl_acquisition_stop_wr_o"}


async def _count_high(dut, port: str, box: list) -> None:
    """Count the sys_clk edges at which `port` is high."""
    sig = getattr(dut, port)
    while True:
        await RisingEdge(dut.sys_clk)
        await ReadOnly()
        box[0] += int(sig.value)

# Strings the map serves (big-endian, NULL-padded to field width).
DEV_VENDOR_NAME   = (G.DEVICE_VENDOR_NAME_STR,       G.DEVICE_VENDOR_NAME_LEN)
DEV_MODEL_NAME    = (G.DEVICE_MODEL_NAME_STR,        G.DEVICE_MODEL_NAME_LEN)
DEV_MFR_INFO      = (G.DEVICE_MANUFACTURER_INFO_STR, G.DEVICE_MANUFACTURER_INFO_LEN)
DEV_VERSION_STR   = (G.DEVICE_VERSION_STR,           G.DEVICE_VERSION_LEN)
DEV_SERIAL_NUMBER = (G.DEVICE_SERIAL_NUMBER_STR,     G.DEVICE_SERIAL_NUMBER_LEN)  # §10.3.17

# Address map — CXP-001-2015 Table 45, the generated map's addresses.
A_STANDARD       = G.STANDARD
A_REVISION       = G.REVISION
A_XML_MFST_SIZE  = G.XML_MANIFEST_SIZE
A_XML_MFST_SEL   = G.XML_MANIFEST_SELECTOR
A_XML_VERSION    = G.XML_VERSION
A_XML_SCHEMA     = G.XML_SCHEMA_VERSION
A_XML_URL        = G.XML_URL_ADDRESS
A_IIDC2_ADDR     = G.IIDC2_ADDRESS
A_VENDOR_BASE    = G.DEVICE_VENDOR_NAME
A_MODEL_BASE     = G.DEVICE_MODEL_NAME
A_MFR_INFO_BASE  = G.DEVICE_MANUFACTURER_INFO
A_VERSION_BASE   = G.DEVICE_VERSION
A_SERIAL_BASE    = G.DEVICE_SERIAL_NUMBER
A_USER_ID_BASE   = G.DEVICE_USER_ID
# §10.3.19-27 use-case feature-address block
A_WIDTH_ADDR     = G.WIDTH_SLOT
A_HEIGHT_ADDR    = G.HEIGHT_SLOT
A_ACQ_MODE_ADDR  = G.ACQUISITION_MODE_SLOT
A_ACQ_START_ADDR = G.ACQUISITION_START_SLOT
A_ACQ_STOP_ADDR  = G.ACQUISITION_STOP_SLOT
A_PIXFMT_ADDR    = G.PIXEL_FORMAT_SLOT
A_TAPGEO_ADDR    = G.TAP_GEOMETRY_SLOT
A_IMG1_SID_ADDR  = G.IMAGE1_STREAM_ID_SLOT
# §10.3.28-41 connection / test block
A_CONN_RESET     = G.CONNECTION_RESET
A_DEV_CONN_ID    = G.DEVICE_CONNECTION_ID
A_MST_HOST_ID    = G.MASTER_HOST_CONNECTION_ID
A_CTRL_PKT_SIZE  = G.CONTROL_PACKET_SIZE_MAX
A_STR_PKT_SIZE   = G.STREAM_PACKET_SIZE_MAX
A_CONN_CONFIG    = G.CONNECTION_CONFIG
A_CONN_CFG_DEF   = G.CONNECTION_CONFIG_DEFAULT
A_TEST_MODE      = G.TEST_MODE
A_TEST_ERR_SEL   = G.TEST_ERROR_COUNT_SELECTOR
A_TEST_ERR_CNT   = G.TEST_ERROR_COUNT
A_TEST_PKT_TX_HI = G.TEST_PACKET_COUNT_TX       # 8 bytes, high word first
A_TEST_PKT_TX_LO = G.TEST_PACKET_COUNT_TX + 4
A_TEST_PKT_RX_HI = G.TEST_PACKET_COUNT_RX
A_TEST_PKT_RX_LO = G.TEST_PACKET_COUNT_RX + 4
A_ECOMPL_TEST    = G.ELECTRICAL_COMPLIANCE_TEST
A_HS_UPCONN      = G.HS_UPCONNECTION

CLK_PERIOD_NS = 10


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def string_to_words(s: str, field_width: int) -> list[int]:
    """Pack an ASCII string into NULL-padded big-endian 32-bit words."""
    raw = s.encode("ascii") + b"\x00" * (field_width - len(s))
    assert len(raw) == field_width
    words: list[int] = []
    for i in range(0, field_width, 4):
        w = (raw[i] << 24) | (raw[i + 1] << 16) | (raw[i + 2] << 8) | raw[i + 3]
        words.append(w)
    return words


# -----------------------------------------------------------------------------
# Bring-up
# -----------------------------------------------------------------------------
echo = {"on": True}


async def echo_active(dut):
    """Return ctl_connection_reset_active_o on conn_reset_done three cycles
    later, as the tx domain does through its synchronisers — unless a
    test sets echo["on"] = False."""
    hist = [0, 0, 0]
    while True:
        await RisingEdge(dut.sys_clk)
        hist.append(int(dut.ctl_connection_reset_active_o.value))
        dut.conn_reset_done.value = hist.pop(0) if echo["on"] else 0


async def reset(dut, nv_id: int = 0):
    """Start the clock; hold `sys_rst_n` low 4 cycles, then 2 idle cycles."""
    cocotb.start_soon(Clock(dut.sys_clk, CLK_PERIOD_NS, unit="ns").start())
    dut.device_user_id_nv.value       = nv_id
    dut.test_err_count_link0.value    = 0
    dut.test_pkt_count_tx_link0.value = 0
    dut.test_pkt_count_rx_link0.value = 0
    dut.sys_rst_n.value               = 0
    dut.addr.value                    = 0
    dut.wdata.value                   = 0
    dut.we.value                      = 0
    dut.re.value                      = 0
    # §10.3.28: a local ConnectionReset request, and the echo of the
    # active level that the tx domain returns in the device.
    dut.conn_reset_req.value          = 0
    dut.conn_reset_done.value         = 0
    echo["on"] = True
    cocotb.start_soon(echo_active(dut))
    for _ in range(4):
        await RisingEdge(dut.sys_clk)
    dut.sys_rst_n.value = 1
    for _ in range(2):
        await RisingEdge(dut.sys_clk)


# -----------------------------------------------------------------------------
# TC 1 — Standard Magic And Revision
# -----------------------------------------------------------------------------
@cxp_test()
async def test_01_standard_and_revision(dut):
    """Standard reads the CoaXPress magic and Revision reads v1.1 (0x00010001).

    §10.3.5/§10.3.6: the host identifies a CoaXPress Device and the spec
    version it implements from these two words before anything else.

    Stimulus: after reset, read 0x0000, then 0x0004.
    Checks:   Standard == 0xC0A79AE5; Revision == 0x00010001, with major
              (bits 31:16) == 1 and minor (bits 15:0) == 1.
    """
    dut.TESTCASE.value = 1
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    assert await bus.read(A_STANDARD) == STANDARD_MAGIC, "Standard magic mismatch"
    rev = await bus.read(A_REVISION)
    assert rev == REVISION, f"Revision must be 0x00010001 (v1.1.1), got 0x{rev:08x}"
    assert (rev >> 16) & 0xFFFF == 1, "major revision must be 1"
    assert rev & 0xFFFF == 1, "minor revision must be 1 (v1.1)"


# -----------------------------------------------------------------------------
# TC 2 — XML Block And Iidc2Address
# -----------------------------------------------------------------------------
@cxp_test()
async def test_02_xml_block_and_iidc2(dut):
    """The XML manifest words and Iidc2Address read their Table 45 values.

    §10.3.7–12: v1.1.1 moved XmlUrlAddress from 0x001C to 0x0018 and put
    Iidc2Address (0 = no IIDC2 support) at 0x001C.

    Stimulus: read 0x0008, 0x0010, 0x0014, 0x0018 and 0x001C.
    Checks:   XmlManifestSize == 1, XmlVersion == 0x100 (0.1.0) and
              XmlSchemaVersion == 0x00010100 (1.1.0), both as the XML
              declares; XmlUrlAddress == 0x6000, Iidc2Address == 0.
    Note:     the XmlUrl string at 0x6000 itself is not read by any test.
    """
    dut.TESTCASE.value = 2
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    assert await bus.read(A_XML_MFST_SIZE) == XML_MANIFEST_SIZE
    assert await bus.read(A_XML_VERSION)   == XML_VERSION
    assert await bus.read(A_XML_SCHEMA)    == XML_SCHEMA_VERSION
    assert await bus.read(A_XML_URL)       == XML_URL_ADDRESS, "XmlUrlAddress@0x0018"
    assert await bus.read(A_IIDC2_ADDR)    == IIDC2_ADDRESS, "Iidc2Address@0x001C=0"


# -----------------------------------------------------------------------------
# TC 3 — XmlManifestSelector Bound
# -----------------------------------------------------------------------------
@cxp_test()
async def test_03_xml_selector_bound(dut):
    """XmlManifestSelector drops writes of values ≥ XmlManifestSize.

    §10.3.8: the selector may only address an existing manifest entry.

    Stimulus: read 0x000C; write 0xDEADBEEF; read; write 0; read.
    Checks:   every read returns 0.
    Note:     with XmlManifestSize = 1 the only legal value (0) is also
              the reset value, so the accept branch is not observable.
    """
    dut.TESTCASE.value = 3
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    assert await bus.read(A_XML_MFST_SEL) == 0
    await bus.write(A_XML_MFST_SEL, 0xDEAD_BEEF)
    assert await bus.read(A_XML_MFST_SEL) == 0
    await bus.write(A_XML_MFST_SEL, 0)
    assert await bus.read(A_XML_MFST_SEL) == 0


# -----------------------------------------------------------------------------
# TC 4 — GenICam XML Blob ROM
# -----------------------------------------------------------------------------
@cxp_test()
async def test_04_xml_blob_rom(dut):
    """The GenICam XML named by XmlUrl is served from the ROM at 0x90000000.

    §10.3.11: the XmlUrl string "Local:cxp_camera.xml;90000000;<size>"
    promises the file at that address.  Exercises the full 32-bit blob
    decode, the `$readmemh` image, big-endian word packing and the ROM
    being read-only.

    Stimulus: take the generated XML (`gregmap.xml_blob()`); read ROM
              words 0, 1, 2, 7, n/2, n-2 and n-1 (n = ceil(XML_BLOB_BYTES / 4) words); then
              read 0x9000000C, write 0xDEADBEEF to it and read it again.
    Checks:   the file is `XML_BLOB_BYTES` bytes (lock-step with the map);
              each probed word equals the file's big-endian packing (tail
              zero-padded); words 0–1 start with "<?xml"; the ROM word is
              unchanged by the write; XmlManifestSelector (0x000C, the
              write's low-16 alias) still reads 0.
    Note:     the alias check cannot catch a missing ROM write gate —
              0xDEADBEEF is rejected by the selector's own bound check
              anyway.  Only 7 ROM words are read.
    """
    dut.TESTCASE.value = 4
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    # The generated XML, the bytes the ROM image was built from.
    raw = gregmap.xml_blob()
    assert len(raw) == XML_BLOB_BYTES, (
        f"generated cxp_camera.xml is {len(raw)} B but XML_BLOB_BYTES="
        f"{XML_BLOB_BYTES}; run `make regmap`"
    )
    padded = raw + b"\x00" * ((-len(raw)) % 4)
    nwords = len(padded) // 4

    def want(widx: int) -> int:
        i = widx * 4
        return ((padded[i] << 24) | (padded[i + 1] << 16)
                | (padded[i + 2] << 8) | padded[i + 3])

    # Head proves byte order ("<?xml"); spot-check a midpoint and the
    # zero-padded tail word instead of all of them to keep the run short.
    probe = [0, 1, 2, 7, nwords // 2, nwords - 2, nwords - 1]
    for widx in probe:
        got = await bus.read(XML_BLOB_ADDR + widx * 4)
        assert got == want(widx), (
            f"XML word {widx} @0x{XML_BLOB_ADDR + widx*4:08x}: "
            f"got 0x{got:08x} want 0x{want(widx):08x}"
        )
    head = b"".join(want(w).to_bytes(4, "big") for w in (0, 1))
    assert head.startswith(b"<?xml"), f"XML header wrong: {head!r}"

    # Read-only: a write into the blob range is ignored and must not reach
    # the bootstrap register its low-16 bits alias (here XmlManifestSelector
    # @0x000C — XML_BLOB_ADDR+0xC is inside the XML window).
    alias = XML_BLOB_ADDR + A_XML_MFST_SEL
    before = await bus.read(alias)
    await bus.write(alias, 0xDEAD_BEEF)
    assert await bus.read(alias) == before, "XML ROM is read-only"
    assert await bus.read(A_XML_MFST_SEL) == 0, (
        "blob-range write leaked into XmlManifestSelector"
    )


# -----------------------------------------------------------------------------
# TC 5 — Device-Control Registers
# -----------------------------------------------------------------------------
@cxp_test()
async def test_05_device_control_regs(dut):
    """The 0x3000 slots point at the use-case features (Table 45: R).

    §10.3.19-27: each register "shall provide the address in the
    manufacturer-specific register space" of its feature.

    Stimulus: for each `DEVICE_CTRL` row read the slot; write the slot;
              read the feature; write two values it takes at the feature
              (within the XML's Min / Max or enumeration) and read each
              back; write the values it does not take.
    Checks:   the slot reads the feature address before and after its
              write, which is refused with 0x43; the feature powers up at
              its default; each accepted value reads back and reaches the
              `ctl_*` port; a write of AcquisitionStart / AcquisitionStop
              also pulses its `_wr_o` port for one cycle, and a read of
              either is refused with 0x44 (write-only); a value outside
              the feature's range is refused with 0x41 and changes nothing.
    """
    dut.TESTCASE.value = 5
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)
    vals = {"Width": (1, 4096), "Height": (1, 4096), "AcquisitionMode": (0, 0),
            "AcquisitionStart": (1, 1), "AcquisitionStop": (1, 1),
            "PixelFormat": (0x0110_0003, 0x0110_0025), "TapGeometry": (0, 0),  # PFNC Mono10, Mono14
            "Image1StreamID": (0x00, 0xFF)}
    bad = {"Width": (0, 4097), "Height": (0, 4097), "AcquisitionMode": (1,),
           "PixelFormat": (0x0107, 0x0102), "TapGeometry": (1,),  # PixelF codes are not PFNC
           "Image1StreamID": (0x100,)}

    for name, slot, feat, rst, out in DEVICE_CTRL:
        wo = name in WR_PULSE
        assert await bus.read(slot) == feat, f"{name} slot 0x{slot:04x}"
        await bus.write(slot, 0x1234_5678)
        assert bus.err == 0x43, f"{name} slot write: 0x{bus.err:02x}"
        assert await bus.read(slot) == feat, f"{name} slot changed"
        if wo:
            await bus.read(feat)
            assert bus.err == 0x44, f"{name} read: 0x{bus.err:02x} (write-only)"
        else:
            assert await bus.read(feat) == rst, f"{name} reset @0x{feat:05x}"
        wr = WR_PULSE.get(name)
        for v in vals[name]:
            pulses = [0]
            mon = cocotb.start_soon(_count_high(dut, wr, pulses)) if wr else None
            await bus.write(feat, v)
            assert bus.err == 0x00, f"{name} = 0x{v:x}: 0x{bus.err:02x}"
            if not wo:
                assert await bus.read(feat) == v, f"{name} R/W @0x{feat:05x}"
            await ReadOnly()
            assert int(getattr(dut, out).value) == v, f"{name} -> {out}"
            await RisingEdge(dut.sys_clk)
            if mon:
                mon.cancel()
                assert pulses[0] == 1, f"{name} write: {wr} high {pulses[0]} cycles (1)"
        for v in bad.get(name, ()):
            before = int(getattr(dut, out).value)
            await bus.write(feat, v)
            assert bus.err == 0x41, f"{name} = 0x{v:x}: 0x{bus.err:02x} (0x41)"
            await ReadOnly()
            assert int(getattr(dut, out).value) == before, f"{name} changed by a refused write"
            await RisingEdge(dut.sys_clk)


# -----------------------------------------------------------------------------
# TC 6 — Device-Control Registers Survive ConnectionReset
# -----------------------------------------------------------------------------
@cxp_test()
async def test_06_device_control_survives_connection_reset(dut):
    """ConnectionReset leaves the device-control registers untouched.

    §10.3.28 lists the registers a ConnectionReset clears; the use-case
    features (Width, Height, PixelFormat, …) are not among them.

    Stimulus: write Width = 1920, Height = 1080 and PixelFormat =
              0x01100007 (PFNC Mono16) at their feature addresses; write 1
              to ConnectionReset (0x4000); wait 20 cycles.
    Checks:   the three features still read 1920, 1080 and 0x01100007.
    """
    dut.TESTCASE.value = 6
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    await bus.write(G.WIDTH_ALIAS, 1920)
    await bus.write(G.HEIGHT_ALIAS, 1080)
    await bus.write(G.PIXEL_FORMAT_ALIAS, 0x0110_0007)  # PFNC Mono16

    await bus.write(A_CONN_RESET, 0x1)
    for _ in range(20):
        await RisingEdge(dut.sys_clk)

    assert await bus.read(G.WIDTH_ALIAS) == 1920, "Width survives ConnectionReset"
    assert await bus.read(G.HEIGHT_ALIAS) == 1080, "Height survives"
    assert await bus.read(G.PIXEL_FORMAT_ALIAS) == 0x0110_0007, "PixelFormat survives"


# -----------------------------------------------------------------------------
# TC 7 — DeviceConnectionID Strap
# -----------------------------------------------------------------------------
@cxp_test()
async def test_07_device_connection_id_strap(dut):
    """DeviceConnectionID reads back the strap value.

    §10.3.29: identifies the Device connection (0 = master connection).

    Stimulus: read 0x4004.
    Checks:   the read and `ctl_device_connection_id_o` both equal 0 (the
              default `DEVICE_CONNECTION_ID_STRAP`).
    Note:     with a strap of 0 the result is indistinguishable from an
              unmapped address.
    """
    dut.TESTCASE.value = 7
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    assert await bus.read(A_DEV_CONN_ID) == DEVICE_CONNECTION_ID
    assert int(dut.ctl_device_connection_id_o.value) == DEVICE_CONNECTION_ID


# -----------------------------------------------------------------------------
# TC 8 — ConnectionReset Pulse And Self-Clear
# -----------------------------------------------------------------------------
@cxp_test()
async def test_08_connection_reset_pulse_and_self_clear(dut):
    """ConnectionReset reads 1 while its clear timer runs, then self-clears.

    §10.3.28: a write of 1 to 0x4000 is fire-and-forget and the register
    returns to 0 on its own (LINK_RESET_CLEAR_CYCLES = 8 in the wrapper).

    Stimulus: write 1 to 0x4000; wait one edge; read 0x4000; wait 20
              edges; read 0x4000 again.
    Checks:   `ctl_connection_reset_pulse_o` == 0 before the write and in
              ReadOnly one edge after it (one-cycle strobe); the first
              read has bit 0 == 1; the second read == 0.
    Note:     the pulse is never observed at 1, so a stuck-at-0
              `ctl_connection_reset_pulse_o` passes; the side-effects are
              checked by TC 9, not here.
    """
    dut.TESTCASE.value = 8
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    assert int(dut.ctl_connection_reset_pulse_o.value) == 0

    await bus.write(A_CONN_RESET, 0x1)

    await RisingEdge(dut.sys_clk)
    await ReadOnly()
    assert int(dut.ctl_connection_reset_pulse_o.value) == 0, "pulse is one cycle"

    # Register still 1 while the discovery-config timer counts.
    val = await bus.read(A_CONN_RESET)
    assert val & 0x1 == 1, f"ConnectionReset should still be 1; got {val}"

    for _ in range(20):
        await RisingEdge(dut.sys_clk)

    val = await bus.read(A_CONN_RESET)
    assert val == 0, f"ConnectionReset should have self-cleared, got {val}"


# -----------------------------------------------------------------------------
# TC 9 — ConnectionReset Side-Effects
# -----------------------------------------------------------------------------
@cxp_test()
async def test_09_connection_reset_side_effects(dut):
    """A ConnectionReset write applies the §10.3.28 side-effect set at once.

    §10.3.28: a connection reset zeroes MasterHostConnectionID,
    StreamPacketSizeMax, TestMode, TestErrorCountSelector and the test
    counters, and returns ConnectionConfig to its reset configuration.

    Stimulus: write 0xA5A5A5A5 → MasterHostConnectionID (0x4008), 0x200 →
              StreamPacketSizeMax (0x4010), 1 → TestMode (0x401C), 0 →
              TestErrorCountSelector (0x4020), 0x00020030 →
              ConnectionConfig (0x4014); then write 1 to 0x4000.
    Checks:   all three test-counter clears == 1 in ReadOnly on the ConnectionReset
              write edge; afterwards 0x4008, 0x4010 and 0x401C read 0 and
              0x4014 reads `CONNECTION_CONFIG_DEFAULT` (0x00010028).
    Note:     TestErrorCountSelector is written 0 beforehand and never
              read, so its clear is not proven; the dirtied values are not
              read back before the reset either.
    """
    dut.TESTCASE.value = 9
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    # Dirty the writable state.
    await bus.write(A_MST_HOST_ID, 0xA5A5_A5A5)
    await bus.write(A_STR_PKT_SIZE, 0x0000_0200)
    await bus.write(A_TEST_MODE, 0x0000_0001)
    await bus.write(A_TEST_ERR_SEL, 0x0000_0000)
    await bus.write(A_CONN_CONFIG, 0x0002_0030)

    # Fire the connection reset.
    await bus.write(A_CONN_RESET, 0x1)
    await ReadOnly()
    clrs = (int(dut.ctl_test_err_count_clr_o.value), int(dut.ctl_test_pkt_tx_clr_o.value),
            int(dut.ctl_test_pkt_rx_clr_o.value))
    assert clrs == (1, 1, 1), f"test counters cleared on reset: {clrs}"
    await RisingEdge(dut.sys_clk)

    assert await bus.read(A_MST_HOST_ID)  == 0, "MasterHostConnectionID->0"
    assert await bus.read(A_STR_PKT_SIZE) == 0, "StreamPacketSizeMax->0"
    assert await bus.read(A_TEST_MODE)    == 0, "TestMode->0"
    assert await bus.read(A_CONN_CONFIG)  == CONNECTION_CONFIG_DEFAULT, \
        "ConnectionConfig restored to default"


# -----------------------------------------------------------------------------
# TC 10 — MasterHostConnectionID R/W
# -----------------------------------------------------------------------------
@cxp_test()
async def test_10_master_host_connection_id_rw(dut):
    """MasterHostConnectionID is a plain R/W register driving its port.

    §10.3.30: the Host writes its own ID here during discovery.

    Stimulus: write then read 0x4008 for 0x00000001, 0xA5A5A5A5 and
              0x00000000.
    Checks:   each readback and `ctl_master_host_connection_id_o` equal
              the written value.
    """
    dut.TESTCASE.value = 10
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    for v in (0x0000_0001, 0xA5A5_A5A5, 0x0000_0000):
        await bus.write(A_MST_HOST_ID, v)
        assert await bus.read(A_MST_HOST_ID) == v
        assert int(dut.ctl_master_host_connection_id_o.value) == v


# -----------------------------------------------------------------------------
# TC 11 — StreamPacketSizeMax Default And R/W
# -----------------------------------------------------------------------------
@cxp_test()
async def test_11_stream_pkt_size_default_and_rw(dut):
    """StreamPacketSizeMax powers up at 0 and is R/W.

    §10.3.32; §10.3.28: power-up executes a connection reset, which sets
    it to 0 ("not initialized", no stream packet, Table 44).

    Stimulus: after reset read `ctl_stream_pkt_dsize_o` and 0x4010; write
              0x80; read 0x4010 and the port again.
    Checks:   port and register == 0 after reset; both == 0x80 after the
              write.
    """
    dut.TESTCASE.value = 11
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    assert int(dut.ctl_stream_pkt_dsize_o.value) == 0
    assert await bus.read(A_STR_PKT_SIZE) == 0
    await bus.write(A_STR_PKT_SIZE, 0x0000_0080)
    assert await bus.read(A_STR_PKT_SIZE) == 0x0000_0080
    assert int(dut.ctl_stream_pkt_dsize_o.value) == 0x0000_0080


# -----------------------------------------------------------------------------
# TC 12 — ControlPacketSizeMax Read-Only
# -----------------------------------------------------------------------------
@cxp_test()
async def test_12_control_packet_size_max(dut):
    """ControlPacketSizeMax is a read-only byte count, multiple of 4, ≥ 128.

    §10.3.31: tells the Host the largest control packet the Device takes.

    Stimulus: read 0x400C; write 0xDEADBEEF; read again.
    Checks:   the register-map value (280 bytes), a multiple of 4 and ≥ 128;
              it is unchanged after the write.
    """
    dut.TESTCASE.value = 12
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    v = await bus.read(A_CTRL_PKT_SIZE)
    assert v == CONTROL_PKT_SIZE_MAX
    assert v % 4 == 0 and v >= 128, "must be a multiple of 4 and >=128 bytes"
    before = v
    await bus.write(A_CTRL_PKT_SIZE, 0xDEAD_BEEF)
    assert await bus.read(A_CTRL_PKT_SIZE) == before, "R/O"


# -----------------------------------------------------------------------------
# TC 13 — ConnectionConfig And ConnectionConfigDefault
# -----------------------------------------------------------------------------
@cxp_test()
async def test_13_connection_config_default_and_rw(dut):
    """ConnectionConfig is R/W with a write strobe; its Default is read-only.

    §10.3.33/34: a ConnectionConfig write must reset the stream Packet
    Tags, exported as the one-cycle `ctl_connection_config_wr_o` strobe.

    Stimulus: read 0x4014 and 0x4018; write 0x00010028 (the only
              configuration this device supports) to 0x4014; write
              0x00020038 (2 connections at 3.125 Gbps); write 0x12345678 to
              0x4018.
    Checks:   both registers and `ctl_connection_config_o` == 0x00010028
              after reset; the supported write is accepted (`err` 0) and
              pulses `ctl_connection_config_wr_o`; 0x00020038 is refused
              with 0x41, no pulse, the value unchanged; the 0x4018 write is
              refused with 0x43 and it still reads 0x00010028.
    Note:     the strobe width (one cycle) is not asserted.
    """
    dut.TESTCASE.value = 13
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    assert await bus.read(A_CONN_CONFIG)  == CONNECTION_CONFIG_DEFAULT
    assert await bus.read(A_CONN_CFG_DEF) == CONNECTION_CONFIG_DEFAULT
    assert int(dut.ctl_connection_config_o.value) == CONNECTION_CONFIG_DEFAULT

    # Write strobes ctl_connection_config_wr_o (Packet-Tag reset, §10.3.33).
    await bus.write(A_CONN_CONFIG, CONNECTION_CONFIG_DEFAULT)
    await ReadOnly()
    assert bus.err == 0x00
    assert int(dut.ctl_connection_config_wr_o.value) == 1, "ConnectionConfig write strobe"
    await RisingEdge(dut.sys_clk)

    await bus.write(A_CONN_CONFIG, 0x0002_0038)   # 2 connections @ 3.125 G
    await ReadOnly()
    assert bus.err == 0x41
    assert int(dut.ctl_connection_config_wr_o.value) == 0, "strobe on a refused write"
    await RisingEdge(dut.sys_clk)
    assert await bus.read(A_CONN_CONFIG) == CONNECTION_CONFIG_DEFAULT
    assert int(dut.ctl_connection_config_o.value) == CONNECTION_CONFIG_DEFAULT

    # ConnectionConfigDefault is read-only.
    await bus.write(A_CONN_CFG_DEF, 0x1234_5678)
    assert bus.err == 0x43
    assert await bus.read(A_CONN_CFG_DEF) == CONNECTION_CONFIG_DEFAULT


# -----------------------------------------------------------------------------
# TC 14 — Shifted Test Block
# -----------------------------------------------------------------------------
@cxp_test()
async def test_14_test_block_shifted(dut):
    """The v1.1.1 test block sits at its shifted addresses with live counters.

    v1.1.1 inserted ConnectionConfigDefault at 0x4018, moving TestMode to
    0x401C, TestErrorCountSelector to 0x4020 and TestErrorCount to 0x4024,
    and added TestPacketCountTx/Rx (§10.3.38/39) and HsUpconnection
    (§10.3.41).

    Stimulus: drive `test_err_count_link0` = 0xCAFEBEEF, write selector 0
              and read 0x4024; write TestMode 1, read, write 0, read; drive
              the TX/RX 64-bit counters (0x1122334455667788 /
              0x00AABBCCDDEEFF01) and read 0x4028–0x4034; write 0 to
              0x4024; read 0x403C, write 1 to it, read it again.
    Checks:   0x4024 == 0xCAFEBEEF; TestMode reads 1 with
              `ctl_test_mode_o` == 1, then 0; counters read MS word first
              (0x11223344, 0x55667788, 0x00AABBCC, 0xDDEEFF01);
              writing 0 to 0x4024, 0x4028, 0x4030 each pulses its own clear
              and no other (§10.3.37-39);
              HsUpconnection reads 0 before and after its write.
    Note:     the selector is only ever 0 (NUM_LINKS = 1); a non-zero
              write to 0x4024 is not exercised.
    """
    dut.TESTCASE.value = 14
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    dut.test_err_count_link0.value = 0xCAFE_BEEF
    await RisingEdge(dut.sys_clk)

    await bus.write(A_TEST_ERR_SEL, 0)
    assert await bus.read(A_TEST_ERR_CNT) == 0xCAFE_BEEF

    await bus.write(A_TEST_MODE, 0x0000_0001)
    assert await bus.read(A_TEST_MODE) == 0x0000_0001
    await ReadOnly()
    assert int(dut.ctl_test_mode_o.value) == 1
    await RisingEdge(dut.sys_clk)
    await bus.write(A_TEST_MODE, 0x0000_0000)
    assert await bus.read(A_TEST_MODE) == 0x0000_0000

    # §10.3.38/39 TestPacketCountTx/Rx: 8-byte, big-endian word order
    # (lower address = MS word), selected by TestErrorCountSelector.
    dut.test_pkt_count_tx_link0.value = 0x1122_3344_5566_7788
    dut.test_pkt_count_rx_link0.value = 0x00AA_BBCC_DDEE_FF01
    await RisingEdge(dut.sys_clk)
    assert await bus.read(A_TEST_PKT_TX_HI) == 0x1122_3344
    assert await bus.read(A_TEST_PKT_TX_LO) == 0x5566_7788
    assert await bus.read(A_TEST_PKT_RX_HI) == 0x00AA_BBCC
    assert await bus.read(A_TEST_PKT_RX_LO) == 0xDDEE_FF01

    # Writing 0 to a count register strobes that counter's clear only.
    for addr, want in ((A_TEST_ERR_CNT, (1, 0, 0)), (A_TEST_PKT_TX_HI, (0, 1, 0)),
                       (A_TEST_PKT_RX_HI, (0, 0, 1))):
        await bus.write(addr, 0x0000_0000)
        await ReadOnly()
        got = (int(dut.ctl_test_err_count_clr_o.value), int(dut.ctl_test_pkt_tx_clr_o.value),
               int(dut.ctl_test_pkt_rx_clr_o.value))
        assert got == want, f"write 0 to 0x{addr:04x}: clears {got}, want {want}"
        await RisingEdge(dut.sys_clk)

    # §10.3.41 HsUpconnection read-only support flag.
    assert await bus.read(A_HS_UPCONN) == HS_UPCONNECTION_SUPPORT
    await bus.write(A_HS_UPCONN, 0x1)
    assert await bus.read(A_HS_UPCONN) == HS_UPCONNECTION_SUPPORT


# -----------------------------------------------------------------------------
# TC 15 — ElectricalComplianceTest Stub
# -----------------------------------------------------------------------------
@cxp_test()
async def test_15_electrical_compliance_test_stub(dut):
    """ElectricalComplianceTest is an R/W stub cleared by ConnectionReset.

    §10.3.40 (optional register); §10.3.28 puts it in the clear set.

    Stimulus: read 0x4038; write 0x38; read; write 1 to 0x4000; wait one
              edge; read 0x4038.
    Checks:   the reads return 0, then 0x38, then 0.
    """
    dut.TESTCASE.value = 15
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    assert await bus.read(A_ECOMPL_TEST) == 0
    await bus.write(A_ECOMPL_TEST, 0x0000_0038)
    assert await bus.read(A_ECOMPL_TEST) == 0x0000_0038
    # ConnectionReset must clear it (ComplianceTest = 0, §10.3.28).
    await bus.write(A_CONN_RESET, 0x1)
    await RisingEdge(dut.sys_clk)
    assert await bus.read(A_ECOMPL_TEST) == 0


# -----------------------------------------------------------------------------
# TC 16 — GenICam String Layout
# -----------------------------------------------------------------------------
@cxp_test()
async def test_16_genicam_string_layout(dut):
    """The GenICam strings read back as big-endian, NULL-padded ASCII.

    §10.3.13–17.  v1.1.1 dropped the v1.0-invented DeviceFirmwareVersion
    at 0x2090 and renamed DeviceID to DeviceSerialNumber (0x20B0).

    Stimulus: read every word of DeviceVendorName (0x2000, 32 B),
              DeviceModelName (0x2020, 32 B), DeviceManufacturerInfo
              (0x2040, 48 B), DeviceVersion (0x2070, 32 B) and
              DeviceSerialNumber (0x20B0, 16 B) — 40 reads; read 0x2090.
    Checks:   each word equals `string_to_words` of the default string
              ("AcmeCXP", "CamModel", "AcmeCXP camera", "1.1.1",
              "SN-0001"); 0x2090 reads 0.
    Note:     the non-32-byte-aligned bases (0x2070) guard the fixed
              string-mux offset bug (src/tb_unit/README.md); the XmlUrl string
              and DeviceUserID are not covered here.
    """
    dut.TESTCASE.value = 16
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    fields = [
        (A_VENDOR_BASE,   *DEV_VENDOR_NAME),
        (A_MODEL_BASE,    *DEV_MODEL_NAME),
        (A_MFR_INFO_BASE, *DEV_MFR_INFO),
        (A_VERSION_BASE,  *DEV_VERSION_STR),
        (A_SERIAL_BASE,   *DEV_SERIAL_NUMBER),
    ]
    for base, text, width in fields:
        expected = string_to_words(text, width)
        for i, w in enumerate(expected):
            got = await bus.read(base + 4 * i)
            assert got == w, (
                f"{text!r} word {i}: addr=0x{base + 4*i:04x} "
                f"expected 0x{w:08x}, got 0x{got:08x}"
            )

    # The dropped 0x2090 field must no longer alias a string (reads 0).
    assert await bus.read(0x2090) == 0, "DeviceFirmwareVersion@0x2090 removed"


# -----------------------------------------------------------------------------
# TC 17 — DeviceUserID R/W And NV Restore
# -----------------------------------------------------------------------------
@cxp_test()
async def test_17_user_id_rw_and_nv_restore(dut):
    """DeviceUserID is R/W and is reloaded from the NV port on every reset.

    §10.3.18: the user-defined name persists across power cycles; the RTL
    models that storage with the 128-bit `device_user_id_nv` input.

    Stimulus: reset with `device_user_id_nv` = 0x1122…EEFF00; read the four
              words at 0x20C0–0x20CC; write four new words and read them
              back; hold `sys_rst_n` low 4 cycles, wait 2; read the four
              words again.
    Checks:   the reads return the NV value (MS word first), then the
              written words, then the NV value again.
    Note:     `ctl_device_user_id_o` is never checked.
    """
    dut.TESTCASE.value = 17
    nv = 0x1122334455667788_99AABBCCDDEEFF00
    await reset(dut, nv_id=nv)
    bus = CxpRegBus(dut, dut.sys_clk)

    expected_words = [
        (nv >> 96) & 0xFFFF_FFFF,
        (nv >> 64) & 0xFFFF_FFFF,
        (nv >> 32) & 0xFFFF_FFFF,
        (nv >>  0) & 0xFFFF_FFFF,
    ]
    for i, w in enumerate(expected_words):
        got = await bus.read(A_USER_ID_BASE + 4 * i)
        assert got == w, f"NV word {i}: expected 0x{w:08x}, got 0x{got:08x}"

    new_words = [0xDEAD_BEEF, 0xCAFE_F00D, 0x1234_5678, 0x9ABC_DEF0]
    for i, w in enumerate(new_words):
        await bus.write(A_USER_ID_BASE + 4 * i, w)
    for i, w in enumerate(new_words):
        got = await bus.read(A_USER_ID_BASE + 4 * i)
        assert got == w, f"writeback word {i}: expected 0x{w:08x}, got 0x{got:08x}"

    await RisingEdge(dut.sys_clk)
    dut.sys_rst_n.value = 0
    for _ in range(4):
        await RisingEdge(dut.sys_clk)
    dut.sys_rst_n.value = 1
    for _ in range(2):
        await RisingEdge(dut.sys_clk)
    for i, w in enumerate(expected_words):
        got = await bus.read(A_USER_ID_BASE + 4 * i)
        assert got == w, f"after reset word {i}: expected 0x{w:08x}, got 0x{got:08x}"


# -----------------------------------------------------------------------------
# TC 18 — Read-Only Writes Ignored
# -----------------------------------------------------------------------------
@cxp_test()
async def test_18_readonly_writes_ignored(dut):
    """Writes to Table 45 read-only addresses leave their values unchanged.

    Table 45 Access column: R registers must ignore Host writes.

    Stimulus: for each of 16 RO addresses — 0x0000–0x001C except 0x000C,
              0x4004, 0x400C, 0x4018, 0x403C and the five string bases —
              read, write the bitwise inverse, read again.
    Checks:   before == after at every address.
    Note:     none of these addresses has a write handler, so the test
              passes with or without the `bs_ro_hit` write gate.
    """
    dut.TESTCASE.value = 18
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    ro_addrs = [
        A_STANDARD,
        A_REVISION,
        A_XML_MFST_SIZE,
        A_XML_VERSION,
        A_XML_SCHEMA,
        A_XML_URL,
        A_IIDC2_ADDR,
        A_DEV_CONN_ID,
        A_CTRL_PKT_SIZE,
        A_CONN_CFG_DEF,
        A_HS_UPCONN,
        A_VENDOR_BASE,
        A_MODEL_BASE,
        A_MFR_INFO_BASE,
        A_VERSION_BASE,
        A_SERIAL_BASE,
    ]
    for addr in ro_addrs:
        before = await bus.read(addr)
        await bus.write(addr, ~before & 0xFFFF_FFFF)
        after = await bus.read(addr)
        assert before == after, (
            f"R/O addr 0x{addr:04x} changed: 0x{before:08x} -> 0x{after:08x}"
        )


# -----------------------------------------------------------------------------
# TC 19 — Registered Ready
# -----------------------------------------------------------------------------
@cxp_test()
async def test_19_ready_signal(dut):
    """`ready` is the registered `we | re` request acknowledge.

    The bus bridges derive `pready` from `ready`, so it must rise one
    cycle after a request and drop one cycle after the request ends.

    Stimulus: drive `addr` = 0x0000 and `re` = 1 directly (no bus helper)
              and hold `re` for two edges, then drop it.
    Checks:   `ready` == 1 in ReadOnly after the first edge that samples
              `re` = 1; `ready` == 0 after the first edge that samples
              `re` = 0.
    Note:     `ready` on writes and `we` together with `re` are not tested.
    """
    dut.TESTCASE.value = 19
    await reset(dut)
    _ = CxpRegBus(dut, dut.sys_clk)

    await RisingEdge(dut.sys_clk)
    dut.addr.value = A_STANDARD
    dut.re.value   = 1
    await RisingEdge(dut.sys_clk)
    await ReadOnly()
    assert int(dut.ready.value) == 1, "ready should pulse on request cycle"

    await RisingEdge(dut.sys_clk)
    dut.re.value   = 0
    dut.addr.value = 0
    await RisingEdge(dut.sys_clk)
    await ReadOnly()
    assert int(dut.ready.value) == 0, "ready should clear when no request pending"


# -----------------------------------------------------------------------------
# TC 20 — Access Codes
# -----------------------------------------------------------------------------
@cxp_test()
async def test_20_access_codes(dut):
    """Each access returns its Table 22 code; a refused write changes nothing.

    Table 22: 0x40 invalid address, 0x41 invalid data, 0x43 write to a
    read-only address.  The decode is on all 32 address bits.

    Stimulus / checks, one access each:
      read 0x0002 (unaligned), 0x0020, 0x0001_4000 (no register)     0x40
      write 1 to 0x0001_4000: no ConnectionReset pulse                 0x40
      write Standard, a vendor-string word, the XML ROM                0x43
      write XmlManifestSelector 1, PixelFormat (0x10008) 0x0199; each keeps
        its value                                                      0x41
      read 0x3020 (Image2StreamIDAddress) = 0                          0x00
      write 0x3020                                                     0x43
    """
    dut.TESTCASE.value = 20
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    for a in (0x0002, 0x0020, 0x0001_4000):
        await bus.read(a)
        assert bus.err == 0x40, f"read 0x{a:x}: 0x{bus.err:02x}"

    pulses = []

    async def watch():
        while True:
            await RisingEdge(dut.sys_clk)
            await ReadOnly()
            if int(dut.ctl_connection_reset_pulse_o.value):
                pulses.append(1)

    w = cocotb.start_soon(watch())
    await bus.write(0x0001_4000, 1)
    assert bus.err == 0x40
    for _ in range(4):
        await RisingEdge(dut.sys_clk)
    w.cancel()
    assert not pulses, "a write above 0xFFFF reached ConnectionReset"

    for a in (A_STANDARD, A_VENDOR_BASE, XML_BLOB_ADDR):
        before = await bus.read(a)
        await bus.write(a, 0x1234_5678)
        assert bus.err == 0x43, f"write 0x{a:x}: 0x{bus.err:02x}"
        assert await bus.read(a) == before

    for a, v in ((A_XML_MFST_SEL, 1), (G.PIXEL_FORMAT_ALIAS, 0x0199)):
        before = await bus.read(a)
        await bus.write(a, v)
        assert bus.err == 0x41, f"write 0x{a:x}=0x{v:x}: 0x{bus.err:02x}"
        assert await bus.read(a) == before, f"0x{a:x} changed"

    assert await bus.read(0x3020) == 0 and bus.err == 0x00
    await bus.write(0x3020, 5)
    assert bus.err == 0x43


# -----------------------------------------------------------------------------
# TC 21 — TestErrorCountSelector Bound
# -----------------------------------------------------------------------------
@cxp_test()
async def test_21_test_err_sel_bound(dut):
    """TestErrorCountSelector takes only a connection the device has.

    §10.3.36: the selector names the connection whose TestErrorCount is
    read.  With NUM_LINKS = 1 only 0 has a connection behind it.

    Stimulus: write 1, 0xA5A5A5A5 and 0xFFFFFFFF to 0x4020, reading it
              after each; then write 0.
    Checks:   each non-zero write returns 0x41 and the selector still
              reads 0; the write of 0 returns 0x00.
    """
    dut.TESTCASE.value = 21
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)

    for v in (1, 0xA5A5_A5A5, 0xFFFF_FFFF):
        await bus.write(A_TEST_ERR_SEL, v)
        assert bus.err == 0x41, f"selector 0x{v:x}: 0x{bus.err:02x}"
        assert await bus.read(A_TEST_ERR_SEL) == 0, f"selector 0x{v:x} stored"
    await bus.write(A_TEST_ERR_SEL, 0)
    assert bus.err == 0x00


# -----------------------------------------------------------------------------
# TC 22 — ConnectionReset Waits For The Echo
# -----------------------------------------------------------------------------
@cxp_test()
async def test_22_conn_reset_waits_for_echo(dut):
    """The ConnectionReset bit clears only once the other domains saw it.

    §10.3.28: the register returns to 0 when the device has activated its
    discovery configuration; part of that is applied in the tx domain,
    which echoes the level back on `conn_reset_done`.

    Stimulus: echo withheld; write 1 to 0x4000; 30 cycles (past the
              8-cycle minimum); read 0x4000; release the echo; 10 cycles;
              read again.
    Checks:   `ctl_connection_reset_active_o` = 1 and the register reads 1
              while the echo is withheld; both 0 after it.
    """
    dut.TESTCASE.value = 22
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)
    echo["on"] = False
    await bus.write(A_CONN_RESET, 1)
    for _ in range(30):
        await RisingEdge(dut.sys_clk)
    assert int(dut.ctl_connection_reset_active_o.value) == 1
    assert await bus.read(A_CONN_RESET) == 1
    echo["on"] = True
    for _ in range(10):
        await RisingEdge(dut.sys_clk)
    assert int(dut.ctl_connection_reset_active_o.value) == 0
    assert await bus.read(A_CONN_RESET) == 0


# -----------------------------------------------------------------------------
# TC 23 — ConnectionReset Timeout
# -----------------------------------------------------------------------------
@cxp_test()
async def test_23_conn_reset_timeout(dut):
    """Without the echo the ConnectionReset bit still ends.

    A wait on another block needs an exit: `p_CONN_RESET_TIMEOUT` (64 in
    the wrapper) bounds it.

    Stimulus: echo withheld for the whole test; write 1 to 0x4000; 80
              cycles.
    Checks:   the bit is 1 after 40 cycles and 0 after 80.
    """
    dut.TESTCASE.value = 23
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)
    echo["on"] = False
    await bus.write(A_CONN_RESET, 1)
    for _ in range(40):
        await RisingEdge(dut.sys_clk)
    assert int(dut.ctl_connection_reset_active_o.value) == 1
    for _ in range(40):
        await RisingEdge(dut.sys_clk)
    assert int(dut.ctl_connection_reset_active_o.value) == 0


# -----------------------------------------------------------------------------
# TC 24 — Local ConnectionReset Request
# -----------------------------------------------------------------------------
@cxp_test()
async def test_24_conn_reset_request_input(dut):
    """`conn_reset_req` applies §10.3.28 exactly as a write of 1 does.

    Stimulus: MasterHostConnectionID = 0x12345678, StreamPacketSizeMax =
              0x200, TestMode = 1; one-cycle `conn_reset_req`.
    Checks:   each of the three counter clears pulses while the request
              is held; the bit reads 1; the three registers read 0; the
              bit clears.
    """
    dut.TESTCASE.value = 24
    await reset(dut)
    bus = CxpRegBus(dut, dut.sys_clk)
    await bus.write(A_MST_HOST_ID, 0x1234_5678)
    await bus.write(A_STR_PKT_SIZE, 0x200)
    await bus.write(A_TEST_MODE, 1)
    await RisingEdge(dut.sys_clk)
    # Held for 3 edges: a 1-then-0 pair on consecutive edges can collapse
    # in cocotb; a held request re-applies and ends as one.
    dut.conn_reset_req.value = 1
    clrs = [0, 0, 0]
    for n in range(8):
        if n == 3:
            dut.conn_reset_req.value = 0
        await RisingEdge(dut.sys_clk)
        await ReadOnly()
        for i, sig in enumerate((dut.ctl_test_err_count_clr_o, dut.ctl_test_pkt_tx_clr_o,
                                 dut.ctl_test_pkt_rx_clr_o)):
            clrs[i] += int(sig.value)
        await NextTimeStep()
    assert all(c >= 1 for c in clrs), clrs
    await RisingEdge(dut.sys_clk)
    assert await bus.read(A_CONN_RESET) == 1
    assert await bus.read(A_MST_HOST_ID) == 0
    assert await bus.read(A_STR_PKT_SIZE) == 0
    assert await bus.read(A_TEST_MODE) == 0
    for _ in range(30):
        await RisingEdge(dut.sys_clk)
    assert await bus.read(A_CONN_RESET) == 0
