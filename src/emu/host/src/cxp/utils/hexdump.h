// Hex-dump / packet-trace formatting helpers (cxp/utils/hexdump.py).
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace cxp {

// Classic offset / hex / ASCII dump.
std::string hexdump(const std::vector<uint8_t>& data, int width = 16);

// Format a list of 32-bit words as grouped hex.
std::string wordsHex(const std::vector<uint32_t>& words, int per_line = 8);

}  // namespace cxp
