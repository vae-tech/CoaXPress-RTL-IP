// CXP-CAM-GEN-007.  See cases/_common.h.

#include "cxp/validation/cases/gen/_helpers.h"

namespace cxp::validation::checks::gen {

namespace {

void gen007(Context& c) {
    c.need("Width");
    c.need("Height");
    Feature& pf = c.need("PixelFormat");
    for (const char* f : {"Width", "Height", "PixelFormat", "OffsetX", "OffsetY"}) preserveFeature(c, f);
    std::vector<std::string> fmts;
    for (const auto& e : pf.enum_entries) {
        if (fmts.size() < size_t(c.iparam("formats")) && pfncTable25(e.first)) fmts.push_back(e.first);
    }
    const bool offs = c.feature("OffsetX") && c.feature("OffsetY");
    for (int64_t w : c.ilist("widths")) {
        for (int64_t h : c.ilist("heights")) {
            for (const auto& fmt : fmts) {
                const int64_t ox = offs ? w / 4 : 0, oy = offs ? h / 4 : 0;
                if (!trySet(c, "Width", Value::ofInt(w)) || !trySet(c, "Height", Value::ofInt(h)) ||
                    !trySet(c, "PixelFormat", Value::ofString(fmt))) {
                    continue;
                }
                if (offs) {
                    trySet(c, "OffsetX", Value::ofInt(ox));
                    trySet(c, "OffsetY", Value::ofInt(oy));
                }
                auto ims = imagesOf(c.acquire(c.iparam("images")));
                if (ims.empty()) {
                    c.expect(false, "%lldx%lld %s: no image", (long long)w, (long long)h, fmt.c_str());
                    continue;
                }
                const ImageRec& im = ims.front();
                c.expect(im.xsize == w && im.ysize == h && im.pixel_f == *pfncTable25(fmt) &&
                             (!offs || (im.xoffs == ox && im.yoffs == oy)),
                         "%lldx%lld+%lld+%lld %s: header %ux%u+%u+%u PixelF 0x%04X", (long long)w, (long long)h,
                         (long long)ox, (long long)oy, fmt.c_str(), im.xsize, im.ysize, im.xoffs, im.yoffs, im.pixel_f);
            }
        }
    }
}
CXP_CHECK("CXP-CAM-GEN-007", gen007);

}  // namespace

}  // namespace cxp::validation::checks::gen
