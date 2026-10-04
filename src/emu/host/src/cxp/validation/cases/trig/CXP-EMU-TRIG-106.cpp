// CXP-EMU-TRIG-106.  See cases/_common.h.
//
// Triggers back to back.  On the low-speed link a Table 15 packet lasts 60
// bits while its Delay spans under 10, so a second trigger can never arrive
// while the first is still waiting out its Delay; the tightest real case is
// packets with no IDLE between them.  Rising - falling - rising with Delay
// pairs at the extremes: every packet acknowledged, both rising triggers
// recreated, and (bench times) their spacing is the leaders' spacing plus
// the Delay difference, as Figure 20 recreates each event independently.

#include "cxp/validation/cases/trig/_host_trig.h"

namespace cxp::validation::checks::trig {

namespace {

using namespace htrig;

void trig106(Context& c) {
    c.needBench(bench::CAP_CHARS | bench::CAP_TRIG_OUT, "send Table 15 triggers and watch the recreated trigger");
    uvm::uvmIdleConfig(c);
    const bool timed = (c.benchCaps() & bench::CAP_TIMES) != 0;
    const double limit = c.iparam("rx_clk_ns") + c.iparam("grain_ns");
    for (const auto& pr : c.rows("delay_pairs")) {
        const uint8_t d1 = uint8_t(pr[0]), d3 = uint8_t(pr[1]);
        settleLow(c);
        Chars ch = lsTrigger(true, d1);
        for (const Chars& x : {lsTrigger(false, 120), lsTrigger(true, d3)}) ch.insert(ch.end(), x.begin(), x.end());
        const Shot s = shoot(c, ch, 3, false);
        std::vector<PinEdge> up;
        for (const auto& e : s.out) {
            if (e.value == 1) up.push_back(e);
        }
        c.expect(s.ioacks.size() == 3 && up.size() == 2,
                 "rising %u, falling, rising %u back to back: %zu acknowledgments (3), %zu recreated rising edges (2)", d1,
                 d3, s.ioacks.size(), up.size());
        if (!timed) continue;
        if (s.leaders.size() != 3 || up.size() != 2) {
            c.expect(false, "  ... %zu leaders on the uplink: spacing not judged", s.leaders.size());
            continue;
        }
        const double u_ns = s.leaders[0].bit_ps / 1000.0 / 24.0;
        const double lead_ns = (double(s.leaders[2].time_ps) - double(s.leaders[0].time_ps)) / 1000.0;
        const double want = lead_ns + (int(d3) - int(d1)) * u_ns;
        const double got = double(up[1].device_ns) - double(up[0].device_ns);
        c.expect(std::fabs(got - want) <= limit, "  ... recreated edges %.1f ns apart, leaders %.1f ns + Delay "
                 "difference %d x %.2f ns = %.1f ns (+- %.0f)", got, lead_ns, int(d3) - int(d1), u_ns, want, limit);
    }
    if (!timed) c.note("no bench times: the spacing of the recreated edges is not judged");
    settleLow(c);
    c.expect(responsive(c), "a read answers after the triggers");
}
CXP_CHECK("CXP-EMU-TRIG-106", trig106);

}  // namespace

}  // namespace cxp::validation::checks::trig
