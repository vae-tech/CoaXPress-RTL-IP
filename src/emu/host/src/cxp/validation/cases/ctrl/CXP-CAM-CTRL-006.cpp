// CXP-CAM-CTRL-006.  See cases/_common.h.

#include "cxp/validation/cases/ctrl/_helpers.h"

namespace cxp::validation::checks::ctrl {

namespace {

void ctrl006(Context& c) {
    auto a = c.exchange(resetCmd());
    c.expect(is(a, Ack::RESET_OK) && !a->long_form, "idle control channel reset: %s", ackStr(a).c_str());
    c.expect(is(c.readRaw(Reg::STANDARD, 4), Ack::READ_OK), "next read succeeds");
    Words trunc = readCmd(Reg::STANDARD, 4);
    trunc.resize(5);
    auto acks = c.exchangeMany({trunc, resetCmd()}, 2, 500);
    const bool got_reset = std::any_of(acks.begin(), acks.end(), [](const RawAck& x) { return x.code == Ack::RESET_OK; });
    c.expect(got_reset, "reset after a truncated command: %zu acks, %s", acks.size(),
             acks.empty() ? "none" : ackName(acks.back().code).c_str());
    c.expect(is(c.readRaw(Reg::STANDARD, 4), Ack::READ_OK), "next read succeeds");

    c.prepareStreaming();
    const uint32_t cc = c.rd32(Reg::CONNECTION_CONFIG), mh = c.rd32(Reg::MASTER_HOST_CONNECTION_ID),
                   sp = c.rd32(Reg::STREAM_PACKET_SIZE_MAX);
    c.startRecording();
    const size_t h0 = c.headersSeen();
    c.acqStart();
    c.waitHeaders(h0 + 1, c.opt().first_image_timeout_ms);
    a = c.exchange(resetCmd());
    const double t_reset = c.nowMs();
    c.sleepMs(c.iparam("after_ms"));
    c.acqStop();
    c.waitQuiet();
    auto pk = streamPackets(c.stopRecording());
    c.expect(is(a, Ack::RESET_OK), "reset while streaming: %s", ackStr(a).c_str());
    c.expect(c.rd32(Reg::CONNECTION_CONFIG) == cc && c.rd32(Reg::MASTER_HOST_CONNECTION_ID) == mh &&
                 c.rd32(Reg::STREAM_PACKET_SIZE_MAX) == sp,
             "ConnectionConfig, MasterHostConnectionID and StreamPacketSizeMax unchanged");
    size_t after = 0;
    for (const auto& p : pk) after += p.t_ms > t_reset;
    c.expect(after > 0, "%zu stream packets after the reset (stream not disrupted)", after);
    if (!pk.empty()) {
        const auto tags = pktTags(pk, pk.front().stream_id);
        c.expect(tagBreaks(tags) == 0, "packet tags continuous across the reset (%zu breaks)", tagBreaks(tags));
    }
}
CXP_CHECK("CXP-CAM-CTRL-006", ctrl006);

}  // namespace

}  // namespace cxp::validation::checks::ctrl
