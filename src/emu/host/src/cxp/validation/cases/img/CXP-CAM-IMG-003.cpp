// CXP-CAM-IMG-003.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::img {

namespace {

void img003(Context& c) {
    c.need("Width");
    c.need("Height");
    for (const char* f : {"Width", "Height", "OffsetX", "OffsetY"}) preserveFeature(c, f);
    const bool offs = c.feature("OffsetX") && c.feature("OffsetY");
    struct Roi { int64_t w, h, x, y; };
    Feature& fw = c.need("Width");
    const int64_t wmin = fw.min ? int64_t(*fw.min) : 1;
    std::vector<Roi> sweep;
    for (const auto& r : c.rows("roi_list")) sweep.push_back({std::max(wmin, r.at(0)), r.at(1), r.at(2), r.at(3)});
    for (const Roi& r : sweep) {
        if (r.x && !offs) continue;
        if (offs) {
            trySet(c, "OffsetX", Value::ofInt(r.x));
            trySet(c, "OffsetY", Value::ofInt(r.y));
        }
        if (!trySet(c, "Width", Value::ofInt(r.w)) || !trySet(c, "Height", Value::ofInt(r.h))) continue;
        auto ims = imagesOf(c.acquire(c.iparam("images")));
        if (ims.empty()) {
            c.expect(false, "ROI %lldx%lld+%lld+%lld: no image", (long long)r.w, (long long)r.h, (long long)r.x, (long long)r.y);
            continue;
        }
        const ImageRec& im = ims.front();
        const size_t want = specLineWords(im);
        c.expect(im.xsize == r.w && im.ysize == r.h && (!offs || (im.xoffs == r.x && im.yoffs == r.y)),
                 "ROI %lldx%lld+%lld+%lld: header %ux%u+%u+%u", (long long)r.w, (long long)r.h, (long long)r.x,
                 (long long)r.y, im.xsize, im.ysize, im.xoffs, im.yoffs);
        c.expect(want && im.dsize_l == want, "ROI %lldx%lld: DsizeL = %u (ceil(Xsize x bpp / 32) = %zu words)",
                 (long long)r.w, (long long)r.h, im.dsize_l, want);
        c.expect(!im.line_words.empty() && im.line_words.front() == want, "ROI %lldx%lld: %zu words per line on the wire (%zu)",
                 (long long)r.w, (long long)r.h, im.line_words.empty() ? 0 : im.line_words.front(), want);
    }
}
CXP_CHECK("CXP-CAM-IMG-003", img003);

}  // namespace

}  // namespace cxp::validation::checks::img
