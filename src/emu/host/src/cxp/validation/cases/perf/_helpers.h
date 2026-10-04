// Helpers the perf/ cases share (moved from checks/perf.cpp).
// Dissolved into fixtures / scoreboards in M2.
#pragma once

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::perf {

// ===========================================================================
// 16. Performance
// ===========================================================================
struct StreamStats {
    size_t packets = 0, crc_bad = 0, tag_breaks = 0, images = 0, incomplete = 0, st_breaks = 0;
    uint64_t payload_bytes = 0;
};

StreamStats analyse(const std::vector<Captured>& cap);

std::vector<Captured> streamFor(Context& c, int seconds);

}  // namespace cxp::validation::checks::perf
