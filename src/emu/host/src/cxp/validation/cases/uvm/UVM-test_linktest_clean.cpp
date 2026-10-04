// UVM-test_linktest_clean.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void linktestClean(Context& c) {
    uvmIdleConfig(c);
    // §6.7: each end may be 100 ppm off the nominal low-speed bit rate, so
    // host and device can differ by 200 ppm; the packets go up at that
    // limit, as the UVM test's host does.
    const int ppm = c.iparam("uplink_ppm");
    const bool off = ppm != 0 && (c.benchCaps() & bench::CAP_UPLINK_PPM);
    if (off) {
        c.benchSend({bench::UPLINK_PPM, uint32_t(int32_t(ppm))});
        c.benchSync();
        c.info("host bit rate %+d ppm from nominal", ppm);
    } else if (ppm != 0) {
        c.note("no bench bit-rate control: the host runs at nominal");
    }
    resetTestCounters(c);
    const int n = c.iparam("test_packets");
    c.sendOnly(std::vector<Words>(size_t(n), hostTestPacket(0)), 100);
    expectCounters(c, uint64_t(n), 0);
    if (off) {
        c.benchSend({bench::UPLINK_PPM, 0});
        c.benchSync();
    }
}
CXP_CHECK("UVM-test_linktest_clean", linktestClean);

}  // namespace

}  // namespace cxp::validation::checks::uvm
