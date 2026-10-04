// Helpers the neg/ cases share (moved from checks/neg.cpp).
// Dissolved into fixtures / scoreboards in M2.
#pragma once

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::neg {

bool coveredByXml(Context& c, uint32_t addr);

void rejectValue(Context& c, const char* name, uint32_t addr, uint32_t bad);

}  // namespace cxp::validation::checks::neg
