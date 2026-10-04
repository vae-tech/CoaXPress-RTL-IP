// Host -> Device triggers (Table 15) for the trig/, prot/ and neg/ cases:
// sending triggers at a character position, and what the device did about
// them (Table 17 acknowledgments, the recreated trigger, bench times).
// Header only; reads no catalogue parameter.
#pragma once

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::htrig {

// The IDLE word (§8.2.5): K28.5 K28.1 K28.1 D21.5.
inline Chars idleWord() { return {{0xBC, true}, {0x3C, true}, {0x3C, true}, {0xB5, false}}; }

// A trigger inserted into an IDLE word after its first k characters (§8.2.4:
// at any character boundary).  The bridge starts a queued frame on a word
// boundary, so k is the trigger's character phase within the word.
inline Chars inIdle(const Chars& trig, size_t k) { return insertAt(idleWord(), trig, k); }

inline bool isLeaderMark(const UplinkMark& m) {
    return m.chr == (0x100u | K28_2) || m.chr == (0x100u | K28_4);
}

inline size_t count(const std::vector<TimedShort>& v, ShortPacket::Kind k) {
    return size_t(std::count_if(v.begin(), v.end(), [k](const TimedShort& s) { return s.pkt.kind == k; }));
}

// What followed one stimulus.
struct Shot {
    double since = 0;
    std::vector<RawAck> cmd;          // command acknowledgments (with_cmd)
    std::vector<TimedShort> ioacks;   // Table 17 packets
    std::vector<PinEdge> out;         // TRIG_OUT edges
    std::vector<PinEdge> glitch;      // TRIG_GLITCH pulses
    std::vector<UplinkMark> leaders;  // trigger leaders on the uplink (CAP_TIMES)
    size_t rises() const {
        return size_t(std::count_if(out.begin(), out.end(), [](const PinEdge& e) { return e.value == 1; }));
    }
};

// Send the characters (one CXC1 frame), wait until want_acks I/O acks are in
// (or a while), then let the device settle wait_dev_ms of its milliseconds
// (bench time when there is one) and collect everything since.
inline Shot shoot(Context& c, const Chars& chars, size_t want_acks, bool with_cmd, double wait_dev_ms = 2.0) {
    Shot s;
    s.since = c.nowMs();
    if (with_cmd) {
        s.cmd = c.exchangeChars(chars, 1);
    } else {
        c.sendChars(chars);
    }
    if (want_acks) c.waitShort(ShortPacket::Kind::IoAck, want_acks, s.since, 1000);
    waitBenchMs(c, wait_dev_ms, 20000);
    c.benchSync();
    for (const auto& x : c.shortPackets(s.since)) {
        if (x.pkt.kind == ShortPacket::Kind::IoAck) s.ioacks.push_back(x);
    }
    s.out = c.pinEdges(bench::TRIG_OUT, s.since);
    s.glitch = c.pinEdges(bench::TRIG_GLITCH, s.since);
    for (const auto& m : c.uplinkMarks(s.since)) {
        if (isLeaderMark(m)) s.leaders.push_back(m);
    }
    return s;
}

// The recreated trigger in a known state: one clean falling trigger.
inline void settleLow(Context& c) { shoot(c, lsTrigger(false, 0), 1, false); }

// A read the device answers with its right data, retried a few times: a
// damaged uplink may cost the first ones while the receiver re-aligns.
inline bool responsive(Context& c, int tries = 5) {
    for (int i = 0; i < tries; ++i) {
        auto a = c.readRaw(Reg::STANDARD, 4);
        if (is(a, Ack::READ_OK) && a->values().size() == 1 && a->values()[0] == CXP_MAGIC) return true;
    }
    return false;
}

}  // namespace cxp::validation::checks::htrig
