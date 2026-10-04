// CXP-CAM-NEG-003.  See cases/_common.h.

#include "cxp/validation/cases/neg/_helpers.h"

namespace cxp::validation::checks::neg {

namespace {

void neg003(Context& c) {
    preserveLink(c);
    c.preserve(Reg::XML_MANIFEST_SELECTOR);
    c.preserve(Reg::TEST_ERROR_COUNT_SELECTOR);
    c.preserve(Reg::TEST_MODE);
    const uint32_t ccd = c.rd32(Reg::CONNECTION_CONFIG_DEFAULT);
    for (uint32_t code : {0x00u, 0x29u, 0x50u}) rejectValue(c, "ConnectionConfig speed", Reg::CONNECTION_CONFIG, 1u << 16 | code);  // 1 connection
    rejectValue(c, "ConnectionConfig 0 connections", Reg::CONNECTION_CONFIG, 0x00000028u);
    rejectValue(c, "ConnectionConfig too many connections", Reg::CONNECTION_CONFIG, ((ccd >> 16) + 1) << 16 | 0x28u);
    rejectValue(c, "XmlManifestSelector", Reg::XML_MANIFEST_SELECTOR, c.rd32(Reg::XML_MANIFEST_SIZE));
    rejectValue(c, "TestErrorCountSelector", Reg::TEST_ERROR_COUNT_SELECTOR, 2);
    rejectValue(c, "TestMode", Reg::TEST_MODE, 2);
    rejectValue(c, "ConnectionReset", Reg::CONNECTION_RESET, 2);
    rejectValue(c, "StreamPacketSizeMax (not a multiple of 4)", Reg::STREAM_PACKET_SIZE_MAX, 1025);
    if (!c.tree()) return;
    for (Feature* f : c.tree()->features()) {
        if (!f->reg || f->reg->address < Reg::MANUFACTURER_SPACE || f->reg->access != "RW" || f->reg->length != 4) continue;
        if (f->kind == FeatureKind::Integer && f->max && *f->max < 0xFFFFFFFF) {
            rejectValue(c, (f->name + " > Max").c_str(), uint32_t(f->reg->address), uint32_t(*f->max) + 1);
        } else if (f->kind == FeatureKind::Enumeration && !f->enum_entries.empty()) {
            int64_t hi = 0;
            for (const auto& e : f->enum_entries) hi = std::max(hi, e.second);
            rejectValue(c, (f->name + " not an entry").c_str(), uint32_t(f->reg->address), uint32_t(hi + 1));
        }
    }
}
CXP_CHECK("CXP-CAM-NEG-003", neg003);

}  // namespace

}  // namespace cxp::validation::checks::neg
