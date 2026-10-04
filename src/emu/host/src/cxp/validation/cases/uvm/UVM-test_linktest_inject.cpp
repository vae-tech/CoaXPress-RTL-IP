// UVM-test_linktest_inject.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void linktestInject(Context& c) {
    uvmIdleConfig(c);
    resetTestCounters(c);
    const int clean = c.iparam("test_packets.clean"), bad = c.iparam("test_packets.corrupted");
    const int words = c.iparam("bad_words");
    std::vector<Words> pk(size_t(clean), hostTestPacket(0));
    for (int i = 0; i < bad; ++i) pk.push_back(hostTestPacket(words));
    c.sendOnly(pk, 100);
    expectCounters(c, uint64_t(clean + bad), uint32_t(bad * words));
}
CXP_CHECK("UVM-test_linktest_inject", linktestInject);

}  // namespace

}  // namespace cxp::validation::checks::uvm
