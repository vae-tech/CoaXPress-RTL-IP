// CXP-CAM-CTRL-005.  See cases/_common.h.

#include "cxp/validation/cases/ctrl/_helpers.h"

namespace cxp::validation::checks::ctrl {

namespace {

void ctrl005(Context& c) {
    c.prepareStreaming();
    const size_t h0 = c.headersSeen();
    c.acqStart();
    c.waitHeaders(h0 + 1, c.opt().first_image_timeout_ms);
    size_t waits = 0, n = 0;
    const int passes = c.iparam("passes");
    for (int rep = 0; rep < passes; ++rep) {
        for (const BootReg& r : bootstrapTable()) {
            auto a = c.readRaw(r.addr, r.bytes);
            ++n;
            if (is(a, Ack::WAIT)) ++waits;
        }
    }
    const uint32_t cc = c.rd32(Reg::CONNECTION_CONFIG);
    waits += is(c.writeRaw(Reg::CONNECTION_CONFIG, {cc}), Ack::WAIT);
    const auto uid = c.readBlock(Reg::DEVICE_USER_ID, 4);
    const uint32_t w0 = uint32_t(uid[0]) << 24 | uint32_t(uid[1]) << 16 | uint32_t(uid[2]) << 8 | uid[3];
    waits += is(c.writeRaw(Reg::DEVICE_USER_ID, {w0}), Ack::WAIT);
    n += 2;
    c.acqStop();
    c.expect(waits == 0, "%zu wait acknowledgments over %zu bootstrap accesses under stream load", waits, n);
}
CXP_CHECK("CXP-CAM-CTRL-005", ctrl005);

}  // namespace

}  // namespace cxp::validation::checks::ctrl
