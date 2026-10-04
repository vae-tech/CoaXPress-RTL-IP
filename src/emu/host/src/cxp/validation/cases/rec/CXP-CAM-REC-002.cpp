// CXP-CAM-REC-002.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::rec {

namespace {

void rec002(Context& c) {
    preserveLink(c);
    c.prepareStreaming();
    const int trials = c.iparam("trials");
    size_t ok = 0;
    for (int t = 0; t < trials; ++t) {
        const size_t h0 = c.headersSeen();
        c.acqStart();
        c.waitHeaders(h0 + 1, c.opt().first_image_timeout_ms);
        c.info("trial %d: host restart while the device streams", t + 1);
        c.cam().reconnect();
        connectionReset(c);
        const uint32_t cc = c.rd32(Reg::CONNECTION_CONFIG);
        c.expect(isDiscoveryConfig(cc), "after rediscovery ConnectionConfig = %s (discovery)", hex(cc).c_str());
        if (c.rd32(Reg::STREAM_PACKET_SIZE_MAX) == 0) c.wr32(Reg::STREAM_PACKET_SIZE_MAX, c.opt().host_spsm);
        auto ims = completeImages(imagesOf(c.acquire(c.iparam("images"))));
        ok += !ims.empty();
        c.expect(!ims.empty(), "acquisition resumes after the restart (%zu complete images)", ims.size());
    }
    c.info("%zu of %d restarts recovered", ok, trials);
}
CXP_CHECK("CXP-CAM-REC-002", rec002);

}  // namespace

}  // namespace cxp::validation::checks::rec
