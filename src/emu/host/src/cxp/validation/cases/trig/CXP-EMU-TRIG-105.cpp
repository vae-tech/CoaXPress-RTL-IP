// CXP-EMU-TRIG-105.  See cases/_common.h.
//
// The trigger input's sense (bench TRIG_POLARITY): a device trigger packet
// carries the edge of the trigger *signal* (§8.3.2.2 Table 16: 4 x K28.4
// rising, 4 x K28.2 falling), so with an active-low input the pin's falling
// edge is the trigger's rising edge.

#include "cxp/validation/cases/trig/_device_trig.h"

namespace cxp::validation::checks::trig {

namespace {

void trig105(Context& c) {
    c.needBench(bench::CAP_TRIG_IN | bench::CAP_TRIG_POLARITY | bench::CAP_CHARS,
                "drive the trigger input and its polarity strap");
    const int cycles = c.iparam("cycles");
    for (uint32_t pol : {0u, 1u}) {
        const std::string what = pol ? "active-low input" : "active-high input";
        const double since = devtrig::cleanStart(c, pol);
        std::string want;
        uint32_t level = devtrig::idleLevel(pol);
        for (int i = 0; i < 2 * cycles; ++i) {
            level ^= 1u;
            const bool asserted = level != devtrig::idleLevel(pol);
            want += asserted ? 'R' : 'F';
            c.benchSend({bench::PIN, bench::TRIG_IN, level});
            const bool got = devtrig::waitTrigs(c, size_t(i) + 1, since, 2000);
            c.sendChars(ioAck());  // §8.3.3: the host acknowledges each trigger
            if (!got) break;
            c.sleepRawMs(c.iparam("gap_ms"));
        }
        c.sleepMs(100);
        const auto t = devtrig::trigs(c, since);
        size_t bad = 0;
        for (const auto& s : t) bad += !(s.pkt.clean && s.pkt.value >= 0 && s.pkt.value <= 3);
        c.expect(devtrig::kinds(t) == want,
                 "%s: pin edges %s (R = asserted), trigger packets %s (R = 4 x K28.4, F = 4 x K28.2)", what.c_str(),
                 want.c_str(), devtrig::kinds(t).c_str());
        c.expect(!t.empty() && bad == 0, "%s: %zu of %zu packets clean Table 16 with Delay 0..3", what.c_str(),
                 t.size() - bad, t.size());
    }
}
CXP_CHECK("CXP-EMU-TRIG-105", trig105);

}  // namespace

}  // namespace cxp::validation::checks::trig
