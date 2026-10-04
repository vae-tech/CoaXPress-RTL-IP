// CXP-CAM-IMG-004.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::img {

namespace {

void img004(Context& c) {
    Feature& pf = c.need("PixelFormat");
    preserveFeature(c, "PixelFormat");
    preserveFeature(c, "Width");
    trySet(c, "Width", Value::ofInt(c.iparam("test_width")));
    for (const auto& [name, value] : pf.enum_entries) {
        auto code = pfncTable25(name);
        if (!code || !pixelBits(*code)) continue;
        if (!trySet(c, "PixelFormat", Value::ofString(name))) continue;
        auto ims = imagesOf(c.acquire(c.iparam("images")));
        if (ims.empty()) {
            c.expect(false, "%s: no image", name.c_str());
            continue;
        }
        const ImageRec& im = ims.front();
        const int bits = *pixelBits(*code);
        const size_t want = (size_t(im.xsize) * size_t(bits) + 31) / 32;
        const size_t used = (size_t(im.xsize) * size_t(bits)) % 32;
        // Figure 28-30: the line's bits run MSB first from P0 bit 7; in the
        // last word the first `used` bits of P0, P1, ... are pixel data and
        // every later bit is 0.
        uint32_t pad_mask = 0;
        for (size_t i = used; used && i < 32; ++i) pad_mask |= 1u << (8 * (i / 8) + 7 - i % 8);
        size_t len_bad = 0, pad_bad = 0;
        for (const auto& line : im.lines) {
            if (line.size() != want) {
                ++len_bad;
                continue;
            }
            if (line.back() & pad_mask) ++pad_bad;
        }
        c.expect(len_bad == 0, "%s, width %u: %zu of %zu lines not %zu words (no packing across lines)", name.c_str(),
                 im.xsize, len_bad, im.lines.size(), want);
        c.expect(pad_bad == 0, "%s, width %u: %zu lines with non-zero bits after the first %zu bits (P0 bit 7 on) "
                 "of the last word (padding, or first pixel not in P0)", name.c_str(), im.xsize, pad_bad, used);
    }
}
CXP_CHECK("CXP-CAM-IMG-004", img004);

}  // namespace

}  // namespace cxp::validation::checks::img
