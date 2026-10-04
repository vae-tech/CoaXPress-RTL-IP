#include "cxp/validation/cases/neg/_helpers.h"

namespace cxp::validation::checks::neg {

bool coveredByXml(Context& c, uint32_t addr) {
    if (!c.tree()) return false;
    for (Feature* f : c.tree()->features()) {
        if (f->reg && addr >= f->reg->address && addr < f->reg->address + f->reg->length) return true;
    }
    return false;
}

void rejectValue(Context& c, const char* name, uint32_t addr, uint32_t bad) {
    const uint32_t before = c.rd32(addr);
    auto a = c.writeRaw(addr, {bad});
    const uint32_t after = c.rd32(addr);
    c.expect(is(a, Ack::BAD_DATA) && after == before, "%s = %s: %s, value %s", name, hex(bad).c_str(), ackStr(a).c_str(),
             after == before ? "unchanged" : strprintf("changed to %s", hex(after).c_str()).c_str());
    if (after != before) c.writeRaw(addr, {before});
}

}  // namespace cxp::validation::checks::neg
