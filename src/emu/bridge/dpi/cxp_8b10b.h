// cxp_8b10b.h — 8B/10B encoder shared by the src/emu/bridge DPI bridge and its
// golden-vector check (check_8b10b.c).
#ifndef CXP_8B10B_H
#define CXP_8B10B_H

#include <stdint.h>

// IEEE 802.3 Clause 36 8B/10B encoder (CXP-001-2015 §8.2.1).
//   * symbol layout abcdei fghj, bit 0 = 'a' (first transmitted)
//   * rd 0 = RD-, 1 = RD+
//   * D.x.A7 used for x in {17,18,20} at RD- and {11,13,14} at RD+
// Checked against the golden cxp_protocol vectors: make -C src/emu/bridge check_8b10b

// 5b/6b: {RD- form, RD+ form} MSB-first abcdei (bit5=a..bit0=i).
static const uint16_t D5B6B[32][2] = {
    {0x27, 0x18}, {0x1D, 0x22}, {0x2D, 0x12}, {0x31, 0x31},
    {0x35, 0x0A}, {0x29, 0x29}, {0x19, 0x19}, {0x38, 0x07},
    {0x39, 0x06}, {0x25, 0x25}, {0x15, 0x15}, {0x34, 0x34},
    {0x0D, 0x0D}, {0x2C, 0x2C}, {0x1C, 0x1C}, {0x17, 0x28},
    {0x1B, 0x24}, {0x23, 0x23}, {0x13, 0x13}, {0x32, 0x32},
    {0x0B, 0x0B}, {0x2A, 0x2A}, {0x1A, 0x1A}, {0x3A, 0x05},
    {0x33, 0x0C}, {0x26, 0x26}, {0x16, 0x16}, {0x36, 0x09},
    {0x0E, 0x0E}, {0x2E, 0x11}, {0x1E, 0x21}, {0x2B, 0x14},
};

// 3b/4b: {RD- form, RD+ form} MSB-first fghj (bit3=f..bit0=j).
static const uint8_t D3B4B[8][2] = {
    {0xB, 0x4}, {0x9, 0x9}, {0x5, 0x5}, {0xC, 0x3},
    {0xD, 0x2}, {0xA, 0xA}, {0x6, 0x6}, {0xE, 0x1},
};

// K-codes: byte -> {RD- 10b MSB-first, RD+ 10b MSB-first}.
static inline int k_lookup(unsigned b, uint16_t out[2]) {
    switch (b) {
        case 0x1C: out[0] = 0x0F4; out[1] = 0x30B; return 1; // K28.0
        case 0x3C: out[0] = 0x0F9; out[1] = 0x306; return 1; // K28.1
        case 0x5C: out[0] = 0x0F5; out[1] = 0x30A; return 1; // K28.2
        case 0x7C: out[0] = 0x0F3; out[1] = 0x30C; return 1; // K28.3
        case 0x9C: out[0] = 0x0F2; out[1] = 0x30D; return 1; // K28.4
        case 0xBC: out[0] = 0x0FA; out[1] = 0x305; return 1; // K28.5
        case 0xDC: out[0] = 0x0F6; out[1] = 0x309; return 1; // K28.6
        case 0xFC: out[0] = 0x0F8; out[1] = 0x307; return 1; // K28.7
        case 0xF7: out[0] = 0x3A8; out[1] = 0x057; return 1; // K23.7
        case 0xFB: out[0] = 0x368; out[1] = 0x097; return 1; // K27.7
        case 0xFD: out[0] = 0x2E8; out[1] = 0x117; return 1; // K29.7
        case 0xFE: out[0] = 0x1E8; out[1] = 0x217; return 1; // K30.7
        default:   return 0;
    }
}

static inline int popcount(unsigned v) {
    int c = 0;
    while (v) { c += v & 1u; v >>= 1; }
    return c;
}

// MSB-first n-bit value -> LSB-first (din convention: bit0 = first tx'd).
static inline unsigned msb_to_lsb_first(unsigned s, int n) {
    unsigned out = 0;
    for (int i = 0; i < n; i++)
        out |= ((s >> (n - 1 - i)) & 1u) << i;
    return out;
}

// RD leaving a sub-block (IEEE 36.2.4.3): 000111 / 0011 leave RD+,
// 111000 / 1100 leave RD-, other balanced blocks keep the entering RD.
static inline int rd_after_form(unsigned form_msb, int n_bits, int rd_in) {
    int delta = 2 * popcount(form_msb) - n_bits; // +2, 0, -2
    if (delta > 0) return 1;
    if (delta < 0) return 0;
    if ((n_bits == 6 && form_msb == 0x07) || (n_bits == 4 && form_msb == 0x3)) return 1;
    if ((n_bits == 6 && form_msb == 0x38) || (n_bits == 4 && form_msb == 0xC)) return 0;
    return rd_in;
}

// Encode one byte -> 10-bit symbol (din layout). Returns symbol; *rd updated.
static inline unsigned encode_byte(unsigned data, int k_flag, int *rd) {
    data &= 0xFF;
    if (k_flag) {
        uint16_t k[2];
        if (!k_lookup(data, k)) {
            // Not a legal K-code: fall back to a D-code so the sim keeps
            // running (should never happen for SOP/EOP/IDLE/markers).
            k_flag = 0;
        } else {
            unsigned msb = k[*rd & 1];
            int ones = popcount(msb);
            if (ones == 6)      *rd = 1;
            else if (ones == 4) *rd = 0;
            // 5 ones -> RD unchanged
            return msb_to_lsb_first(msb, 10);
        }
    }
    unsigned x = data & 0x1F;
    unsigned y = (data >> 5) & 0x07;

    unsigned sub6 = D5B6B[x][*rd & 1];
    int rd_mid = rd_after_form(sub6, 6, *rd);

    unsigned sub4 = D3B4B[y][rd_mid & 1];
    // D.x.A7 (IEEE 36.2.4.4): avoids a false comma across the boundary.
    if (y == 7 && ((rd_mid == 0 && (x == 17 || x == 18 || x == 20)) ||
                   (rd_mid == 1 && (x == 11 || x == 13 || x == 14))))
        sub4 = rd_mid ? 0x8u : 0x7u;
    *rd = rd_after_form(sub4, 4, rd_mid);

    return msb_to_lsb_first(sub6, 6) | (msb_to_lsb_first(sub4, 4) << 6);
}

// Encode a 32-bit word (4 lanes, P0 = byte 0 = sym40[9:0]). *rd persists.
static inline uint64_t encode_word(unsigned data, unsigned kmask, int *rd) {
    uint64_t sym40 = 0;
    for (int i = 0; i < 4; i++) {
        unsigned sym = encode_byte((data >> (8 * i)) & 0xFF,
                                   (kmask >> i) & 1u, rd);
        sym40 |= (uint64_t)sym << (10 * i);
    }
    return sym40;
}

#endif // CXP_8B10B_H
