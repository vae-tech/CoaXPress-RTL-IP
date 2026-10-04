// CXP pixel-format catalogue, subset of GenICam PFNC §13
// (cxp/image/pixel_formats.py).
#pragma once

#include <cstdint>
#include <optional>
#include <string>

namespace cxp {

enum class PixelFormat : uint16_t {
    Mono8 = 0x0101,
    Mono10 = 0x0102,
    Mono12 = 0x0103,
    Mono16 = 0x0107,
    BayerRG8 = 0x0201,
    BayerGB8 = 0x0202,
    BayerGR8 = 0x0203,
    BayerBG8 = 0x0204,
    RGB8 = 0x0301,
    BGR8 = 0x0302,
};

std::optional<PixelFormat> toPixelFormat(uint32_t code);

// §11.2.1.6: the PixelFormat feature holds the GenICam PFNC value; the
// device sends the matching Table 25 PixelF code.  0 = no PixelF code.
uint32_t pixelFFromPfnc(uint32_t pfnc);
uint32_t pfncFromPixelF(uint32_t pixel_f);
const char* pixelFormatName(PixelFormat fmt);  // "MONO8", "BAYER_RG8", ...

int bitsPerPixel(PixelFormat fmt);  // Mono10/12/16 unpacked into 16-bit containers
uint64_t bytesPerFrame(PixelFormat fmt, uint64_t width, uint64_t height);
bool isBayer(PixelFormat fmt);
bool isColor(PixelFormat fmt);
bool isMono(PixelFormat fmt);
bool isMono16(PixelFormat fmt);  // Mono10 / Mono12 / Mono16

}  // namespace cxp
