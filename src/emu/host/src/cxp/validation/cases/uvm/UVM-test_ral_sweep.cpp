// UVM-test_ral_sweep.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void ralSweep(Context& c) {
    uvmIdleConfig(c);
    preserveLink(c);
    c.preserve(Reg::TEST_ERROR_COUNT_SELECTOR);
    for (uint32_t addr : {Reg::MASTER_HOST_CONNECTION_ID, Reg::STREAM_PACKET_SIZE_MAX, Reg::TEST_ERROR_COUNT_SELECTOR}) {
        for (int64_t pat64 : c.ilist("values")) {
            const uint32_t pat = uint32_t(pat64);
            const uint32_t old = c.rd32(addr);
            auto w = c.writeRaw(addr, {pat});
            const uint32_t r = c.rd32(addr);
            const bool accepted = is(w, Ack::WRITE_OK);
            const bool refused = w && w->code >= Ack::BAD_ADDRESS;
            const bool model = accepted ? r == pat : refused && r == old;
            c.expect(model, "%s = %s: %s, reads %s (%s)", regName(addr), hex(pat).c_str(), ackStr(w).c_str(),
                     hex(r).c_str(), accepted ? "the value written" : strprintf("unchanged %s", hex(old).c_str()).c_str());
            if (addr == Reg::MASTER_HOST_CONNECTION_ID) {
                c.expect(accepted, "MasterHostConnectionID takes %s", hex(pat).c_str());
            } else if (addr == Reg::TEST_ERROR_COUNT_SELECTOR) {
                c.expect(accepted == (pat == 0), "TestErrorCountSelector %s %s", pat == 0 ? "takes" : "refuses",
                         hex(pat).c_str());
            }
        }
    }
}
CXP_CHECK("UVM-test_ral_sweep", ralSweep);

}  // namespace

}  // namespace cxp::validation::checks::uvm
