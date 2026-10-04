// CXP-CAM-DATA-002.  See cases/_common.h.

#include "cxp/validation/cases/data/_helpers.h"

namespace cxp::validation::checks::data {

namespace {

void data002(Context& c) {
    c.prepareStreaming();
    c.preserve(Reg::STREAM_PACKET_SIZE_MAX);
    c.wr32(Reg::STREAM_PACKET_SIZE_MAX, uint32_t(c.iparam("spsm")));
    const int packets = c.iparam("packets");
    auto pk = streamPackets(c.acquirePackets(packets));
    std::set<uint8_t> sids;
    for (const auto& p : pk) sids.insert(p.stream_id);
    bool wrapped = false;
    for (uint8_t sid : sids) {
        auto tags = pktTags(pk, sid);
        int first_bad = -1;
        const size_t br = tagBreaks(tags, &first_bad);
        for (size_t i = 1; i < tags.size(); ++i) wrapped |= tags[i - 1] == 0xFF && tags[i] == 0;
        c.expect(br == 0, "stream %u: %zu packets, %zu tag discontinuities%s", sid, tags.size(), br,
                 br ? strprintf(" (first at %d: %u -> %u)", first_bad, tags[size_t(first_bad) - 1], tags[size_t(first_bad)]).c_str() : "");
    }
    c.expect(pk.size() >= size_t(packets), "%zu stream packets captured (>= %d)", pk.size(), packets);
    c.expect(wrapped, "tag wrap 0xFF -> 0x00 observed");
}
CXP_CHECK("CXP-CAM-DATA-002", data002);

}  // namespace

}  // namespace cxp::validation::checks::data
