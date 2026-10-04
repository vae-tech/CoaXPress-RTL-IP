// CXP-CAM-DATA-006.  See cases/_common.h.

#include "cxp/validation/cases/data/_helpers.h"

namespace cxp::validation::checks::data {

namespace {

void data006(Context& c) {
    c.prepareStreaming();
    c.preserve(Reg::STREAM_PACKET_SIZE_MAX);
    bool static_pattern = false;
    if (Feature* tp = c.feature("TestPattern"); tp && tp->kind == FeatureKind::Enumeration) {
        preserveFeature(c, "TestPattern");
        static_pattern = trySet(c, "TestPattern", Value::ofString(c.sparam("pattern")));
    }
    std::vector<std::vector<Words>> refs;
    for (uint32_t spsm : c.spsmList("spsm_list")) {
        c.wr32(Reg::STREAM_PACKET_SIZE_MAX, spsm);
        auto ims = walkImages(streamPackets(c.acquire(c.iparam("images"))));
        auto done = completeImages(ims);
        c.expect(!done.empty(), "SPSM = %u: %zu of %zu images reassemble with Ysize lines of the spec length", spsm,
                 done.size(), ims.size());
        if (!done.empty()) refs.push_back(done.front().lines);
    }
    if (static_pattern && refs.size() > 1) {
        bool same = true;
        for (const auto& r : refs) same &= r == refs.front();
        c.expect(same, "static test pattern reassembles bit-exact at every packet size");
    } else {
        c.note("no static test pattern selectable; bit-exact comparison across packet sizes skipped");
    }
}
CXP_CHECK("CXP-CAM-DATA-006", data006);

}  // namespace

}  // namespace cxp::validation::checks::data
