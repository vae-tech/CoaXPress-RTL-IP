// UVM-test_trigger_in_ctrl_packet.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void triggerInCtrlPacket(Context& c) {
    c.needBench(bench::CAP_CHARS | bench::CAP_TRIG_OUT, "insert a Table 15 trigger into a command");
    uvmIdleConfig(c);
    const uint32_t id = c.rd32(Reg::MASTER_HOST_CONNECTION_ID);
    const int at = c.iparam("insert_at");
    settleTrigOut(c);
    const double since = c.nowMs();
    // insert_at characters into the six-word read of MasterHostConnectionID.
    const Chars cmd = insertAt(frameChars(readCmd(Reg::MASTER_HOST_CONNECTION_ID, 4)), trigChars(true), size_t(at));
    const auto acks = c.exchangeChars(cmd, 1);
    c.waitShort(ShortPacket::Kind::IoAck, 1, since, 500);
    c.waitEdges(bench::TRIG_OUT, 1, since, 200);
    size_t io = 0;
    for (const auto& s : c.shortPackets(since)) io += s.pkt.kind == ShortPacket::Kind::IoAck;
    const auto edges = c.pinEdges(bench::TRIG_OUT, since);
    const bool cmd_ok = !acks.empty() && acks[0].code == Ack::READ_OK && acks[0].data.size() == 1 &&
                        acks[0].values()[0] == id;
    // Level (one rising edge) or strobe (rising then falling).
    const bool trig_ok = io == 1 && !edges.empty() && edges[0].value == 1 &&
                         (edges.size() == 1 || (edges.size() == 2 && edges[1].value == 0));
    const std::string ack = acks.empty() ? "no acknowledgment" : ackName(acks[0].code);
    c.expect(cmd_ok, "read with a trigger inserted after %d characters: %s (0x00 with %s)", at, ack.c_str(),
             hex(id).c_str());
    c.expect(trig_ok, "the inserted trigger: %zu I/O acknowledgments, %zu recreated edges (1 rising, or a strobe)",
             io, edges.size());
    settleTrigOut(c);
}
CXP_CHECK("UVM-test_trigger_in_ctrl_packet", triggerInCtrlPacket);

}  // namespace

}  // namespace cxp::validation::checks::uvm
