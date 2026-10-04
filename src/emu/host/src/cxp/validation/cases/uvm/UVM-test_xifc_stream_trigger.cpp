// UVM-test_xifc_stream_trigger.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void xifcStreamTrigger(Context& c) {
    c.needBench(bench::CAP_TRIG_IN | bench::CAP_CHARS, "toggle the device's trigger input while it streams");
    const int pat = selectBars(c);
    std::vector<TimedShort> trig;
    const size_t edges = size_t(c.iparam("edges"));
    auto cap = streamAround(c, size_t(c.iparam("images")), [&] { trig = deviceTriggers(c, edges, c.iparam("gap_ms")); });
    const auto sb = streamScoreboard(c, streamPackets(cap), pat);
    c.expect(sb.complete >= size_t(c.iparam("min_complete")), "%zu complete images (>= %d)", sb.complete,
             c.iparam("min_complete"));
    judgeDeviceTriggers(c, trig, edges);
}
CXP_CHECK("UVM-test_xifc_stream_trigger", xifcStreamTrigger);

}  // namespace

}  // namespace cxp::validation::checks::uvm
