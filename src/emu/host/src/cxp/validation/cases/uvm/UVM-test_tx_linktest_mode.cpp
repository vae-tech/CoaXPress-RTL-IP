// UVM-test_tx_linktest_mode.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void txLinktestMode(Context& c) {
    uvmIdleConfig(c);
    stopAcqIfPossible(c);
    c.onExit([&c] { c.writeRaw(Reg::TEST_MODE, {0}); });
    write64(c, Reg::TEST_PACKET_COUNT_TX, 0);
    c.startRecording();
    c.wr32(Reg::TEST_MODE, 1);
    c.sleepMs(c.iparam("testmode_ms"));
    c.wr32(Reg::TEST_MODE, 0);
    const double t_off = c.nowMs();
    c.sleepMs(c.iparam("after_ms"));
    const int grace = c.iparam("grace_ms");
    const auto tp = framesOfType(c.stopRecording(), 0x04);
    const uint64_t count = c.rd64(Reg::TEST_PACKET_COUNT_TX);
    size_t bad = 0, late = 0;
    for (const auto& f : tp) {
        if (linkTestErrors(f.frame) != 0 && ++bad <= size_t(c.opt().max_reported)) {
            c.expect(false, "test packet of %zu words, %d words off the Table 23 sequence", f.frame.size(),
                     linkTestErrors(f.frame));
        }
        late += f.t_ms > t_off + grace;
    }
    c.expect(!tp.empty(), "%zu test packets in %d ms of TestMode = 1", tp.size(), c.wait(c.iparam("testmode_ms")));
    c.expect(bad == 0, "%zu of %zu test packets match Table 23", tp.size() - bad, tp.size());
    c.expect(!tp.empty() && std::abs(int64_t(count) - int64_t(tp.size())) <= 1,
             "TestPacketCountTx = %llu vs %zu received (+-1)", (unsigned long long)count, tp.size());
    c.expect(late == 0, "%zu test packets later than %d ms after TestMode = 0", late, grace);
}
CXP_CHECK("UVM-test_tx_linktest_mode", txLinktestMode);

}  // namespace

}  // namespace cxp::validation::checks::uvm
