// CXP-EMU-NEG-102.  See cases/_common.h.
//
// The Size field is 24 bits (Table 21).  A read of B = 0xFFFFFC .. 0xFFFFFF
// bytes needs an acknowledgment of ceil(B / 4) + 6 words; a receiver that
// computes N = ceil(B / 4) in 24 bits wraps 0xFFFFFD .. 0xFFFFFF to 0 and
// would answer data.  Each is 0x45 (Table 22: the acknowledgment would
// exceed the packet size limit, §8.6.4).

#include "cxp/validation/cases/neg/_helpers.h"

namespace cxp::validation::checks::neg {

namespace {

void neg102(Context& c) {
    c.preserve(Reg::MASTER_HOST_CONNECTION_ID);
    const uint32_t sentinel = uint32_t(c.iparam("sentinel"));
    c.wr32(Reg::MASTER_HOST_CONNECTION_ID, sentinel);
    usableCpsm(c);
    for (int64_t size : c.ilist("sizes")) {
        for (uint32_t addr : {Reg::STANDARD, Reg::MASTER_HOST_CONNECTION_ID}) {
            auto a = c.readRaw(addr, uint32_t(size));
            c.expect(is(a, Ack::SIZE_TOO_LARGE) && !a->long_form && a->data.empty(),
                     "read of 0x%06X bytes at 0x%04X: %s (0x45, no data)", uint32_t(size), addr, ackStr(a).c_str());
            auto v = c.readRaw(Reg::STANDARD, 4);
            c.expect(is(v, Ack::READ_OK) && v->values().size() == 1 && v->values()[0] == CXP_MAGIC,
                     "  next read of Standard answered: %s", ackStr(v).c_str());
        }
        // A write that declares the same Size and carries one word: the command
        // message would exceed the limit (0x45) or is inconsistent with its
        // Size (0x46); either way nothing is written.
        CmdSpec w;
        w.opcode = 0x01;
        w.size_bytes = uint32_t(size);
        w.address = Reg::MASTER_HOST_CONNECTION_ID;
        w.data = {bswap32(0x0BAD0000u | uint32_t(size & 0xFF))};
        auto a = c.exchange(buildCmd(w));
        c.expect(is(a, Ack::SIZE_TOO_LARGE) || is(a, Ack::SIZE_MISMATCH),
                 "write with Size 0x%06X and one data word: %s (0x45 or 0x46)", uint32_t(size), ackStr(a).c_str());
    }
    c.expect(c.rd32(Reg::MASTER_HOST_CONNECTION_ID) == sentinel,
             "MasterHostConnectionID still 0x%08X: no oversize write executed", sentinel);
}
CXP_CHECK("CXP-EMU-NEG-102", neg102);

}  // namespace

}  // namespace cxp::validation::checks::neg
