// CXP-CAM-NEG-007.  See cases/_common.h.

#include "cxp/validation/cases/neg/_helpers.h"

namespace cxp::validation::checks::neg {

namespace {

void neg007(Context& c) {
    c.preserve(Reg::MASTER_HOST_CONNECTION_ID);
    const uint32_t mh = c.rd32(Reg::MASTER_HOST_CONNECTION_ID);
    auto recovered = [&](const char* what, const std::vector<RawAck>& acks) {
        std::string codes;
        for (const auto& a : acks) codes += " " + ackName(a.code);
        c.note("%s: acknowledgments:%s", what, codes.empty() ? " none" : codes.c_str());
        auto v = c.readRaw(Reg::STANDARD, 4);
        c.expect(is(v, Ack::READ_OK) && v->values()[0] == CXP_MAGIC, "valid read after %s: %s", what, ackStr(v).c_str());
    };
    Words no_eop = readCmd(Reg::STANDARD, 4);
    no_eop.pop_back();
    auto acks = c.exchangeMany({no_eop, readCmd(Reg::REVISION, 4)}, 2, 500);
    c.expect(std::any_of(acks.begin(), acks.end(), [](const RawAck& a) { return a.code == Ack::READ_OK; }),
             "read following a packet without EOP answered");
    recovered("missing EOP", acks);
    Words trunc = writeCmd(Reg::MASTER_HOST_CONNECTION_ID, {0xDEAD0001});
    trunc.resize(10);
    acks = c.exchangeMany({trunc}, 1, c.iparam("idle_ms"));
    recovered("packet truncated after the address, then an idle wait", acks);
    CmdSpec s;
    s.opcode = 0x00;
    s.size_bytes = 4;
    s.address = Reg::STANDARD;
    Words two_bad = buildCmd(s);
    two_bad[1] ^= 0x00005A5Au;  // lanes P0 and P1
    acks = c.exchangeMany({two_bad}, 1, 300);
    recovered("type word with 2 of 4 characters corrupted", acks);
    acks = c.exchangeMany({readCmd(Reg::STANDARD, 0)}, 1, 300);
    recovered("Size = 0 read", acks);
    CmdSpec w0;
    w0.opcode = 0x01;
    w0.size_bytes = 0;
    w0.address = Reg::MASTER_HOST_CONNECTION_ID;
    acks = c.exchangeMany({buildCmd(w0)}, 1, 300);
    recovered("Size = 0 write", acks);
    CmdSpec r1;
    r1.opcode = 0xFF;
    r1.size_bytes = 4;
    acks = c.exchangeMany({buildCmd(r1)}, 1, 300);
    recovered("control channel reset with Size = 4", acks);
    CmdSpec r2;
    r2.opcode = 0xFF;
    r2.size_bytes = 0;
    r2.address = 0x10;
    acks = c.exchangeMany({buildCmd(r2)}, 1, 300);
    recovered("control channel reset with Addr = 0x10", acks);
    c.expect(c.rd32(Reg::MASTER_HOST_CONNECTION_ID) == mh, "no spurious write executed (MasterHostConnectionID unchanged)");
}
CXP_CHECK("CXP-CAM-NEG-007", neg007);

}  // namespace

}  // namespace cxp::validation::checks::neg
