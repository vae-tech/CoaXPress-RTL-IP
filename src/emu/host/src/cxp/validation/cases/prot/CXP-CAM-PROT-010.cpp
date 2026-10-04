// CXP-CAM-PROT-010.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::prot {

namespace {

void prot010(Context& c) {
    preserveLink(c);
    size_t single = 0, n = 0;
    const int commands = c.iparam("commands");
    for (int i = 0; i < commands; ++i) {
        const Words cmd = (i % 2) ? readCmd(Reg::STANDARD, 4)
                                  : writeCmd(Reg::MASTER_HOST_CONNECTION_ID, {uint32_t(0x100 + i)});
        auto acks = c.exchangeMany({cmd}, 2, c.iparam("collect_ms"));
        ++n;
        single += acks.size() == 1;
        if (acks.size() != 1) c.expect(false, "command %d drew %zu acknowledgments", i, acks.size());
    }
    c.expect(single == n, "%zu of %zu commands drew exactly one final acknowledgment", single, n);
    auto after_reset = c.exchangeMany({writeCmd(Reg::CONNECTION_RESET, {1})}, 3, c.iparam("reset_collect_ms"));
    c.expect(after_reset.size() <= 1, "ConnectionReset write drew %zu acknowledgments (at most one)",
             after_reset.size());
    auto idle = framesOfType(c.record(c.iparam("silent_ms")), 0x03);
    c.expect(idle.empty(), "%zu unsolicited acknowledgments in %d ms without commands", idle.size(),
             c.wait(c.iparam("silent_ms")));
}
CXP_CHECK("CXP-CAM-PROT-010", prot010);

}  // namespace

}  // namespace cxp::validation::checks::prot
