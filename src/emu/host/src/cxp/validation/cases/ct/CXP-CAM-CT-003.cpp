// CXP-CAM-CT-003.  See cases/_common.h.

#include "cxp/validation/cases/ct/_helpers.h"

namespace cxp::validation::checks::ct {

namespace {

void ct003(Context& c) {
    stopAcqIfPossible(c);
    c.onExit([&c] { c.writeRaw(Reg::TEST_MODE, {0}); });
    c.wr32(Reg::TEST_MODE, 1);
    std::vector<double> lat;
    std::minstd_rand rng(c.seed(3));
    const int reads = c.iparam("reads");
    const auto gap = c.ilist("read_gap_range"), on = c.ilist("on_range");
    for (int i = 0; i < reads; ++i) {
        c.sleepRawMs(std::uniform_int_distribution<int>(int(gap[0]), int(gap[1]))(rng));
        auto a = c.readRaw(Reg::STANDARD, 4);
        if (is(a, Ack::READ_OK)) lat.push_back(a->latency_ms);
    }
    c.wr32(Reg::TEST_MODE, 0);
    c.expect(lat.size() == size_t(reads), "%zu of %d reads answered in Test Mode", lat.size(), reads);
    if (!lat.empty()) {
        c.expect(*std::max_element(lat.begin(), lat.end()) <= c.opt().ack_latency_ms,
                 "ack latency in Test Mode max %.2f ms, p50 %.2f ms (<= %d ms)", *std::max_element(lat.begin(), lat.end()),
                 percentile(lat, 50), c.opt().ack_latency_ms);
    }
    size_t truncated = 0, continued = 0, seen = 0;
    const int trials = c.iparam("trials"), grace = c.iparam("grace_ms");
    for (int t = 0; t < trials; ++t) {
        c.startRecording();
        c.wr32(Reg::TEST_MODE, 1);
        c.sleepRawMs(std::uniform_int_distribution<int>(int(on[0]), int(on[1]))(rng));
        c.wr32(Reg::TEST_MODE, 0);
        const double t_off = c.nowMs();
        c.sleepMs(c.iparam("record_ms"));
        for (const auto& f : framesOfType(c.stopRecording(), 0x04)) {
            ++seen;
            truncated += linkTestErrors(f.frame) != 0;
            continued += f.t_ms > t_off + grace;
        }
    }
    c.expect(seen > 0, "%zu test packets recorded over %d Test Mode periods", seen, trials);
    c.expect(truncated == 0, "%zu truncated or corrupt test packets around TestMode 1 -> 0", truncated);
    c.expect(continued == 0, "%zu test packets later than %d ms after TestMode = 0", continued, grace);
}
CXP_CHECK("CXP-CAM-CT-003", ct003);

}  // namespace

}  // namespace cxp::validation::checks::ct
