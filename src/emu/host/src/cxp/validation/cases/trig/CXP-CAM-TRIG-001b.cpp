// CXP-CAM-TRIG-001b.  See cases/_common.h.
//
// The extension-connection step of CXP-CAM-TRIG-001: §8.3 defines the I/O
// channel (triggers, I/O acknowledgments) for the Master connection only, so
// a Table 15 trigger arriving on an extension connection (bench EXT_LINK
// strap) has no effect.

#include "cxp/validation/cases/trig/_host_trig.h"

namespace cxp::validation::checks::trig {

namespace {

using namespace htrig;

void trig001b(Context& c) {
    c.needBench(bench::CAP_CHARS | bench::CAP_TRIG_OUT | bench::CAP_EXT_LINK,
                "send Table 15 triggers on an extension connection and watch the recreated trigger");
    uvm::uvmIdleConfig(c);
    settleLow(c);
    c.benchPin(bench::EXT_LINK, 1);
    c.benchSync();
    const int n = c.iparam("triggers");
    size_t acks = 0, rises = 0;
    for (int i = 0; i < n; ++i) {
        const Shot s = shoot(c, lsTrigger(i % 2 == 0, uint8_t(i * 37 % 240)), 0, false, 5.0);
        acks += s.ioacks.size();
        rises += s.rises();
    }
    c.expect(acks == 0, "extension connection: %d triggers, %zu I/O acknowledgments (none)", n, acks);
    c.expect(rises == 0, "extension connection: %zu recreated rising edges (none)", rises);
    c.benchSend({bench::PIN, bench::EXT_LINK, 0});
    c.benchSync();
    c.expect(responsive(c), "master connection again: a read answers");
    const Shot s = shoot(c, lsTrigger(true, 0), 1, false);
    c.expect(s.ioacks.size() == 1 && s.rises() == 1,
             "master connection again: a rising trigger acknowledged (%zu) and recreated (%zu rising edges)",
             s.ioacks.size(), s.rises());
    settleLow(c);
}
CXP_CHECK("CXP-CAM-TRIG-001b", trig001b);

}  // namespace

}  // namespace cxp::validation::checks::trig
