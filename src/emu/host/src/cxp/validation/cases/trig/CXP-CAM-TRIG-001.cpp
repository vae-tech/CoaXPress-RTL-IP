// CXP-CAM-TRIG-001.  See cases/_common.h.
//
// Table 15 triggers at every character position of an IDLE word and of a
// read command, both edges, Delay 0 / 120 / 239, idle and while streaming.
// The extension-connection step of the plan is CXP-CAM-TRIG-001b.

#include "cxp/validation/cases/trig/_host_trig.h"

namespace cxp::validation::checks::trig {

namespace {

using namespace htrig;

struct Run {
    uvm::HostTrigRun r;
    size_t cmd_bad = 0, cmds = 0;
};

// One trigger (inside a read command when at >= 0), judged later as a run.
void one(Context& c, Run& run, bool rising, uint8_t delay, int idle_k, int cmd_at, uint32_t id) {
    const Chars trig = lsTrigger(rising, delay);
    Shot s;
    if (cmd_at >= 0) {
        s = shoot(c, insertAt(frameChars(readCmd(Reg::MASTER_HOST_CONNECTION_ID, 4)), trig, size_t(cmd_at)), 1, true);
        ++run.cmds;
        const bool ok = !s.cmd.empty() && s.cmd[0].code == Ack::READ_OK && s.cmd[0].values().size() == 1 &&
                        s.cmd[0].values()[0] == id;
        if (!ok && ++run.cmd_bad <= size_t(c.opt().max_reported)) {
            c.expect(false, "read with a %s trigger (Delay %u) after %d characters: %s", rising ? "rising" : "falling",
                     delay, cmd_at, s.cmd.empty() ? "no acknowledgment" : ackName(s.cmd[0].code).c_str());
        }
    } else {
        s = shoot(c, inIdle(trig, size_t(idle_k)), 1, false);
    }
    ++run.r.sent;
    run.r.rising += rising;
    const uint32_t level = run.r.want_edges.empty() ? 0 : run.r.want_edges.back();
    if (uint32_t(rising) != level) run.r.want_edges.push_back(uint32_t(rising));
    run.r.acks.insert(run.r.acks.end(), s.ioacks.begin(), s.ioacks.end());
    run.r.edges.insert(run.r.edges.end(), s.out.begin(), s.out.end());
}

void judge(Context& c, const Run& run, const char* what) {
    uvm::judgeAcks(c, run.r, what);
    uvm::judgeEdges(c, run.r, what);
    if (run.cmds) {
        c.expect(run.cmd_bad == 0, "%s%zu of %zu reads with a trigger inside answered 0x00 with the register", what,
                 run.cmds - run.cmd_bad, run.cmds);
    }
}

void trig001(Context& c) {
    c.needBench(bench::CAP_CHARS | bench::CAP_TRIG_OUT, "send Table 15 triggers and watch the recreated trigger");
    uvm::uvmIdleConfig(c);
    const auto delays = c.ilist("delays");
    const uint32_t id = c.rd32(Reg::MASTER_HOST_CONNECTION_ID);

    // Idle: every character position of the IDLE word, both edges, every Delay.
    settleLow(c);
    Run idle;
    for (int k = 0; k < 4; ++k) {
        for (int64_t d : delays) {
            for (bool rising : {true, false}) one(c, idle, rising, uint8_t(d), k, -1, id);
        }
    }
    judge(c, idle, "idle, inside IDLE words: ");

    // Every character position of a read command (before its SOP .. after its EOP).
    settleLow(c);
    Run cmd;
    const size_t n = frameChars(readCmd(Reg::MASTER_HOST_CONNECTION_ID, 4)).size();
    for (size_t at = 0; at <= n; ++at) {
        one(c, cmd, at % 2 == 0, uint8_t(delays[at % delays.size()]), -1, int(at), id);
    }
    judge(c, cmd, "idle, inside a read command: ");

    // Streaming: the IDLE positions and a few command positions again.
    uvm::runTestPattern(c);
    settleLow(c);
    Run str;
    const auto cap = uvm::streamAround(c, 1, [&] {
        for (int k = 0; k < 4; ++k) one(c, str, k % 2 == 0, uint8_t(delays[size_t(k) % delays.size()]), k, -1, id);
        for (int64_t at : c.ilist("stream_cmd_positions")) one(c, str, at % 2 == 0, 120, -1, int(at), id);
    });
    judge(c, str, "streaming: ");
    uvm::streamScoreboard(c, streamPackets(cap), -1, 0, {}, "streaming: ");

    settleLow(c);
}
CXP_CHECK("CXP-CAM-TRIG-001", trig001);

}  // namespace

}  // namespace cxp::validation::checks::trig
