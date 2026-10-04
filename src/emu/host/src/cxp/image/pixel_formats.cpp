#include "cxp/image/pixel_formats.h"

namespace cxp {

std::optional<PixelFormat> toPixelFormat(uint32_t code) {
    switch (code) {
    case 0x0101: case 0x0102: case 0x0103: case 0x0107:
    case 0x0201: case 0x0202: case 0x0203: case 0x0204:
    case 0x0301: case 0x0302:
        return static_cast<PixelFormat>(code);
    default:
        return std::nullopt;
    }
}

namespace {
struct PfncPixelF {
    uint32_t pfnc, pixel_f;
};
// The monochrome formats the device lists (Table 25 / PFNC).
constexpr PfncPixelF kPfnc[] = {{0x01080001, 0x0101}, {0x01100003, 0x0102}, {0x01100005, 0x0103},
                                {0x01100025, 0x0104}, {0x01100007, 0x0105}};
}  // namespace

uint32_t pixelFFromPfnc(uint32_t pfnc) {
    for (const auto& e : kPfnc) {
        if (e.pfnc == pfnc) return e.pixel_f;
    }
    return 0;
}

uint32_t pfncFromPixelF(uint32_t pixel_f) {
    for (const auto& e : kPfnc) {
        if (e.pixel_f == pixel_f) return e.pfnc;
    }
    return 0;
}

const char* pixelFormatName(PixelFormat fmt) {
    switch (fmt) {
    case PixelFormat::Mono8:    return "MONO8";
    case PixelFormat::Mono10:   return "MONO10";
    case PixelFormat::Mono12:   return "MONO12";
    case PixelFormat::Mono16:   return "MONO16";
    case PixelFormat::BayerRG8: return "BAYER_RG8";
    case PixelFormat::BayerGB8: return "BAYER_GB8";
    case PixelFormat::BayerGR8: return "BAYER_GR8";
    case PixelFormat::BayerBG8: return "BAYER_BG8";
    case PixelFormat::RGB8:     return "RGB8";
    case PixelFormat::BGR8:     return "BGR8";
    }
    return "?";
}

int bitsPerPixel(PixelFormat fmt) {
    if (isMono16(fmt)) return 16;
    if (isColor(fmt)) return 24;
    return 8;
}

uint64_t bytesPerFrame(PixelFormat fmt, uint64_t width, uint64_t height) {
    return (uint64_t(bitsPerPixel(fmt)) * width * height + 7) / 8;
}

bool isBayer(PixelFormat fmt) {
    auto v = static_cast<uint16_t>(fmt);
    return v >= 0x0201 && v <= 0x0204;
}

bool isColor(PixelFormat fmt) {
    return fmt == PixelFormat::RGB8 || fmt == PixelFormat::BGR8;
}

bool isMono(PixelFormat fmt) {
    auto v = static_cast<uint16_t>(fmt);
    return v >= 0x0101 && v <= 0x0107;
}

bool isMono16(PixelFormat fmt) {
    return fmt == PixelFormat::Mono10 || fmt == PixelFormat::Mono12 ||
           fmt == PixelFormat::Mono16;
}

}  // namespace cxp
