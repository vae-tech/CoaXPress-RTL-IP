// CXP-CAM-IMG-011.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::img {

namespace {

void img011(Context& c) {
    Feature& tp = c.need("TestPattern");
    preserveFeature(c, "TestPattern");
    preserveFeature(c, "PixelFormat");
    if (c.feature("PixelFormat")) trySet(c, "PixelFormat", Value::ofString(c.sparam("pixel_format")));
    size_t crc_bad = 0, checked = 0, mismatched = 0, rev_ok = 0;
    for (const std::string& pat : c.slist("patterns")) {
        const bool known = std::any_of(tp.enum_entries.begin(), tp.enum_entries.end(),
                                       [&](const auto& e) { return e.first == pat; });
        if (!known || !trySet(c, "TestPattern", Value::ofString(pat))) continue;
        const auto pat_id = testPatternId(pat);
        if (!pat_id || !testPatternStatic(*pat_id)) {
            c.note("TestPattern %s has no static golden image; not compared", pat.c_str());
            continue;
        }
        auto pk = streamPackets(c.acquire(c.iparam("images")));
        for (const auto& p : pk) crc_bad += !p.crc_ok;
        for (const auto& im : completeImages(walkImages(pk))) {
            const auto gold = renderTestPattern(PixelFormat::Mono8, im.xsize, im.ysize, *pat_id, 0);
            bool ok = true, ok_rev = true;
            for (uint32_t y = 0; y < im.ysize; ++y) {
                auto a = lineBytesP0(im.lines[y]), b = lineBytesP3(im.lines[y]);
                ok &= std::equal(gold.begin() + y * im.xsize, gold.begin() + (y + 1) * im.xsize, a.begin());
                ok_rev &= std::equal(gold.begin() + y * im.xsize, gold.begin() + (y + 1) * im.xsize, b.begin());
            }
            ++checked;
            mismatched += !ok;
            rev_ok += !ok && ok_rev;
        }
    }
    if (!checked) c.skip("no Bars / GreyBars test pattern could be streamed");
    c.expect(mismatched == 0, "%zu of %zu images match the golden Bars/GreyBars model bit-exact (P0 first)",
             checked - mismatched, checked);
    if (rev_ok) c.note("%zu images match only with bytes read MSB-first (first pixel in P3, not P0)", rev_ok);
    c.expect(crc_bad == 0, "%zu stream CRC errors", crc_bad);
}
CXP_CHECK("CXP-CAM-IMG-011", img011);

}  // namespace

}  // namespace cxp::validation::checks::img
