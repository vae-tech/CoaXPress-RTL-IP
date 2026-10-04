// CXP-EMU-TRIG-107.  See cases/_common.h.
//
// A resent trigger packet.  §8.3.3 lets the Host resend the last trigger
// packet when its acknowledgment does not come within the timeout (one
// low-speed character).  The resent packet carries the same edge of the
// same trigger signal: the Device acknowledges it again, but the trigger it
// recreates must not see a second event (the signal did not change).

#include "cxp/validation/cases/trig/_host_trig.h"

namespace cxp::validation::checks::trig {

namespace {

using namespace htrig;

void trig107(Context& c) {
    c.needBench(bench::CAP_CHARS | bench::CAP_TRIG_OUT, "send Table 15 triggers and watch the recreated trigger");
    uvm::uvmIdleConfig(c);
    const int n = c.iparam("resends");
    for (bool back_to_back : {true, false}) {
        const char* how = back_to_back ? "back to back" : "a while later";
        settleLow(c);
        size_t acks = 0, ups = 0;
        if (back_to_back) {
            Chars ch;
            for (int i = 0; i <= n; ++i) {
                const Chars t = lsTrigger(true, 60);
                ch.insert(ch.end(), t.begin(), t.end());
            }
            const Shot s = shoot(c, ch, size_t(n) + 1, false);
            acks = s.ioacks.size();
            ups = s.rises();
        } else {
            for (int i = 0; i <= n; ++i) {
                const Shot s = shoot(c, lsTrigger(true, 60), 1, false);
                acks += s.ioacks.size();
                ups += s.rises();
            }
        }
        c.expect(acks == size_t(n) + 1, "rising trigger resent %d times %s: %zu acknowledgments (%d)", n, how, acks, n + 1);
        c.expect(ups == 1, "rising trigger resent %d times %s: %zu recreated rising edges (1: one event)", n, how, ups);
    }
    settleLow(c);
}
CXP_CHECK("CXP-EMU-TRIG-107", trig107);

}  // namespace

}  // namespace cxp::validation::checks::trig
