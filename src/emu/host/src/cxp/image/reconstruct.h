// Image-header codec and frame reconstruction / file output
// (cxp/image/reconstruct.py).
//
// The stream-payload protocol mirrors CXP-001-2015 §9.4 — the same wire
// format the RTL cxp_app_image_header.sv and cxp_app_line_marker.sv
// produce:
//
// * Each frame begins with a 25-word rectangular image header (Table 38):
//   K28.3, HDR_TYPE_REC (0x01), then streamid / sourcetag / xsize / xoffs /
//   ysize / yoffs / DsizeL / pixfmt / tapg / flags — every field byte 4x
//   replicated across its 32-bit word (§8.2.2.1).
// * Every line begins with a 2-word rectangular line marker (Table 39):
//   K28.3 + LINE_TYPE_REC (0x02), also byte-replicated.
// * Between markers, raw pixel bytes flow big-endian-packed (4 bytes per
//   32-bit word) in raster order.
//
// The word stream is then chopped into stream packets of DsizeP words;
// markers may straddle packet boundaries (the parser state persists).
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include <QImage>

#include "cxp/image/pixel_formats.h"
#include "cxp/protocol/constants.h"

namespace cxp {

// CXP §9.4 in-band marker constants (single-byte forms).
inline constexpr uint8_t HDR_TYPE_REC = 0x01;   // rectangular image header (25 words)
inline constexpr uint8_t HDR_TYPE_ARB = 0x03;   // arbitrary image header (16 words)
inline constexpr uint8_t LINE_TYPE_REC = 0x02;  // rectangular line marker (2 words)
inline constexpr uint8_t LINE_TYPE_ARB = 0x04;  // arbitrary line marker (11 words)

inline constexpr int REC_HDR_WORDS = 25;
inline constexpr int REC_LM_WORDS = 2;
inline constexpr int ARB_LM_WORDS = 11;

// Build a Table 38 rectangular image header as 25 on-wire words.
Words cxpImageHeaderRect(uint32_t streamid, uint32_t sourcetag, uint32_t xsize,
                         uint32_t xoffs, uint32_t ysize, uint32_t yoffs,
                         uint32_t dsizeL, uint32_t pixfmt, uint32_t tapg = 0,
                         uint32_t flags = 0);

// Build a Table 39 rectangular line marker (2 words, byte-replicated).
Words cxpLineMarkerRect();

// Per-frame descriptor produced by the parser after decoding the in-stream
// Table 38 header.  frame_id is synthesised (§9.4 has no frame counter).
struct ImageHeader {
    uint32_t frame_id = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    PixelFormat pixel_format = PixelFormat::Mono8;
    uint32_t n_payload_packets = 0;
    uint32_t x_offs = 0;  // ROI horizontal offset (Table 38 xoffs)
    uint32_t y_offs = 0;  // ROI vertical offset   (Table 38 yoffs)

    uint64_t frameBytes() const { return bytesPerFrame(pixel_format, width, height); }
};

struct ReconstructedFrame {
    ImageHeader header;
    std::vector<uint8_t> data;
    uint32_t missing_packets = 0;
    uint32_t crc_errors = 0;

    bool complete() const {
        return data.size() >= header.frameBytes() && missing_packets == 0;
    }

    // Write the frame; returns the actual path written.  .png/.bmp/.jpg go
    // through QImage; anything else (or a failed encode) is written as
    // Netpbm with the extension corrected to .pgm / .ppm.
    std::string save(const std::string& path) const;
};

// Render a reconstructed frame into a detached QImage.  Null image when the
// geometry is unknown; a short payload is zero-padded.
QImage frameToQImage(const ReconstructedFrame& frame);

// Big-endian-pack 32-bit payload words back into pixel bytes (appends).
void wordsToBytesBe(const uint32_t* words, size_t n, std::vector<uint8_t>& out);

// Inverse of wordsToBytesBe (zero-pads a short tail).
Words bytesToWordsBe(const uint8_t* data, size_t n);

}  // namespace cxp
