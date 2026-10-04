// UVM-test_trigger_uplink.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void triggerUplink(Context& c) {
    c.needBench(bench::CAP_CHARS | bench::CAP_TRIG_OUT, "send Table 15 triggers and watch the recreated trigger");
    uvmIdleConfig(c);
    const auto kinds = randomKinds(size_t(c.iparam("triggers")), c.seed(3));
    const int spacing = c.iparam("spacing_ms");
    const HostTrigRun r = hostTriggers(c, kinds, spacing);
    judgeEdges(c, r, "");
    judgeAcks(c, r, "");
    settleTrigOut(c);
}
CXP_CHECK("UVM-test_trigger_uplink", triggerUplink);

}  // namespace

}  // namespace cxp::validation::checks::uvm
