// CXP-CAM-BOOT-007.  See cases/_common.h.

#include "cxp/image/pixel_formats.h"
#include "cxp/validation/cases/boot/_helpers.h"

namespace cxp::validation::checks::boot {

namespace {

void boot007(Context& c) {
    uint32_t ptr[8];
    for (int i = 0; i < 8; ++i) {
        ptr[i] = c.rd32(kUseCases[i].addr);
        if (ptr[i] == 0) c.abort(strprintf("%s is 0; the bootstrap-only use case cannot run", kUseCases[i].reg));
    }
    const uint32_t w = c.rd32(ptr[0]), h = c.rd32(ptr[1]), pf = c.rd32(ptr[5]), sid = c.rd32(ptr[7]);
    // §11.2.1.6: PixelFormat holds the PFNC value; the image header carries
    // the PixelF code the device maps it to.
    const uint32_t pixel_f = pixelFFromPfnc(pf);
    c.info("via bootstrap addresses: Width %u, Height %u, PixelFormat 0x%08X (PixelF 0x%04X), Image1StreamID %u", w,
           h, pf, pixel_f, sid);
    c.expect(pixel_f != 0, "PixelFormat 0x%08X is the PFNC value of a Table 25 format", pf);
    c.preserve(Reg::STREAM_PACKET_SIZE_MAX);
    if (c.rd32(Reg::STREAM_PACKET_SIZE_MAX) == 0) c.wr32(Reg::STREAM_PACKET_SIZE_MAX, c.opt().host_spsm);
    c.startRecording();
    const size_t h0 = c.headersSeen();
    c.wr32(ptr[3], 1);
    c.onExit([&c, stop = ptr[4]] { c.writeRaw(stop, {1}); });
    c.waitHeaders(h0 + size_t(c.iparam("images")), c.iparam("acquire_timeout_ms"));
    c.wr32(ptr[4], 1);
    // §11.2.1.5: the image in flight completes; no new image begins.  An
    // image the device began before the stop write reached it may still
    // show its header within the host's command latency.
    c.sleepMs(100);
    const size_t h_stop = c.headersSeen();
    const bool quiet = c.waitQuiet();
    c.sleepMs(c.iparam("after_ms"));
    auto cap = c.stopRecording();
    auto ims = completeImages(imagesOf(cap));
    c.expect(ims.size() >= size_t(c.iparam("min_images")), "%zu complete images after writing 1 to the AcquisitionStart "
             "target (>= %d)", ims.size(), c.iparam("min_images"));
    size_t mismatch = 0;
    for (const auto& im : ims) mismatch += im.xsize != w || im.ysize != h || im.pixel_f != pixel_f || im.stream_id != sid;
    c.expect(!ims.empty() && mismatch == 0, "%zu images disagree with Width/Height/PixelFormat/Image1StreamID",
             mismatch);
    const size_t late = c.headersSeen() - h_stop;
    c.expect(quiet && late == 0, "stop effective: %zu images begun after the stop write, link %s", late,
             quiet ? "quiet" : "still streaming");
}
CXP_CHECK("CXP-CAM-BOOT-007", boot007);

}  // namespace

}  // namespace cxp::validation::checks::boot
