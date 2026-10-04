// UVM-test_stream_tpg.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void streamTpg(Context& c) {
    const int pat = selectBars(c);
    OptAck rd;
    auto cap = streamAround(c, size_t(c.iparam("images")), [&] { rd = c.readRaw(Reg::STANDARD, 4); });
    c.expect(readsStandard(rd), "read of Standard while streaming: %s", ackStr(rd).c_str());
    const auto sb = streamScoreboard(c, streamPackets(cap), pat);
    c.expect(sb.complete >= size_t(c.iparam("min_complete")), "%zu complete images (>= %d)", sb.complete,
             c.iparam("min_complete"));
}
CXP_CHECK("UVM-test_stream_tpg", streamTpg);

}  // namespace

}  // namespace cxp::validation::checks::uvm
