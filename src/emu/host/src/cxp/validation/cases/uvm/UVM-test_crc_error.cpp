// UVM-test_crc_error.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void crcError(Context& c) {
    uvmIdleConfig(c);
    size_t nack = 0;
    const int n = c.iparam("reads");
    for (int i = 0; i < n; ++i) {
        CmdSpec s;
        s.opcode = 0x00;
        s.size_bytes = 4;
        s.address = Reg::STANDARD;
        s.corrupt_crc = true;
        auto a = c.exchange(buildCmd(s));
        nack += is(a, Ack::CRC_ERROR);
        if (!is(a, Ack::CRC_ERROR)) c.expect(false, "read %d with a corrupted CRC: %s", i + 1, ackStr(a).c_str());
    }
    c.expect(nack == size_t(n), "%zu of %d reads with a corrupted CRC answered 0x80", nack, n);
    auto r = c.readRaw(Reg::STANDARD, 4);
    c.expect(readsStandard(r), "plain read of Standard afterwards: %s", ackStr(r).c_str());
}
CXP_CHECK("UVM-test_crc_error", crcError);

}  // namespace

}  // namespace cxp::validation::checks::uvm
