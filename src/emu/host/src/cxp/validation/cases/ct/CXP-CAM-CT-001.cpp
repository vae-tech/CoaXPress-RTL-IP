// CXP-CAM-CT-001.  See cases/_common.h.

#include "cxp/validation/cases/ct/_helpers.h"

namespace cxp::validation::checks::ct {

namespace {

void ct001(Context& c) {
    stopAcqIfPossible(c);
    c.onExit([&c] { c.writeRaw(Reg::TEST_MODE, {0}); });
    write64(c, Reg::TEST_PACKET_COUNT_TX, 0);
    c.startRecording();
    c.wr32(Reg::TEST_MODE, 1);
    c.sleepMs(c.iparam("testmode_ms"));
    c.wr32(Reg::TEST_MODE, 0);
    c.sleepMs(c.iparam("after_ms"));
    auto tp = framesOfType(c.stopRecording(), 0x04);
    const uint64_t count = c.rd64(Reg::TEST_PACKET_COUNT_TX);
    size_t bad = 0;
    for (const auto& f : tp) {
        const int e = linkTestErrors(f.frame);
        if (e != 0) {
            if (++bad <= size_t(c.opt().max_reported)) {
                c.expect(false, "test packet of %zu words, %d words off the Table 23 sequence", f.frame.size(), e);
            }
        }
    }
    c.expect(!tp.empty(), "%zu test packets received in %d ms of TestMode = 1", tp.size(), c.wait(c.iparam("testmode_ms")));
    c.expect(bad == 0, "%zu of %zu test packets match Table 23 (1027 words, counting 0x00..0xFF)", tp.size() - bad,
             tp.size());
    const int64_t diff = int64_t(count) - int64_t(tp.size());
    c.expect(!tp.empty() && std::abs(diff) <= 1, "TestPacketCountTx = %llu vs %zu counted (+-1)",
             (unsigned long long)count, tp.size());
}
CXP_CHECK("CXP-CAM-CT-001", ct001);

}  // namespace

}  // namespace cxp::validation::checks::ct
