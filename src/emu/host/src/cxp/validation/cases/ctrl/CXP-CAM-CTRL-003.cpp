// CXP-CAM-CTRL-003.  See cases/_common.h.

#include "cxp/validation/cases/ctrl/_helpers.h"

namespace cxp::validation::checks::ctrl {

namespace {

void ctrl003(Context& c) {
    const auto regs = registerList(c);
    std::vector<double> idle, load;
    size_t lost = 0;
    latencySweep(c, regs, c.iparam("reads_idle"), c.iparam("write_backs"), "idle", idle, lost);
    c.prepareStreaming();
    const size_t h0 = c.headersSeen();
    c.acqStart();
    c.waitHeaders(h0 + 1, c.opt().first_image_timeout_ms);
    latencySweep(c, regs, c.iparam("reads_streaming"), 0, "streaming", load, lost);
    c.acqStop();
    for (auto* v : {&idle, &load}) {
        if (v->empty()) continue;
        const double mx = *std::max_element(v->begin(), v->end());
        c.expect(mx <= c.opt().ack_latency_ms, "%s: %zu transactions, max %.2f ms, p99.9 %.2f ms (<= %d ms)",
                 v == &idle ? "idle" : "streaming", v->size(), mx, percentile(*v, 99.9), c.opt().ack_latency_ms);
    }
    c.expect(lost == 0, "%zu commands without a final acknowledgment over %zu registers", lost, regs.size());
}
CXP_CHECK("CXP-CAM-CTRL-003", ctrl003);

}  // namespace

}  // namespace cxp::validation::checks::ctrl
