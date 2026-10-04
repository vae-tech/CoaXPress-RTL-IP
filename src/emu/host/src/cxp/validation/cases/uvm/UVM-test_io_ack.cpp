// UVM-test_io_ack.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void ioAckCheck(Context& c) {
    c.needBench(bench::CAP_CHARS, "send Table 15 triggers and receive I/O acknowledgments");
    uvmIdleConfig(c);
    const auto kinds = randomKinds(size_t(c.iparam("triggers")), c.seed(3));
    const int spacing = c.iparam("spacing_ms");
    const HostTrigRun r = hostTriggers(c, kinds, spacing);
    judgeAcks(c, r, "");
    settleTrigOut(c);
}
CXP_CHECK("UVM-test_io_ack", ioAckCheck);

}  // namespace

}  // namespace cxp::validation::checks::uvm
