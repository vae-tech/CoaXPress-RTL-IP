// UVM-test_tpg_formats.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void tpgFormats(Context& c) {
    Feature& tp = c.need("TestPattern");
    runTestPattern(c);
    preserveFeature(c, "TestPattern");
    preserveFeature(c, "PixelFormat");
    Feature* pf = c.feature("PixelFormat");
    const size_t images = size_t(c.iparam("images"));
    if (!pf) {
        c.expect(false, "no PixelFormat feature: formats cannot be swept");
        return;
    }
    const std::string fmt_pat = c.sparam("format_pattern");
    if (hasEntry(&tp, fmt_pat)) trySet(c, "TestPattern", Value::ofString(fmt_pat));
    for (const auto& [name, value] : pf->enum_entries) {
        const auto code = pfncTable25(name);
        if (!code || !pixelBits(*code)) continue;
        if (!trySet(c, "PixelFormat", Value::ofString(name))) {
            c.expect(false, "PixelFormat = %s not accepted", name.c_str());
            continue;
        }
        const std::string tag = name + ": ";
        const auto pk = streamPackets(c.acquire(images));
        const auto fmt_id = testPatternId(fmt_pat);
        const bool golden = *code == 0x0101 && hasEntry(&tp, fmt_pat) && fmt_id && testPatternStatic(*fmt_id);
        const auto sb = streamScoreboard(c, pk, golden ? int(*fmt_id) : -1, 0, {}, tag.c_str());
        size_t wrong = 0;
        const auto ims = walkImages(pk);
        for (const auto& im : ims) wrong += im.pixel_f != *code;
        c.expect(!ims.empty() && wrong == 0, "%s%zu of %zu headers carry PixelF 0x%04X", tag.c_str(), ims.size() - wrong,
                 ims.size(), *code);
        c.expect(sb.complete >= 1, "%s%zu complete images", tag.c_str(), sb.complete);
    }
}
CXP_CHECK("UVM-test_tpg_formats", tpgFormats);

}  // namespace

}  // namespace cxp::validation::checks::uvm
