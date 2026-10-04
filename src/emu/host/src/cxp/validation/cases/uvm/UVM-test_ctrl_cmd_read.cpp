// UVM-test_ctrl_cmd_read.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void ctrlCmdRead(Context& c) {
    uvmIdleConfig(c);
    preserveLink(c);
    connectionReset(c);
    std::mt19937 rng(c.seed(1));
    size_t good = 0;
    const int n = c.iparam("commands");
    for (int i = 0; i < n; ++i) {
        const uint32_t addr = kUvmRw[rng() % 4];
        auto a = c.readRaw(addr, 4);
        bool ok = is(a, Ack::READ_OK) && a->data.size() == 1;
        const uint32_t v = ok ? a->values()[0] : 0;
        const bool value_ok = addr == Reg::CONNECTION_CONFIG ? isDiscoveryConfig(v) : v == 0;
        good += ok && value_ok;
        c.expect(ok && value_ok, "read %s after reset: %s, %s (%s)", regName(addr), ackStr(a).c_str(), hex(v).c_str(),
                 addr == Reg::CONNECTION_CONFIG ? "a discovery configuration" : "0");
    }
    c.expect(good == size_t(n), "%zu of %d reads return the §10.3.28 reset value", good, n);
}
CXP_CHECK("UVM-test_ctrl_cmd_read", ctrlCmdRead);

}  // namespace

}  // namespace cxp::validation::checks::uvm
