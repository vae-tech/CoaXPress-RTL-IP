// UVM-test_idle_baseline.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void idleBaseline(Context& c) {
    uvmIdleConfig(c);
    stopAcqIfPossible(c);
    std::map<int, size_t> types;
    const int record_ms = c.iparam("record_ms");
    for (const auto& f : c.record(record_ms)) types[f.frame.size() > 1 ? int(uint8_t(f.frame[1])) : -1]++;
    size_t bad = 0, ext = 0;
    for (auto [t, n] : types) {
        if (t == 0x05 || t == 0x06) ext += n;
        else bad += n;
    }
    c.expect(bad == 0, "%zu stream, acknowledgment or test packets in %d ms without commands", bad, c.wait(record_ms));
    if (ext) c.note("%zu host-stack extension frames (0x05/0x06) on the idle link", ext);
}
CXP_CHECK("UVM-test_idle_baseline", idleBaseline);

}  // namespace

}  // namespace cxp::validation::checks::uvm
