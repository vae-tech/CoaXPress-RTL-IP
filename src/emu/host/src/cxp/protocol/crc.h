// CXP CRC32 and byte-replication helpers (cxp/protocol/crc.py).
//
// These match the golden cxp_protocol package (cxp_protocol/crc.py), which
// the RTL device speaks:
//
// * CRC32 is the IEEE 802.3 polynomial computed over 32-bit words, P0
//   (the LSB byte) first.  crc32Words() returns the zlib value; the wire
//   word is crcToWire() of it (§8.2.2.2, see below).
// * TYPE, CODE, and stream header bytes are 4x byte-replicated (§8.2.2.1).
#pragma once

#include <cstddef>
#include <cstdint>

namespace cxp {

// Spec §9.2.5 per-byte 4x replication into a 32-bit word.
constexpr uint32_t replicateByte(uint8_t b) {
    return uint32_t(b) | (uint32_t(b) << 8) | (uint32_t(b) << 16) |
           (uint32_t(b) << 24);
}

struct Majority {
    uint8_t value;
    bool ok;  // false when the four lanes do not form a 3-of-4 majority
};

// Inverse of replicateByte().
Majority majorityByte(uint32_t word);

constexpr uint32_t bswap32(uint32_t v) {
    return (v >> 24) | ((v >> 8) & 0x0000FF00u) | ((v << 8) & 0x00FF0000u) |
           (v << 24);
}

// zlib-compatible CRC32 over raw bytes; pass the previous result to chain.
uint32_t crc32Bytes(const uint8_t* data, size_t n, uint32_t crc = 0);

// CRC32 over a sequence of 32-bit on-wire words (little-endian bytes).
uint32_t crc32Words(const uint32_t* words, size_t n);

// Lay a CRC32 value (zlib form, as crc32Words returns it) out as the
// on-wire word.  §8.2.2.2: no final XOR, and the reflected register goes
// out as-is (P0 = register[7:0]), so the wire word is ~zlib.  The
// §8.2.2.2 worked example (read of address 0) ends in 0x56 0x86 0x5D 0x6F;
// cxp_protocol/crc.py is the reference.
constexpr uint32_t crcToWire(uint32_t crc) { return ~crc; }
constexpr uint32_t wireToCrc(uint32_t word) { return ~word; }

}  // namespace cxp
