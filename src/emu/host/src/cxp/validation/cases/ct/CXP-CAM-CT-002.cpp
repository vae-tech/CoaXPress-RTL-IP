// CXP-CAM-CT-002.  See cases/_common.h.

#include "cxp/validation/cases/ct/_helpers.h"

namespace cxp::validation::checks::ct {

namespace {

void ct002(Context& c) {
    c.prepareStreaming();
    c.onExit([&c] { c.writeRaw(Reg::TEST_MODE, {0}); });
    c.startRecording();
    const size_t h0 = c.headersSeen();
    c.acqStart();
    c.waitHeaders(h0 + 1, c.opt().first_image_timeout_ms);
    auto a = c.writeRaw(Reg::TEST_MODE, {1});
    const double t_on = c.nowMs();
    c.expect(is(a, Ack::WRITE_OK), "TestMode = 1 while streaming: %s", ackStr(a).c_str());
    size_t answered = 0;
    const int reads = c.iparam("reads");
    for (int i = 0; i < reads; ++i) {
        c.sleepMs(c.iparam("read_gap_ms"));
        answered += is(c.readRaw(Reg::STANDARD, 4), Ack::READ_OK);
    }
    c.wr32(Reg::TEST_MODE, 0);
    const double t_off = c.nowMs();
    c.acqStop();
    c.waitQuiet();
    auto cap = c.stopRecording();
    size_t stream_in_tm = 0;
    const int grace = c.iparam("grace_ms");
    for (const auto& f : framesOfType(cap, 0x01)) stream_in_tm += f.t_ms > t_on + grace && f.t_ms < t_off;
    const size_t tests = framesOfType(cap, 0x04).size();
    c.expect(stream_in_tm == 0, "%zu stream packets during Test Mode (%d ms grace after the TestMode write)",
             stream_in_tm, grace);
    c.expect(tests > 0, "%zu test packets during Test Mode", tests);
    c.expect(answered == size_t(reads), "%zu of %d register reads answered during Test Mode", answered, reads);
    c.note("the >= 16-word gap and the IDLE rule are not observable on the FIFO link (no IDLE words)");
}
CXP_CHECK("CXP-CAM-CT-002", ct002);

}  // namespace

}  // namespace cxp::validation::checks::ct
