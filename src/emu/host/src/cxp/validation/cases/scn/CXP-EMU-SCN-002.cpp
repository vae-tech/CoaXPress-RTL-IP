// CXP-EMU-SCN-002.  See cases/_common.h.
//
// Resets of every kind while the device streams.  A control channel reset
// (0xFF, §8.6.1.2) is not a Device reset: the stream goes on, tags +1,
// registers kept.  A ConnectionReset (§10.3.28) and a power-on reset of any
// one clock domain (the bench's reset inputs; the device resets as a whole)
// leave StreamPacketSizeMax 0, so no data packet follows until the Host
// programs it again; then the first packet carries tag 0 and opens a fresh
// image: nothing from before the reset is sent after it.

#include "cxp/validation/cases/data/_helpers.h"

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::scn {

namespace {

constexpr uint32_t kMhcid = 0x0BADF00D;

// §9.4: 4 x K28.3 then 4 x 0x01 / 0x03, an image header.
bool opensWithHeader(const StreamPkt& p) {
    return p.payload.size() >= 2 && p.payload[0] == replicateByte(K28_3) &&
           (p.payload[1] == replicateByte(0x01) || p.payload[1] == replicateByte(0x03));
}

// After a Device reset: nothing on the downlink while SPSM is 0, the reset
// values, then the stream programmed again from scratch.
void afterDeviceReset(Context& c, const std::string& what) {
    const auto quiet = streamPackets(c.record(c.iparam("quiet_ms")));
    c.expect(quiet.empty(), "%s: %zu stream packets in the %d ms after it (none: StreamPacketSizeMax is 0)",
             what.c_str(), quiet.size(), c.iparam("quiet_ms"));
    const auto spsm = c.tryRd32(Reg::STREAM_PACKET_SIZE_MAX), mh = c.tryRd32(Reg::MASTER_HOST_CONNECTION_ID);
    c.expect(spsm == 0u && mh == 0u, "%s: StreamPacketSizeMax %s, MasterHostConnectionID %s (0, 0)", what.c_str(),
             spsm ? hex(*spsm).c_str() : "unread", mh ? hex(*mh).c_str() : "unread");
    c.wr32(Reg::MASTER_HOST_CONNECTION_ID, kMhcid);
    c.wr32(Reg::STREAM_PACKET_SIZE_MAX, c.opt().host_spsm);
    c.startRecording();
    std::vector<Captured> cap;
    try {
        c.acqStart();
        const size_t h = c.headersSeen();
        if (c.waitHeaders(h + size_t(c.iparam("images")), c.iparam("images_timeout_ms"))) c.waitTail(c.headersSeen());
        cap = c.takeRecording();
        c.stopRecording();
    } catch (...) {
        c.stopRecording();
        throw;
    }
    const auto pk = streamPackets(cap);
    if (!c.expect(!pk.empty(), "%s: the stream comes back once programmed (%zu packets)", what.c_str(), pk.size())) {
        return;
    }
    c.expect(pk.front().tag == 0 && opensWithHeader(pk.front()),
             "%s: the first packet after it has tag %u and %s (tag 0, an image header first)", what.c_str(),
             pk.front().tag, opensWithHeader(pk.front()) ? "opens an image" : "does not open an image");
    const auto sb = uvm::streamScoreboard(c, pk, -1, c.opt().host_spsm, {}, (what + ": ").c_str());
    c.expect(sb.complete >= size_t(c.iparam("images")) - 1, "%s: %zu complete images after it", what.c_str(),
             sb.complete);
}

void scn002(Context& c) {
    data::smallTpg(c, uint32_t(c.iparam("width")), uint32_t(c.iparam("height")), c.opt().host_spsm);
    c.wr32(Reg::MASTER_HOST_CONNECTION_ID, kMhcid);

    // Control channel resets while streaming.
    c.startRecording();
    std::vector<Captured> cap;
    try {
        c.acqStart();
        if (!c.waitHeaders(c.headersSeen() + 1, c.opt().first_image_timeout_ms)) c.abort("no image header");
        size_t ok = 0;
        for (int i = 0; i < c.iparam("control_resets"); ++i) {
            auto a = c.exchange(resetCmd());
            ok += is(a, Ack::RESET_OK) && !a->long_form;
        }
        c.expect(ok == size_t(c.iparam("control_resets")), "%zu of %d control channel resets answered 0x03", ok,
                 c.iparam("control_resets"));
        Words trunc = readCmd(Reg::STANDARD, 4);
        trunc.resize(4);
        const auto acks = c.exchangeMany({trunc, resetCmd()}, 2, c.iparam("ack_window_ms"));
        c.expect(!acks.empty() && acks.back().code == Ack::RESET_OK,
                 "0xFF after a truncated command: %zu acknowledgments, the last %s (0x03)", acks.size(),
                 acks.empty() ? "none" : ackName(acks.back().code).c_str());
        const size_t h = c.headersSeen();
        if (c.waitHeaders(h + size_t(c.iparam("images")), c.iparam("images_timeout_ms"))) c.waitTail(c.headersSeen());
        cap = c.takeRecording();
        c.stopRecording();
    } catch (...) {
        c.stopRecording();
        throw;
    }
    const auto pk = streamPackets(cap);
    uvm::streamScoreboard(c, pk, -1, c.opt().host_spsm, {}, "control channel resets: ");
    c.expect(c.rd32(Reg::MASTER_HOST_CONNECTION_ID) == kMhcid && c.rd32(Reg::STREAM_PACKET_SIZE_MAX) == c.opt().host_spsm,
             "control channel resets: MasterHostConnectionID and StreamPacketSizeMax kept (not a Device reset)");

    // ConnectionReset while streaming.
    connectionReset(c);
    afterDeviceReset(c, "ConnectionReset while streaming");

    // One clock domain's reset inputs while streaming.
    const auto pins = c.benchPins();
    const bool domains = (c.benchCaps() & bench::CAP_RESET_DOMAINS) && pins;
    if (!domains) c.note("the bench cannot reset one clock domain: the domain resets are not run");
    for (uint32_t d : {1u, 2u, 4u}) {
        if (!domains) break;
        const char* name = d == 1 ? "app" : d == 2 ? "tx" : "rx";
        c.expect(c.benchReset(*pins, 5000, d), "%s-domain reset while streaming: the device answers afterwards", name);
        afterDeviceReset(c, strprintf("%s-domain reset while streaming", name));
    }

    // No phantom reset later: the registers written after the last reset stay.
    const auto late = streamPackets(c.record(c.iparam("quiet_ms")));
    uvm::streamScoreboard(c, late, -1, c.opt().host_spsm, {}, "afterwards: ");
    c.expect(c.rd32(Reg::MASTER_HOST_CONNECTION_ID) == kMhcid, "afterwards: MasterHostConnectionID still %s",
             hex(kMhcid).c_str());
    c.acqStop();
    c.waitQuiet();
}
CXP_CHECK("CXP-EMU-SCN-002", scn002);

}  // namespace

}  // namespace cxp::validation::checks::scn
