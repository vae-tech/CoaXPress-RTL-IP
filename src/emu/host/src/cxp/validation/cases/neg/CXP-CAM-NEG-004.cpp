// CXP-CAM-NEG-004.  See cases/_common.h.

#include "cxp/validation/cases/neg/_helpers.h"

namespace cxp::validation::checks::neg {

namespace {

void neg004(Context& c) {
    size_t good = 0, n = 0;
    for (int op = 0x02; op <= 0xFE; ++op) {
        CmdSpec s;
        s.opcode = uint8_t(op);
        s.size_bytes = 4;
        s.address = 0;
        auto a = c.exchange(buildCmd(s));
        auto v = c.readRaw(Reg::STANDARD, 4);
        ++n;
        const bool ok = is(a, Ack::BAD_OPCODE) && is(v, Ack::READ_OK);
        good += ok;
        if (!ok && n - good <= size_t(c.opt().max_reported)) {
            c.expect(false, "Cmd 0x%02X: %s, following read %s", op, ackStr(a).c_str(), ackStr(v).c_str());
        }
        if (good == 0 && n == size_t(c.iparam("give_up_after"))) {
            c.note("the first %zu reserved codes all failed; the remaining %d are not tried", n, 0xFE - op);
            break;
        }
    }
    c.expect(good == n && n == 253, "%zu of 253 reserved operation codes answered 0x42 and discarded", good);
}
CXP_CHECK("CXP-CAM-NEG-004", neg004);

}  // namespace

}  // namespace cxp::validation::checks::neg
