// UVM-test_tpg_config.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void tpgConfig(Context& c) {
    Feature& tp = c.need("TestPattern");
    runTestPattern(c);
    preserveFeature(c, "TestPattern");
    preserveFeature(c, "PixelFormat");
    if (c.feature("PixelFormat")) trySet(c, "PixelFormat", Value::ofString(c.sparam("pixel_format")));
    size_t tried = 0;
    const size_t images = size_t(c.iparam("images"));
    for (const std::string& name : c.slist("patterns")) {
        if (!hasEntry(&tp, name)) continue;
        if (!trySet(c, "TestPattern", Value::ofString(name))) {
            c.expect(false, "TestPattern = %s not accepted", name.c_str());
            continue;
        }
        ++tried;
        const auto id = testPatternId(name);
        const int pat = id && testPatternStatic(*id) ? int(*id) : -1;
        const std::string tag = name + ": ";
        const auto sb = streamScoreboard(c, streamPackets(c.acquire(images)), pat, 0, {}, tag.c_str());
        c.expect(sb.complete >= 1, "%s%zu complete images", tag.c_str(), sb.complete);
    }
    c.expect(tried > 0, "%zu test patterns streamed", tried);
}
CXP_CHECK("UVM-test_tpg_config", tpgConfig);

}  // namespace

}  // namespace cxp::validation::checks::uvm
