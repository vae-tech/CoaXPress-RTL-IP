// CXP-CAM-TRIG-006.  See cases/_common.h.
//
// I/O acknowledgment latency against the Host's low-speed trigger timeout
// (§8.3.3: one low-speed character), under a full stream: the time from the
// end of each Table 15 trigger on the uplink to the start of its Table 17
// acknowledgment on the downlink, both from the bench's clock.

#include "cxp/validation/cases/trig/_host_trig.h"

namespace cxp::validation::checks::trig {

namespace {

using namespace htrig;

void trig006(Context& c) {
    c.needBench(bench::CAP_CHARS | bench::CAP_TIMES, "time Table 15 triggers and their acknowledgments");
    uvm::uvmIdleConfig(c);
    uvm::runTestPattern(c);
    settleLow(c);
    std::mt19937 rng(c.seed(6));
    const int n = c.iparam("triggers"), gap = c.iparam("max_gap_ms");
    double since = 0;
    const auto cap = uvm::streamAround(c, 1, [&] {
        since = c.nowMs();
        for (int i = 0; i < n; ++i) {
            c.sendChars(lsTrigger(i % 2 == 0, uint8_t(rng() % 240)));
            c.waitShort(ShortPacket::Kind::IoAck, size_t(i) + 1, since, 1000);
            c.sleepRawMs(int(rng() % uint32_t(gap + 1)));
        }
    });
    c.benchSync();
    std::vector<TimedShort> acks;
    for (const auto& s : c.shortPackets(since)) {
        if (s.pkt.kind == ShortPacket::Kind::IoAck) acks.push_back(s);
    }
    std::vector<UplinkMark> leads;
    for (const auto& m : c.uplinkMarks(since)) {
        if (isLeaderMark(m)) leads.push_back(m);
    }
    c.expect(acks.size() == size_t(n) && leads.size() == size_t(n),
             "%d triggers under the stream: %zu leaders timed on the uplink, %zu acknowledgments", n, leads.size(),
             acks.size());
    const size_t m = std::min(acks.size(), leads.size());
    if (m == 0) return;
    std::vector<double> lat;
    size_t inside = 0, untimed = 0;
    double limit = 0;
    for (size_t i = 0; i < m; ++i) {
        if (acks[i].inside < 0) {
            ++untimed;
            continue;
        }
        const double end_ns = (double(leads[i].time_ps) + 60.0 * leads[i].bit_ps) / 1000.0;
        lat.push_back(double(acks[i].device_ps) / 1000.0 - end_ns);
        inside += acks[i].inside == 1;
        limit = 10.0 * leads[i].bit_ps / 1000.0 + c.iparam("tx_word_ns");
    }
    c.expect(untimed == 0, "%zu acknowledgments without a bench time", untimed);
    if (lat.empty()) return;
    std::vector<double> sorted = lat;
    std::sort(sorted.begin(), sorted.end());
    const double mean = std::accumulate(lat.begin(), lat.end(), 0.0) / double(lat.size());
    c.info("latency trigger end -> acknowledgment start: min %.1f ns, median %.1f, mean %.1f, max %.1f; %zu of %zu "
           "acknowledgments inserted into a packet", sorted.front(), sorted[sorted.size() / 2], mean, sorted.back(),
           inside, lat.size());
    c.expect(sorted.front() >= 0.0, "no acknowledgment before its trigger ended (min %.1f ns)", sorted.front());
    c.expect(sorted.back() <= limit, "max latency %.1f ns <= one low-speed character + one high-speed word (%.1f ns)",
             sorted.back(), limit);
    const auto sb = uvm::streamScoreboard(c, streamPackets(cap), -1, 0, {}, "stream: ");
    c.expect(sb.packets > 0 && inside > 0, "the triggers met the stream: %zu acknowledgments inside packets", inside);
    settleLow(c);
}
CXP_CHECK("CXP-CAM-TRIG-006", trig006);

}  // namespace

}  // namespace cxp::validation::checks::trig
