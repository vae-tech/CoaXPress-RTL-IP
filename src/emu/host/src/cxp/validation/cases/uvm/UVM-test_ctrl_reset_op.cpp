// UVM-test_ctrl_reset_op.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void ctrlResetOp(Context& c) {
    uvmIdleConfig(c);
    for (int i = 1; i <= c.iparam("resets"); ++i) {
        auto a = c.exchange(resetCmd());
        c.expect(is(a, Ack::RESET_OK), "control channel reset %d: %s", i, ackStr(a).c_str());
    }
    auto r = c.readRaw(Reg::STANDARD, 4);
    c.expect(readsStandard(r), "read of Standard after the resets: %s", ackStr(r).c_str());
}
CXP_CHECK("UVM-test_ctrl_reset_op", ctrlResetOp);

}  // namespace

}  // namespace cxp::validation::checks::uvm
