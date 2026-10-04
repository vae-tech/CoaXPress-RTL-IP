// UVM-test_stream_video_ragged.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

// Frames whose last packet is 1, 2 and DsizeP - 1 words long, first at the
// UVM test's 64-word packets, then at 16-word packets (StreamPacketSizeMax =
// 4 x (payload + 8) bytes).
void streamVideoRagged(Context& c) {
    c.needBench(bench::CAP_PIXEL, "send frames of chosen sizes into the pixel port");
    usePixelPort(c);
    c.prepareStreaming();
    c.preserve(Reg::STREAM_PACKET_SIZE_MAX);
    const std::vector<std::pair<uint32_t, std::vector<std::pair<uint32_t, uint32_t>>>> rounds = {
        {uint32_t(c.iparam("spsm_64")), {{4, 4}, {5, 4}, {29, 4}, {49, 7}, {57, 6}}},
        {uint32_t(c.iparam("spsm_16")), {{21, 1}, {25, 1}, {13, 1}}},
    };
    for (const auto& [spsm, sizes] : rounds) {
        c.wr32(Reg::STREAM_PACKET_SIZE_MAX, spsm);
        std::vector<PixelImage> sent;
        for (const auto& [x, y] : sizes) {
            sent.push_back(rampImage(x, y));
            // The scoreboard pairs images with frames by SourceTag.
            sent.back().sourcetag = uint32_t(sent.size() - 1);
        }
        const std::string what = strprintf("SPSM %u: ", spsm);
        injectedScoreboard(c, streamPackets(injectImages(c, sent)), sent, false, spsm, what.c_str());
    }
}
CXP_CHECK("UVM-test_stream_video_ragged", streamVideoRagged);

}  // namespace

}  // namespace cxp::validation::checks::uvm
