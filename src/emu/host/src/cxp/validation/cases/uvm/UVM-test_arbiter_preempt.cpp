// UVM-test_arbiter_preempt.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void arbiterPreempt(Context& c) {
    preserveLink(c);
    c.preserve(Reg::TEST_ERROR_COUNT_SELECTOR);
    const bool pixel = usePixelPort(c);
    preemptRound(c, pixel ? -1 : selectBars(c), pixel, c.seed(2), "");
}
CXP_CHECK("UVM-test_arbiter_preempt", arbiterPreempt);

}  // namespace

}  // namespace cxp::validation::checks::uvm
