"""cxp_protocol checked against CXP-001-2015 and IEEE 802.3, never the RTL."""

from cxp_protocol import crc, enc8b10b as e, packets as p, stream as s
from cxp_protocol.kcodes import (IDLE_KMASK, IDLE_WORD, K27_7, K28_1, K28_2, K28_4, K28_5,
                                 K28_6, K29_7, lanes, rep4)
from cxp_protocol.quirks import DEVICE, SPEC


def msb(sym):
    """din layout -> IEEE 'abcdei fghj' string."""
    return format(int(format(sym, "010b")[::-1], 2), "010b")


# --------------------------------------------------------------- 8B/10B ----
def test_8b10b_table_spot_values():
    # IEEE 802.3 Table 36-1a/b and 36-2.
    assert msb(e.encode_byte(0x00, False, 0)[0]) == "1001110100"   # D0.0 RD-
    assert msb(e.encode_byte(0x00, False, 1)[0]) == "0110001011"   # D0.0 RD+
    assert msb(e.encode_byte(0xB5, False, 0)[0]) == "1010101010"   # D21.5
    assert msb(e.encode_byte(K28_5, True, 0)[0]) == "0011111010"   # K28.5 RD-
    assert msb(e.encode_byte(K28_5, True, 1)[0]) == "1100000101"   # K28.5 RD+
    assert msb(e.encode_byte(K27_7, True, 0)[0]) == "1101101000"


def test_8b10b_a7_rule():
    # D17.7 / D18.7 / D20.7 at RD- and D11.7 / D13.7 / D14.7 at RD+ use A7.
    assert msb(e.encode_byte(0xF1, False, 0)[0]) == "1000110111"
    assert msb(e.encode_byte(0xF2, False, 0)[0]) == "0100110111"
    assert msb(e.encode_byte(0xF4, False, 0)[0]) == "0010110111"
    assert msb(e.encode_byte(0xEB, False, 1)[0]) == "1101001000"
    assert msb(e.encode_byte(0xED, False, 1)[0]) == "1011001000"
    assert msb(e.encode_byte(0xEE, False, 1)[0]) == "0111001000"
    # ...and the primary form elsewhere.
    assert msb(e.encode_byte(0xF1, False, 1)[0]) == "1000110001"   # D17.P7 at RD+


def _comma(bits):
    return "0011111" in bits or "1100000" in bits


def test_8b10b_no_false_comma_in_data():
    # A comma (0011111 / 1100000) never appears in a data stream, not even
    # straddling two characters (the property the A7 rule exists for).
    for rd0 in (0, 1):
        for a in range(256):
            sa, rd1 = e.encode_byte(a, False, rd0)
            assert not _comma(msb(sa))
            for b in range(256):
                sb, _ = e.encode_byte(b, False, rd1)
                assert not _comma((msb(sa) + msb(sb))[1:-1]), (a, b, rd0)


def test_8b10b_disparity_and_round_trip():
    for rd in (0, 1):
        for b in list(range(256)):
            sym, rd_out = e.encode_byte(b, False, rd)
            ones = bin(sym).count("1")
            assert ones in (4, 5, 6)
            assert e.decode_symbol(sym, rd)[:3] == (b, False, rd_out)
        for k in e._K10:
            sym, rd_out = e.encode_byte(k, True, rd)
            assert e.decode_symbol(sym, rd)[:3] == (k, True, rd_out)
    # A symbol at the wrong RD is a disparity error; garbage is a code error.
    sym, _ = e.encode_byte(0x00, False, 0)
    assert e.decode_symbol(sym, 1)[4]
    assert e.decode_symbol(0, 0)[3]


def test_8b10b_running_disparity_bounded():
    import random
    rng = random.Random(1)
    rd, disp = 0, 0
    for _ in range(20000):
        sym, rd = e.encode_byte(rng.randrange(256), False, rd)
        disp += 2 * bin(sym).count("1") - 10
        assert -2 <= disp <= 2


# ------------------------------------------------------------------- CRC ----
def test_crc_worked_example_8_2_2_2():
    # K27.7 x4, 0x02 x4, 00 00 00 04, 00 00 00 00, CRC 56 86 5D 6F, K29.7 x4
    beats = p.ctrl_cmd(p.OP_READ, 0x0000_0000, 4)
    assert beats[0] == (rep4(K27_7), 0xF)
    assert beats[1] == (rep4(0x02), 0)
    assert lanes(beats[2][0]) == [0x00, 0x00, 0x00, 0x04]
    assert lanes(beats[3][0]) == [0x00, 0x00, 0x00, 0x00]
    assert lanes(beats[4][0]) == [0x56, 0x86, 0x5D, 0x6F]
    assert beats[5] == (rep4(K29_7), 0xF)
    assert len(beats) == 1 + 6 - 1  # N + 6 words, N = 0


def test_crc_residue():
    words = [0x1234_5678, 0x9ABC_DEF0]
    reg = crc.crc32(words)
    # Folding the transmitted CRC word leaves a zero register (§8.2.2.2 comment).
    assert crc.crc_update(reg, [crc.crc_wire(reg)]) == 0
    # The register is the 802.3 CRC without its final XOR.
    import zlib
    raw = b"".join(w.to_bytes(4, "little") for w in words)
    assert reg == zlib.crc32(raw) ^ 0xFFFF_FFFF
    assert crc.crc_word([0x0400_0000, 0]) == 0x6F5D_8656   # 56 86 5D 6F


# ------------------------------------------------------ control channel ----
def test_ctrl_write_and_ack_layout():
    b = p.ctrl_cmd(p.OP_WRITE, 0x0000_4014, data=[0x0001_0028])
    assert len(b) == 1 + 6                       # N + 6 words (N = 1)
    assert lanes(b[2][0]) == [0x01, 0x00, 0x00, 0x04]
    assert lanes(b[3][0]) == [0x00, 0x00, 0x40, 0x14]
    assert lanes(b[4][0]) == [0x00, 0x01, 0x00, 0x28]   # big-endian data
    c = p.parse_ctrl_cmd([w for w, _ in b[1:-1]])
    assert (c.op, c.addr, c.size, c.data, c.crc_ok) == (1, 0x4014, 4, [0x10028], True)

    a = p.ctrl_ack(p.ACK_OK_DATA, [0xC0A7_9AE5])
    assert len(a) == 1 + 6                       # N + 6 words
    assert a[2] == (rep4(0x00), 0)
    assert lanes(a[3][0]) == [0, 0, 0, 4]        # one Size word = B
    r = p.parse_ctrl_ack([w for w, _ in a[1:-1]])
    assert (r.code, r.size, r.data, r.crc_ok) == (0, 4, [0xC0A7_9AE5], True)
    # B = 3: one data word, the pad byte (P3 on the big-endian wire) is 0.
    a3 = p.ctrl_ack(p.ACK_OK_DATA, [0x1122_3344], size=3)
    assert lanes(a3[3][0]) == [0, 0, 0, 3] and lanes(a3[4][0]) == [0x11, 0x22, 0x33, 0x00]
    assert p.parse_ctrl_ack([w for w, _ in a3[1:-1]]).crc_ok
    # Codes other than 0x00 / 0x04 omit Length, Data and CRC.
    assert [w for w, _ in p.ctrl_ack(p.ACK_OK_WRITE)] == [
        rep4(K27_7), rep4(0x03), rep4(0x01), rep4(K29_7)]


def test_device_quirk_profile_round_trips():
    b = p.ctrl_cmd(p.OP_READ, 0x10, 8, q=DEVICE)
    assert len(b) == 6                           # Table 21 header, N = 0
    c = p.parse_ctrl_cmd([w for w, _ in b[1:-1]], DEVICE)
    assert (c.op, c.addr, c.size, c.crc_ok) == (0, 0x10, 8, True)
    a = p.ctrl_ack(0, [1, 2], q=DEVICE)
    assert len(a) == 2 + 6                       # Table 22, N = 2
    r = p.parse_ctrl_ack([w for w, _ in a[1:-1]], DEVICE)
    assert (r.size, r.data, r.crc_ok) == (8, [1, 2], True)


# -------------------------------------------------------------- I/O ----
def test_trigger_and_ioack_formats():
    assert p.trigger_ls_chars(True, 51) == [(K28_2, True), (K28_4, True), (K28_4, True),
                                             (51, False), (51, False), (51, False)]
    assert p.trigger_ls_chars(False, 187)[:3] == [(K28_4, True), (K28_2, True), (K28_2, True)]
    assert p.trigger_hs(True, 2) == [(rep4(K28_4), 0xF), (rep4(2), 0)]
    assert p.trigger_hs(False) == [(rep4(K28_2), 0xF), (0, 0)]
    assert p.io_ack() == [(rep4(K28_6), 0xF), (rep4(0x01), 0)]


def test_uplink_receiver_extracts_ls_trigger_inside_packet():
    cmd = p.beats_to_chars(p.ctrl_cmd(p.OP_READ, 0x4000, 4))
    idle = p.beats_to_chars([p.IDLE] * 3)
    chars = idle + p.insert_chars(cmd, 9, p.trigger_ls_chars(True, 51)) + idle
    rx = p.UplinkReceiver()
    ev = rx.feed(chars)
    kinds = [x.kind for x in ev]
    assert kinds == ["trigger", "long"]
    assert ev[0].rising and ev[0].delay == 51
    c = p.parse_ctrl_cmd([w for w, _ in ev[1].beats[1:-1]])
    assert c.addr == 0x4000 and c.crc_ok


def test_uplink_receiver_votes_ls_trigger():
    """§8.2.2: two of three leader / Delay characters decide; no majority
    of the Delay is a glitch, and the six characters still go."""
    cmd = p.beats_to_chars(p.ctrl_cmd(p.OP_READ, 0x4000, 4))
    idle = p.beats_to_chars([p.IDLE] * 3)
    bad_lead = p.trigger_ls_chars(False, 187)
    bad_lead[1] = (0x55, False)                  # K28.2 hit by an error
    no_vote = p.trigger_ls_chars(True, 51)[:3] + [(10, False), (20, False), (30, False)]
    chars = idle + p.insert_chars(p.insert_chars(cmd, 13, no_vote), 5, bad_lead) + idle
    ev = p.UplinkReceiver().feed(chars)
    assert [x.kind for x in ev] == ["trigger", "trigger_glitch", "long"]
    assert not ev[0].rising and ev[0].delay == 187
    assert p.parse_ctrl_cmd([w for w, _ in ev[2].beats[1:-1]]).crc_ok


def test_device_speaks_the_spec():
    assert DEVICE.active() == [] and DEVICE == SPEC


# ---------------------------------------------------------------- stream ----
def test_stream_packet_table19():
    pl = [(w, 0) for w in (10, 20, 30)]
    b = p.stream_packet(1, 5, pl)
    assert len(b) == 3 + 8                       # N + 8 words
    assert [w for w, _ in b[2:6]] == [rep4(1), rep4(5), rep4(0), rep4(3)]
    assert b[-2][0] == crc.crc_wire(crc.crc32([10, 20, 30]))   # data only
    sp = p.parse_stream_packet(b[1:-1])
    assert (sp.streamid, sp.tag, sp.dsizep, sp.crc_ok) == (1, 5, 3, True)


def test_linktest_table23():
    b = p.linktest_packet()
    assert len(b) == 1027
    assert lanes(b[2][0]) == [0x00, 0x01, 0x02, 0x03]
    assert lanes(b[2 + 63][0]) == [0xFC, 0xFD, 0xFE, 0xFF]
    assert lanes(b[2 + 64][0]) == [0x00, 0x01, 0x02, 0x03]
    assert p.linktest_errors(p.linktest_payload()) == 0


def test_pixel_packing_figures_27_to_31():
    # Figure 28: D(0) bits 9..2 in P0, D(0) 1..0 in P1 bits 7..6.
    w = s.pack_line([0x3FF, 0, 0, 0], 10)
    assert lanes(w[0])[:2] == [0xFF, 0xC0]
    assert len(s.pack_line(range(16), 10)) == 5
    assert len(s.pack_line(range(16), 14)) == 7          # Figure 30
    assert lanes(s.pack_line([0xABCD], 16)[0])[:2] == [0xAB, 0xCD]
    for bits in (8, 10, 12, 14, 16):
        px = [(i * 37) & ((1 << bits) - 1) for i in range(13)]
        assert s.unpack_line(s.pack_line(px, bits), bits, 13) == px


def test_pixfmt_table25():
    assert s.pixfmt_desc(0x0105).bits == 16
    assert s.pixfmt_desc(0x0104).bits == 14
    assert s.device_bits(0x0104) == 14 and s.device_bits(0x0104, DEVICE) == 14
    assert s.pixfmt_desc(0x0311).supported and s.pixfmt_desc(0x0401).components == 3
    assert not s.pixfmt_desc(0x0107).supported and s.pixfmt_desc(0x0105).bits == 16


def test_image_header_tables_38_to_41():
    m = s.ImageMeta(streamid=1, sourcetag=0x1234, xsize=640, xoffs=8, ysize=480,
                    pixfmt=s.PIXFMT_MONO10)
    h = s.image_header(m, s.dsizel(640, 10))
    assert len(h) == 25 and h[0] == s.MARKER
    assert [w for w, _ in h[17:20]] == [rep4(0), rep4(0), rep4(200)]   # DsizeL words
    assert len(s.line_marker(m)) == 2
    m.arbitrary = True
    assert len(s.image_header(m)) == 16 and len(s.line_marker(m, 200)) == 11


def test_reassembler_round_trip():
    m = s.ImageMeta(streamid=3, xsize=10, ysize=4, pixfmt=s.PIXFMT_MONO12)
    lines = [[(x * 100 + y) & 0xFFF for x in range(10)] for y in range(4)]
    payload = s.image_stream(m, lines)
    ra = s.StreamReassembler()
    for tag, chunk in enumerate(s.chop(payload, 7)):
        ra.push_packet(p.stream_packet(3, tag, chunk)[1:-1])
    assert not ra.errors
    assert len(ra.images) == 1
    assert ra.images[0].pixels(12) == lines


def test_deframer_handles_insertion_and_stretching():
    pkt = p.stream_packet(1, 0, [(i, 0) for i in range(6)])
    beats = pkt[:4] + p.trigger_hs(True) + [p.IDLE] + pkt[4:] + p.io_ack()
    d = p.Deframer()
    d.feed(beats)
    assert [f.kind for f in d.frames] == ["trigger", "long", "ioack"]
    assert not d.errors
    assert d.frames[1].beats == pkt
    assert IDLE_WORD & 0xFF == K28_5 and IDLE_KMASK == 0b0111 and (IDLE_WORD >> 8) & 0xFF == K28_1


def test_pfnc_and_msb_align():
    """§11.2.1.6 PFNC -> PixelF; §9.4.2 Figure 32 MSB alignment."""
    from cxp_protocol import stream as gs
    assert gs.pfnc_to_pixelf(0x0108_0001) == 0x0101
    assert gs.pfnc_to_pixelf(0x0110_0025) == 0x0104
    assert gs.pfnc_to_pixelf(0x0101) == 0
    assert gs.msb_align(0xABC, 12, 8) == 0xAB
    assert gs.msb_align(0x7FFF, 15, 16) == 0xFFFE
    assert gs.msb_align(0xAB, 8, 8) == 0xAB
