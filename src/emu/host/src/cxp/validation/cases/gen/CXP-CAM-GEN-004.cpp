// CXP-CAM-GEN-004.  See cases/_common.h.

#include "cxp/validation/cases/gen/_helpers.h"

namespace cxp::validation::checks::gen {

namespace {

void gen004(Context& c) {
    if (!c.tree()) c.skip("no XML loaded");
    const uint32_t x_regs[] = {Reg::REVISION, Reg::XML_VERSION, Reg::DEVICE_VENDOR_NAME, Reg::DEVICE_MODEL_NAME,
                               Reg::DEVICE_MANUFACTURER_INFO, Reg::DEVICE_VERSION, Reg::DEVICE_SERIAL_NUMBER,
                               Reg::DEVICE_USER_ID, Reg::CONNECTION_CONFIG, Reg::CONNECTION_CONFIG_DEFAULT,
                               Reg::TEST_MODE, Reg::TEST_ERROR_COUNT_SELECTOR, Reg::TEST_ERROR_COUNT,
                               Reg::TEST_PACKET_COUNT_TX, Reg::TEST_PACKET_COUNT_RX};
    for (uint32_t addr : x_regs) {
        const BootReg* br = nullptr;
        for (const auto& r : bootstrapTable()) {
            if (r.addr == addr) br = &r;
        }
        Feature* hit = nullptr;
        for (Feature* f : c.tree()->features()) {
            if (f->reg && f->reg->address == addr) hit = f;
        }
        if (!hit) {
            c.expect(false, "%s @0x%04X ('X' in Table 45) is not described in the XML", br->name, addr);
            continue;
        }
        const bool len_ok = hit->reg->length == br->bytes;
        const bool acc_ok = br->access == Access::RO ? hit->reg->access == "RO"
                                                     : hit->reg->access == "RW";
        c.expect(len_ok && acc_ok, "%s @0x%04X in XML as '%s': %u bytes %s (Table 45: %u bytes %s)", br->name, addr,
                 hit->name.c_str(), hit->reg->length, hit->reg->access.c_str(), br->bytes,
                 br->access == Access::RO ? "RO" : "RW");
        if (hit->name != br->name) c.note("%s is named '%s' in the XML", br->name, hit->name.c_str());
    }
    for (Feature* f : c.tree()->features()) {
        if (!f->reg || f->reg->address >= Reg::MANUFACTURER_SPACE) continue;
        bool known = false;
        for (const auto& r : bootstrapTable()) known |= f->reg->address == r.addr;
        if (!known) c.expect(false, "XML node %s at 0x%04X is not a Table 45 address", f->name.c_str(), uint32_t(f->reg->address));
    }
}
CXP_CHECK("CXP-CAM-GEN-004", gen004);

}  // namespace

}  // namespace cxp::validation::checks::gen
