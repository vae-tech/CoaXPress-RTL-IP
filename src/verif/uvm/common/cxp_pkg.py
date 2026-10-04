"""CoaXPress 1.1.1 device IP — environment-local constants and types.

Wire formats live in `cxp_protocol` (rule U4) and nothing here duplicates
one: what is left is the shape of the environment's own transactions and
the beat classification the wire monitor publishes.

Table numbers are CXP-001-2015: Table 14 IDLE, Table 18 packet types,
Table 21 control command, Table 22 acknowledgment, Tables 37-40 image
headers and line markers, Table 45 bootstrap registers.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Sequence, Tuple

from cxp_protocol import regmap as _rm


# ---------------------------------------------------------------------------
# K-code byte values (also re-exported from src/verif/common/cxp_8b10b.py;
# duplicated here so the UVM package has no hard dependency on the unit-TB
# helpers being importable in every back-end).
# ---------------------------------------------------------------------------
K28_0 = 0x1C
K28_1 = 0x3C
K28_2 = 0x5C
K28_3 = 0x7C
K28_4 = 0x9C
K28_5 = 0xBC
K28_6 = 0xDC   # §8.3.3 I/O-acknowledgment indication (new in v1.1)
K27_7 = 0xFB
K29_7 = 0xFD
D21_5 = 0xB5


class PacketType(IntEnum):
    STREAM = 0x01
    CTRL_ACK = 0x03


# ---------------------------------------------------------------------------
# Word-level helpers
# ---------------------------------------------------------------------------
def replicate_byte(b: int) -> int:
    """Spec §7.4.6 / 7.4.7 per-byte 4× replication into a 32-bit word."""
    b &= 0xFF
    return b | (b << 8) | (b << 16) | (b << 24)


def byteswap32(w: int) -> int:
    """Swap the four bytes of a 32-bit word.

    §8.3: register space is big-endian, so a non-replicated 32-bit control
    read/write data word is transmitted byte-swapped relative to the
    device's native register value.  4× replicated header bytes are
    unaffected (all four lanes equal); only data words need this.
    """
    w &= 0xFFFF_FFFF
    return (((w & 0x0000_00FF) << 24) | ((w & 0x0000_FF00) << 8)
            | ((w & 0x00FF_0000) >> 8) | ((w & 0xFF00_0000) >> 24))


def majority_byte(word: int) -> Tuple[int, bool]:
    """Reverse of replicate_byte: return (byte, ok) where ok=False if the
    replicas don't form a 3-of-4 or 4-of-4 majority."""
    lanes = [(word >> (8 * i)) & 0xFF for i in range(4)]
    # Count occurrences; pick the byte with the largest count.
    best, best_count = 0, 0
    for b in set(lanes):
        c = lanes.count(b)
        if c > best_count:
            best, best_count = b, c
    return best, best_count >= 3


def crc32_words(words: Sequence[int]) -> int:
    """§8.2.2.2 CRC register over a list of 32-bit on-wire words.

    Bytes are folded P0 (LSB byte) first, bit 0 first, from seed
    0xFFFFFFFF with the 802.3 polynomial and no final XOR — zlib's CRC
    without its final XOR.  The register is also the CRC word on the wire
    (LSByte in P0), so no byte swap is needed to compare it.
    """
    buf = bytearray()
    for w in words:
        buf += (w & 0xFFFF_FFFF).to_bytes(4, "little")
    return (zlib.crc32(bytes(buf)) ^ 0xFFFF_FFFF) & 0xFFFF_FFFF


# ---------------------------------------------------------------------------
# Packet kinds emitted/observed on the wire
# ---------------------------------------------------------------------------
class WireBeatKind(IntEnum):
    """Classification of a single 32-bit tx_clk beat (cxp_tx_wire_agent)."""
    IDLE = 0
    SOP = 1            # K27.7 ×4
    EOP = 2            # K29.7 ×4
    KMARK = 3          # K28.3 ×4 stream image-header / line marker
    PAYLOAD = 4
    UNKNOWN = 5
    TRIG_RISE = 6      # K28.4 ×4 — §8.3.2 rising-edge trigger packet header
    TRIG_FALL = 7      # K28.2 ×4 — §8.3.2 falling-edge trigger packet header
    IOACK = 8          # K28.6 ×4 — §8.3.3 I/O-acknowledgment packet header


# Header K-byte -> short-packet WireBeatKind (trigger / I/O-ack 2-word
# packets do NOT use K27.7/K29.7 framing — their first word is a 4x
# replication of a dedicated K-character, the second is plain payload).
SHORT_PKT_HDR_KINDS = {
    K28_4: WireBeatKind.TRIG_RISE,
    K28_2: WireBeatKind.TRIG_FALL,
    K28_6: WireBeatKind.IOACK,
}


IDLE_WORD = (
    (D21_5 << 24) | (K28_1 << 16) | (K28_1 << 8) | K28_5,
    0b0111,  # kmask: lanes 0..2 are K (K28.5 K28.1 K28.1); lane 3 is D21.5
)


def classify_wire_beat(data: int, kmask: int) -> WireBeatKind:
    """Cheap classifier for one tx_clk beat."""
    lanes = [(data >> (8 * i)) & 0xFF for i in range(4)]
    if kmask == IDLE_WORD[1] and tuple(lanes) == tuple(
        (IDLE_WORD[0] >> (8 * i)) & 0xFF for i in range(4)
    ):
        return WireBeatKind.IDLE
    if kmask == 0b1111 and all(b == K27_7 for b in lanes):
        return WireBeatKind.SOP
    if kmask == 0b1111 and all(b == K29_7 for b in lanes):
        return WireBeatKind.EOP
    if kmask == 0b1111 and all(b == K28_3 for b in lanes):
        return WireBeatKind.KMARK  # CXP image-header / line marker
    # §8.3.2 trigger and §8.3.3 I/O-ack short packets — first word is a
    # 4x replication of a dedicated K-character (no K27.7 SOP framing).
    if kmask == 0b1111 and all(b == lanes[0] for b in lanes) \
            and lanes[0] in SHORT_PKT_HDR_KINDS:
        return SHORT_PKT_HDR_KINDS[lanes[0]]
    if kmask == 0b0000:
        return WireBeatKind.PAYLOAD
    return WireBeatKind.UNKNOWN


# ---------------------------------------------------------------------------
# CXP §6.3.2 host->device control-command packet (table 20).  Encoded form
# emitted on the 20.83 Mbps uplink wire by cxp_host_uplink_agent.
# ---------------------------------------------------------------------------
class UplinkKind(IntEnum):
    IDLE = 0
    CTRL_CMD_READ = 1
    CTRL_CMD_WRITE = 2
    CTRL_CMD_RESET = 3
    TRIGGER_RISE = 4
    TRIGGER_FALL = 5
    LINKTEST = 6
    IOACK = 7          # §8.3.3 host -> device I/O acknowledgment (Table 17)
    RAW = 8            # any beats (`raw_beats`): a packet no other kind makes


# Short packets go on the driver's insertion lane (§8.2.4): they pre-empt
# whatever long packet is in flight at the next character boundary.
SHORT_KINDS = (UplinkKind.TRIGGER_RISE, UplinkKind.TRIGGER_FALL,
               UplinkKind.IOACK)


@dataclass
class UplinkTxn:
    """One host→device packet — host_uplink_agent transaction object.

    It says *what* to send; `cxp_protocol` turns it into characters.
    """
    kind: UplinkKind = UplinkKind.IDLE
    address: int = 0
    nwords: int = 1
    payload: List[int] = field(default_factory=list)

    # Table 21 Size (B, bytes).  None = 4 x nwords, which is what a
    # whole-word access asks for; a test that wants a Size that is not a
    # multiple of four sets it here.
    size: int | None = None

    # Table 15 / 16 trigger Delay, and the Table 17 acknowledgment code.
    delay: int = 0
    ioack_code: int = 0x01
    # Table 15 damage (§8.2.2): the index (0..2) of a leader character sent
    # as the other K code, and three Delay characters sent in place of
    # three copies of `delay` (None = three copies).
    trig_bad_leader: int = -1
    trig_delays: tuple | None = None

    # A command a test builds by hand: `opcode` overrides the Table 21
    # opcode, `beats_edit(beats) -> beats` rewrites the encoded packet
    # (drop a word, add one, damage a replica), and `expect_codes` is the
    # set of final acknowledgment codes the test expects (None in the set:
    # no acknowledgment at all), which the control scoreboard then uses in
    # place of its own prediction; such a command makes no access.
    opcode: int | None = None
    beats_edit: object = None
    expect_codes: set | None = None
    # Damage the standard leaves to the implementation (§8.2.2.1: two of
    # four replicas): the command may be executed as sent — then it must be
    # executed right — or refused 0x47, or dropped.
    may_drop: bool = False
    raw_beats: list = field(default_factory=list)

    # Error injection.  The two `_at` fields are character indices into the
    # packet's own character stream, so an error can be placed on the SOP,
    # in the header, in the data or on the CRC.
    inject_crc_err: bool = False
    inject_one_bit_in_replica: int = -1  # -1 = none; else replica idx 0..3
    inject_code_at: int = -1             # an invalid code group -> code error
    inject_disp_at: int = -1             # running disparity flipped after it

    # Connection test (§8.7.2, Table 23) — only read when kind == LINKTEST.
    # `lt_n_data` is the number of data words in the body.  The spec value
    # is 1024; the default stays at 64 (one 0x00..0xFF wrap) so a packet
    # costs 64 rather than 1024 word times, and the device counts the 960
    # missing words as errors (§8.7.1).
    lt_n_data: int = 64
    # Word indices (0..lt_n_data-1) corrupted with a 1-bit flip.
    lt_error_indices: List[int] = field(default_factory=list)

    # Filled by the serializer as it goes: "start" (ns) when the first
    # character went out, "done" when the last one left the pin.
    times: dict = field(default_factory=dict)

    @property
    def t_start_ns(self) -> float:
        return self.times.get("start", -1.0)

    @t_start_ns.setter
    def t_start_ns(self, v: float) -> None:
        self.times["start"] = v

    @property
    def t_done_ns(self) -> float:
        return self.times.get("done", -1.0)

    @t_done_ns.setter
    def t_done_ns(self, v: float) -> None:
        self.times["done"] = v

    @property
    def trig_repairable(self) -> bool:
        """The device takes this trigger (two of three Delay copies alike,
        Delay at most 239, at most one leader character wrong)."""
        ds = list(self.trig_delays) if self.trig_delays else [self.delay] * 3
        d = max(set(ds), key=ds.count)
        return ds.count(d) >= 2 and 0 <= d <= 239

    @property
    def trig_delay_taken(self) -> int:
        ds = list(self.trig_delays) if self.trig_delays else [self.delay] * 3
        return max(set(ds), key=ds.count)

    @property
    def size_bytes(self) -> int:
        """Table 21 Size of this command."""
        if self.size is not None:
            return int(self.size)
        if self.kind == UplinkKind.CTRL_CMD_RESET:
            return 0
        return 4 * max(1, self.nwords)


# ---------------------------------------------------------------------------
# Bootstrap register address map — CoaXPress 1.1.1 (CXP-001-2015) Table 45,
# under the env's names.  The values are the generated map's
# (cxp_protocol.regmap, from src/regmap/cxp_regmap.yaml): the §10.3.19-27
# slots at 0x3000 (DEV_*) read the addresses of the use-case features,
# which live in the manufacturer window (FEAT_*).
# ---------------------------------------------------------------------------
class BootstrapAddr(IntEnum):
    STANDARD        = _rm.STANDARD
    REVISION        = _rm.REVISION
    XML_MFST_SIZE   = _rm.XML_MANIFEST_SIZE
    XML_MFST_SEL    = _rm.XML_MANIFEST_SELECTOR
    XML_VERSION     = _rm.XML_VERSION
    XML_SCHEMA      = _rm.XML_SCHEMA_VERSION
    XML_URL         = _rm.XML_URL_ADDRESS
    IIDC2_ADDR      = _rm.IIDC2_ADDRESS
    VENDOR_BASE     = _rm.DEVICE_VENDOR_NAME
    MODEL_BASE      = _rm.DEVICE_MODEL_NAME
    MFR_INFO_BASE   = _rm.DEVICE_MANUFACTURER_INFO
    VERSION_BASE    = _rm.DEVICE_VERSION
    SERIAL_BASE     = _rm.DEVICE_SERIAL_NUMBER
    USER_ID_BASE    = _rm.DEVICE_USER_ID
    # §10.3.19-27: each slot reads the address of its feature (RO).
    DEV_WIDTH       = _rm.WIDTH_SLOT
    DEV_HEIGHT      = _rm.HEIGHT_SLOT
    DEV_ACQ_MODE    = _rm.ACQUISITION_MODE_SLOT
    DEV_ACQ_START   = _rm.ACQUISITION_START_SLOT
    DEV_ACQ_STOP    = _rm.ACQUISITION_STOP_SLOT
    DEV_PIXFMT      = _rm.PIXEL_FORMAT_SLOT
    DEV_TAPGEO      = _rm.TAP_GEOMETRY_SLOT
    DEV_IMG1_SID    = _rm.IMAGE1_STREAM_ID_SLOT
    # §10.3.28-41 connection / test block.
    LINK_RESET      = _rm.CONNECTION_RESET
    DEV_LINK_ID     = _rm.DEVICE_CONNECTION_ID
    MST_HOST_ID     = _rm.MASTER_HOST_CONNECTION_ID            # (RW)
    CTRL_PKT_DSIZE  = _rm.CONTROL_PACKET_SIZE_MAX              # (RO)
    STR_PKT_DSIZE   = _rm.STREAM_PACKET_SIZE_MAX               # (RW)
    LINK_CONFIG     = _rm.CONNECTION_CONFIG                    # (RW)
    LINK_CONFIG_DEF = _rm.CONNECTION_CONFIG_DEFAULT            # (RO)
    TEST_MODE       = _rm.TEST_MODE                            # (RW, 1-bit)
    TEST_ERR_SEL    = _rm.TEST_ERROR_COUNT_SELECTOR            # (RW)
    TEST_ERR_CNT    = _rm.TEST_ERROR_COUNT                     # (live)
    TEST_PKT_TX_HI  = _rm.TEST_PACKET_COUNT_TX                 # [63:32] (live)
    TEST_PKT_TX_LO  = _rm.TEST_PACKET_COUNT_TX + 4
    TEST_PKT_RX_HI  = _rm.TEST_PACKET_COUNT_RX                 # [63:32] (live)
    TEST_PKT_RX_LO  = _rm.TEST_PACKET_COUNT_RX + 4
    # Manufacturer feature window (a R/W store — round-trips any 32-bit
    # value, so ideal for RAL bit-bash / random ctrl traffic).
    MFR_BASE        = _rm.MFR_BASE
    # Use-case features (the addresses the 0x3000 slots read).
    FEAT_WIDTH      = _rm.WIDTH_ALIAS
    FEAT_HEIGHT     = _rm.HEIGHT_ALIAS
    FEAT_PIXFMT     = _rm.PIXEL_FORMAT_ALIAS
    FEAT_ACQ_START  = _rm.ACQUISITION_START_ALIAS
    FEAT_ACQ_STOP   = _rm.ACQUISITION_STOP_ALIAS
    FEAT_TAPGEO     = _rm.TAP_GEOMETRY_ALIAS
    FEAT_IMG1_SID   = _rm.IMAGE1_STREAM_ID_ALIAS
    FEAT_ACQ_MODE   = _rm.ACQUISITION_MODE_ALIAS
    MFR_FRAMECOUNT  = _rm.FRAME_COUNT
    MFR_TESTPATTERN = _rm.TEST_PATTERN     # -> ctl_test_pattern_o (TPG select)
    MFR_OFFSET_X    = _rm.OFFSET_X
    MFR_OFFSET_Y    = _rm.OFFSET_Y
    MFR_SOURCETAG   = _rm.SOURCE_TAG
    MFR_STREAMFLAGS = _rm.STREAM_FLAGS


# ---------------------------------------------------------------------------
# Stream packet header / line-marker word counts (spec PDF, not MD).
# ---------------------------------------------------------------------------
RECT_HDR_WORDS  = 25   # spec table 37
RECT_LINE_WORDS = 9    # spec table 38
ARB_HDR_WORDS   = 16   # spec table 39
ARB_LINE_WORDS  = 11   # spec table 40


# ---------------------------------------------------------------------------
# Stream-packet payload size (cfg_dsizeP), in 32-bit words.  Tests can
# override via ConfigDB key "cfg_dsizeP"; CfgAgent / apply_defaults /
# StreamScoreboard all read the same key.
# ---------------------------------------------------------------------------
DEFAULT_PKT_DSIZE_P = 256

# 6-word stream-packet header (SOP + type + streamid + tag + DsizeP MSB/LSB)
# plus 2-word trailer (CRC32 + EOP); see cxp_tx_stream_pkt.sv layout.
STREAM_PKT_OVERHEAD_WORDS = 8


CFG_DSIZE_P_KEY = "cfg_dsizeP"

# Host bit-clock offsets (uvm/agents/host_uplink_agent.py).  A test sets
# them through CxpTopTest.HOST_*; the driver reads them in build_phase.
CFG_HOST_PPM_KEY       = "host_ppm"
CFG_HOST_PHASE_PS_KEY  = "host_phase_ps"
CFG_HOST_JITTER_UI_KEY = "host_jitter_ui"
