// CXP-CAM-CT-004.  See cases/_common.h.

#include "cxp/validation/cases/ct/_helpers.h"

namespace cxp::validation::checks::ct {

namespace {

void ct004(Context& c) {
    c.prepareStreaming();
    resetTestCounters(c);
    const size_t h0 = c.headersSeen();
    c.acqStart();
    c.waitHeaders(h0 + 1, c.opt().first_image_timeout_ms);
    const int clean = c.iparam("test_packets.clean");
    const int bad1 = c.iparam("test_packets.one_bad_word"), bad2 = c.iparam("test_packets.two_bad_words");
    sendTestPackets(c, clean, 0);
    uint32_t err = c.rd32(Reg::TEST_ERROR_COUNT);
    uint64_t rx = c.rd64(Reg::TEST_PACKET_COUNT_RX);
    c.expect(rx == uint64_t(clean) && err == 0,
             "%d clean packets while streaming: TestPacketCountRx %llu, TestErrorCount %u", clean,
             (unsigned long long)rx, err);
    sendTestPackets(c, bad1, 1);
    sendTestPackets(c, bad2, 2);
    err = c.rd32(Reg::TEST_ERROR_COUNT);
    rx = c.rd64(Reg::TEST_PACKET_COUNT_RX);
    const uint64_t want_rx = uint64_t(clean + bad1 + bad2);
    const uint32_t want_err = uint32_t(bad1 + 2 * bad2);
    c.expect(rx == want_rx, "TestPacketCountRx = %llu after %d more packets (%llu)", (unsigned long long)rx, bad1 + bad2,
             (unsigned long long)want_rx);
    c.expect(err == want_err, "TestErrorCount = %u after %d x 1 and %d x 2 corrupted words (%u)", err, bad1, bad2,
             want_err);
    c.expect(streamContinues(c, 200), "stream still running after the test traffic");
}
CXP_CHECK("CXP-CAM-CT-004", ct004);

}  // namespace

}  // namespace cxp::validation::checks::ct
