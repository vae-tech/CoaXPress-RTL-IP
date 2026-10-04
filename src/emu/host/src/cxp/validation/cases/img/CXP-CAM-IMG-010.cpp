// CXP-CAM-IMG-010.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::img {

namespace {

void img010(Context& c) {
    std::string used;
    Feature* f = featureOrAlias(c, "DeviceTapGeometry", "TapGeometry", &used);
    if (!f) c.skip("no DeviceTapGeometry / TapGeometry feature");
    if (used != "DeviceTapGeometry") c.warn("tap geometry read from vendor feature '%s'", used.c_str());
    if (c.tree()) c.tree()->invalidate();
    const Value v = f->getValue();
    int64_t code = v.type == Value::Type::Int ? v.i : -1;
    if (v.type == Value::Type::String) {
        for (const auto& e : f->enum_entries) {
            if (e.first == v.s) code = e.second;
        }
    }
    auto ims = imagesOf(c.acquire(c.iparam("images")));
    imageRequired(c, 1, ims.size(), "image headers");
    size_t bad = 0;
    for (const auto& im : ims) bad += int64_t(im.tap_g) != code;
    c.expect(bad == 0, "header TapG 0x%04X equals %s = 0x%04llX in %zu of %zu images", ims.front().tap_g, used.c_str(),
             (long long)code, ims.size() - bad, ims.size());
    c.note("multi-tap streams (MTAP) are not implemented by this device class; per-tap checks not run");
}
CXP_CHECK("CXP-CAM-IMG-010", img010);

}  // namespace

}  // namespace cxp::validation::checks::img
