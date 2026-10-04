// CXP-CAM-REC-006.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::rec {

namespace {

void rec006(Context& c) {
    preserveLink(c);
    c.prepareStreaming();
    const size_t h0 = c.headersSeen();
    c.acqStart();
    c.waitHeaders(h0 + 1, c.opt().first_image_timeout_ms);
    size_t nack = 0;
    const int commands = c.iparam("commands");
    for (int i = 0; i < commands; ++i) {
        CmdSpec s;
        s.opcode = 0x00;
        s.size_bytes = 4;
        s.address = Reg::STANDARD;
        s.corrupt_crc = true;
        nack += is(c.exchange(buildCmd(s)), Ack::CRC_ERROR);
    }
    c.expect(nack == size_t(commands), "%zu of %d corrupted commands answered 0x80 during streaming", nack, commands);
    c.expect(streamContinues(c, 200), "stream continues after the corrupted commands");
    auto a = c.exchange(resetCmd());
    c.expect(is(a, Ack::RESET_OK), "control channel reset during streaming: %s", ackStr(a).c_str());
    c.expect(streamContinues(c, 200), "stream continues after the control channel reset");
    c.acqStop();
    c.waitQuiet();
    c.wr32(Reg::CONNECTION_CONFIG, c.rd32(Reg::CONNECTION_CONFIG));
    auto pk = streamPackets(c.acquire(c.iparam("images")));
    auto ims = completeImages(walkImages(pk));
    c.expect(!ims.empty(), "stop -> ConnectionConfig rewrite -> start: %zu complete images", ims.size());
    c.expect(!pk.empty() && pk.front().tag == 0, "first packet tag after the rewrite = %u (0)", pk.empty() ? 0 : pk.front().tag);
}
CXP_CHECK("CXP-CAM-REC-006", rec006);

}  // namespace

}  // namespace cxp::validation::checks::rec
