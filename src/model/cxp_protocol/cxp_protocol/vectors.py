"""Emit golden vectors as JSON for the C/C++/Rust ports.

    python -m cxp_protocol.vectors > vectors.json
    python -m cxp_protocol.vectors --8b10b-text   # "byte k rd_in sym rd_out"

The ports check themselves against this file instead of mirroring the
Python code, so a bug shared by two implementations cannot hide.
"""

from __future__ import annotations

import json
import sys

from . import crc, enc8b10b, packets, stream
from .kcodes import be_word
from .quirks import DEVICE, SPEC, Quirks


def packet_vectors(q: Quirks) -> dict:
    """Encoded packets under one quirk profile.  Register values are
    native; ``*_data_wire`` gives the words they become on the wire."""
    wr = [0x0001_0028, 0xCAFE_BABE]
    rd = [0xC0A7_9AE5, 0x0001_0001]
    pk = {
        "ctrl_read": packets.ctrl_cmd(packets.OP_READ, 0x0000_0000, 4, q=q),
        "ctrl_write": packets.ctrl_cmd(packets.OP_WRITE, 0x0000_4014, data=wr, q=q),
        "ctrl_reset": packets.ctrl_cmd(packets.OP_RESET, q=q),
        "ack_read": packets.ctrl_ack(packets.ACK_OK_DATA, rd, q=q),
        "ack_write": packets.ctrl_ack(packets.ACK_OK_WRITE, q=q),
        "stream": packets.stream_packet(1, 7, [(w, 0) for w in range(8)], q=q),
    }
    out = {k: [w for w, _ in v] for k, v in pk.items()}
    out["ctrl_write_data"] = wr
    out["ctrl_write_data_wire"] = [be_word(v) for v in wr]
    out["ack_read_data"] = rd
    out["ack_read_data_wire"] = [be_word(v) for v in rd]
    out["quirks"] = q.active()
    return out


def build() -> dict:
    enc = []
    for rd in (0, 1):
        for b in range(256):
            s, r = enc8b10b.encode_byte(b, False, rd)
            enc.append({"byte": b, "k": 0, "rd_in": rd, "sym": s, "rd_out": r})
        for b in sorted(enc8b10b._K10):
            s, r = enc8b10b.encode_byte(b, True, rd)
            enc.append({"byte": b, "k": 1, "rd_in": rd, "sym": s, "rd_out": r})
    crc_cases = [
        {"words": [0x0400_0000, 0x0000_0000], "reg": crc.crc32([0x0400_0000, 0])},
        {"words": list(range(1, 17)), "reg": crc.crc32(range(1, 17))},
    ]
    pk = {
        "ctrl_read": packets.ctrl_cmd(packets.OP_READ, 0x0000_0000, 4),
        "ctrl_write": packets.ctrl_cmd(packets.OP_WRITE, 0x0000_4014, data=[0x0001_0028]),
        "ctrl_reset": packets.ctrl_cmd(packets.OP_RESET),
        "ack_read": packets.ctrl_ack(packets.ACK_OK_DATA, [0xC0A7_9AE5, 0x0001_0001]),
        "ack_write": packets.ctrl_ack(packets.ACK_OK_WRITE),
        "trigger_hs_rise": packets.trigger_hs(True, 3),
        "io_ack": packets.io_ack(),
        "stream": packets.stream_packet(1, 7, [(w, 0) for w in range(8)]),
        "linktest": packets.linktest_packet(),
        "rect_header": stream.image_header(stream.ImageMeta(1, 2, 640, 0, 480, 0,
                                                            stream.PIXFMT_MONO8), 160),
    }
    return {
        "enc8b10b": enc,
        "crc": crc_cases,
        "packets": {k: [[w, m] for w, m in v] for k, v in pk.items()},
        "trigger_ls_rise_51": [[b, int(k)] for b, k in packets.trigger_ls_chars(True, 51)],
        "pack10": {"pixels": list(range(16)), "words": stream.pack_line(range(16), 10)},
        "spec_packets": packet_vectors(SPEC),
        "device_packets": packet_vectors(DEVICE),
    }


if __name__ == "__main__":
    if "--8b10b-text" in sys.argv:
        for v in build()["enc8b10b"]:
            print(v["byte"], v["k"], v["rd_in"], v["sym"], v["rd_out"])
    else:
        json.dump(build(), sys.stdout, indent=1)
        sys.stdout.write("\n")
