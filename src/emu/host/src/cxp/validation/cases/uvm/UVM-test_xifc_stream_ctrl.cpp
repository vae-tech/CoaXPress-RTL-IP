// UVM-test_xifc_stream_ctrl.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void xifcStreamCtrl(Context& c) {
    preserveLink(c);
    c.preserve(Reg::TEST_ERROR_COUNT_SELECTOR);
    const int pat = selectBars(c);
    RegModel m = readModel(c);
    std::mt19937 rng(c.seed(11));
    std::vector<double> cc_writes;
    auto cap = streamAround(c, size_t(c.iparam("images")), [&] { ctrlTraffic(c, m, c.iparam("commands"), -1, rng, &cc_writes); });
    const auto sb = streamScoreboard(c, streamPackets(cap), pat, 0, cc_writes);
    c.expect(sb.complete >= size_t(c.iparam("min_complete")), "%zu complete images (>= %d)", sb.complete,
             c.iparam("min_complete"));
}
CXP_CHECK("UVM-test_xifc_stream_ctrl", xifcStreamCtrl);

}  // namespace

}  // namespace cxp::validation::checks::uvm
