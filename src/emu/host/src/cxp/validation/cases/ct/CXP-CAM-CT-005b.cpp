// CXP-CAM-CT-005b.  See cases/_common.h.
//
// The counters are cleared in the middle of a burst of host test packets.
// On one low-speed upconnection a command can only sit between two test
// packets (it is not a trigger, §8.2.4), so the clear falls exactly between
// packet `clear_after` and the next: one character stream carries the burst,
// the two clearing writes and the rest of the burst, and the counters must
// equal what came after the clear.

#include "cxp/validation/cases/ct/_helpers.h"

namespace cxp::validation::checks::ct {

namespace {

void ct005b(Context& c) {
    c.needBench(bench::CAP_CHARS, "send a burst with commands between its packets");
    preserveLink(c);
    resetTestCounters(c);
    const int n = c.iparam("test_packets"), k = c.iparam("clear_after"), bad = c.iparam("bad_words");
    if (k < 1 || k >= n) c.abort("clear_after must lie inside the burst");
    // Every packet carries `bad` corrupted words, so the error counter moves
    // with the packet counter and a missed clear of either shows.
    Chars burst;
    for (int i = 0; i < k; ++i) appendFrame(burst, hostTestPacket(bad));
    appendFrame(burst, writeCmd(Reg::TEST_ERROR_COUNT, {0}));
    appendFrame(burst, writeCmd(Reg::TEST_PACKET_COUNT_RX, {0, 0}));
    for (int i = k; i < n; ++i) appendFrame(burst, hostTestPacket(bad));
    c.info("%d test packets with %d corrupted words each; TestErrorCount and TestPacketCountRx written 0 after "
           "packet %d", n, bad, k);
    const double t0 = c.nowMs();
    const auto acks = c.exchangeChars(burst, 2, c.iparam("host_wait_ms"));
    std::string codes;
    for (const auto& a : acks) codes += (codes.empty() ? "" : ", ") + ackName(a.code);
    c.expect(acks.size() == 2 && acks[0].code == Ack::WRITE_OK && acks[1].code == Ack::WRITE_OK,
             "both clearing writes inside the burst answered 0x01 (%s)", codes.empty() ? "none" : codes.c_str());
    const auto t = readTestCounters(c, t0, size_t(n + 2), c.iparam("host_wait_ms"));
    if (!t.ok) return;
    const uint64_t want_rx = uint64_t(n - k);
    const uint32_t want_err = uint32_t((n - k) * bad);
    c.expect(t.rx == want_rx, "TestPacketCountRx %llu = the %llu packets after the clear", (unsigned long long)t.rx,
             (unsigned long long)want_rx);
    c.expect(t.err == want_err, "TestErrorCount %u = the %u corrupted words after the clear", t.err, want_err);
    // Neither clear touched the other counter's later counting: a second
    // burst adds on top.
    Chars more;
    for (int i = 0; i < c.iparam("after_packets"); ++i) appendFrame(more, hostTestPacket(0));
    const double t1 = c.nowMs();
    c.sendChars(more);
    const auto t2 = readTestCounters(c, t1, size_t(c.iparam("after_packets")), c.iparam("host_wait_ms"));
    if (!t2.ok) return;
    c.expect(t2.rx == want_rx + uint64_t(c.iparam("after_packets")) && t2.err == want_err,
             "%d clean packets more: TestPacketCountRx %llu, TestErrorCount %u (%llu, %u)", c.iparam("after_packets"),
             (unsigned long long)t2.rx, t2.err, (unsigned long long)(want_rx + uint64_t(c.iparam("after_packets"))),
             want_err);
}
CXP_CHECK("CXP-CAM-CT-005b", ct005b);

}  // namespace

}  // namespace cxp::validation::checks::ct
