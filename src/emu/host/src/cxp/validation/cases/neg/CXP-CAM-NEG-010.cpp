// CXP-CAM-NEG-010.  See cases/_common.h.

#include "cxp/validation/cases/neg/_helpers.h"

namespace cxp::validation::checks::neg {

namespace {

void neg010(Context& c) {
    c.preserve(Reg::MASTER_HOST_CONNECTION_ID);
    const int collect = c.iparam("collect_ms");
    auto acks = c.exchangeMany({readCmd(Reg::STANDARD, 4), readCmd(Reg::REVISION, 4)}, 3, collect);
    c.expect(acks.size() <= 2, "two back-to-back reads drew %zu final acknowledgments (<= 2)", acks.size());
    c.note("overlapping reads: %zu answered", acks.size());
    const Words w = writeCmd(Reg::MASTER_HOST_CONNECTION_ID, {0x0000C0DE});
    acks = c.exchangeMany({w, w}, 3, collect);
    c.expect(acks.size() <= 2, "duplicate write drew %zu final acknowledgments (<= 2)", acks.size());
    c.expect(c.rd32(Reg::MASTER_HOST_CONNECTION_ID) == 0x0000C0DE, "duplicate write left the value written once");
    c.expect(is(c.readRaw(Reg::STANDARD, 4), Ack::READ_OK), "control channel responsive afterwards");
}
CXP_CHECK("CXP-CAM-NEG-010", neg010);

}  // namespace

}  // namespace cxp::validation::checks::neg
