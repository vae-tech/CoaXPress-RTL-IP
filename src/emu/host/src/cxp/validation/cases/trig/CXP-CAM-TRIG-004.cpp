// CXP-CAM-TRIG-004.  See cases/_common.h.
//
// Device -> host triggers while the device streams: Table 16 format and
// Delay, insertion into the packet being sent (§8.2.4: a trigger has the
// highest priority and is inserted at the next word boundary), and the
// §8.3.3 rule with a host that acknowledges every trigger at once.

#include "cxp/validation/cases/trig/_device_trig.h"

namespace cxp::validation::checks::trig {

namespace {

void trig004(Context& c) {
    const double t_start = devtrig::cleanStart(c, 0);
    (void)t_start;
    const bool timed = devtrig::timed(c);
    const uint64_t timeout_ps = uint64_t(c.iparam("timeout_ns")) * 1000;
    const int edges = c.iparam("edges");
    uvm::runTestPattern(c);
    c.prepareStreaming();
    std::mt19937 rng(c.seed(4));
    const auto gap = c.ilist("gap_range_ms");
    double since = 0;
    std::vector<uint64_t> set_ps;  // bench time each level took effect
    auto cap = uvm::streamAround(c, 1, [&] {
        since = c.nowMs();
        uint32_t level = 0;
        for (int i = 0; i < edges; ++i) {
            c.sleepRawMs(std::uniform_int_distribution<int>(int(gap[0]), int(gap[1]))(rng));
            level ^= 1u;
            set_ps.push_back(devtrig::setPin(c, level));
            devtrig::waitTrigs(c, size_t(i) + 1, since, 2000);
            c.sendChars(ioAck());  // a host acknowledges each trigger at once
        }
        c.sleepMs(50);
    });
    c.benchTime();
    const auto t = devtrig::trigs(c, since);
    std::string want;
    for (int i = 0; i < edges; ++i) want += i % 2 ? 'F' : 'R';
    c.expect(devtrig::kinds(t) == want, "%d edges at random phases while streaming: trigger packets %s (%s)", edges,
             devtrig::kinds(t).c_str(), want.c_str());
    size_t bad = 0, dly0 = 0;
    for (const auto& s : t) {
        bad += !(s.pkt.clean && s.pkt.value >= 0 && s.pkt.value <= 3);
        dly0 += s.pkt.value == 0;
    }
    c.expect(!t.empty() && bad == 0,
             "%zu of %zu packets 4 x K28.4 / 4 x K28.2 + 4 x Delay, Delay 0..3 (3 - whole characters, or 0 unused)",
             t.size() - bad, t.size());
    if (dly0 == t.size()) c.note("every Delay is 0: the device does not use the Delay (Table 16 allows it)");
    const auto pk = streamPackets(cap);
    uvm::streamScoreboard(c, pk, -1, 0, {}, "stream around the triggers: ");
    if (!timed) {
        c.note("no bench times: insertion latency and §8.3.3 pacing not judged");
    } else {
        // A trigger whose predecessor's acknowledgment had not yet reached the
        // device when the input changed waits for it (§8.3.3); the others go
        // out at once, inserted into a packet in progress if there is one.
        const auto acks = devtrig::hostAckMarks(c, since);
        const uint64_t decode_ps = uint64_t(c.iparam("ack_decode_ns")) * 1000;
        size_t inside = 0, late = 0, free = 0, gated = 0;
        double worst = 0, worst_gated = 0;
        for (size_t i = 0; i < t.size() && i < set_ps.size(); ++i) {
            if (!t[i].device_ps || !set_ps[i]) continue;
            inside += t[i].inside == 1;
            uint64_t ack_end = 0;
            if (i > 0) {
                for (const auto& m : acks) {
                    if (m.time_ps > t[i - 1].device_ps) {
                        ack_end = m.time_ps + uint64_t(80) * m.bit_ps;
                        break;
                    }
                }
            }
            const double ns = (double(t[i].device_ps) - double(set_ps[i])) / 1e3;
            if (ack_end + decode_ps > set_ps[i]) {
                ++gated;
                worst_gated = std::max(worst_gated, (double(t[i].device_ps) - double(ack_end)) / 1e3);
                continue;
            }
            ++free;
            worst = std::max(worst, ns);
            late += ns > c.iparam("max_latency_ns");
        }
        c.expect(free > 0 && late == 0,
                 "%zu triggers whose predecessor was acknowledged: each on the wire within %d ns of the input's change "
                 "(worst %.0f ns)",
                 free, c.iparam("max_latency_ns"), worst);
        if (gated) {
            c.info("%zu triggers waited for the previous one's acknowledgment: at most %.0f ns after its last bit on "
                   "the uplink",
                   gated, worst_gated);
        }
        std::string w;
        for (const auto& s : t) {
            if (s.inside == 1 && w.size() < 40) w += strprintf("%s%d", w.empty() ? "" : ", ", s.word_index);
        }
        c.expect(inside > 0, "%zu of %zu triggers met a packet in progress and were inserted into it (after word %s)",
                 inside, t.size(), w.empty() ? "-" : w.c_str());
        const size_t v = devtrig::judgePacing(c, t, acks, timeout_ps, "");
        c.expect(v == 0, "no trigger before the host's acknowledgment of the previous one reached the device");
    }
    c.note("§8.3.3 recommends a register for the acknowledgment timeout; the device documents none (its timeout "
           "is a design parameter, %d ns on this bench)",
           c.iparam("timeout_ns"));
}
CXP_CHECK("CXP-CAM-TRIG-004", trig004);

}  // namespace

}  // namespace cxp::validation::checks::trig
