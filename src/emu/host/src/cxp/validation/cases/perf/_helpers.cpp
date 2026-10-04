#include "cxp/validation/cases/perf/_helpers.h"

namespace cxp::validation::checks::perf {

StreamStats analyse(const std::vector<Captured>& cap) {
    StreamStats s;
    auto pk = streamPackets(cap);
    s.packets = pk.size();
    for (const auto& p : pk) {
        s.crc_bad += !p.crc_ok;
        s.payload_bytes += p.payload.size() * 4;
    }
    if (!pk.empty()) s.tag_breaks = tagBreaks(pktTags(pk, pk.front().stream_id));
    auto ims = walkImages(pk);
    for (size_t i = 0; i < ims.size(); ++i) {
        ++s.images;
        if (!imageComplete(ims[i]) && i + 1 < ims.size()) ++s.incomplete;
        if (i && ims[i].source_tag != ((ims[i - 1].source_tag + 1) & 0xFFFF)) ++s.st_breaks;
    }
    return s;
}

std::vector<Captured> streamFor(Context& c, int seconds) {
    c.prepareStreaming();
    c.startRecording();
    const size_t h0 = c.headersSeen();
    c.acqStart();
    c.waitHeaders(h0 + 1, c.opt().first_image_timeout_ms);
    c.sleepRawMs(seconds * 1000);  // the measured window
    c.acqStop();
    c.waitQuiet();
    return c.stopRecording();
}

}  // namespace cxp::validation::checks::perf
