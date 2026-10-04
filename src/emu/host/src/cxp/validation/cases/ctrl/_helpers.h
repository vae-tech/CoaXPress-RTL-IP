// Helpers the ctrl/ cases share (moved from checks/ctrl.cpp).
// Dissolved into fixtures / scoreboards in M2.
#pragma once

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::ctrl {

struct RegRef { std::string name; uint32_t addr; uint32_t bytes; bool writable; };

std::vector<RegRef> registerList(Context& c);

// `reads` reads of every register; after each of the first `write_backs`
// reads of a writable one, the value goes back.
void latencySweep(Context& c, const std::vector<RegRef>& regs, int reads, int write_backs, const char* mode,
                  std::vector<double>& all, size_t& lost);

}  // namespace cxp::validation::checks::ctrl
