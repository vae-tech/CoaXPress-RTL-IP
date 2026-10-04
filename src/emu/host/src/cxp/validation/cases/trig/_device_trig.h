// Device -> host triggers (§8.3.2.2 Table 16, §8.3.3): what the cases of the
// device trigger source share.  Header only; nothing here reads a parameter.
#pragma once

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::devtrig {

inline bool isTrig(const TimedShort& s) {
    return s.pkt.kind == ShortPacket::Kind::TriggerRise || s.pkt.kind == ShortPacket::Kind::TriggerFall;
}

// The device's trigger packets (Table 16) since `since`.
inline std::vector<TimedShort> trigs(Context& c, double since) {
    std::vector<TimedShort> out;
    for (const auto& s : c.shortPackets(since)) {
        if (isTrig(s)) out.push_back(s);
    }
    return out;
}

// The device's I/O acknowledgments (Table 17) since `since`.
inline std::vector<TimedShort> ioAcks(Context& c, double since) {
    std::vector<TimedShort> out;
    for (const auto& s : c.shortPackets(since)) {
        if (s.pkt.kind == ShortPacket::Kind::IoAck) out.push_back(s);
    }
    return out;
}

// "RFR": the kinds of trigger packets in order.
inline std::string kinds(const std::vector<TimedShort>& t) {
    std::string s;
    for (const auto& x : t) s += x.pkt.kind == ShortPacket::Kind::TriggerRise ? 'R' : 'F';
    return s.empty() ? "none" : s;
}

// Wait until n trigger packets have come since `since` (host ms, scaled).
inline bool waitTrigs(Context& c, size_t n, double since, int timeout_ms) {
    const double end = c.nowMs() + c.wait(timeout_ms);
    while (trigs(c, since).size() < n) {
        if (c.nowMs() > end) return false;
        c.sleepRawMs(2);
    }
    return true;
}

// The pin level at which the trigger is de-asserted: 0, or 1 with an
// active-low input (TRIG_POLARITY = 1).
inline uint32_t idleLevel(uint32_t polarity) { return polarity ? 1u : 0u; }

// A known start: the device out of a bench reset with TRIG_IN de-asserted
// for `polarity` (the source then follows the pin), or, without a bench
// reset, the two pins driven.  Trigger packets the start produced are
// acknowledged and left behind.  Returns the host time after the start.
inline double cleanStart(Context& c, uint32_t polarity) {
    c.needBench(bench::CAP_TRIG_IN | bench::CAP_CHARS | (polarity ? bench::CAP_TRIG_POLARITY : 0u),
                "drive the trigger input and see the downlink's trigger packets");
    const double t0 = c.nowMs();
    const uint32_t inputs = (idleLevel(polarity) << (bench::TRIG_IN - 1)) | (polarity << (bench::TRIG_POLARITY - 1));
    if (c.benchCaps() & bench::CAP_RESET) {
        if (!c.benchReset(inputs)) c.abort("the device did not answer after a bench reset");
    } else {
        if (polarity) c.benchPin(bench::TRIG_POLARITY, polarity);
        c.benchPin(bench::TRIG_IN, idleLevel(polarity));
        c.benchSync();
    }
    c.sleepMs(50);
    const auto left = trigs(c, t0);
    for (size_t i = 0; i < left.size(); ++i) c.sendChars(ioAck());
    if (!left.empty()) {
        c.info("start: %zu trigger packets from the reset (%s), acknowledged", left.size(), kinds(left).c_str());
        c.sleepMs(50);
    }
    return c.nowMs();
}

// Drive TRIG_IN (restored by the bench reset's exit action) and return the
// bench time just after the level took effect (0 without bench times).
inline uint64_t setPin(Context& c, uint32_t level) {
    c.benchSend({bench::PIN, bench::TRIG_IN, level});
    const auto t = c.benchTime();
    if (!t) c.benchSync();
    return t ? t->now_ps : 0;
}

// The uplink times of the host's Table 17 acknowledgments since `since`.
inline std::vector<UplinkMark> hostAckMarks(Context& c, double since) {
    std::vector<UplinkMark> out;
    for (const auto& m : c.uplinkMarks(since)) {
        if (m.chr == (0x100u | K28_6)) out.push_back(m);
    }
    return out;
}

inline bool timed(Context& c) { return (c.benchCaps() & bench::CAP_TIMES) != 0; }

// §8.3.3 "after completion of a trigger packet transmission it shall not send
// a new trigger packet until it has received an acknowledgment ... or ... for
// more than a defined time": each trigger after the first starts no earlier
// than the end (on the uplink) of the first host acknowledgment that left
// after the previous trigger, or than timeout_ps after the previous trigger.
// Needs bench times; returns the number of violations and reports the first
// few.  Packets without a time are skipped.
inline size_t judgePacing(Context& c, const std::vector<TimedShort>& t, const std::vector<UplinkMark>& acks,
                          uint64_t timeout_ps, const char* what) {
    size_t bad = 0;
    for (size_t i = 1; i < t.size(); ++i) {
        const uint64_t prev = t[i - 1].device_ps, now = t[i].device_ps;
        if (!prev || !now) continue;
        uint64_t release = prev + timeout_ps;
        for (const auto& m : acks) {
            if (m.time_ps > prev) {
                release = std::min(release, m.time_ps + uint64_t(80) * m.bit_ps);  // 8 characters
                break;
            }
        }
        if (now + 20000 < release && ++bad <= size_t(c.opt().max_reported)) {
            c.expect(false, "%strigger %zu at %.3f us: %.3f us after trigger %zu, before an acknowledgment or the "
                            "timeout (released at %.3f us)",
                     what, i, now / 1e6, (now - prev) / 1e6, i - 1, release / 1e6);
        }
    }
    return bad;
}

}  // namespace cxp::validation::checks::devtrig
