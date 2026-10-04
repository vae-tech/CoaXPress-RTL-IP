"""K-characters, IDLE word and word-lane helpers (CXP-001-2015 §8.2).

Word convention used throughout the package: a 32-bit word holds the four
characters P0..P3 with P0 in bits [7:0] (P0 is transmitted first), and a
4-bit kmask flags which lanes are K-characters (bit i = lane Pi).
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

MASK32 = 0xFFFF_FFFF

# 8B/10B control characters as byte values (Kx.y = y*32 + x).
K28_0 = 0x1C
K28_1 = 0x3C
K28_2 = 0x5C   # trigger indication (Tables 15/16)
K28_3 = 0x7C   # stream marker (§9.4)
K28_4 = 0x9C   # trigger indication (Tables 15/16)
K28_5 = 0xBC   # comma, IDLE P0
K28_6 = 0xDC   # I/O acknowledgment indication (Table 17)
K28_7 = 0xFC
K23_7 = 0xF7
K27_7 = 0xFB   # start of packet (Table 18)
K29_7 = 0xFD   # end of packet (Table 18)
K30_7 = 0xFE
D21_5 = 0xB5   # IDLE P3

K_CODES = (K28_0, K28_1, K28_2, K28_3, K28_4, K28_5, K28_6, K28_7,
           K23_7, K27_7, K29_7, K30_7)

KMASK_ALL = 0b1111
KMASK_NONE = 0b0000

# Table 14: IDLE = K28.5 K28.1 K28.1 D21.5 (P0..P3).
IDLE_WORD = K28_5 | (K28_1 << 8) | (K28_1 << 16) | (D21_5 << 24)
IDLE_KMASK = 0b0111

# §8.2.5.1: at least one IDLE every N words.
IDLE_MAX_INTERVAL_HS = 100
IDLE_MAX_INTERVAL_LS = 10_000


def rep4(b: int) -> int:
    """Replicate a byte into all four lanes (§8.2.2.1)."""
    b &= 0xFF
    return b | (b << 8) | (b << 16) | (b << 24)


def lanes(word: int) -> List[int]:
    """Split a word into [P0, P1, P2, P3]."""
    return [(word >> (8 * i)) & 0xFF for i in range(4)]


def from_lanes(p: Sequence[int]) -> int:
    """Join [P0, P1, P2, P3] into a word."""
    return (p[0] & 0xFF) | ((p[1] & 0xFF) << 8) | ((p[2] & 0xFF) << 16) | ((p[3] & 0xFF) << 24)


def bswap32(w: int) -> int:
    return int.from_bytes((w & MASK32).to_bytes(4, "little"), "big")


def be_word(value: int) -> int:
    """A 32-bit value sent big-endian (§8.2.1: P0 = MSB) as a word."""
    return bswap32(value)


def from_be_word(word: int) -> int:
    return bswap32(word)


def vote(word: int) -> Tuple[int, bool]:
    """3-of-4 majority of a replicated word: (byte, ok).

    ok is False when no byte appears in at least three lanes.
    """
    ls = lanes(word)
    best = max(set(ls), key=ls.count)
    return best, ls.count(best) >= 3


def all_k(word: int, kmask: int, kc: int) -> bool:
    return kmask == KMASK_ALL and all(b == kc for b in lanes(word))


def is_idle(word: int, kmask: int) -> bool:
    return kmask == IDLE_KMASK and (word & 0x00FF_FFFF) == (IDLE_WORD & 0x00FF_FFFF)
