// UVM-test_arbiter_underflow_ctrl.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void arbiterUnderflowCtrl(Context& c) {
    const uint32_t id = c.rd32(Reg::MASTER_HOST_CONNECTION_ID);
    shortTailImage(c);
    size_t good = 0;
    const int n = c.iparam("reads");
    for (int i = 0; i < n; ++i) {
        c.sleepRawMs(c.iparam("read_gap_ms"));
        auto a = c.readRaw(Reg::MASTER_HOST_CONNECTION_ID, 4);
        const bool ok = is(a, Ack::READ_OK) && a->data.size() == 1 && a->values()[0] == id;
        good += ok;
        if (!ok) c.expect(false, "read %d of MasterHostConnectionID: %s", i + 1, ackStr(a).c_str());
    }
    c.expect(good == size_t(n), "%zu of %d spaced reads after the image answered 0x00 with %s", good, n, hex(id).c_str());
}
CXP_CHECK("UVM-test_arbiter_underflow_ctrl", arbiterUnderflowCtrl);

}  // namespace

}  // namespace cxp::validation::checks::uvm
