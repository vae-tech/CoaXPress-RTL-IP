// CXP-CAM-CTRL-010.  See cases/_common.h.

#include "cxp/validation/cases/ctrl/_helpers.h"

namespace cxp::validation::checks::ctrl {

namespace {

void ctrl010(Context& c) {
    c.prepareStreaming();
    const size_t h0 = c.headersSeen();
    c.acqStart();
    c.waitHeaders(h0 + 1, c.opt().first_image_timeout_ms);
    std::vector<double> lat;
    size_t lost = 0;
    std::minstd_rand rng(c.seed(10));
    const int reads = c.iparam("reads");
    const auto gap = c.ilist("gap_us_range");
    for (int i = 0; i < reads; ++i) {
        std::this_thread::sleep_for(
            std::chrono::microseconds(std::uniform_int_distribution<int>(int(gap[0]), int(gap[1]))(rng)));
        auto a = c.readRaw(Reg::STANDARD, 4);
        if (a) lat.push_back(a->latency_ms);
        else ++lost;
    }
    c.acqStop();
    if (lat.empty()) c.abort("no read answered");
    const double mx = *std::max_element(lat.begin(), lat.end());
    c.info("latency under load: p50 %.2f ms, p99 %.2f ms, max %.2f ms over %zu reads", percentile(lat, 50),
           percentile(lat, 99), mx, lat.size());
    c.expect(mx <= c.opt().ack_latency_ms, "max ack latency under stream load %.2f ms (<= %d ms)", mx,
             c.opt().ack_latency_ms);
    c.expect(lost == 0, "%zu reads unanswered", lost);
}
CXP_CHECK("CXP-CAM-CTRL-010", ctrl010);

}  // namespace

}  // namespace cxp::validation::checks::ctrl
