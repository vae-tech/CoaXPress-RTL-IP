// CXP-CAM-GEN-005.  See cases/_common.h.

#include "cxp/validation/cases/gen/_helpers.h"

namespace cxp::validation::checks::gen {

namespace {

void gen005(Context& c) {
    Feature& start = c.need("AcquisitionStart");
    Feature& stop = c.need("AcquisitionStop");
    c.expect(start.command_value == 1 && stop.command_value == 1, "AcquisitionStart/Stop CommandValue = %lld / %lld (1)",
             (long long)start.command_value, (long long)stop.command_value);
    c.prepareStreaming();
    size_t ok = 0;
    const int cycles = c.iparam("cycles");
    const int start_ms = c.wait(c.iparam("start_timeout_ms"));
    for (int i = 0; i < cycles; ++i) {
        const size_t s0 = c.streamPacketsSeen();
        c.acqStart();
        const double t0 = c.nowMs();
        while (c.streamPacketsSeen() == s0 && c.nowMs() - t0 < start_ms) c.sleepRawMs(5);
        const bool started = c.streamPacketsSeen() > s0;
        c.acqStop();
        const bool quiet = c.waitQuiet();
        ok += started && quiet;
        if (!(started && quiet) && ok + 2 >= size_t(i)) {
            c.expect(false, "cycle %d: %s", i + 1,
                     !started ? strprintf("no stream within %d ms of Start", start_ms).c_str() : "stream did not stop");
        }
    }
    c.expect(ok == size_t(cycles), "%zu of %d Start/Stop cycles start and stop the stream", ok, cycles);
    if (start.reg) {
        auto a = c.readRaw(uint32_t(start.reg->address), 4);
        c.note("AcquisitionStart register read after use: %s%s", ackStr(a).c_str(),
               is(a, Ack::READ_OK) ? strprintf(" = %s", hex(a->values()[0]).c_str()).c_str() : "");
    }
}
CXP_CHECK("CXP-CAM-GEN-005", gen005);

}  // namespace

}  // namespace cxp::validation::checks::gen
