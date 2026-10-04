// CXP-CAM-TRIG-007.  See cases/_common.h.
//
// Host triggers while the acquisition is stopped, while the device streams,
// and back to back at the full low-speed rate (every character a trigger's):
// §8.3.3 every trigger packet acknowledged, the recreated trigger following
// them, the stream unharmed.  The camera-functional part (images per trigger,
// overlap and overrun) needs a trigger mode the device does not have.

#include "cxp/validation/cases/trig/_device_trig.h"

namespace cxp::validation::checks::trig {

namespace {

// Triggers alternating rising / falling from a de-asserted host level:
// `per_frame` of them back to back in one character frame, `frames` frames
// gap_ms apart.
uvm::HostTrigRun run(Context& c, int frames, int per_frame, int gap_ms) {
    uvm::HostTrigRun r;
    const double since = c.nowMs();
    uint32_t level = 0;
    for (int f = 0; f < frames; ++f) {
        Chars ch;
        for (int i = 0; i < per_frame; ++i) {
            const bool rising = r.sent % 2 == 0;
            const Chars t = lsTrigger(rising, 0);
            ch.insert(ch.end(), t.begin(), t.end());
            ++r.sent;
            r.rising += rising;
            if (uint32_t(rising) != level) r.want_edges.push_back(level = uint32_t(rising));
        }
        c.sendChars(ch);
        c.waitShort(ShortPacket::Kind::IoAck, r.sent, since, 2000);
        c.sleepRawMs(gap_ms);
    }
    c.waitShort(ShortPacket::Kind::IoAck, r.sent, since, 2000);
    c.sleepMs(20);
    r.acks = devtrig::ioAcks(c, since);
    r.edges = c.pinEdges(bench::TRIG_OUT, since);
    return r;
}

void judge(Context& c, const uvm::HostTrigRun& r, const char* what) {
    uvm::judgeAcks(c, r, what);
    if (c.benchCaps() & bench::CAP_TRIG_OUT) uvm::judgeEdges(c, r, what);
}

void trig007(Context& c) {
    c.needBench(bench::CAP_CHARS, "send host triggers and see the I/O acknowledgments");
    uvm::uvmIdleConfig(c);
    uvm::settleTrigOut(c);
    judge(c, run(c, c.iparam("triggers"), 1, c.iparam("gap_ms")), "acquisition stopped: ");

    uvm::runTestPattern(c);
    c.prepareStreaming();
    uvm::HostTrigRun spaced, burst;
    auto cap = uvm::streamAround(c, 1, [&] {
        spaced = run(c, c.iparam("triggers"), 1, c.iparam("gap_ms"));
        burst = run(c, c.iparam("bursts"), c.iparam("burst_triggers"), c.iparam("gap_ms"));
    });
    judge(c, spaced, "streaming: ");
    judge(c, burst, "streaming, back to back at the full LS rate: ");
    uvm::streamScoreboard(c, streamPackets(cap), -1, 0, {}, "stream under the triggers: ");
    c.note("frames per trigger, overlap and overrun need a trigger mode (camera datasheet); the device has none");
}
CXP_CHECK("CXP-CAM-TRIG-007", trig007);

}  // namespace

}  // namespace cxp::validation::checks::trig
