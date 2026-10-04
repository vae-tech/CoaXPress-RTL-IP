// UVM-test_tx_trigger.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void txTrigger(Context& c) {
    c.needBench(bench::CAP_TRIG_IN | bench::CAP_CHARS, "toggle the device's trigger input and receive its triggers");
    uvmIdleConfig(c);
    const size_t edges = size_t(c.iparam("edges"));
    judgeDeviceTriggers(c, deviceTriggers(c, edges, c.iparam("gap_ms")), edges);
}
CXP_CHECK("UVM-test_tx_trigger", txTrigger);

}  // namespace

}  // namespace cxp::validation::checks::uvm
