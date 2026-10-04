// CXP-CAM-GEN-009.  See cases/_common.h.

#include "cxp/validation/cases/gen/_helpers.h"

namespace cxp::validation::checks::gen {

namespace {

void gen009(Context& c) {
    const uint32_t base = c.rd32(Reg::IIDC2_ADDRESS);
    if (base == 0) c.skip("Iidc2Address = 0: not an IIDC2 device");
    size_t nodes = 0;
    if (c.tree()) {
        for (Feature* f : c.tree()->features()) nodes += f->reg && f->reg->address >= base;
    }
    c.expect(nodes > 0, "%zu XML nodes in the IIDC2 space from %s", nodes, hex(base).c_str());
}
CXP_CHECK("CXP-CAM-GEN-009", gen009);

}  // namespace

}  // namespace cxp::validation::checks::gen
