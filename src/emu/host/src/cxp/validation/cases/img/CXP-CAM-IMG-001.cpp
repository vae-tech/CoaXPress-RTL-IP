// CXP-CAM-IMG-001.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::img {

namespace {

void img001(Context& c) {
    const int n = c.iparam("images");
    auto ims = imagesOf(c.acquire(size_t(n)));
    imageRequired(c, size_t(n), ims.size(), "image headers");
    size_t bad = 0;
    for (const auto& im : ims) {
        std::string why;
        if (!im.header_complete) why = "header cut short";
        else if (!im.replicas_ok) why = "a header byte is not 4x replicated";
        else if (im.flags & 0xFC) why = strprintf("Flags 0x%02X: bits 7:2 not zero", im.flags);
        else if ((im.flags & 3) == 3) why = "Flags interlace field = 3 (reserved)";
        else if (!pixelBits(im.pixel_f)) why = strprintf("PixelF 0x%04X not a Table 25 code", im.pixel_f);
        if (!why.empty() && ++bad <= size_t(c.opt().max_reported)) c.expect(false, "header SourceTag %u: %s", im.source_tag, why.c_str());
    }
    c.expect(bad == 0, "%zu of %zu rectangular headers well formed (Table 38)", ims.size() - bad, ims.size());
}
CXP_CHECK("CXP-CAM-IMG-001", img001);

}  // namespace

}  // namespace cxp::validation::checks::img
