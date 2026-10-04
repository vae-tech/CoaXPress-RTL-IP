// UVM-test_byte_replication_robust.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void byteReplicationRobust(Context& c) {
    uvmIdleConfig(c);
    std::mt19937 rng(c.seed(4));
    size_t good = 0;
    const int n = c.iparam("reads");
    for (int i = 0; i < n; ++i) {
        const int lane = int(rng() % 4);
        Words f = readCmd(Reg::STANDARD, 4);
        f[1] ^= 1u << (8 * lane);  // bit 0 of one TYPE replica
        auto a = c.exchange(f);
        good += readsStandard(a);
        c.expect(readsStandard(a), "read of Standard, TYPE replica P%d bit 0 flipped: %s", lane, ackStr(a).c_str());
    }
    c.expect(good == size_t(n), "%zu of %d reads with one bit flipped in one TYPE replica answered correctly", good, n);
    auto r = c.readRaw(Reg::STANDARD, 4);
    c.expect(readsStandard(r), "plain read of Standard afterwards: %s", ackStr(r).c_str());
}
CXP_CHECK("UVM-test_byte_replication_robust", byteReplicationRobust);

}  // namespace

}  // namespace cxp::validation::checks::uvm
