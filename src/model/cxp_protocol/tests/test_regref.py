"""The reference register file against CXP-001-2015 §10.3 and the map."""

import pytest

from cxp_protocol import packets as p, regmap as rm, regmodel as rmod
from cxp_protocol.regref import RegRef, Unknown


@pytest.fixture
def ref():
    return RegRef()


# ------------------------------------------------------------- reads ----
def test_reset_values_are_the_map(ref):
    assert ref.read(rm.STANDARD) == (p.ACK_OK_DATA, [rm.STANDARD_VALUE])
    assert ref.read(rm.REVISION) == (p.ACK_OK_DATA, [rm.REVISION_VALUE])
    assert ref.read(rm.CONTROL_PACKET_SIZE_MAX)[1] == [rm.CONTROL_PACKET_SIZE_MAX_VALUE]
    assert ref.read(rm.STREAM_PACKET_SIZE_MAX)[1] == [rm.STREAM_PACKET_SIZE_MAX_RESET]
    assert ref.read(rm.CONNECTION_CONFIG)[1] == [rm.CONNECTION_CONFIG_RESET]
    assert ref.read(rm.CONNECTION_CONFIG_DEFAULT)[1] == [rm.CONNECTION_CONFIG_DEFAULT_VALUE]


def test_strings_are_big_endian_and_nul_padded(ref):
    code, words = ref.read(rm.XML_URL, rm.XML_URL_LEN)
    assert code == p.ACK_OK_DATA
    raw = b"".join(w.to_bytes(4, "big") for w in words)
    assert len(raw) == rm.XML_URL_LEN
    assert raw.rstrip(b"\0").decode() == rm.XML_URL_STR
    assert raw[len(rm.XML_URL_STR)] == 0

    raw = b"".join(w.to_bytes(4, "big")
                   for w in ref.read(rm.DEVICE_VENDOR_NAME, rm.DEVICE_VENDOR_NAME_LEN)[1])
    assert raw.rstrip(b"\0").decode() == rm.DEVICE_VENDOR_NAME_STR


def test_zero_range_and_unmapped(ref):
    # §10.3.27: Image<n>StreamIDAddress reads 0 beyond the first stream.
    assert ref.read(rm.IMAGE_N_STREAM_ID_ADDRESS)[1] == [0]
    # Nothing decoded -> 0x40, and so is an unaligned address.
    assert ref.read(0x5000)[0] == p.ACK_BAD_ADDR
    assert ref.read(rm.STANDARD + 1)[0] == p.ACK_BAD_ADDR


def test_feature_slot_reads_the_feature_address(ref):
    # §10.3.19-27: the bootstrap slot holds the address, not the value.
    assert ref.read(rm.WIDTH_SLOT)[1] == [rm.WIDTH_ALIAS]
    assert ref.read(rm.WIDTH_ALIAS)[1] == [rm.WIDTH_RESET]
    assert ref.read(rm.PIXEL_FORMAT_SLOT)[1] == [rm.PIXEL_FORMAT_ALIAS]


def test_multi_word_read(ref):
    code, words = ref.read(rm.STANDARD, 8)
    assert code == p.ACK_OK_DATA
    assert words == [rm.STANDARD_VALUE, rm.REVISION_VALUE]


def test_live_counter_is_not_invented(ref):
    with pytest.raises(Unknown):
        ref.read(rm.TEST_ERROR_COUNT)
    ref.set_counter("TestErrorCount", 7)
    assert ref.read(rm.TEST_ERROR_COUNT)[1] == [7]
    # §10.3.38: the 64-bit counters read high word first.
    ref.set_counter("TestPacketCountTx", (3 << 32) | 9)
    assert ref.read(rm.TEST_PACKET_COUNT_TX, 8)[1] == [3, 9]


# ------------------------------------------------------------ writes ----
def test_write_read_round_trip(ref):
    assert ref.write(rm.MASTER_HOST_CONNECTION_ID, [0xA5A5_A5A5]) == p.ACK_OK_WRITE
    assert ref.read(rm.MASTER_HOST_CONNECTION_ID)[1] == [0xA5A5_A5A5]


def test_read_only_write_is_refused_and_changes_nothing(ref):
    before = ref.read(rm.STANDARD)[1]
    assert ref.write(rm.STANDARD, [0]) == p.ACK_RO_WRITE
    assert ref.read(rm.STANDARD)[1] == before
    assert ref.write(rm.XML_URL, [0]) == p.ACK_RO_WRITE
    assert ref.write(rm.WIDTH_SLOT, [0]) == p.ACK_RO_WRITE
    assert ref.write(rm.IMAGE_N_STREAM_ID_ADDRESS, [0]) == p.ACK_RO_WRITE


def test_every_read_only_address_refuses_a_write(ref):
    for addr in ref.readonly_addrs():
        assert ref.write(addr, [0xFFFF_FFFF]) == p.ACK_RO_WRITE, hex(addr)


def test_value_out_of_range(ref):
    # ConnectionConfig takes only its default (one connection, one rate).
    assert ref.write(rm.CONNECTION_CONFIG, [0x1234]) == p.ACK_BAD_DATA
    assert ref.write(rm.CONNECTION_CONFIG, [rm.CONNECTION_CONFIG_RESET]) == p.ACK_OK_WRITE
    # XmlManifestSelector is bounded by XmlManifestSize - 1 = 0.
    assert ref.write(rm.XML_MANIFEST_SELECTOR, [1]) == p.ACK_BAD_DATA
    assert ref.write(rm.XML_MANIFEST_SELECTOR, [0]) == p.ACK_OK_WRITE
    # PixelFormat takes the PFNC values of Mono8..Mono16 (§11.2.1.6), not
    # the Table 25 PixelF codes.
    assert ref.write(rm.PIXEL_FORMAT_ALIAS, [0x9999]) == p.ACK_BAD_DATA
    assert ref.write(rm.PIXEL_FORMAT_ALIAS, [0x0101]) == p.ACK_BAD_DATA
    assert ref.write(rm.PIXEL_FORMAT_ALIAS, [0x0108_0001]) == p.ACK_OK_WRITE
    assert ref.write(rm.PIXEL_FORMAT_ALIAS, [0x0110_0007]) == p.ACK_OK_WRITE


def test_refused_write_changes_nothing(ref):
    ref.write(rm.CONNECTION_CONFIG, [rm.CONNECTION_CONFIG_RESET])
    ref.write(rm.CONNECTION_CONFIG, [0xDEAD])
    assert ref.read(rm.CONNECTION_CONFIG)[1] == [rm.CONNECTION_CONFIG_RESET]


def test_counter_takes_only_zero(ref):
    ref.set_counter("TestErrorCount", 5)
    assert ref.write(rm.TEST_ERROR_COUNT, [1]) == p.ACK_BAD_DATA
    assert ref.write(rm.TEST_ERROR_COUNT, [0]) == p.ACK_OK_WRITE


# -------------------------------------------------------- §10.3.28 ----
def test_connection_reset_loads_the_listed_values(ref):
    ref.write(rm.MASTER_HOST_CONNECTION_ID, [0xDEAD_BEEF])
    ref.write(rm.STREAM_PACKET_SIZE_MAX, [1024])
    ref.write(rm.TEST_MODE, [1])
    ref.write(rm.TEST_ERROR_COUNT_SELECTOR, [1])
    ref.write(rm.WIDTH_ALIAS, [123])

    assert ref.write(rm.CONNECTION_RESET, [1]) == p.ACK_OK_WRITE

    assert ref.read(rm.MASTER_HOST_CONNECTION_ID)[1] == [0]
    # §10.3.32: StreamPacketSizeMax is "not initialized" after the reset.
    assert ref.read(rm.STREAM_PACKET_SIZE_MAX)[1] == [0]
    assert ref.read(rm.TEST_MODE)[1] == [0]
    assert ref.read(rm.TEST_ERROR_COUNT_SELECTOR)[1] == [0]
    assert ref.read(rm.CONNECTION_CONFIG)[1] == [rm.CONNECTION_CONFIG_RESET]
    # The use-case features are not on the §10.3.28 list.
    assert ref.read(rm.WIDTH_ALIAS)[1] == [123]
    # ConnectionReset self-clears.
    assert ref.read(rm.CONNECTION_RESET)[1] == [0]


def test_test_mode_is_one_bit(ref):
    # §10.3.35: 0 or 1; anything else is refused and changes nothing.
    assert ref.write(rm.TEST_MODE, [1]) == p.ACK_OK_WRITE
    assert ref.write(rm.TEST_MODE, [0xFFFF_FFFF]) == p.ACK_BAD_DATA
    assert ref.read(rm.TEST_MODE)[1] == [1]


def test_feature_limits_and_write_only(ref):
    # Width takes 1..4096 (the XML's Min / Max); a read of the write-only
    # AcquisitionStart is refused.
    assert ref.write(rm.WIDTH_ALIAS, [0]) == p.ACK_BAD_DATA
    assert ref.write(rm.WIDTH_ALIAS, [4097]) == p.ACK_BAD_DATA
    assert ref.write(rm.WIDTH_ALIAS, [4096]) == p.ACK_OK_WRITE
    assert ref.write(rm.STREAM_PACKET_SIZE_MAX, [1025]) == p.ACK_BAD_DATA
    assert ref.read(rm.ACQUISITION_START_ALIAS)[0] == p.ACK_WO_READ


# ------------------------------------------------------------- XML ----
def test_xml_rom(ref):
    with pytest.raises(Unknown):
        ref.read(rm.XML_BLOB_ADDR)
    ref.set_xml(b"<?xml version=")
    assert ref.read(rm.XML_BLOB_ADDR)[1] == [int.from_bytes(b"<?xm", "big")]
    assert ref.write(rm.XML_BLOB_ADDR, [0]) == p.ACK_RO_WRITE


# ----------------------------------------------------------- the map ----
def test_map_matches_the_generated_addresses():
    # regmodel and regmap are generated from one YAML; a divergence here
    # means one of them was edited by hand.
    by_name = {r[0]: r[1] for r in rmod.BOOTSTRAP}
    assert by_name["Standard"] == rm.STANDARD
    assert by_name["ConnectionReset"] == rm.CONNECTION_RESET
    assert by_name["TestPacketCountRx"] == rm.TEST_PACKET_COUNT_RX
    assert rmod.XML_BLOB_BYTES == rm.XML_BLOB_BYTES
    assert rmod.XML_URL == rm.XML_URL_STR
