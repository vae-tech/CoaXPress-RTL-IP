// UVM-test_pslverr_burst.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void pslverrBurst(Context& c) {
    c.needBench(bench::CAP_REG_ERR, "make the register bus answer every access with an error");
    uvmIdleConfig(c);
    preserveLink(c);
    c.preserve(Reg::TEST_ERROR_COUNT_SELECTOR);
    RegModel m = readModel(c);
    c.benchSend({bench::REG_ERR, Ack::BAD_ADDRESS});
    c.onExit([&c] {
        c.benchSend({bench::REG_ERR, 0});
        c.benchSync();
    });
    c.benchSync();
    std::mt19937 rng(c.seed(1));
    size_t good = 0;
    const int n = c.iparam("writes");
    for (int i = 0; i < n; ++i) {
        const uint32_t addr = kUvmRw[rng() % 4];
        const uint32_t v = legalValue(c, addr, rng, m);
        auto a = c.writeRaw(addr, {v});
        good += is(a, Ack::BAD_ADDRESS);
        c.expect(is(a, Ack::BAD_ADDRESS), "write %s = %s on a faulting bus: %s (0x40)", regName(addr), hex(v).c_str(),
                 ackStr(a).c_str());
    }
    c.expect(good == size_t(n), "%zu of %d writes on a faulting bus answered 0x40", good, n);
    c.benchSend({bench::REG_ERR, 0});
    c.benchSync();
    auto r = c.readRaw(Reg::STANDARD, 4);
    c.expect(readsStandard(r), "bus fault removed: read of Standard %s", ackStr(r).c_str());
}
CXP_CHECK("UVM-test_pslverr_burst", pslverrBurst);

}  // namespace

}  // namespace cxp::validation::checks::uvm
