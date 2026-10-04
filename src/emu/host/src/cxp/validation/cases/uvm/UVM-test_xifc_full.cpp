// UVM-test_xifc_full.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void xifcFull(Context& c) {
    c.needBench(bench::CAP_TRIG_IN | bench::CAP_TRIG_OUT | bench::CAP_CHARS,
                "send host triggers and toggle the device's trigger input while it streams");
    preserveLink(c);
    c.preserve(Reg::TEST_ERROR_COUNT_SELECTOR);
    const int pat = selectBars(c);
    RegModel m = readModel(c);
    std::mt19937 rng(c.seed(14));
    std::vector<double> cc_writes;
    HostTrigRun host;
    std::vector<TimedShort> dev;
    const auto kinds = randomKinds(size_t(c.iparam("triggers")), c.seed(15));
    const size_t edges = size_t(c.iparam("edges"));
    auto cap = streamAround(c, size_t(c.iparam("images")), [&] {
        ctrlTraffic(c, m, c.iparam("commands"), -1, rng, &cc_writes);
        dev = deviceTriggers(c, edges, c.iparam("gap_ms"));
        host = hostTriggers(c, kinds, c.iparam("spacing_ms"));  // last: see preemptRound
    });
    const auto sb = streamScoreboard(c, streamPackets(cap), pat, 0, cc_writes);
    c.expect(sb.complete >= size_t(c.iparam("min_complete")), "%zu complete images (>= %d)", sb.complete,
             c.iparam("min_complete"));
    judgeAcks(c, host, "host triggers: ");
    judgeEdges(c, host, "host triggers: ");
    judgeDeviceTriggers(c, dev, edges, "device triggers: ");
    settleTrigOut(c);
}
CXP_CHECK("UVM-test_xifc_full", xifcFull);

}  // namespace

}  // namespace cxp::validation::checks::uvm
