// UVM-test_arbitrary_image.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void arbitraryImage(Context& c) {
    c.needBench(bench::CAP_PIXEL | bench::CAP_ARBITRARY, "drive the pixel port with arbitrary-image framing");
    usePixelPort(c);
    c.benchPin(bench::ARBITRARY, 1);
    c.benchSync();
    std::mt19937 rng(c.seed(0xCAFE));
    std::vector<PixelImage> sent = randomImages(c, rng, size_t(c.iparam("frames")));
    for (auto& im : sent) {
        im.xoffs = uint32_t(c.iparam("xoffs"));
        c.info("frame %ux%u with Xoffs %u, arbitrary framing", im.xsize, im.ysize, im.xoffs);
    }
    injectedScoreboard(c, streamPackets(injectImages(c, sent)), sent, true, 0);
    c.note("every line has the frame's Xsize and Xoffs, as UVM drives it; REQ-IMG-013 says a rectangular image "
           "should not use the arbitrary form, so this is a stimulus choice, not a device fault");
}
CXP_CHECK("UVM-test_arbitrary_image", arbitraryImage);

}  // namespace

}  // namespace cxp::validation::checks::uvm
