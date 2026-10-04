// CXP-CAM-PROT-004.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::prot {

namespace {

void prot004(Context& c) {
    auto a = c.readRaw(Reg::STANDARD, 4);
    if (c.expect(is(a, Ack::READ_OK) && !a->data.empty(), "read Standard: %s", ackStr(a).c_str())) {
        c.expect((a->data[0] & 0xFF) == 0xC0, "Standard P0 = 0x%02X (0xC0)", a->data[0] & 0xFF);
    }
    c.preserve(Reg::MASTER_HOST_CONNECTION_ID);
    c.wr32(Reg::MASTER_HOST_CONNECTION_ID, 0x11223344);
    a = c.readRaw(Reg::MASTER_HOST_CONNECTION_ID, 4);
    if (c.expect(is(a, Ack::READ_OK) && !a->data.empty(), "read MasterHostConnectionID: %s", ackStr(a).c_str())) {
        const uint32_t w = a->data[0];
        c.expect(w == 0x44332211, "wire characters P0..P3 = %02X %02X %02X %02X (11 22 33 44)", w & 0xFF,
                 (w >> 8) & 0xFF, (w >> 16) & 0xFF, w >> 24);
    }
    auto width = featInt(c, "Width"), height = featInt(c, "Height");
    auto cap = c.acquire(c.iparam("images"));
    auto pk = streamPackets(cap);
    size_t dsize_bad = 0;
    for (const auto& p : pk) dsize_bad += p.dsize_p != p.payload.size();
    c.expect(!pk.empty() && dsize_bad == 0, "DsizeP decoded big-endian equals the payload length in %zu of %zu packets",
             pk.size() - dsize_bad, pk.size());
    auto ims = imagesOf(cap);
    if (ims.empty()) c.abort("no image header received");
    if (width && height) {
        c.expect(ims[0].xsize == *width && ims[0].ysize == *height,
                 "header Xsize/Ysize decoded big-endian = %u x %u (Width/Height %lld x %lld)", ims[0].xsize,
                 ims[0].ysize, (long long)*width, (long long)*height);
    }
}
CXP_CHECK("CXP-CAM-PROT-004", prot004);

}  // namespace

}  // namespace cxp::validation::checks::prot
