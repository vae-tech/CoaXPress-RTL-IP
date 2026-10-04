// UVM-test_ctrl_reset_storm.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void ctrlResetStorm(Context& c) {
    uvmIdleConfig(c);
    const size_t n = size_t(c.iparam("resets"));
    auto acks = c.exchangeMany(std::vector<Words>(n, resetCmd()), n, c.iparam("collect_ms"));
    size_t ok = 0;
    for (const auto& a : acks) ok += a.code == Ack::RESET_OK;
    c.expect(acks.size() == n && ok == n, "%zu back-to-back control channel resets: %zu acknowledgments, %zu of them 0x03",
             n, acks.size(), ok);
    auto r = c.readRaw(Reg::STANDARD, 4);
    c.expect(readsStandard(r), "read of Standard after the resets: %s", ackStr(r).c_str());
}
CXP_CHECK("UVM-test_ctrl_reset_storm", ctrlResetStorm);

}  // namespace

}  // namespace cxp::validation::checks::uvm
