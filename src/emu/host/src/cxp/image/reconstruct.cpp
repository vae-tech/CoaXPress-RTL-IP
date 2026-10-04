#include "cxp/image/reconstruct.h"

#include <algorithm>
#include <cstdio>
#include <stdexcept>

#include "cxp/protocol/crc.h"

namespace cxp {

namespace {

bool endsWith(const std::string& s, const std::string& suf) {
    return s.size() >= suf.size() &&
           s.compare(s.size() - suf.size(), suf.size(), suf) == 0;
}

std::string replaceExt(const std::string& path, const std::string& ext) {
    size_t slash = path.find_last_of('/');
    size_t dot = path.find_last_of('.');
    if (dot == std::string::npos || (slash != std::string::npos && dot < slash)) {
        return path + ext;
    }
    return path.substr(0, dot) + ext;
}

std::string writeNetpbm(const std::string& path, const char* magic, uint32_t w,
                        uint32_t h, int maxval, const std::vector<uint8_t>& body) {
    FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) throw std::runtime_error("cannot open " + path);
    std::fprintf(f, "%s\n%u %u\n%d\n", magic, w, h, maxval);
    std::fwrite(body.data(), 1, body.size(), f);
    std::fclose(f);
    return path;
}

}  // namespace

Words cxpImageHeaderRect(uint32_t streamid, uint32_t sourcetag, uint32_t xsize,
                         uint32_t xoffs, uint32_t ysize, uint32_t yoffs,
                         uint32_t dsizeL, uint32_t pixfmt, uint32_t tapg,
                         uint32_t flags) {
    const uint8_t bs[REC_HDR_WORDS] = {
        K28_3,                                                    // 0
        HDR_TYPE_REC,                                             // 1
        uint8_t(streamid),                                        // 2
        uint8_t(sourcetag >> 8), uint8_t(sourcetag),              // 3..4
        uint8_t(xsize >> 16), uint8_t(xsize >> 8), uint8_t(xsize),  // 5..7
        uint8_t(xoffs >> 16), uint8_t(xoffs >> 8), uint8_t(xoffs),  // 8..10
        uint8_t(ysize >> 16), uint8_t(ysize >> 8), uint8_t(ysize),  // 11..13
        uint8_t(yoffs >> 16), uint8_t(yoffs >> 8), uint8_t(yoffs),  // 14..16
        0x00,                                                     // 17 DsizeL[23:16]
        uint8_t(dsizeL >> 8), uint8_t(dsizeL),                    // 18..19
        uint8_t(pixfmt >> 8), uint8_t(pixfmt),                    // 20..21
        uint8_t(tapg >> 8), uint8_t(tapg),                        // 22..23
        uint8_t(flags),                                           // 24
    };
    Words out;
    out.reserve(REC_HDR_WORDS);
    for (uint8_t b : bs) out.push_back(replicateByte(b));
    return out;
}

Words cxpLineMarkerRect() {
    return {replicateByte(K28_3), replicateByte(LINE_TYPE_REC)};
}

std::string ReconstructedFrame::save(const std::string& path) const {
    const PixelFormat fmt = header.pixel_format;
    const uint32_t w = header.width, h = header.height;
    std::vector<uint8_t> buf(data.begin(),
                             data.begin() + std::min<uint64_t>(data.size(), header.frameBytes()));
    bool netpbm_color = isColor(fmt) || isBayer(fmt);
    std::string out = path;
    if (endsWith(path, ".png") || endsWith(path, ".bmp") || endsWith(path, ".jpg")) {
        QImage img = frameToQImage(*this);
        if (!img.isNull() && img.save(QString::fromStdString(path))) return path;
        out = replaceExt(path, netpbm_color ? ".ppm" : ".pgm");
    }
    if (isColor(fmt)) return writeNetpbm(out, "P6", w, h, 255, buf);
    // Bayer mosaics are written as raw 8-bit grey for a portable view.
    // Netpbm 16-bit is big-endian, which matches the big-endian-packed words.
    return writeNetpbm(out, "P5", w, h, isMono16(fmt) ? 65535 : 255, buf);
}

QImage frameToQImage(const ReconstructedFrame& frame) {
    const ImageHeader& hdr = frame.header;
    const int w = static_cast<int>(hdr.width), h = static_cast<int>(hdr.height);
    if (w <= 0 || h <= 0) return {};
    const PixelFormat fmt = hdr.pixel_format;
    const uint64_t need = hdr.frameBytes();
    std::vector<uint8_t> buf(need, 0);
    std::copy(frame.data.begin(),
              frame.data.begin() + std::min<uint64_t>(need, frame.data.size()),
              buf.begin());

    if (isColor(fmt)) {
        QImage img(buf.data(), w, h, w * 3, QImage::Format_RGB888);
        return fmt == PixelFormat::BGR8 ? img.rgbSwapped() : img.copy();
    }
    if (isMono16(fmt)) {
        // Pixel words are big-endian-packed; QImage wants host-native samples.
        for (size_t i = 0; i + 1 < buf.size(); i += 2) std::swap(buf[i], buf[i + 1]);
        QImage img(buf.data(), w, h, w * 2, QImage::Format_Grayscale16);
        return img.copy();
    }
    // MONO8 and the Bayer mosaics both display as raw 8-bit grey.
    QImage img(buf.data(), w, h, w, QImage::Format_Grayscale8);
    return img.copy();
}

void wordsToBytesBe(const uint32_t* words, size_t n, std::vector<uint8_t>& out) {
    out.reserve(out.size() + n * 4);
    for (size_t i = 0; i < n; ++i) {
        uint32_t w = words[i];
        out.push_back(uint8_t(w >> 24));
        out.push_back(uint8_t(w >> 16));
        out.push_back(uint8_t(w >> 8));
        out.push_back(uint8_t(w));
    }
}

Words bytesToWordsBe(const uint8_t* data, size_t n) {
    Words out((n + 3) / 4, 0);
    for (size_t i = 0; i < n; ++i) {
        out[i / 4] |= uint32_t(data[i]) << (8 * (3 - i % 4));
    }
    return out;
}

}  // namespace cxp
