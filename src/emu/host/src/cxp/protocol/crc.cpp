#include "cxp/protocol/crc.h"

#include <array>

namespace cxp {

namespace {

std::array<uint32_t, 256> makeTable() {
    std::array<uint32_t, 256> t{};
    for (uint32_t i = 0; i < 256; ++i) {
        uint32_t c = i;
        for (int k = 0; k < 8; ++k) c = (c & 1) ? 0xEDB88320u ^ (c >> 1) : c >> 1;
        t[i] = c;
    }
    return t;
}

const std::array<uint32_t, 256>& table() {
    static const std::array<uint32_t, 256> t = makeTable();
    return t;
}

}  // namespace

Majority majorityByte(uint32_t word) {
    uint8_t lanes[4];
    for (int i = 0; i < 4; ++i) lanes[i] = static_cast<uint8_t>(word >> (8 * i));
    uint8_t best = 0;
    int best_count = 0;
    for (int i = 0; i < 4; ++i) {
        int c = 0;
        for (int j = 0; j < 4; ++j) c += lanes[j] == lanes[i];
        if (c > best_count) {
            best = lanes[i];
            best_count = c;
        }
    }
    return {best, best_count >= 3};
}

uint32_t crc32Bytes(const uint8_t* data, size_t n, uint32_t crc) {
    const auto& t = table();
    crc = ~crc;
    for (size_t i = 0; i < n; ++i) crc = t[(crc ^ data[i]) & 0xFF] ^ (crc >> 8);
    return ~crc;
}

uint32_t crc32Words(const uint32_t* words, size_t n) {
    const auto& t = table();
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < n; ++i) {
        uint32_t w = words[i];
        for (int k = 0; k < 4; ++k) {
            crc = t[(crc ^ (w >> (8 * k))) & 0xFF] ^ (crc >> 8);
        }
    }
    return ~crc;
}

}  // namespace cxp
