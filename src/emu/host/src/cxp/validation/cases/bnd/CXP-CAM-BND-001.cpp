// CXP-CAM-BND-001.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::bnd {

namespace {

void bnd001(Context& c) {
    for (const char* f : {"Width", "Height", "OffsetX", "OffsetY"}) preserveFeature(c, f);
    const int64_t streamed_h = c.iparam("streamed_height"), streamed_w = c.iparam("streamed_width");
    for (const char* name : {"Width", "Height"}) {
        Feature& f = c.need(name);
        if (!f.reg) continue;
        const uint32_t addr = uint32_t(f.reg->address);
        const int64_t lo = f.min ? int64_t(*f.min) : 1;
        const int64_t hi = f.max ? int64_t(*f.max) : 0;
        const std::string other = std::string(name) == "Width" ? "Height" : "Width";
        trySet(c, other, Value::ofInt(std::string(name) == "Width" ? streamed_h : streamed_w));
        for (int64_t v : {lo, hi}) {
            if (v <= 0) continue;
            if (!trySet(c, name, Value::ofInt(v))) {
                c.expect(false, "%s = %lld (legal extreme) not accepted", name, (long long)v);
                continue;
            }
            auto ims = imagesOf(c.acquire(c.iparam("images"), c.iparam("acquire_timeout_ms")));
            const uint32_t got = ims.empty() ? 0 : (std::string(name) == "Width" ? ims[0].xsize : ims[0].ysize);
            c.expect(!ims.empty() && got == v, "%s = %lld streams with header value %u", name, (long long)v, got);
        }
        const uint32_t before = c.rd32(addr);
        std::vector<uint32_t> illegal = {0, 0xFFFFFF};
        if (lo > 1) illegal.push_back(uint32_t(lo - 1));
        if (hi) illegal.push_back(uint32_t(hi + 1));
        for (uint32_t v : illegal) {
            if (int64_t(v) >= lo && (!hi || int64_t(v) <= hi)) continue;
            auto w = c.writeRaw(addr, {v});
            const uint32_t after = c.rd32(addr);
            c.expect(is(w, Ack::BAD_DATA) && after == before, "%s = %u (illegal): %s, value %s", name, v, ackStr(w).c_str(),
                     after == before ? "unchanged" : "CHANGED");
            if (after != before) c.writeRaw(addr, {before});
        }
    }
}
CXP_CHECK("CXP-CAM-BND-001", bnd001);

}  // namespace

}  // namespace cxp::validation::checks::bnd
