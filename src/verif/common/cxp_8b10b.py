"""8B/10B for the cocotb testbenches: a thin view of the golden encoder.

The tables and the D.x.A7 rule live in ``cxp_protocol.enc8b10b`` (repo
root), checked against IEEE 802.3 Clause 36, never against the RTL.

    encode_byte(data, k_flag, rd_in)        -> (symbol, rd_out)
    encode_word(byte_lanes, kmask, rd_in)   -> (40-bit symbol, rd_out)
    encode_stream([(lanes, kmask), ...])    -> ([40-bit symbols], rd_out)

Symbol layout: bit 0 = ``a`` (first bit on the line); P0 in bits [9:0].
"""

from cxp_protocol import enc8b10b as _enc
from cxp_protocol.enc8b10b import (  # noqa: F401
    decode_symbol, encode_chars, encode_stream, encode_word,
)
from cxp_protocol.kcodes import (  # noqa: F401
    D21_5, K27_7, K28_0, K28_1, K28_2, K28_3, K28_4, K28_5, K28_6, K29_7,
)


def encode_byte(data: int, k_flag: bool, rd_in: int):
    """Encode one character: (symbol, rd_out)."""
    return _enc.encode_byte(data, k_flag, rd_in)
