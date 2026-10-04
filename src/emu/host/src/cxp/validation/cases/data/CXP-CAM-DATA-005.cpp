// CXP-CAM-DATA-005.  See cases/_common.h.

#include "cxp/validation/cases/data/_helpers.h"

namespace cxp::validation::checks::data {

namespace {

void data005(Context& c) {
    std::string used;
    Feature* f = featureOrAlias(c, "Image1StreamID", "StreamId", &used);
    if (!f) c.skip("the XML has neither Image1StreamID nor a StreamId feature");
    if (used != "Image1StreamID") c.warn("stream ID read from vendor feature '%s' (SFNC name is Image1StreamID)", used.c_str());
    auto sid = featInt(c, used);
    if (!sid) c.abort("cannot read " + used);
    auto pk = streamPackets(c.acquire(c.iparam("images")));
    auto ims = walkImages(pk);
    std::set<uint32_t> pkt_ids, hdr_ids;
    for (const auto& p : pk) pkt_ids.insert(p.stream_id);
    for (const auto& im : ims) hdr_ids.insert(im.stream_id);
    c.expect(pkt_ids.size() == 1, "%zu distinct StreamIDs in stream packets (one stream)", pkt_ids.size());
    c.expect(pkt_ids.size() == 1 && *pkt_ids.begin() == *sid, "packet StreamID %u equals %s = %lld",
             pkt_ids.empty() ? 0 : *pkt_ids.begin(), used.c_str(), (long long)*sid);
    c.expect(hdr_ids.size() == 1 && *hdr_ids.begin() == *sid, "image-header StreamID %u equals %s = %lld",
             hdr_ids.empty() ? 0 : *hdr_ids.begin(), used.c_str(), (long long)*sid);
    if (*sid != 0) c.warn("primary stream ID is %lld; §9.3 recommends 0", (long long)*sid);
}
CXP_CHECK("CXP-CAM-DATA-005", data005);

}  // namespace

}  // namespace cxp::validation::checks::data
