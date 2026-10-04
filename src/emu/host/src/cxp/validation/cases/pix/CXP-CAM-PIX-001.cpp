// CXP-CAM-PIX-001.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::pix {

namespace {

void pix001(Context& c) {
    Feature& pf = c.need("PixelFormat");
    preserveFeature(c, "PixelFormat");
    for (const auto& [name, value] : pf.enum_entries) {
        auto code = pfncTable25(name);
        if (!c.expect(code.has_value(), "PixelFormat entry '%s' is a Table 25 PFNC name", name.c_str())) continue;
        // §11.2.1.6: the feature's value is the PFNC one; the device maps it
        // to the PixelF code it sends.
        if (auto pfnc = pfncValue(name)) {
            c.expect(uint32_t(value) == *pfnc, "%s: entry value 0x%08llX is the PFNC value 0x%08X", name.c_str(),
                     (unsigned long long)value, *pfnc);
        }
        if (!trySet(c, "PixelFormat", Value::ofString(name))) {
            c.expect(false, "PixelFormat = %s not settable", name.c_str());
            continue;
        }
        auto ims = imagesOf(c.acquire(c.iparam("images")));
        if (ims.empty()) {
            c.expect(false, "%s: no image", name.c_str());
            continue;
        }
        c.expect(ims.front().pixel_f == *code, "%s: header PixelF 0x%04X (Table 25: 0x%04X)", name.c_str(),
                 ims.front().pixel_f, *code);
    }
}
CXP_CHECK("CXP-CAM-PIX-001", pix001);

}  // namespace

}  // namespace cxp::validation::checks::pix
