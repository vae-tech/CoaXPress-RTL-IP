"""IEEE 802.3 Clause 36 8B/10B code, as used by CoaXPress (§8.2.1).

Symbol layout (same as the RTL decoder input ``din``): bit 0 is ``a``, the
first bit on the line, and bit 9 is ``j``.  The IEEE tables below are
written MSB-first (``abcdei fghj`` left to right) and converted once.

Running disparity: 0 = RD-, 1 = RD+.

The encoder applies the D.x.A7 rule (IEEE 36.2.4.4): D.x.7 uses the
alternate 4b form when RD- and x in {17, 18, 20}, or RD+ and x in
{11, 13, 14}, so no false comma is ever produced.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# IEEE 802.3 Table 36-1a (5b/6b) and 36-1b (3b/4b), MSB-first: (RD-, RD+).
# ---------------------------------------------------------------------------
_6B: Dict[int, Tuple[int, int]] = {
    0: (0b100111, 0b011000), 1: (0b011101, 0b100010), 2: (0b101101, 0b010010),
    3: (0b110001, 0b110001), 4: (0b110101, 0b001010), 5: (0b101001, 0b101001),
    6: (0b011001, 0b011001), 7: (0b111000, 0b000111), 8: (0b111001, 0b000110),
    9: (0b100101, 0b100101), 10: (0b010101, 0b010101), 11: (0b110100, 0b110100),
    12: (0b001101, 0b001101), 13: (0b101100, 0b101100), 14: (0b011100, 0b011100),
    15: (0b010111, 0b101000), 16: (0b011011, 0b100100), 17: (0b100011, 0b100011),
    18: (0b010011, 0b010011), 19: (0b110010, 0b110010), 20: (0b001011, 0b001011),
    21: (0b101010, 0b101010), 22: (0b011010, 0b011010), 23: (0b111010, 0b000101),
    24: (0b110011, 0b001100), 25: (0b100110, 0b100110), 26: (0b010110, 0b010110),
    27: (0b110110, 0b001001), 28: (0b001110, 0b001110), 29: (0b101110, 0b010001),
    30: (0b011110, 0b100001), 31: (0b101011, 0b010100),
}
_4B: Dict[int, Tuple[int, int]] = {
    0: (0b1011, 0b0100), 1: (0b1001, 0b1001), 2: (0b0101, 0b0101),
    3: (0b1100, 0b0011), 4: (0b1101, 0b0010), 5: (0b1010, 0b1010),
    6: (0b0110, 0b0110), 7: (0b1110, 0b0001),          # D.x.P7
}
_4B_A7 = (0b0111, 0b1000)                               # D.x.A7

# IEEE 802.3 Table 36-2: the twelve K code groups, full 10 bits MSB-first.
_K10: Dict[int, Tuple[int, int]] = {
    0x1C: (0b0011110100, 0b1100001011),  # K28.0
    0x3C: (0b0011111001, 0b1100000110),  # K28.1
    0x5C: (0b0011110101, 0b1100001010),  # K28.2
    0x7C: (0b0011110011, 0b1100001100),  # K28.3
    0x9C: (0b0011110010, 0b1100001101),  # K28.4
    0xBC: (0b0011111010, 0b1100000101),  # K28.5
    0xDC: (0b0011110110, 0b1100001001),  # K28.6
    0xFC: (0b0011111000, 0b1100000111),  # K28.7
    0xF7: (0b1110101000, 0b0001010111),  # K23.7
    0xFB: (0b1101101000, 0b0010010111),  # K27.7
    0xFD: (0b1011101000, 0b0100010111),  # K29.7
    0xFE: (0b0111101000, 0b1000010111),  # K30.7
}


def _rev(v: int, n: int) -> int:
    return int(format(v, f"0{n}b")[::-1], 2)


def _rd_after(block: int, n: int, rd: int) -> int:
    """Running disparity after a sub-block (IEEE 36.2.4.3)."""
    ones = bin(block).count("1")
    if 2 * ones > n:
        return 1
    if 2 * ones < n:
        return 0
    # Balanced: 000111 / 0011 leave RD+, 111000 / 1100 leave RD-.
    if (n == 6 and block == 0b000111) or (n == 4 and block == 0b0011):
        return 1
    if (n == 6 and block == 0b111000) or (n == 4 and block == 0b1100):
        return 0
    return rd


def _encode_msb(byte: int, k: bool, rd: int) -> Tuple[int, int]:
    """(10-bit MSB-first symbol, rd_out)."""
    if k:
        if byte not in _K10:
            raise ValueError(f"0x{byte:02X} is not a K code")
        sym = _K10[byte][rd]
        rd_mid = _rd_after(sym >> 4, 6, rd)
        return sym, _rd_after(sym & 0xF, 4, rd_mid)
    x, y = byte & 0x1F, byte >> 5
    b6 = _6B[x][rd]
    rd_mid = _rd_after(b6, 6, rd)
    if y == 7 and ((rd_mid == 0 and x in (17, 18, 20)) or (rd_mid == 1 and x in (11, 13, 14))):
        b4 = _4B_A7[rd_mid]
    else:
        b4 = _4B[y][rd_mid]
    return (b6 << 4) | b4, _rd_after(b4, 4, rd_mid)


def encode_byte(byte: int, k: bool, rd: int) -> Tuple[int, int]:
    """Encode one character.  Returns (symbol in din layout, rd_out)."""
    if not 0 <= byte <= 0xFF:
        raise ValueError(f"byte {byte:#x} out of range")
    if rd not in (0, 1):
        raise ValueError("rd must be 0 or 1")
    msb, rd_out = _encode_msb(byte, bool(k), rd)
    return _rev(msb, 10), rd_out


def encode_word(lanes4: Sequence[int], kmask: int, rd: int) -> Tuple[int, int]:
    """Encode P0..P3 into a 40-bit value (P0 in bits [9:0])."""
    out = 0
    for i in range(4):
        sym, rd = encode_byte(lanes4[i], bool((kmask >> i) & 1), rd)
        out |= sym << (10 * i)
    return out, rd


def encode_stream(words: Iterable[Tuple[Sequence[int], int]], rd: int = 0) -> Tuple[List[int], int]:
    out = []
    for lanes4, kmask in words:
        sym, rd = encode_word(lanes4, kmask, rd)
        out.append(sym)
    return out, rd


def encode_chars(chars: Iterable[Tuple[int, bool]], rd: int = 0) -> Tuple[List[int], int]:
    """Encode a character stream [(byte, k), ...] into 10-bit symbols."""
    out = []
    for byte, k in chars:
        sym, rd = encode_byte(byte, k, rd)
        out.append(sym)
    return out, rd


# ---------------------------------------------------------------------------
# Decoder (host-side model): exact table inverse.
# ---------------------------------------------------------------------------
_DEC: Dict[int, List[Tuple[int, bool, int, int]]] = {}


def _build_decoder() -> None:
    for rd in (0, 1):
        for b in range(256):
            sym, rd_out = encode_byte(b, False, rd)
            _DEC.setdefault(sym, []).append((b, False, rd, rd_out))
        for b in _K10:
            sym, rd_out = encode_byte(b, True, rd)
            _DEC.setdefault(sym, []).append((b, True, rd, rd_out))


_build_decoder()


def decode_symbol(sym: int, rd: int) -> Tuple[Optional[int], bool, int, bool, bool]:
    """Decode one symbol: (byte or None, k, rd_out, code_err, disp_err)."""
    entries = _DEC.get(sym & 0x3FF)
    if not entries:
        ones = bin(sym & 0x3FF).count("1")
        rd_out = 1 if ones > 5 else 0 if ones < 5 else rd
        return None, False, rd_out, True, False
    for b, k, rd_in, rd_out in entries:
        if rd_in == rd:
            return b, k, rd_out, False, False
    b, k, _, rd_out = entries[0]
    return b, k, rd_out, False, True


def legal_symbols() -> Dict[int, List[Tuple[int, bool, int, int]]]:
    """Every legal code group -> [(byte, k, rd_in, rd_out), ...]."""
    return dict(_DEC)
