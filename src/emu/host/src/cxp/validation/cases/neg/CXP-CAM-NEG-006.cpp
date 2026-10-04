// CXP-CAM-NEG-006.  See cases/_common.h.

#include "cxp/validation/cases/neg/_helpers.h"

namespace cxp::validation::checks::neg {

namespace {

void neg006(Context& c) {
    c.preserve(Reg::MASTER_HOST_CONNECTION_ID);
    c.wr32(Reg::MASTER_HOST_CONNECTION_ID, 0x0BADF00D);
    const uint32_t cpsm = usableCpsm(c);
    auto a = c.readRaw(Reg::DEVICE_VENDOR_NAME, cpsm);
    c.expect(is(a, Ack::SIZE_TOO_LARGE), "read of %u bytes (ack would exceed CPSM): %s", cpsm, ackStr(a).c_str());
    struct Case { uint32_t size; size_t words; };
    for (const Case& k : {Case{8, 1}, Case{8, 3}, Case{4, 2}}) {
        CmdSpec s;
        s.opcode = 0x01;
        s.size_bytes = k.size;
        s.address = Reg::MASTER_HOST_CONNECTION_ID;
        s.data.assign(k.words, bswap32(0x11111111));
        a = c.exchange(buildCmd(s));
        c.expect(is(a, Ack::SIZE_MISMATCH), "write Size = %u with %zu data words: %s (0x46)", k.size, k.words,
                 ackStr(a).c_str());
    }
    c.expect(c.rd32(Reg::MASTER_HOST_CONNECTION_ID) == 0x0BADF00D, "target unchanged by the inconsistent writes");
}
CXP_CHECK("CXP-CAM-NEG-006", neg006);

}  // namespace

}  // namespace cxp::validation::checks::neg
