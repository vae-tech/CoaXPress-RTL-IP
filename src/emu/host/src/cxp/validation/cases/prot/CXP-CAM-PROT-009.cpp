// CXP-CAM-PROT-009.  See cases/_common.h.
//
// §8.2.4: the I/O acknowledgment (priority 1) of a host trigger is inserted
// into the lower-priority packet being sent, at a word boundary, and that
// packet resumes intact.  Host triggers at random instants while stream
// packets and, in TestMode, connection-test packets go out; the bench times
// each trigger on the uplink and each acknowledgment on the downlink and says
// whether it was inserted into a packet.

#include "cxp/validation/cases/trig/_device_trig.h"

namespace cxp::validation::checks::prot {

namespace {

struct Round {
    size_t sent = 0;
    double since = 0;
};

// n host triggers (alternating rising / falling, Delay 0) at random gaps.
Round triggers(Context& c, int n, std::mt19937& rng) {
    const auto gap = c.ilist("gap_range_ms");
    Round r;
    r.since = c.nowMs();
    for (int i = 0; i < n; ++i) {
        c.sleepRawMs(std::uniform_int_distribution<int>(int(gap[0]), int(gap[1]))(rng));
        c.sendChars(lsTrigger(i % 2 == 0, 0));
        ++r.sent;
    }
    c.waitShort(ShortPacket::Kind::IoAck, r.sent, r.since, 2000);
    return r;
}

void judge(Context& c, const Round& r, const char* what) {
    c.benchTime();  // the bench events before it are in
    const auto acks = devtrig::ioAcks(c, r.since);
    size_t clean = 0;
    for (const auto& a : acks) clean += a.pkt.clean && a.pkt.value == IOACK_OK;
    c.expect(acks.size() == r.sent && clean == r.sent, "%s%zu triggers, %zu I/O acknowledgments, %zu clean", what,
             r.sent, acks.size(), clean);
    if (!devtrig::timed(c)) return;
    std::vector<UplinkMark> trig;
    for (const auto& m : c.uplinkMarks(r.since)) {
        if (m.chr == (0x100u | K28_2) || m.chr == (0x100u | K28_4)) trig.push_back(m);
    }
    const uint64_t word_ps = uint64_t(c.iparam("word_ns")) * 1000;
    size_t inside = 0, late = 0, n = 0;
    double worst = 0;
    std::string where;
    for (size_t i = 0; i < acks.size() && i < trig.size(); ++i) {
        if (!acks[i].device_ps) continue;
        ++n;
        const uint64_t end = trig[i].time_ps + uint64_t(60) * trig[i].bit_ps;  // 6 characters
        const uint64_t bound = end + uint64_t(10) * trig[i].bit_ps + word_ps;   // one LS character + one word
        const double ns = (double(acks[i].device_ps) - double(end)) / 1e3;
        worst = std::max(worst, ns);
        late += acks[i].device_ps > bound;
        if (acks[i].inside == 1) {
            ++inside;
            if (where.size() < 48) where += strprintf("%s%d", where.empty() ? "" : ", ", acks[i].word_index);
        }
    }
    c.expect(n == r.sent && late == 0,
             "%severy acknowledgment starts within one LS character and one word of its trigger's end (worst %.0f ns, "
             "%zu timed)",
             what, worst, n);
    c.expect(inside > 0, "%s%zu acknowledgments inserted into a packet in progress (after word %s)", what, inside,
             where.empty() ? "-" : where.c_str());
}

void prot009(Context& c) {
    c.needBench(bench::CAP_CHARS, "send host triggers and see the I/O acknowledgments");
    uvm::uvmIdleConfig(c);
    uvm::runTestPattern(c);
    c.prepareStreaming();
    std::mt19937 rng(c.seed(9));
    if (c.benchCaps() & bench::CAP_TIMES) c.benchRequest({bench::DL_STATS});

    // Stream packets.
    Round r;
    auto cap = uvm::streamAround(c, 1, [&] { r = triggers(c, c.iparam("triggers"), rng); });
    judge(c, r, "streaming: ");
    uvm::streamScoreboard(c, streamPackets(cap), -1, 0, {}, "streaming: ");

    // Connection-test packets (TestMode = 1): 1027 words each.
    c.onExit([&c] { c.writeRaw(Reg::TEST_MODE, {0}); });
    c.startRecording();
    c.wr32(Reg::TEST_MODE, 1);
    r = triggers(c, c.iparam("triggers"), rng);
    c.wr32(Reg::TEST_MODE, 0);
    c.sleepMs(100);
    const auto tp = framesOfType(c.stopRecording(), 0x04);
    judge(c, r, "test mode: ");
    size_t bad = 0;
    for (const auto& f : tp) bad += linkTestErrors(f.frame) != 0;
    c.expect(!tp.empty() && bad == 0, "test mode: %zu of %zu connection-test packets intact (Table 23, 1027 words)",
             tp.size() - bad, tp.size());
    if (c.benchCaps() & bench::CAP_TIMES) {
        if (const auto st = c.benchRequest({bench::DL_STATS}); st && st->size() >= 4) {
            c.info("downlink: %u packets, %u IDLE words and %u short packets inside packets, %u between", (*st)[0],
                   (*st)[1], (*st)[2], (*st)[3]);
        }
    }
}
CXP_CHECK("CXP-CAM-PROT-009", prot009);

}  // namespace

}  // namespace cxp::validation::checks::prot
