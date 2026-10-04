// CXP-CAM-INIT-009.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::init {

namespace {

void init009(Context& c) {
    c.preserve(Reg::CONNECTION_CONFIG);
    const uint32_t ccd = c.rd32(Reg::CONNECTION_CONFIG_DEFAULT);
    c.expect((ccd >> 16) >= 1, "ConnectionConfigDefault connection count = %u (>= 1)", ccd >> 16);
    c.expect(isValidSpeedCode(ccd & 0xFFFF), "ConnectionConfigDefault speed code 0x%02X is in Table 46",
             ccd & 0xFFFF);
    auto a = c.writeRaw(Reg::CONNECTION_CONFIG, {ccd});
    c.expect(is(a, Ack::WRITE_OK), "writing ConnectionConfig = default: %s", ackStr(a).c_str());
    c.expect(c.rd32(Reg::CONNECTION_CONFIG) == ccd, "ConnectionConfig reads back the default mode");
    c.note("reprogramming the default (MMODE, power cycle) needs the vendor mechanism and is not run");
}
CXP_CHECK("CXP-CAM-INIT-009", init009);

}  // namespace

}  // namespace cxp::validation::checks::init
