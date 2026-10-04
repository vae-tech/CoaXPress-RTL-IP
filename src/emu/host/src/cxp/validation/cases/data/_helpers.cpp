#include "cxp/validation/cases/data/_helpers.h"

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::data {

std::vector<uint32_t> tagsOfRun(Context& c, size_t n_images) {
    auto pk = streamPackets(c.acquire(n_images));
    if (pk.empty()) c.abort("no stream packets received");
    return pktTags(pk, pk.front().stream_id);
}

void smallTpg(Context& c, uint32_t width, uint32_t height, uint32_t spsm) {
    uvm::runTestPattern(c);
    preserveLink(c);
    c.prepareStreaming();
    preserveFeature(c, "Width");
    preserveFeature(c, "Height");
    if (!trySet(c, "Width", Value::ofInt(width)) || !trySet(c, "Height", Value::ofInt(height))) {
        c.abort(strprintf("cannot set the %u x %u geometry", width, height));
    }
    c.wr32(Reg::STREAM_PACKET_SIZE_MAX, spsm);
}

double ackTime(const std::vector<Captured>& cap, double t_ms) {
    for (const auto& f : cap) {
        if (f.t_ms >= t_ms && f.frame.size() >= 3 && (f.frame[1] & 0xFF) == 0x03) return f.t_ms;
    }
    return -1;
}

}  // namespace cxp::validation::checks::data
