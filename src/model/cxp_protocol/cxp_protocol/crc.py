"""CoaXPress CRC-32 (CXP-001-2015 §8.2.2.2).

Polynomial 0x04C11DB7 (reflected 0xEDB88320), seed 0xFFFFFFFF, data bit 0
first within a character, P0 first within a word, no final XOR.  The
register is sent with its MSB in P0 bit 0, which for the reflected
register means the word on the wire (P0 in bits [7:0]) is the register
value itself.  The §8.2.2.2 worked example (control read of address 0)
pins this down: the CRC characters are 0x56 0x86 0x5D 0x6F.
"""

from __future__ import annotations

from typing import Iterable

from .kcodes import MASK32

CRC_SEED = 0xFFFF_FFFF
CRC_POLY_REFLECTED = 0xEDB8_8320

_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ (CRC_POLY_REFLECTED if _c & 1 else 0)
    _TABLE.append(_c)


def crc_update(reg: int, words: Iterable[int]) -> int:
    for w in words:
        for i in range(4):
            reg = (reg >> 8) ^ _TABLE[(reg ^ (w >> (8 * i))) & 0xFF]
    return reg & MASK32


def crc32(words: Iterable[int]) -> int:
    """CRC register after folding ``words`` (P0 first) from the seed."""
    return crc_update(CRC_SEED, words)


def crc_wire(reg: int) -> int:
    """The CRC word as transmitted (P0 in bits [7:0])."""
    return reg & MASK32


def crc_word(words: Iterable[int]) -> int:
    """CRC word to append after ``words``."""
    return crc_wire(crc32(words))
