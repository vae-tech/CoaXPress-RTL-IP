// UVM-test_link_reset.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void linkReset(Context& c) {
    uvmIdleConfig(c);
    preserveLink(c);
    auto w = c.writeRaw(Reg::MASTER_HOST_CONNECTION_ID, {0xA5A5A5A5});
    c.expect(is(w, Ack::WRITE_OK), "MasterHostConnectionID = 0xA5A5A5A5: %s", ackStr(w).c_str());
    connectionReset(c);
    const uint32_t r = c.rd32(Reg::MASTER_HOST_CONNECTION_ID);
    c.expect(r == 0, "MasterHostConnectionID after ConnectionReset: %s (0)", hex(r).c_str());
}
CXP_CHECK("UVM-test_link_reset", linkReset);

}  // namespace

}  // namespace cxp::validation::checks::uvm
