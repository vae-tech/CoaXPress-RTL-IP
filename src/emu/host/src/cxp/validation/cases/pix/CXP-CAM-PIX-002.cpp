// CXP-CAM-PIX-002.  See cases/_common.h.
//
// Packing golden vectors: frames of unique pixel values go in through the
// bench pixel port in every monochrome format the XML lists, with lines of
// 1 .. 64 pixels, and every line on the link is compared word for word with
// the Figure 27-31 packing (padding included).

#include "cxp/validation/cases/pix/_helpers.h"

namespace cxp::validation::checks::pix {

namespace {

void pix002(Context& c) {
    c.needBench(bench::CAP_PIXEL, "drive the pixel port");
    uvm::usePixelPort(c);
    Feature& pf = c.need("PixelFormat");
    preserveFeature(c, "PixelFormat");
    uint32_t tag = 1;
    size_t formats = 0;
    for (const std::string& name : c.slist("formats")) {
        if (!uvm::hasEntry(&pf, name)) {
            c.note("the XML lists no PixelFormat %s; not sent", name.c_str());
            continue;
        }
        const auto code = pfncTable25(name);
        const auto bits = code ? pixelBits(*code) : std::nullopt;
        if (!code || !bits) {
            c.expect(false, "%s is not a Table 25 monochrome code", name.c_str());
            continue;
        }
        // The sensor's format is the host's PixelFormat (the camera packs what
        // the host selected); the port's metadata says the same.
        if (!trySet(c, "PixelFormat", Value::ofString(name))) {
            c.expect(false, "PixelFormat = %s not accepted", name.c_str());
            continue;
        }
        ++formats;
        std::vector<PortFrame> frames;
        for (int64_t w : c.ilist("widths")) {
            PortFrame f;
            f.img.xsize = uint32_t(w);
            f.img.ysize = uint32_t(c.iparam("lines"));
            f.img.pixfmt = *code;
            f.img.sourcetag = tag++;
            f.img.pixels = uniquePixels(size_t(f.img.xsize) * f.img.ysize, *bits, 0x55u + f.img.sourcetag);
            frames.push_back(std::move(f));
        }
        // One frame at a time: back to back, the header of a short frame
        // takes the next frame's metadata (CXP-EMU-PIX-108 shows that).
        std::vector<Captured> cap;
        for (const auto& f : frames) {
            auto one = sendPortFrames(c, {f});
            cap.insert(cap.end(), one.begin(), one.end());
        }
        const auto pk = streamPackets(cap);
        const std::string what = name + ": ";
        uvm::streamScoreboard(c, pk, -1, 0, {}, what.c_str());
        const auto ims = walkImages(pk);
        size_t ok = 0;
        for (const auto& f : frames) {
            ok += judgeImage(c, imageByTag(ims, f.img.sourcetag), f.img, *code, false,
                             strprintf("%s%u px x %u lines", what.c_str(), f.img.xsize, f.img.ysize));
        }
        c.expect(ok == frames.size(), "%s%zu of %zu frames packed as Figures 27-31 bit for bit, padding 0 (%d-bit)",
                 what.c_str(), ok, frames.size(), *bits);
    }
    c.expect(formats > 0, "%zu pixel formats sent", formats);
}
CXP_CHECK("CXP-CAM-PIX-002", pix002);

}  // namespace

}  // namespace cxp::validation::checks::pix
