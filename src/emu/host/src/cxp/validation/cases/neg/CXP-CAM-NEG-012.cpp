// CXP-CAM-NEG-012.  See cases/_common.h.
//
// Corrupted and out-of-range Table 15 packets.  §8.2.2: the replicated
// characters are decoded with immunity to one bad copy, so a leader with two
// of its three characters in place is that leader, and a Delay with two
// alike copies is that Delay.  A packet whose leader has no majority is not
// a trigger at all; one whose Delay has no two alike data copies, or whose
// Delay is outside 0..239 (§8.3.2.1), carries no usable trigger: neither may
// recreate a trigger.  The device's documented choice for the latter (the
// RTL: a glitch, not acknowledged) is reported, and the link must survive.

#include "cxp/validation/cases/trig/_host_trig.h"

namespace cxp::validation::checks::neg {

namespace {

using namespace htrig;

constexpr Char kR2{K28_2, true}, kR4{K28_4, true}, kR5{K28_5, true}, kR1{0x3C, true};

struct Stim {
    const char* what;
    Chars chars;
    int edge;  // 1 rising, 0 falling, -1 no usable Delay, -2 no leader, -3 ambiguous (reported only)
};

Chars pkt(Char a, Char b, Char d, Char x, Char y, Char z) { return {a, b, d, x, y, z}; }
Char D(uint8_t v) { return {v, false}; }

void neg012(Context& c) {
    c.needBench(bench::CAP_CHARS | bench::CAP_TRIG_OUT, "send damaged Table 15 packets and watch the recreated trigger");
    uvm::uvmIdleConfig(c);
    const std::vector<Stim> stims = {
        {"clean rising, Delay 100", pkt(kR2, kR4, kR4, D(100), D(100), D(100)), 1},
        {"rising leader character 0 hit (a data character)", pkt(D(0x5C), kR4, kR4, D(100), D(100), D(100)), 1},
        {"rising leader character 1 hit (a data character)", pkt(kR2, D(0x9C), kR4, D(100), D(100), D(100)), 1},
        {"rising leader character 2 hit (a data character)", pkt(kR2, kR4, D(0x9C), D(100), D(100), D(100)), 1},
        {"falling leader character 0 hit (a data character)", pkt(D(0x9C), kR2, kR2, D(100), D(100), D(100)), 0},
        {"falling leader character 1 hit (a data character)", pkt(kR4, D(0x5C), kR2, D(100), D(100), D(100)), 0},
        {"falling leader character 2 hit (a data character)", pkt(kR4, kR2, D(0x5C), D(100), D(100), D(100)), 0},
        {"leader K28.4 K28.2 K28.4 (a falling leader, one wrong K)", pkt(kR4, kR2, kR4, D(100), D(100), D(100)), 0},
        {"one Delay copy different (100 100 37)", pkt(kR2, kR4, kR4, D(100), D(100), D(37)), 1},
        {"one Delay copy a K character", pkt(kR2, kR4, kR4, kR1, D(100), D(100)), 1},
        {"no two Delay copies alike (10 20 30)", pkt(kR2, kR4, kR4, D(10), D(20), D(30)), -1},
        {"Delay 240", pkt(kR2, kR4, kR4, D(240), D(240), D(240)), -1},
        {"Delay 255", pkt(kR2, kR4, kR4, D(255), D(255), D(255)), -1},
        {"every Delay copy a K character", pkt(kR2, kR4, kR4, kR1, kR1, kR1), -1},
        {"two leader characters wrong (K28.2 K28.5 K28.5)", pkt(kR2, kR5, kR5, D(100), D(100), D(100)), -2},
        {"two leader characters wrong (K28.2 D D)", pkt(kR2, D(0x9C), D(0x9C), D(100), D(100), D(100)), -2},
        // A wrong copy that is itself a leader character can make a leader of
        // the opposite edge one character earlier, with the character before
        // the packet as its bad copy: §8.2.2 does not settle which reading is
        // right, so these are reported, not judged.
        {"K28.2 K28.2 K28.4 (rising with a bad copy, or falling one character earlier)",
         pkt(kR2, kR2, kR4, D(100), D(100), D(100)), -3},
        {"K28.4 K28.4 K28.4 (rising with a bad copy, also one character earlier)",
         pkt(kR4, kR4, kR4, D(100), D(100), D(100)), -3},
        {"K28.2 K28.2 K28.2 (falling with a bad copy)", pkt(kR2, kR2, kR2, D(100), D(100), D(100)), -3},
    };
    size_t false_trig = 0, missed = 0, wedged = 0;
    std::string rejected;
    for (const Stim& s : stims) {
        settleLow(c);
        const Shot r = shoot(c, s.chars, s.edge >= 0 ? 1 : 0, false, 5.0);
        const size_t up = r.rises();
        bool ok;
        if (s.edge == -3) {
            const bool first = responsive(c, 1);
            c.info("%s: %zu acknowledgments, %zu recreated rising edges, %zu glitch pulses; the next read %s", s.what,
                   r.ioacks.size(), up, r.glitch.size(), first ? "answered" : "lost (the receiver re-aligned)");
            if (!first && !responsive(c)) {
                ++wedged;
                c.expect(false, "%s: the link no longer answers a read", s.what);
            }
            continue;
        }
        if (s.edge >= 0) {
            ok = r.ioacks.size() == 1 && up == size_t(s.edge);
            missed += !ok;
            c.expect(ok, "%s: a %s trigger, %zu acknowledgment (1), %zu recreated rising edges (%d)", s.what,
                     s.edge ? "rising" : "falling", r.ioacks.size(), up, s.edge);
        } else {
            ok = up == 0;
            false_trig += !ok;
            c.expect(ok, "%s: no trigger, %zu recreated rising edges (0)", s.what, up);
            rejected += strprintf("\n    %s: %zu acknowledgments, %zu glitch pulses", s.what, r.ioacks.size(),
                                  r.glitch.size());
            if (s.edge == -1) {
                c.expect(r.ioacks.size() == r.glitch.size() || r.ioacks.empty(),
                         "%s: handled consistently (%zu acknowledgments, %zu glitch pulses)", s.what, r.ioacks.size(),
                         r.glitch.size());
            } else {
                c.expect(r.ioacks.empty(), "%s: not a trigger packet, %zu acknowledgments (0)", s.what,
                         r.ioacks.size());
            }
        }
        // A trigger the device took whole leaves the word framing alone: the
        // next read answers at once.  After a non-trigger the receiver may
        // have to re-align first.
        if (s.edge >= -1) {
            c.expect(responsive(c, 1), "%s: the next read answers (the packet taken whole)", s.what);
        }
        const bool alive = responsive(c);
        wedged += !alive;
        if (!alive) c.expect(false, "%s: the link no longer answers a read", s.what);
    }
    c.info("packets without a usable trigger:%s", rejected.c_str());
    settleLow(c);
    const Shot last = shoot(c, lsTrigger(true, 0), 1, false);
    c.expect(last.ioacks.size() == 1 && last.rises() == 1,
             "a clean rising trigger after them: %zu acknowledgment, %zu recreated rising edge", last.ioacks.size(),
             last.rises());
    c.expect(false_trig == 0 && missed == 0 && wedged == 0,
             "%zu false triggers, %zu repairable packets not taken, %zu wedges", false_trig, missed, wedged);
    settleLow(c);
}
CXP_CHECK("CXP-CAM-NEG-012", neg012);

}  // namespace

}  // namespace cxp::validation::checks::neg
