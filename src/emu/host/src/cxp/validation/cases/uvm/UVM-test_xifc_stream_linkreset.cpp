// UVM-test_xifc_stream_linkreset.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void xifcStreamLinkreset(Context& c) {
    preserveLink(c);
    const int pat = selectBars(c);
    c.prepareStreaming();
    c.startRecording();
    std::vector<Captured> cap;
    double t_reset = 0, t_restart = 0;
    try {
        size_t h = c.headersSeen();
        const size_t before_n = size_t(c.iparam("images_before")), after_n = size_t(c.iparam("images_after"));
        c.acqStart();
        c.waitHeaders(h + before_n, 10000);
        t_reset = c.nowMs();
        connectionReset(c);
        c.wr32(Reg::STREAM_PACKET_SIZE_MAX, c.opt().host_spsm);
        t_restart = c.nowMs();
        h = c.headersSeen();
        c.acqStart();
        if (c.waitHeaders(h + after_n, 10000)) c.waitTail(h + after_n);
        c.acqStop();
        c.waitQuiet();
        cap = c.stopRecording();
    } catch (...) {
        c.stopRecording();
        throw;
    }
    std::vector<StreamPkt> before, after;
    size_t between = 0;
    for (auto& p : streamPackets(cap)) {
        between += p.t_ms > t_reset + c.wait(200) && p.t_ms < t_restart;
        (p.t_ms < t_restart ? before : after).push_back(std::move(p));
    }
    c.info("%zu stream packets before the ConnectionReset, %zu after the restart", before.size(), after.size());
    if (between) c.note("%zu stream packets later than 200 ms after the reset, before the restart", between);
    streamScoreboard(c, before, -1, 0, {}, "before: ");
    const auto sb = streamScoreboard(c, after, pat, 0, {}, "after: ");
    c.expect(!after.empty() && after.front().tag == 0, "first packet tag after the reset: %s (0)",
             after.empty() ? "none" : std::to_string(after.front().tag).c_str());
    c.expect(sb.complete >= 1, "after: %zu complete images", sb.complete);
}
CXP_CHECK("UVM-test_xifc_stream_linkreset", xifcStreamLinkreset);

}  // namespace

}  // namespace cxp::validation::checks::uvm
