// UVM-test_stream_video.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void streamVideo(Context& c) {
    const bool pixel = usePixelPort(c);  // first: a bench reset clears the registers
    // UVM's 64-word packets (PKT_DSIZE_P = 64, 8 words of framing).
    const uint32_t spsm = uint32_t(c.iparam("spsm"));
    c.prepareStreaming();
    c.preserve(Reg::STREAM_PACKET_SIZE_MAX);
    c.wr32(Reg::STREAM_PACKET_SIZE_MAX, spsm);
    if (pixel) {
        std::mt19937 rng(c.seed(0xCAFE));
        const std::vector<PixelImage> sent = randomImages(c, rng, size_t(c.iparam("frames")));
        for (const auto& s : sent) c.info("frame %ux%u, %u/1000 of the cycles valid", s.xsize, s.ysize, s.valid_permille);
        injectedScoreboard(c, streamPackets(injectImages(c, sent)), sent, false, spsm);
        return;
    }
    const int pat = selectBars(c);
    preserveFeature(c, "Width");
    preserveFeature(c, "Height");
    std::mt19937 rng(c.seed(1));
    const auto wr = c.ilist("width_range"), hr = c.ilist("height_range");
    for (int i = 0; i < c.iparam("geometries"); ++i) {
        const int64_t w = std::uniform_int_distribution<int64_t>(wr[0], wr[1])(rng);
        const int64_t h = std::uniform_int_distribution<int64_t>(hr[0], hr[1])(rng);
        if (!trySet(c, "Width", Value::ofInt(w)) || !trySet(c, "Height", Value::ofInt(h))) {
            c.expect(false, "geometry %lld x %lld not accepted", (long long)w, (long long)h);
            continue;
        }
        const std::string tag = strprintf("%lldx%lld: ", (long long)w, (long long)h);
        const auto pk = streamPackets(c.acquire(c.iparam("images")));
        const auto sb = streamScoreboard(c, pk, pat, spsm, {}, tag.c_str());
        size_t geo_bad = 0;
        const auto ims = walkImages(pk);
        for (const auto& im : ims) geo_bad += im.xsize != w || im.ysize != h;
        c.expect(!ims.empty() && geo_bad == 0, "%s%zu of %zu headers show the programmed geometry", tag.c_str(),
                 ims.size() - geo_bad, ims.size());
        c.expect(sb.complete >= 1, "%s%zu complete images", tag.c_str(), sb.complete);
    }
}
CXP_CHECK("UVM-test_stream_video", streamVideo);

}  // namespace

}  // namespace cxp::validation::checks::uvm
