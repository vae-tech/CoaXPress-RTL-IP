// CXP-CAM-NEG-005.  See cases/_common.h.

#include "cxp/validation/cases/neg/_helpers.h"

namespace cxp::validation::checks::neg {

namespace {

void neg005(Context& c) {
    for (const BootReg& r : bootstrapTable()) {
        if (r.access != Access::RO) continue;
        auto a = c.readRaw(r.addr, r.bytes);
        if (!is(a, Ack::READ_OK)) {
            c.expect(false, "%s: read %s", r.name, ackStr(a).c_str());
            continue;
        }
        auto w = c.writeRaw(r.addr, a->values());
        auto b = c.readRaw(r.addr, r.bytes);
        c.expect(is(w, Ack::RO_WRITE) && b && b->values() == a->values(), "write to read-only %s: %s%s", r.name,
                 ackStr(w).c_str(), b && b->values() == a->values() ? "" : ", VALUE CHANGED");
    }
    size_t wo = 0;
    if (c.tree()) {
        for (Feature* f : c.tree()->features()) {
            if (!f->reg || f->reg->access != "WO") continue;
            ++wo;
            auto a = c.readRaw(uint32_t(f->reg->address), f->reg->length);
            c.expect(is(a, Ack::WO_READ), "read from write-only %s @%s: %s", f->name.c_str(),
                     hex(uint32_t(f->reg->address)).c_str(), ackStr(a).c_str());
        }
    }
    if (!wo) c.info("the XML declares no write-only register");
}
CXP_CHECK("CXP-CAM-NEG-005", neg005);

}  // namespace

}  // namespace cxp::validation::checks::neg
