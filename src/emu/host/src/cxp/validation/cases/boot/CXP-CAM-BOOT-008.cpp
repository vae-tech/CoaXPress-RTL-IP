// CXP-CAM-BOOT-008.  See cases/_common.h.

#include "cxp/validation/cases/boot/_helpers.h"

namespace cxp::validation::checks::boot {

namespace {

void boot008(Context& c) {
    if (!c.tree()) c.skip("no XML loaded");
    size_t rw = 0, wo = 0;
    for (Feature* f : c.tree()->features()) {
        if (!f->reg || f->reg->address < Reg::MANUFACTURER_SPACE) continue;
        const std::string acc = f->reg->access;
        if (acc == "RW") {
            ++rw;
            auto a = c.readRaw(uint32_t(f->reg->address), f->reg->length);
            c.expect(is(a, Ack::READ_OK), "%s (RW @%s) readable: %s", f->name.c_str(),
                     hex(uint32_t(f->reg->address)).c_str(), ackStr(a).c_str());
        } else if (acc == "WO") {
            ++wo;
            auto a = c.readRaw(uint32_t(f->reg->address), f->reg->length);
            c.warn("%s is write-only (@%s, read answers %s)%s", f->name.c_str(), hex(uint32_t(f->reg->address)).c_str(),
                   ackStr(a).c_str(), f->kind == FeatureKind::Command ? " — a command register" : "");
        }
    }
    c.info("%zu RW and %zu WO manufacturer registers in the XML", rw, wo);
}
CXP_CHECK("CXP-CAM-BOOT-008", boot008);

}  // namespace

}  // namespace cxp::validation::checks::boot
