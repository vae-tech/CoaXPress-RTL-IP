// UVM-test_arbiter_stream_underflow.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void arbiterStreamUnderflow(Context& c) {
    c.needBench(bench::CAP_CHARS, "send host triggers after the image");
    shortTailImage(c);
    const HostTrigRun r = hostTriggers(c, randomKinds(size_t(c.iparam("triggers")), c.seed(3)), c.iparam("spacing_ms"));
    judgeAcks(c, r, "after the image: ");
    settleTrigOut(c);
}
CXP_CHECK("UVM-test_arbiter_stream_underflow", arbiterStreamUnderflow);

}  // namespace

}  // namespace cxp::validation::checks::uvm
