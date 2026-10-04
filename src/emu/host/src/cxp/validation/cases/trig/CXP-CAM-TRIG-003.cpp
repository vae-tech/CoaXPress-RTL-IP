// CXP-CAM-TRIG-003.  See cases/_common.h.
//
// §8.3.2: Host and Device de-assert the trigger signal as part of link
// discovery, with the effect of a falling-edge trigger packet; §10.3.28 lists
// "Device trigger signal = 0" among the ConnectionReset effects.  The bench
// sees the trigger the device recreates (TRIG_OUT) either as a level or as a
// strobe of the edge the device passes (cfg_trig_polarity: 0 rising, 1
// falling); the case judges what that reading makes observable.

#include "cxp/validation/cases/trig/_host_trig.h"

namespace cxp::validation::checks::trig {

namespace {

using namespace htrig;

size_t rises(const std::vector<PinEdge>& v) {
    return size_t(std::count_if(v.begin(), v.end(), [](const PinEdge& e) { return e.value == 1; }));
}

// ConnectionReset, then the recreated-trigger edges it caused.
std::vector<PinEdge> resetEdges(Context& c) {
    const double t = c.nowMs();
    connectionReset(c);
    waitBenchMs(c, 2.0, 20000);
    c.benchSync();
    return c.pinEdges(bench::TRIG_OUT, t);
}

void trig003(Context& c) {
    c.needBench(bench::CAP_CHARS | bench::CAP_TRIG_OUT | bench::CAP_TRIG_IN | bench::CAP_TRIG_POLARITY,
                "send Table 15 triggers, drive the trigger input and polarity, and watch the recreated trigger");
    const double t_pwr = c.nowMs();
    uvm::uvmIdleConfig(c);
    preserveLink(c);
    waitBenchMs(c, 2.0, 20000);
    c.expect(rises(c.pinEdges(bench::TRIG_OUT, t_pwr)) == 0, "power-up: no recreated trigger");

    // Polarity 0 (rising passed): rising trigger, then ConnectionReset.
    settleLow(c);
    const Shot up = shoot(c, lsTrigger(true, 0), 1, false);
    const bool level = up.out.size() == 1 && up.out[0].value == 1;
    const bool strobe = up.out.size() == 2 && up.out[0].value == 1 && up.out[1].value == 0;
    c.expect(up.ioacks.size() == 1 && (level || strobe), "rising trigger: %zu acknowledgment, recreated as %s",
             up.ioacks.size(), level ? "a level" : strobe ? "a strobe" : "neither a level nor a strobe");
    auto e = resetEdges(c);
    if (level) {
        c.expect(e.size() == 1 && e[0].value == 0, "ConnectionReset after a rising trigger: the recreated trigger "
                 "falls (%zu edges)", e.size());
    } else {
        c.expect(rises(e) == 0, "ConnectionReset after a rising trigger, rising edges passed: no recreated trigger "
                 "(%zu rising edges)", rises(e));
    }
    const Shot again = shoot(c, lsTrigger(true, 0), 1, false);
    c.expect(again.ioacks.size() == 1 && again.rises() == 1,
             "after the ConnectionReset a rising trigger is an edge again: %zu acknowledgment, %zu rising edges",
             again.ioacks.size(), again.rises());
    settleLow(c);

    // Polarity 1 (falling passed), strobe devices: the de-assertion has the
    // effect of a falling edge, so the device's strobe fires once.
    if (strobe) {
        c.benchPin(bench::TRIG_IN, 1);  // active low: keep the device's own trigger de-asserted
        c.benchPin(bench::TRIG_POLARITY, 1);
        c.benchSync();
        waitBenchMs(c, 1.0, 20000);
        settleLow(c);
        const Shot r = shoot(c, lsTrigger(true, 0), 1, false);
        c.expect(r.ioacks.size() == 1 && r.rises() == 0, "falling edges passed: a rising trigger, %zu acknowledgment, "
                 "%zu strobes (none)", r.ioacks.size(), r.rises());
        e = resetEdges(c);
        c.expect(rises(e) == 1, "falling edges passed: ConnectionReset after a rising trigger fires the strobe once, "
                 "as a falling-edge trigger packet would (§8.3.2): %zu strobes", rises(e));
        c.benchSend({bench::PIN, bench::TRIG_POLARITY, 0});
        c.benchSend({bench::PIN, bench::TRIG_IN, 0});
        c.benchSync();
        waitBenchMs(c, 1.0, 20000);
        for (const auto& s : c.shortPackets(r.since)) {
            if (s.pkt.kind != ShortPacket::Kind::IoAck) c.sendChars(ioAck());
        }
        settleLow(c);
    } else {
        c.note("the recreated trigger is a level: the falling-edge strobe step is not run");
    }

    // Device -> Host: the device's trigger asserted across a ConnectionReset.
    double t = c.nowMs();
    c.benchSend({bench::PIN, bench::TRIG_IN, 1});
    c.waitShort(ShortPacket::Kind::TriggerRise, 1, t, 1000);
    c.sendChars(ioAck());
    t = c.nowMs();
    connectionReset(c);
    waitBenchMs(c, 2.0, 20000);
    std::string after;
    for (const auto& s : c.shortPackets(t)) {
        if (s.pkt.kind == ShortPacket::Kind::IoAck) continue;
        after += s.pkt.kind == ShortPacket::Kind::TriggerRise ? " K28.4" : " K28.2";
        c.sendChars(ioAck());
    }
    c.info("device trigger held asserted across the ConnectionReset: trigger packets after it:%s",
           after.empty() ? " none" : after.c_str());
    t = c.nowMs();
    c.benchSend({bench::PIN, bench::TRIG_IN, 0});
    const bool fell = c.waitShort(ShortPacket::Kind::TriggerFall, 1, t, 1000);
    c.sendChars(ioAck());
    waitBenchMs(c, 1.0, 20000);
    t = c.nowMs();
    c.benchSend({bench::PIN, bench::TRIG_IN, 1});
    const bool rose = c.waitShort(ShortPacket::Kind::TriggerRise, 1, t, 1000);
    c.sendChars(ioAck());
    c.info("then the input low -> %s", fell ? "K28.2" : "no packet");
    c.expect(rose, "after the ConnectionReset the device's trigger rises again as a K28.4 packet");
    c.benchSend({bench::PIN, bench::TRIG_IN, 0});
    c.waitShort(ShortPacket::Kind::TriggerFall, 1, c.nowMs() - 1, 500);
    c.sendChars(ioAck());
}
CXP_CHECK("CXP-CAM-TRIG-003", trig003);

}  // namespace

}  // namespace cxp::validation::checks::trig
