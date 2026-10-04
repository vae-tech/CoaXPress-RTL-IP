// CXP-CAM-TRIG-002.  See cases/_common.h.
//
// Trigger delay compensation (§8.3.2.1, Figure 20) measured with the
// bench's clock: the host's trigger event is the first bit of the Table 15
// leader minus (239 - Delay) units of 1/24 bit; the device's recreated
// trigger (TRIG_OUT) must follow the event after a fixed latency, whatever
// the Delay and the character phase.

#include "cxp/validation/cases/trig/_host_trig.h"

namespace cxp::validation::checks::trig {

namespace {

using namespace htrig;

void trig002(Context& c) {
    c.needBench(bench::CAP_CHARS | bench::CAP_TRIG_OUT | bench::CAP_TIMES,
                "time Table 15 triggers and the recreated trigger with the bench's clock");
    uvm::uvmIdleConfig(c);
    settleLow(c);
    std::mt19937 rng(c.seed(2));
    const int n = c.iparam("triggers");
    std::vector<double> lat, raw;
    size_t unmatched = 0;
    for (int i = 0; i < n; ++i) {
        const uint8_t d = uint8_t(rng() % 240);
        const size_t k = rng() % 4;
        const Shot s = shoot(c, inIdle(lsTrigger(true, d), k), 1, false);
        std::vector<PinEdge> up;
        for (const auto& e : s.out) {
            if (e.value == 1) up.push_back(e);
        }
        if (s.leaders.size() != 1 || up.size() != 1) {
            if (++unmatched <= size_t(c.opt().max_reported)) {
                c.expect(false, "trigger %d (Delay %u): %zu leaders on the uplink, %zu recreated rising edges", i, d,
                         s.leaders.size(), up.size());
            }
        } else {
            const double bit_ns = s.leaders[0].bit_ps / 1000.0, mark_ns = s.leaders[0].time_ps / 1000.0;
            const double event_ns = mark_ns - (239 - d) * bit_ns / 24.0;
            lat.push_back(double(up[0].device_ns) - event_ns);
            raw.push_back(double(up[0].device_ns) - mark_ns);
        }
        shoot(c, inIdle(lsTrigger(false, uint8_t(rng() % 240)), rng() % 4), 1, false);
    }
    c.expect(unmatched == 0 && lat.size() == size_t(n),
             "%zu of %d rising triggers timed: one leader on the uplink, one recreated rising edge", lat.size(), n);
    if (lat.empty()) return;
    const auto [lo, hi] = std::minmax_element(lat.begin(), lat.end());
    const auto [rlo, rhi] = std::minmax_element(raw.begin(), raw.end());
    const double mean = std::accumulate(lat.begin(), lat.end(), 0.0) / double(lat.size());
    const double limit = c.iparam("rx_clk_ns") + c.iparam("grain_ns");
    c.info("latency event -> recreated trigger: mean %.1f ns, min %.1f, max %.1f (%zu triggers)", mean, *lo, *hi,
           lat.size());
    c.info("without the Delay (leader -> recreated trigger): spread %.1f ns", *rhi - *rlo);
    c.expect(*hi - *lo <= limit, "jitter of the compensated latency %.1f ns p-p (<= %.0f ns: one rx_clk + the "
             "bench's time grain)", *hi - *lo, limit);
    c.expect(*rhi - *rlo > 4 * limit, "the Delay spread the uncompensated times over %.1f ns (the Delay was used)",
             *rhi - *rlo);
    settleLow(c);
}
CXP_CHECK("CXP-CAM-TRIG-002", trig002);

}  // namespace

}  // namespace cxp::validation::checks::trig
