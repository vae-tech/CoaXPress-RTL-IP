// CXP-EMU-SCN-001.  See cases/_common.h.
//
// Everything at once: while the device streams its Bars test pattern, the
// host interleaves register traffic judged against a model, host test
// packets and Table 15 triggers, round after round on the case's thread (the
// link queues them behind each other, so the device sees them together with
// the stream).  Then all four are judged.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::scn {

namespace {

void scn001(Context& c) {
    c.needBench(bench::CAP_CHARS | bench::CAP_TRIG_OUT, "send host triggers and watch the recreated trigger");
    uvm::uvmIdleConfig(c);
    c.preserve(Reg::MASTER_HOST_CONNECTION_ID);
    c.preserve(Reg::TEST_ERROR_COUNT_SELECTOR);
    const int pat = uvm::selectBars(c);
    preserveFeature(c, "Width");
    preserveFeature(c, "Height");
    if (!trySet(c, "Width", Value::ofInt(c.iparam("image.width"))) ||
        !trySet(c, "Height", Value::ofInt(c.iparam("image.height")))) {
        c.abort("cannot set the image geometry");
    }
    uvm::resetTestCounters(c);
    uvm::settleTrigOut(c);

    // The register model: MasterHostConnectionID (any value), and read-only
    // registers whose values the model knows from a first read.
    std::map<uint32_t, uint32_t> model;
    for (uint32_t a : {Reg::STANDARD, Reg::REVISION, Reg::CONTROL_PACKET_SIZE_MAX, Reg::CONNECTION_CONFIG_DEFAULT,
                       Reg::MASTER_HOST_CONNECTION_ID}) {
        model[a] = c.rd32(a);
    }
    std::mt19937 rng(c.seed(0x5C01));
    const int cmd_to = c.iparam("cmd_timeout_ms");
    size_t cmds = 0, cmd_bad = 0, packets = 0, trig_sent = 0, rising = 0;
    std::vector<uint32_t> want_edges;
    uint32_t level = 0;
    const double t_trig0 = c.nowMs();
    const auto bt0 = c.benchTime();

    auto cap = uvm::streamAround(c, size_t(c.iparam("images_after")), [&] {
        for (int r = 0; r < c.iparam("rounds"); ++r) {
            c.sendOnly({hostTestPacket(0)}, 0);
            ++packets;
            for (int k = 0; k < c.iparam("commands_per_round"); ++k) {
                const bool write = (rng() & 1) != 0;
                std::string why;
                if (write) {
                    const uint32_t v = uint32_t(rng());
                    const auto a = c.exchange(writeCmd(Reg::MASTER_HOST_CONNECTION_ID, {v}), cmd_to);
                    if (!is(a, Ack::WRITE_OK)) why = "write MasterHostConnectionID: " + ackStr(a);
                    else model[Reg::MASTER_HOST_CONNECTION_ID] = v;
                } else {
                    auto it = model.begin();
                    std::advance(it, rng() % model.size());
                    const auto a = c.exchange(readCmd(it->first, 4), cmd_to);
                    if (!is(a, Ack::READ_OK) || a->values().size() != 1) {
                        why = strprintf("read 0x%04X: %s", it->first, ackStr(a).c_str());
                    } else if (a->values()[0] != it->second) {
                        why = strprintf("read 0x%04X: 0x%08X, model 0x%08X", it->first, a->values()[0], it->second);
                    }
                }
                ++cmds;
                if (!why.empty() && ++cmd_bad <= size_t(c.opt().max_reported)) c.expect(false, "command %zu: %s", cmds, why.c_str());
            }
            for (int k = 0; k < c.iparam("triggers_per_round"); ++k) {
                const bool up = (rng() & 1) != 0;
                c.sendChars(lsTrigger(up, uint8_t(rng() % 240)));
                ++trig_sent;
                rising += up;
                if (uint32_t(up) != level) want_edges.push_back(level = uint32_t(up));
            }
        }
    }, c.iparam("image_timeout_ms"));
    c.waitShort(ShortPacket::Kind::IoAck, trig_sent, t_trig0, c.iparam("cmd_timeout_ms"));
    if (const auto bt1 = c.benchTime(); bt0 && bt1) {
        c.info("%.1f device ms of traffic (%zu commands, %zu test packets, %zu triggers)",
               double(bt1->now_ps - bt0->now_ps) / double(bt1->ms_ps), cmds, packets, trig_sent);
    }

    // 1. The stream.
    const auto sb = uvm::streamScoreboard(c, streamPackets(cap), pat, 0, {}, "stream: ");
    c.expect(sb.complete >= size_t(c.iparam("min_complete")), "stream: %zu complete images (>= %d)", sb.complete,
             c.iparam("min_complete"));
    // 2. The commands: none lost, each as the model says.
    c.expect(cmd_bad == 0, "commands: %zu of %zu answered as the register model predicts", cmds - cmd_bad, cmds);
    // 3. The link test.
    uvm::expectCounters(c, packets, 0);
    // 4. The triggers.
    uvm::HostTrigRun run;
    run.sent = trig_sent;
    run.rising = rising;
    run.want_edges = want_edges;
    for (const auto& s : c.shortPackets(t_trig0)) {
        if (s.pkt.kind == ShortPacket::Kind::IoAck) run.acks.push_back(s);
    }
    run.edges = c.pinEdges(bench::TRIG_OUT, t_trig0);
    uvm::judgeAcks(c, run, "triggers: ");
    uvm::judgeEdges(c, run, "triggers: ");
    uvm::settleTrigOut(c);
}
CXP_CHECK("CXP-EMU-SCN-001", scn001);

}  // namespace

}  // namespace cxp::validation::checks::scn
