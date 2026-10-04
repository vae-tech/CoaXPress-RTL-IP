// CXP-CAM-IMG-009.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::img {

namespace {

void img009(Context& c) {
    Feature& tp = c.need("TestPattern");
    preserveFeature(c, "TestPattern");
    preserveFeature(c, "PixelFormat");
    const std::string ramp = c.sparam("pattern");
    bool has_grad = std::any_of(tp.enum_entries.begin(), tp.enum_entries.end(),
                                [&](const auto& e) { return e.first == ramp; });
    if (!has_grad) c.skip("no " + ramp + " (horizontal ramp) test pattern");
    trySet(c, "TestPattern", Value::ofString(ramp));
    if (c.feature("PixelFormat")) trySet(c, "PixelFormat", Value::ofString(c.sparam("pixel_format")));
    auto ims = completeImages(imagesOf(c.acquire(c.iparam("images"))));
    if (ims.empty()) c.abort("no complete image");
    size_t up = 0, up_rev = 0, pairs = 0;
    for (const auto& line : ims.front().lines) {
        auto p0 = lineBytesP0(line), p3 = lineBytesP3(line);
        const size_t w = ims.front().xsize;
        for (size_t x = 1; x < w && x < p0.size(); ++x) {
            ++pairs;
            up += uint8_t(p0[x] - p0[x - 1]) == 1;
            up_rev += uint8_t(p3[x] - p3[x - 1]) == 1;
        }
    }
    const double frac = pairs ? double(up) / double(pairs) : 0;
    const double need = c.iparam("min_percent") / 100.0;
    c.expect(frac >= need, "%.0f%% of neighbouring pixels (P0 first) increase by one along the line (>= %d%%)",
             100 * frac, c.iparam("min_percent"));
    if (frac < need && pairs && double(up_rev) / double(pairs) >= need) {
        c.note("the ramp is intact with bytes read MSB-first: the first pixel of each word is in P3, not P0");
    }
}
CXP_CHECK("CXP-CAM-IMG-009", img009);

}  // namespace

}  // namespace cxp::validation::checks::img
