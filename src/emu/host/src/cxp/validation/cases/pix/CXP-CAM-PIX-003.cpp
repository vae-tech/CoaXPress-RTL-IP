// CXP-CAM-PIX-003.  See cases/_common.h.
//
// In-between widths (§9.4.2, REQ-PIX-004): a 9/11/13/15-bit sensor is sent in
// the next larger container, MSB-aligned.  The device's pixel port takes
// container-width values, so the alignment itself is the integrator's (the
// sensor side of the port); what the device is held to is that such values
// arrive MSB-aligned: the sensor's bits in the container's top bits, the
// unused low bits 0, full scale and a ramp intact.

#include "cxp/validation/cases/pix/_helpers.h"

namespace cxp::validation::checks::pix {

namespace {

void pix003(Context& c) {
    c.needBench(bench::CAP_PIXEL, "drive the pixel port");
    uvm::usePixelPort(c);
    Feature& pf = c.need("PixelFormat");
    preserveFeature(c, "PixelFormat");
    const uint32_t w = uint32_t(c.iparam("width")), h = uint32_t(c.iparam("lines"));
    uint32_t tag = 200;
    size_t depths = 0;
    for (int64_t d : c.ilist("depths")) {
        const int depth = int(d), bits = depth + 1;  // the next container: 10, 12, 14, 16
        const std::string name = strprintf("Mono%d", bits);
        if (!uvm::hasEntry(&pf, name) || !trySet(c, "PixelFormat", Value::ofString(name))) {
            c.note("%d-bit sensor: the XML offers no %s; not sent", depth, name.c_str());
            continue;
        }
        ++depths;
        const uint32_t code = *pfncTable25(name);
        PortFrame f;
        f.img.xsize = w;
        f.img.ysize = h;
        f.img.pixfmt = code;
        f.img.sourcetag = tag++;
        // Sensor values: full scale, zero, then a ramp over the whole range;
        // MSB-aligned into the container (shifted up by one bit).
        std::vector<uint32_t> sensor;
        for (uint32_t i = 0; i < w * h; ++i) {
            const uint32_t full = (1u << depth) - 1;
            sensor.push_back(i == 0 ? full : i == 1 ? 0 : uint32_t((uint64_t(i) * full) / (w * h - 1)));
        }
        for (uint32_t v : sensor) f.img.pixels.push_back(uint16_t(v << (bits - depth)));
        const auto pk = streamPackets(sendPortFrames(c, {f}));
        const auto ims = walkImages(pk);
        const std::string what = strprintf("%d-bit sensor in %s: ", depth, name.c_str());
        judgeImage(c, imageByTag(ims, f.img.sourcetag), f.img, code, false, what + "frame");
        const ImageRec* im = imageByTag(ims, f.img.sourcetag);
        if (!im || im->lines.size() != h) continue;
        size_t msb = 0, lsb_zero = 0, n = 0;
        for (uint32_t y = 0; y < h; ++y) {
            const auto got = uvm::unpackLine(im->lines[y], w, bits);
            for (uint32_t x = 0; x < w; ++x, ++n) {
                msb += (uint32_t(got[x]) >> (bits - depth)) == sensor[size_t(y) * w + x];
                lsb_zero += (got[x] & ((1u << (bits - depth)) - 1)) == 0;
            }
        }
        c.expect(msb == n && lsb_zero == n,
                 "%s%zu of %zu containers carry the sensor value in their top %d bits, %zu of %zu with the low bit 0 "
                 "(MSB-aligned, not LSB-aligned)",
                 what.c_str(), msb, n, depth, lsb_zero, n);
    }
    c.expect(depths > 0, "%zu in-between sensor depths sent", depths);
}
CXP_CHECK("CXP-CAM-PIX-003", pix003);

}  // namespace

}  // namespace cxp::validation::checks::pix
