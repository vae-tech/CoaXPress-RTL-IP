// CXP-CAM-BOOT-002.  See cases/_common.h.

#include "cxp/validation/cases/boot/_helpers.h"

namespace cxp::validation::checks::boot {

namespace {

void boot002(Context& c) {
    auto a = c.readRaw(Reg::STANDARD, 4);
    if (!c.expect(is(a, Ack::READ_OK) && !a->data.empty(), "read Standard: %s", ackStr(a).c_str())) return;
    const uint32_t wire = a->data[0];
    c.expect((wire & 0xFF) == 0xC0, "first character on the wire (P0) = 0x%02X (0xC0, big-endian)", wire & 0xFF);
    c.expect(a->values()[0] == CXP_MAGIC, "Standard = %s (0xC0A79AE5)", hex(a->values()[0]).c_str());
    const uint32_t rev = c.rd32(Reg::REVISION);
    c.expect(rev == 0x00010001, "Revision = %s (0x00010001: major 1 in [31:16], minor 1 in [15:0])",
             hex(rev).c_str());
}
CXP_CHECK("CXP-CAM-BOOT-002", boot002);

}  // namespace

}  // namespace cxp::validation::checks::boot
