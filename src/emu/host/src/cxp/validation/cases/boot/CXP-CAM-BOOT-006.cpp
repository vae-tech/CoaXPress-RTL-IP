// CXP-CAM-BOOT-006.  See cases/_common.h.

#include "cxp/validation/cases/boot/_helpers.h"

namespace cxp::validation::checks::boot {

namespace {

void boot006(Context& c) {
    for (const UseCase& u : kUseCases) {
        const uint32_t p = c.rd32(u.addr);
        if (!c.expect(p >= Reg::MANUFACTURER_SPACE && p % 4 == 0, "%s = %s (manufacturer space >= 0x6000, aligned)", u.reg,
                      hex(p).c_str())) {
            continue;
        }
        std::string used;
        if (Feature* f = featureOrAlias(c, u.sfnc, u.alias, &used); f && f->reg) {
            c.expect(f->reg->address == p, "%s matches XML %s register %s", u.reg, used.c_str(),
                     hex(uint32_t(f->reg->address)).c_str());
        } else {
            c.warn("XML has no %s feature to compare %s with", u.sfnc, u.reg);
        }
        if (u.readable) {
            auto a = c.readRaw(p, 4);
            c.expect(is(a, Ack::READ_OK), "%s target readable: %s", u.reg, ackStr(a).c_str());
        }
    }
    for (int n = 2; n <= 16; ++n) {
        const uint32_t p = c.rd32(Reg::imageStreamIdAddress(n));
        const bool declared = c.feature(strprintf("Image%dStreamID", n)) != nullptr;
        if (declared) {
            c.expect(p >= Reg::MANUFACTURER_SPACE, "Image%dStreamIDAddress = %s (stream declared in XML)", n, hex(p).c_str());
        } else {
            c.expect(p == 0, "Image%dStreamIDAddress = %s (0 for an unsupported stream)", n, hex(p).c_str());
        }
    }
}
CXP_CHECK("CXP-CAM-BOOT-006", boot006);

}  // namespace

}  // namespace cxp::validation::checks::boot
