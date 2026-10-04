// CXP-CAM-NEG-001.  See cases/_common.h.

#include "cxp/validation/cases/neg/_helpers.h"

namespace cxp::validation::checks::neg {

namespace {

void neg001(Context& c) {
    c.preserve(Reg::MASTER_HOST_CONNECTION_ID);
    c.wr32(Reg::MASTER_HOST_CONNECTION_ID, 0xA5A5A5A5);
    size_t nack = 0;
    for (int bit = 0; bit < 32; ++bit) {
        CmdSpec s;
        s.opcode = 0x01;
        s.size_bytes = 4;
        s.address = Reg::MASTER_HOST_CONNECTION_ID;
        s.data = {bswap32(0x5A5A0000u | uint32_t(bit))};
        s.crc_flip_bit = bit;
        auto a = c.exchange(buildCmd(s));
        nack += is(a, Ack::CRC_ERROR);
        if (!is(a, Ack::CRC_ERROR) && bit < 4) c.expect(false, "CRC bit %d flipped: %s", bit, ackStr(a).c_str());
    }
    c.expect(nack == 32, "%zu of 32 single-bit CRC errors answered 0x80", nack);
    CmdSpec s;
    s.opcode = 0x01;
    s.size_bytes = 4;
    s.address = Reg::MASTER_HOST_CONNECTION_ID;
    s.data = {bswap32(0x5A5A5A5A)};
    s.data_flip_bit = 3;
    auto a = c.exchange(buildCmd(s));
    c.expect(is(a, Ack::CRC_ERROR), "payload bit flipped under the original CRC: %s", ackStr(a).c_str());
    const uint32_t v = c.rd32(Reg::MASTER_HOST_CONNECTION_ID);
    c.expect(v == 0xA5A5A5A5, "target still %s (no corrupted write executed)", hex(v).c_str());
    c.expect(is(c.readRaw(Reg::STANDARD, 4), Ack::READ_OK), "valid command after the errors answered normally");
}
CXP_CHECK("CXP-CAM-NEG-001", neg001);

}  // namespace

}  // namespace cxp::validation::checks::neg
