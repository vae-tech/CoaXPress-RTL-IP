// CXP-CAM-INIT-007.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::init {

namespace {

void init007(Context& c) {
    c.prepareStreaming();
    c.preserve(Reg::STREAM_PACKET_SIZE_MAX);
    c.wr32(Reg::STREAM_PACKET_SIZE_MAX, 0);
    c.startRecording();
    c.acqStart();
    c.sleepMs(c.iparam("gate_ms"));
    c.acqStop();
    c.waitQuiet();
    const size_t gated = framesOfType(c.stopRecording(), 0x01).size();
    c.expect(gated == 0, "SPSM = 0: %zu stream packets in %d ms after AcquisitionStart", gated,
             c.wait(c.iparam("gate_ms")));
    // A fixed packet count, not whole images: at SPSM = 36 a packet carries
    // one payload word, so two images would be ~10^5 packets.
    for (uint32_t spsm : c.spsmList("spsm_list")) {
        c.wr32(Reg::STREAM_PACKET_SIZE_MAX, spsm);
        auto pk = streamPackets(c.acquirePackets(c.iparam("packets")));
        size_t over = 0, biggest = 0;
        for (const auto& p : pk) {
            biggest = std::max(biggest, p.total_words * 4);
            over += p.total_words * 4 > spsm;
        }
        c.expect(!pk.empty(), "SPSM = %u: %zu stream packets received", spsm, pk.size());
        c.expect(over == 0, "SPSM = %u: %zu packets larger than SPSM (largest %zu bytes)", spsm, over, biggest);
    }
}
CXP_CHECK("CXP-CAM-INIT-007", init007);

}  // namespace

}  // namespace cxp::validation::checks::init
