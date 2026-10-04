// CXP-CAM-TRIG-004b.  See cases/_common.h.
//
// §8.3.3 from the device's side when the host does not keep up: after a
// trigger packet no new one until the host's acknowledgment or the device's
// transmission timeout; after the timeout the device may resend or send a
// new one.  Edges faster than that round trip may merge, but the host must
// end at the trigger's level.

#include "cxp/validation/cases/trig/_device_trig.h"

namespace cxp::validation::checks::trig {

namespace {

// n edges on TRIG_IN from the de-asserted level, gap_ms apart (host time,
// far below the device's round trip of a host acknowledgment); with `ack`
// the host acknowledges each trigger packet as soon as it sees it.
void burst(Context& c, int n, bool ack, const char* what) {
    const uint64_t timeout_ps = uint64_t(c.iparam("timeout_ns")) * 1000;
    const double since = c.nowMs();
    uint32_t level = 0;
    size_t acked = 0;
    for (int i = 0; i < n; ++i) {
        level ^= 1u;
        // The edges edge_gap_us of the device's time apart (faster than a
        // host acknowledgment's round trip); without bench times, 2 ms apart.
        const uint64_t t_set = devtrig::setPin(c, level);
        if (t_set) {
            const uint64_t until = t_set + uint64_t(c.iparam("edge_gap_us")) * 1000000;
            for (auto bt = c.benchTime(); bt && bt->now_ps < until; bt = c.benchTime()) c.sleepRawMs(1);
        } else {
            c.sleepRawMs(2);
        }
        if (ack) {
            for (size_t k = devtrig::trigs(c, since).size(); acked < k; ++acked) c.sendChars(ioAck());
        }
    }
    // Let every timeout run out and every acknowledgment arrive.
    const double end = c.nowMs() + c.wait(c.iparam("settle_ms"));
    while (c.nowMs() < end) {
        if (ack) {
            for (size_t k = devtrig::trigs(c, since).size(); acked < k; ++acked) c.sendChars(ioAck());
        }
        c.sleepRawMs(5);
    }
    c.benchTime();  // the bench events before it are in
    const auto t = devtrig::trigs(c, since);
    size_t bad = 0;
    for (const auto& s : t) bad += !(s.pkt.clean && s.pkt.value >= 0 && s.pkt.value <= 3);
    const char host = t.empty() ? 'F' : devtrig::kinds(t).back();
    const char pin = level ? 'R' : 'F';
    c.expect(bad == 0 && host == pin,
             "%s%d edges, host %s: trigger packets %s, the host ends %s, the input is %s (%zu malformed)", what, n,
             ack ? "acknowledging" : "silent", devtrig::kinds(t).c_str(), host == 'R' ? "asserted" : "de-asserted",
             pin == 'R' ? "asserted" : "de-asserted", bad);
    if (devtrig::timed(c)) {
        // Every host acknowledgment of the case by its bench time: one sent at
        // the host's time before the burst may reach the device during it.
        const auto acks = devtrig::hostAckMarks(c, 0);
        const size_t v = devtrig::judgePacing(c, t, acks, timeout_ps, what);
        std::string gaps;
        for (size_t i = 1; i < t.size() && i < 9; ++i) {
            gaps += strprintf("%s%.2f", gaps.empty() ? "" : " ", (t[i].device_ps - t[i - 1].device_ps) / 1e6);
        }
        c.expect(v == 0, "%s%d edges: every trigger after an acknowledgment or the %.2f us timeout (gaps us: %s)", what,
                 n, timeout_ps / 1e6, gaps.empty() ? "-" : gaps.c_str());
    }
    // Leave the input de-asserted; a silent host leaves the device to its
    // timeout, an acknowledging one answers the last trigger.
    if (level) {
        c.benchSend({bench::PIN, bench::TRIG_IN, 0});
        devtrig::waitTrigs(c, t.size() + 1, since, 1000);
        if (ack) c.sendChars(ioAck());
    }
    c.sleepMs(50);
}

void trig004b(Context& c) {
    devtrig::cleanStart(c, 0);
    if (!devtrig::timed(c)) c.note("no bench times: the §8.3.3 pacing is not judged, only the packets and the level");
    for (int64_t n : c.ilist("bursts")) burst(c, int(n), false, "no acknowledgment: ");
    for (int64_t n : c.ilist("bursts")) burst(c, int(n), true, "late acknowledgments: ");
}
CXP_CHECK("CXP-CAM-TRIG-004b", trig004b);

}  // namespace

}  // namespace cxp::validation::checks::trig
