// CXP-CAM-INIT-002.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::init {

namespace {

void init002(Context& c) {
    preserveLink(c);
    const int trials = c.iparam("trials");
    const auto delay = c.ilist("reset_delay_range");
    std::minstd_rand rng(c.seed(2));
    for (int t = 1; t <= trials; ++t) {
        c.info("trial %d/%d", t, trials);
        c.prepareStreaming();
        if (c.tryRd32(Reg::STREAM_PACKET_SIZE_MAX).value_or(0) == 0) {
            c.wr32(Reg::STREAM_PACKET_SIZE_MAX, c.opt().host_spsm);
        }
        c.startRecording();
        const size_t h0 = c.headersSeen();
        c.acqStart();
        if (!c.waitHeaders(h0 + 1, c.opt().first_image_timeout_ms)) {
            c.stopRecording();
            c.abort("no stream before the reset; cannot run the trial");
        }
        c.sleepRawMs(std::uniform_int_distribution<int>(int(delay[0]), int(delay[1]))(rng));
        const double t0 = c.nowMs();
        connectionReset(c);
        const uint32_t cr = c.rd32(Reg::CONNECTION_RESET);
        const uint32_t cc = c.rd32(Reg::CONNECTION_CONFIG);
        const uint32_t spsm = c.rd32(Reg::STREAM_PACKET_SIZE_MAX);
        c.sleepMs(c.iparam("after_ms"));
        auto cap = c.stopRecording();
        size_t late = 0;
        for (const auto& f : framesOfType(cap, 0x01)) late += f.t_ms > t0 + 200;
        c.expect(cr == 0, "ConnectionReset reads %s 200 ms after the write (self-clear)", hex(cr).c_str());
        c.expect(isDiscoveryConfig(cc), "ConnectionConfig = %s (discovery configuration)", hex(cc).c_str());
        c.expect(spsm == 0, "StreamPacketSizeMax = %s (0 after reset)", hex(spsm).c_str());
        c.expect(late == 0, "%zu stream packets arrived more than 200 ms after the reset", late);
        c.acqStop();
        c.waitQuiet();
    }
}
CXP_CHECK("CXP-CAM-INIT-002", init002);

}  // namespace

}  // namespace cxp::validation::checks::init
