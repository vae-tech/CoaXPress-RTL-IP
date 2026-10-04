// CXP-CAM-GEN-003.  See cases/_common.h.

#include "cxp/validation/cases/gen/_helpers.h"

namespace cxp::validation::checks::gen {

namespace {

void gen003(Context& c) {
    if (!c.tree()) c.skip("no XML loaded");
    struct Want { const char* name; FeatureKind kind; bool write; };
    const Want want[] = {{"Width", FeatureKind::Integer, true}, {"Height", FeatureKind::Integer, true},
                         {"AcquisitionMode", FeatureKind::Enumeration, true},
                         {"AcquisitionStart", FeatureKind::Command, true}, {"AcquisitionStop", FeatureKind::Command, true},
                         {"PixelFormat", FeatureKind::Enumeration, true},
                         {"DeviceTapGeometry", FeatureKind::Enumeration, false},
                         {"Image1StreamID", FeatureKind::Integer, false}};
    size_t ok = 0;
    for (const Want& w : want) {
        Feature* f = c.feature(w.name);
        const bool good = f && f->kind == w.kind && (!w.write || f->isWritable()) && f->reg && f->reg->length == 4;
        ok += good;
        c.expect(good, "%s: %s", w.name,
                 !f ? "missing" : strprintf("%s, %s, %u-byte register", featureKindName(f->kind), f->effAccess().c_str(),
                                            f->reg ? f->reg->length : 0).c_str());
    }
    if (Feature* m = c.feature("AcquisitionMode")) {
        const bool cont = std::any_of(m->enum_entries.begin(), m->enum_entries.end(),
                                      [](const auto& e) { return e.first == "Continuous"; });
        c.expect(cont, "AcquisitionMode offers Continuous");
    }
    c.info("%zu of 8 mandatory use-case features present", ok);
}
CXP_CHECK("CXP-CAM-GEN-003", gen003);

}  // namespace

}  // namespace cxp::validation::checks::gen
