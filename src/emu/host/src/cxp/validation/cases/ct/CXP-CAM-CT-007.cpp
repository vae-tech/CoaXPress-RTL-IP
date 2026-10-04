// CXP-CAM-CT-007.  See cases/_common.h.
//
// Triggers and I/O acknowledgments while TestMode = 1.  §8.7.4 limits the
// *data* a device sends in TestMode to connection-test packets; §8.3.3 has
// no TestMode exemption for acknowledging triggers.  The device decision
// (D2, src/verif/uvm/common/decisions.py) is "allowed": host triggers are
// acknowledged and act, device triggers go out, inserted into the test
// packets, which stay intact.

#include "cxp/validation/cases/trig/_device_trig.h"

namespace cxp::validation::checks::ct {

namespace {

void ct007(Context& c) {
    devtrig::cleanStart(c, 0);
    const bool trig_out = (c.benchCaps() & bench::CAP_TRIG_OUT) != 0;
    c.onExit([&c] { c.writeRaw(Reg::TEST_MODE, {0}); });
    c.wr32(Reg::TEST_ERROR_COUNT_SELECTOR, 0);
    const uint64_t tx0 = c.rd64(Reg::TEST_PACKET_COUNT_TX);
    c.startRecording();
    c.wr32(Reg::TEST_MODE, 1);
    const double since = c.nowMs();

    // Host -> device: rising / falling triggers.
    uvm::HostTrigRun h;
    uint32_t level = 0;
    for (int i = 0; i < 2 * c.iparam("host_triggers"); ++i) {
        const bool rising = i % 2 == 0;
        c.sendChars(lsTrigger(rising, 0));
        ++h.sent;
        h.rising += rising;
        if (uint32_t(rising) != level) h.want_edges.push_back(level = uint32_t(rising));
        c.waitShort(ShortPacket::Kind::IoAck, h.sent, since, 1000);
        c.sleepRawMs(c.iparam("gap_ms"));
    }
    h.acks = devtrig::ioAcks(c, since);
    h.edges = c.pinEdges(bench::TRIG_OUT, since);

    // Device -> host: edges of the trigger input, each acknowledged.
    const double dsince = c.nowMs();
    std::string want;
    level = 0;
    for (int i = 0; i < 2 * c.iparam("device_edges"); ++i) {
        level ^= 1u;
        want += level ? 'R' : 'F';
        c.benchSend({bench::PIN, bench::TRIG_IN, level});
        devtrig::waitTrigs(c, size_t(i) + 1, dsince, 1000);
        c.sendChars(ioAck());
        c.sleepRawMs(c.iparam("gap_ms"));
    }
    c.sleepMs(c.iparam("hold_ms"));
    c.wr32(Reg::TEST_MODE, 0);
    const double t_off = c.nowMs();
    c.sleepMs(100);
    const auto cap = c.stopRecording();
    const uint64_t tx1 = c.rd64(Reg::TEST_PACKET_COUNT_TX);

    uvm::judgeAcks(c, h, "host triggers in TestMode: ");
    if (trig_out) uvm::judgeEdges(c, h, "host triggers in TestMode: ");
    const auto dt = devtrig::trigs(c, dsince);
    c.expect(devtrig::kinds(dt) == want, "device triggers in TestMode: input edges %s, trigger packets %s", want.c_str(),
             devtrig::kinds(dt).c_str());

    size_t bad = 0, late = 0;
    const auto tp = framesOfType(cap, 0x04);
    for (const auto& f : tp) {
        bad += linkTestErrors(f.frame) != 0;
        late += f.t_ms > t_off + c.iparam("grace_ms");
    }
    c.expect(!tp.empty() && bad == 0, "%zu of %zu connection-test packets intact around the inserted packets",
             tp.size() - bad, tp.size());
    const int64_t d = int64_t(tx1 - tx0) - int64_t(tp.size());
    c.expect(!tp.empty() && d >= -1 && d <= 1, "TestPacketCountTx +%llu vs %zu test packets received (+-1)",
             (unsigned long long)(tx1 - tx0), tp.size());
    c.expect(late == 0, "%zu test packets later than %d ms after TestMode = 0", late, c.iparam("grace_ms"));

    // No wedge: the control channel and the I/O channel answer after it.
    auto a = c.readRaw(Reg::STANDARD, 4);
    c.expect(is(a, Ack::READ_OK) && a->values().size() == 1 && a->values()[0] == CXP_MAGIC,
             "after TestMode = 0: a read answers %s", ackStr(a).c_str());
    const double s2 = c.nowMs();
    c.sendChars(lsTrigger(true, 0));
    c.sendChars(lsTrigger(false, 0));
    c.waitShort(ShortPacket::Kind::IoAck, 2, s2, 1000);
    c.expect(devtrig::ioAcks(c, s2).size() == 2, "after TestMode = 0: 2 host triggers, %zu I/O acknowledgments",
             devtrig::ioAcks(c, s2).size());
}
CXP_CHECK("CXP-CAM-CT-007", ct007);

}  // namespace

}  // namespace cxp::validation::checks::ct
