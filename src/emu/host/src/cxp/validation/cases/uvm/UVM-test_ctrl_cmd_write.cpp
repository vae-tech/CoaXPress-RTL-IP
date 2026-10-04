// UVM-test_ctrl_cmd_write.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void ctrlCmdWrite(Context& c) {
    uvmIdleConfig(c);
    preserveLink(c);
    c.preserve(Reg::TEST_ERROR_COUNT_SELECTOR);
    RegModel m = readModel(c);
    std::set<uint32_t> written;
    std::mt19937 rng(c.seed(1));
    size_t good = 0;
    const int n = c.iparam("commands");
    for (int i = 0; i < n; ++i) {
        const uint32_t addr = kUvmRw[rng() % 4];
        const uint32_t v = legalValue(c, addr, rng, m);
        auto a = c.writeRaw(addr, {v});
        const bool ok = is(a, Ack::WRITE_OK) && !a->long_form && a->n_words == 4;
        good += ok;
        c.expect(ok, "write %s = %s: %s%s", regName(addr), hex(v).c_str(), ackStr(a).c_str(),
                 a && a->long_form ? " (long form)" : "");
        if (ok) {
            m[addr] = v;
            written.insert(addr);
        }
    }
    for (uint32_t addr : written) {
        const uint32_t r = c.rd32(addr);
        c.expect(r == m[addr], "%s reads back %s (%s)", regName(addr), hex(r).c_str(), hex(m[addr]).c_str());
    }
    c.expect(good == size_t(n), "%zu of %d writes answered 0x01 in the short form", good, n);
}
CXP_CHECK("UVM-test_ctrl_cmd_write", ctrlCmdWrite);

}  // namespace

}  // namespace cxp::validation::checks::uvm
