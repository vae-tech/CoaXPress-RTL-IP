// CXP-CAM-CT-005.  See cases/_common.h.

#include "cxp/validation/cases/ct/_helpers.h"

namespace cxp::validation::checks::ct {

namespace {

void ct005(Context& c) {
    preserveLink(c);
    resetTestCounters(c);
    const int first = c.iparam("test_packets.first");
    sendTestPackets(c, first, 1);
    c.expect(c.rd32(Reg::TEST_ERROR_COUNT_SELECTOR) == 0, "TestErrorCountSelector reads 0");
    const uint32_t err = c.rd32(Reg::TEST_ERROR_COUNT);
    const uint64_t rx = c.rd64(Reg::TEST_PACKET_COUNT_RX);
    c.expect(err == uint32_t(first) && rx == uint64_t(first),
             "selector 0: TestErrorCount %u, TestPacketCountRx %llu (%d, %d)", err, (unsigned long long)rx, first, first);
    c.wr32(Reg::TEST_ERROR_COUNT, 0);
    const uint32_t err2 = c.rd32(Reg::TEST_ERROR_COUNT);
    const uint64_t rx2 = c.rd64(Reg::TEST_PACKET_COUNT_RX);
    c.expect(err2 == 0 && rx2 == uint64_t(first), "writing 0 to TestErrorCount clears only it: %u, TestPacketCountRx %llu",
             err2, (unsigned long long)rx2);
    const bool hsup = (c.rd32(Reg::HS_UPCONNECTION) & 1) != 0;
    for (uint32_t bad : {hsup ? 2u : 1u, 2u}) {
        auto w = c.writeRaw(Reg::TEST_ERROR_COUNT_SELECTOR, {bad});
        const uint32_t sel = c.rd32(Reg::TEST_ERROR_COUNT_SELECTOR);
        c.expect(w && w->code != Ack::WRITE_OK && sel == 0, "out-of-range selector %u: %s, selector reads %u", bad,
                 ackStr(w).c_str(), sel);
        if (sel != 0) c.writeRaw(Reg::TEST_ERROR_COUNT_SELECTOR, {0});
    }
    sendTestPackets(c, c.iparam("test_packets.before_reset"), 1);
    connectionReset(c);
    const uint32_t e3 = c.rd32(Reg::TEST_ERROR_COUNT);
    const uint64_t rx3 = c.rd64(Reg::TEST_PACKET_COUNT_RX), tx3 = c.rd64(Reg::TEST_PACKET_COUNT_TX);
    const uint32_t sel3 = c.rd32(Reg::TEST_ERROR_COUNT_SELECTOR);
    c.expect(e3 == 0 && rx3 == 0 && tx3 == 0 && sel3 == 0,
             "after ConnectionReset: TestErrorCount %u, Rx %llu, Tx %llu, selector %u (all 0)", e3,
             (unsigned long long)rx3, (unsigned long long)tx3, sel3);
}
CXP_CHECK("CXP-CAM-CT-005", ct005);

}  // namespace

}  // namespace cxp::validation::checks::ct
