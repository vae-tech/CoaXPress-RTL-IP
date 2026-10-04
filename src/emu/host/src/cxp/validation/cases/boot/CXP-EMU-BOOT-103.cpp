// CXP-EMU-BOOT-103.  See cases/_common.h.
//
// The address of a command is 32 bits (Table 21).  A device that decodes
// only the low bits answers a bootstrap register at 0x00014000 or
// 0x80004000 too, and a write of 1 there would be a ConnectionReset.  Every
// alias of a Table 45 register (the address with one high bit set) that is
// not itself a register of this device answers 0x40 (Table 22), reads and
// writes alike, and a write there has no effect.

#include "cxp/validation/cases/boot/_helpers.h"

namespace cxp::validation::checks::boot {

namespace {

// Addresses this device really decodes above the bootstrap area: the
// manufacturer window (4 KB from MFR_BASE, the XML-described device
// registers), the XML ROM (XmlUrl points into it) and a bench user window.
bool knownRegister(Context& c, uint32_t a) {
    if (a >= Reg::MFR_BASE && a < Reg::MFR_BASE + 0x1000u) return true;
    if (a >= Reg::XML_BLOB_ADDR && a < Reg::XML_BLOB_ADDR + 0x100000u) return true;
    if ((c.benchCaps() & bench::CAP_REG_STALL) && a >= bench::USER_BASE && a < bench::USER_BASE + bench::USER_SIZE) return true;
    return false;
}

void boot103(Context& c) {
    preserveLink(c);
    const uint32_t mh = uint32_t(c.iparam("sentinel"));
    c.wr32(Reg::MASTER_HOST_CONNECTION_ID, mh);
    const uint32_t spsm = c.rd32(Reg::STREAM_PACKET_SIZE_MAX), cc = c.rd32(Reg::CONNECTION_CONFIG);

    std::vector<uint32_t> bits;
    for (int64_t b : c.ilist("alias_bits")) bits.push_back(uint32_t(b));
    size_t probed = 0, bad = 0, skipped = 0;
    for (const BootReg& r : bootstrapTable()) {
        for (uint32_t b : bits) {
            const uint32_t alias = r.addr | (1u << b);
            if (alias == r.addr) continue;
            if (knownRegister(c, alias)) {
                ++skipped;
                continue;
            }
            ++probed;
            auto a = c.readRaw(alias, 4);
            if (!is(a, Ack::BAD_ADDRESS) && ++bad <= size_t(c.opt().max_reported)) {
                c.expect(false, "read of 0x%08X (%s with bit %u): %s", alias, r.name, b, ackStr(a).c_str());
            }
        }
    }
    c.expect(bad == 0, "%zu of %zu aliases of Table 45 registers answer a read 0x40 (%zu that are this device's "
             "registers skipped)", probed - bad, probed, skipped);

    // Writes to aliases of the writable bootstrap registers, ConnectionReset first.
    size_t wbad = 0, wn = 0;
    for (uint32_t base : {Reg::CONNECTION_RESET, Reg::MASTER_HOST_CONNECTION_ID, Reg::STREAM_PACKET_SIZE_MAX,
                          Reg::CONNECTION_CONFIG, Reg::TEST_MODE, Reg::DEVICE_USER_ID}) {
        for (uint32_t b : bits) {
            const uint32_t alias = base | (1u << b);
            if (alias == base || knownRegister(c, alias)) continue;
            ++wn;
            const uint32_t v = base == Reg::CONNECTION_RESET || base == Reg::TEST_MODE ? 1u
                             : base == Reg::STREAM_PACKET_SIZE_MAX ? 64u : 0xA11A5000u | b;
            auto a = c.writeRaw(alias, {v});
            if (!is(a, Ack::BAD_ADDRESS) && ++wbad <= size_t(c.opt().max_reported)) {
                c.expect(false, "write 0x%08X to 0x%08X: %s", v, alias, ackStr(a).c_str());
            }
        }
    }
    c.expect(wbad == 0, "%zu of %zu writes to aliases answered 0x40", wn - wbad, wn);
    c.sleepMs(200);  // a ConnectionReset would be done by now (§10.1.2)
    const uint32_t mh2 = c.rd32(Reg::MASTER_HOST_CONNECTION_ID), spsm2 = c.rd32(Reg::STREAM_PACKET_SIZE_MAX),
                   cc2 = c.rd32(Reg::CONNECTION_CONFIG), tm = c.rd32(Reg::TEST_MODE);
    c.expect(mh2 == mh && spsm2 == spsm && cc2 == cc && tm == 0,
             "no side effect: MasterHostConnectionID 0x%08X (0x%08X), StreamPacketSizeMax %u (%u), "
             "ConnectionConfig 0x%08X (0x%08X), TestMode %u (0)", mh2, mh, spsm2, spsm, cc2, cc, tm);

    // The real registers still answer at their own addresses.
    auto s = c.readRaw(Reg::STANDARD, 4);
    c.expect(is(s, Ack::READ_OK) && s->values().size() == 1 && s->values()[0] == CXP_MAGIC,
             "Standard at 0x00000000 still reads 0xC0A79AE5: %s", ackStr(s).c_str());
}
CXP_CHECK("CXP-EMU-BOOT-103", boot103);

}  // namespace

}  // namespace cxp::validation::checks::boot
