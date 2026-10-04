#include "cxp/image/test_pattern.h"

#include <algorithm>

namespace cxp {

namespace {

// 100%-level colour-bar palette (R, G, B): white, yellow, cyan, green,
// magenta, red, blue, black.
const uint8_t kColourBars[8][3] = {
    {0xFF, 0xFF, 0xFF}, {0xFF, 0xFF, 0x00}, {0x00, 0xFF, 0xFF}, {0x00, 0xFF, 0x00},
    {0xFF, 0x00, 0xFF}, {0xFF, 0x00, 0x00}, {0x00, 0x00, 0xFF}, {0x00, 0x00, 0x00},
};

// Seven lower GreyBars band levels; the eighth (top) band is 0xFF.  Mirrors
// the RTL greybar_val lookup in cxp_app_tpg.sv.
const uint8_t kGreyBars[7] = {0x10, 0x30, 0x50, 0x70, 0x90, 0xC0, 0xE0};

// Column band 0..7 — the line split into exactly 8 equal-width bands (w // 8
// wide); a line narrower than 8 px reads band 7.  Mirrors RTL band_idx.
uint32_t bandIndex(uint32_t x, uint32_t w) {
    uint32_t bw = w / 8;
    if (bw == 0) return 7;
    return std::min<uint32_t>(x / bw, 7);
}

uint8_t greyBand(uint32_t x, uint32_t w) {
    uint32_t band = bandIndex(x, w);
    return band < 7 ? kGreyBars[band] : 0xFF;
}

}  // namespace

const char* testPatternName(uint32_t pattern) {
    switch (pattern) {
    case TPG_GRADIENT: return "Gradient";
    case TPG_BARS: return "Bars";
    case TPG_FLAT: return "Flat";
    case TPG_GREYBARS: return "GreyBars";
    default: return "";
    }
}

std::optional<uint32_t> testPatternId(const std::string& name) {
    for (uint32_t p : {TPG_GRADIENT, TPG_BARS, TPG_FLAT, TPG_GREYBARS}) {
        if (name == testPatternName(p)) return p;
    }
    return std::nullopt;
}

bool testPatternStatic(uint32_t pattern) { return pattern == TPG_BARS || pattern == TPG_GREYBARS; }

std::vector<uint8_t> renderTestPattern(PixelFormat fmt, uint32_t w, uint32_t h,
                                       uint32_t pat, uint32_t fid) {
    const uint64_t n = bytesPerFrame(fmt, w, h);
    std::vector<uint8_t> buf(n, 0);
    if (isColor(fmt)) {
        const bool bgr = fmt == PixelFormat::BGR8;
        for (uint32_t y = 0; y < h; ++y) {
            for (uint32_t x = 0; x < w; ++x) {
                uint8_t r, g, b;
                if (pat == TPG_BARS) {
                    const uint8_t* c = kColourBars[bandIndex(x, w)];
                    r = c[0], g = c[1], b = c[2];
                } else if (pat == TPG_FLAT) {
                    r = g = b = static_cast<uint8_t>(fid * 16);
                } else if (pat == TPG_GREYBARS) {
                    r = g = b = greyBand(x, w);
                } else {  // diagonal RGB gradient
                    r = static_cast<uint8_t>(x + fid);
                    g = static_cast<uint8_t>(y);
                    b = static_cast<uint8_t>(x ^ y);
                }
                uint64_t o = (uint64_t(y) * w + x) * 3;
                buf[o] = bgr ? b : r;
                buf[o + 1] = g;
                buf[o + 2] = bgr ? r : b;
            }
        }
        return buf;
    }
    // mono (8-bit container model is enough for the reference sim; the rest
    // of a 16-bit container frame stays zero)
    for (uint32_t y = 0; y < h; ++y) {
        for (uint32_t x = 0; x < w; ++x) {
            uint8_t v;
            if (pat == TPG_BARS) {
                v = (bandIndex(x, w) & 1) ? 0xFF : 0x00;
            } else if (pat == TPG_FLAT) {
                v = static_cast<uint8_t>(fid * 16);
            } else if (pat == TPG_GREYBARS) {
                v = greyBand(x, w);
            } else {
                v = static_cast<uint8_t>(x + y + fid);
            }
            buf[uint64_t(y) * w + x] = v;
        }
    }
    return buf;
}

}  // namespace cxp
