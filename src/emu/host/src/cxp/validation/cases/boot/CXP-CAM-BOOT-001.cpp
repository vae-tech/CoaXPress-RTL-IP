// CXP-CAM-BOOT-001.  See cases/_common.h.

#include "cxp/validation/cases/boot/_helpers.h"

namespace cxp::validation::checks::boot {

namespace {

void boot001(Context& c) {
    preserveLink(c);
    c.preserve(Reg::XML_MANIFEST_SELECTOR);
    const std::vector<uint8_t> uid0 = c.readBlock(Reg::DEVICE_USER_ID, 16);
    c.onExit([&c, uid0] {
        std::vector<uint32_t> v;
        for (size_t i = 0; i < 16; i += 4) {
            v.push_back(uint32_t(uid0[i]) << 24 | uint32_t(uid0[i + 1]) << 16 | uint32_t(uid0[i + 2]) << 8 | uid0[i + 3]);
        }
        c.writeRaw(Reg::DEVICE_USER_ID, v);
    });
    for (const BootReg& r : bootstrapTable()) {
        c.checkpoint();
        auto a = c.readRaw(r.addr, r.bytes);
        const bool ok = a && a->code == Ack::READ_OK && a->long_form && a->size_field == r.bytes &&
                        a->data.size() == r.bytes / 4 && a->crc_ok;
        c.expect(ok, "%s @0x%04X read %u bytes: %s%s", r.name, r.addr, r.bytes, ackStr(a).c_str(),
                 a && a->code == Ack::READ_OK && !ok
                     ? strprintf(", Size %u, %zu words", a->size_field, a->data.size()).c_str()
                     : "");
        if (!ok) continue;
        const std::vector<uint32_t> before = a->values();
        if (r.access == Access::RO) {
            auto w = c.writeRaw(r.addr, before);
            auto again = c.readRaw(r.addr, r.bytes);
            c.expect(is(w, Ack::RO_WRITE) && again && again->values() == before,
                     "%s is read-only: write answered %s, value %s", r.name, ackStr(w).c_str(),
                     again && again->values() == before ? "unchanged" : "CHANGED");
            continue;
        }
        if (r.addr == Reg::CONNECTION_RESET) {
            c.note("ConnectionReset write not exercised here (it resets the link; see INIT-002)");
            continue;
        }
        std::vector<uint32_t> probe = before;
        switch (r.addr) {
        case Reg::DEVICE_USER_ID: probe = {0x43585056, 0x414C4944, 0x2D555345, 0x52494400}; break;  // "CXPVALID-USERID"
        case Reg::MASTER_HOST_CONNECTION_ID: probe = {0x12345678}; break;
        case Reg::STREAM_PACKET_SIZE_MAX:  // a quarter of the host maximum, at least one data word
            probe = {std::max<uint32_t>(36, c.opt().host_spsm / 16 * 4)};
            break;
        case Reg::TEST_MODE: case Reg::TEST_ERROR_COUNT_SELECTOR: case Reg::XML_MANIFEST_SELECTOR:
        case Reg::TEST_ERROR_COUNT: probe = {0}; break;
        case Reg::TEST_PACKET_COUNT_TX: case Reg::TEST_PACKET_COUNT_RX: probe = {0, 0}; break;
        default: break;  // ConnectionConfig: the current value
        }
        auto w = c.writeRaw(r.addr, probe);
        auto back = c.readRaw(r.addr, r.bytes);
        c.expect(is(w, Ack::WRITE_OK) && back && back->values() == probe, "%s is R/W: write answered %s, read back %s",
                 r.name, ackStr(w).c_str(), back && back->values() == probe ? "equal" : "DIFFERENT");
        if (probe != before && r.addr != Reg::TEST_ERROR_COUNT && r.addr != Reg::TEST_PACKET_COUNT_TX &&
            r.addr != Reg::TEST_PACKET_COUNT_RX) {
            c.writeRaw(r.addr, before);
        }
    }
    struct Bits { const char* name; uint32_t addr; uint32_t mask; };
    for (const Bits& b : {Bits{"HsUpconnection[31:1]", Reg::HS_UPCONNECTION, 0xFFFFFFFEu},
                          Bits{"XmlVersion[31:24]", Reg::XML_VERSION, 0xFF000000u},
                          Bits{"XmlSchemaVersion[31:24]", Reg::XML_SCHEMA_VERSION, 0xFF000000u}}) {
        if (auto v = c.tryRd32(b.addr)) c.expect((*v & b.mask) == 0, "%s = 0 (%s)", b.name, hex(*v).c_str());
    }
    for (int64_t gap : c.ilist("unused_addresses")) {
        auto a = c.readRaw(uint32_t(gap), 4);
        c.note("unused bootstrap address 0x%04X read: %s (0x40 expected, pending clarification)", uint32_t(gap),
               ackStr(a).c_str());
    }
}
CXP_CHECK("CXP-CAM-BOOT-001", boot001);

}  // namespace

}  // namespace cxp::validation::checks::boot
