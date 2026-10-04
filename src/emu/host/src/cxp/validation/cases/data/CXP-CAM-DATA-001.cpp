// CXP-CAM-DATA-001.  See cases/_common.h.

#include "cxp/validation/cases/data/_helpers.h"

namespace cxp::validation::checks::data {

namespace {

void data001(Context& c) {
    c.prepareStreaming();
    c.preserve(Reg::STREAM_PACKET_SIZE_MAX);
    size_t total = 0, bad = 0;
    for (uint32_t spsm : c.spsmList("spsm_list")) {
        c.wr32(Reg::STREAM_PACKET_SIZE_MAX, spsm);
        for (const auto& p : streamPackets(c.acquire(c.iparam("images")))) {
            ++total;
            const bool ok = p.defects.empty() && p.total_words == p.payload.size() + 8;
            if (!ok && ++bad <= size_t(c.opt().max_reported)) {
                c.expect(false, "packet tag %u: %s", p.tag,
                         p.defects.empty() ? "total length != N + 8" : p.defects.front().c_str());
            }
        }
    }
    c.expect(total > 0 && bad == 0, "%zu of %zu stream packets match Table 19", total - bad, total);
}
CXP_CHECK("CXP-CAM-DATA-001", data001);

}  // namespace

}  // namespace cxp::validation::checks::data
