// Helpers the pix/ cases (and the image and scenario cases that drive the
// pixel port) share: frames through the bench pixel port, well formed
// (PIXEL_FRAME) or with their framing given beat by beat (PIXEL_BEATS), and
// the §9.4.2 golden packing.
#pragma once

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::pix {

// One frame for the pixel port: `beats` empty sends img as PIXEL_FRAME (the
// bench frames it: SOF on the first pixel, EOL every Xsize, EOF on the last);
// otherwise the beats go as PIXEL_BEATS with img's metadata.
struct PortFrame {
    PixelImage img;
    std::vector<uint32_t> beats;
};

inline constexpr uint32_t kSof = 1u << 16, kEol = 1u << 17, kEof = 1u << 18;

// The beats of a well-formed frame (what PIXEL_FRAME would send).
std::vector<uint32_t> wellFormedBeats(const PixelImage& im);

// Unique pixel values for a frame: every pixel differs from its neighbours in
// its top and bottom bits, the MSB of the container is used, and value
// `seed` starts the sequence.  bits: the container width.
std::vector<uint16_t> uniquePixels(size_t n, int bits, uint32_t seed);

// Inside an acquisition with the recorder on: send every frame, wait for
// each frame's reply (pixels / beats taken), then for the images to come out
// (a quiet link).  Returns the recording.  `during` runs after the sends;
// reply_ms, when given, gets the host time of each frame's reply.
std::vector<Captured> sendPortFrames(Context& c, const std::vector<PortFrame>& frames,
                                     const std::function<void()>& during = {},
                                     std::vector<double>* reply_ms = nullptr);

// §9.4.2, Figures 27-31: one line of n pixels of `bits` bits, MSB first,
// the first pixel's MSB in P0 bit 7, no packing across lines, padding 0.
// Words as the FIFO link carries them: P0 in bits 7:0.
Words goldenLine(const uint16_t* px, uint32_t n, int bits);

// The image with SourceTag `tag` in ims, or nullptr.
const ImageRec* imageByTag(const std::vector<ImageRec>& ims, uint32_t tag);

// Judge one received image against the frame sent: header fields (form,
// Xsize / Xoffs where the form has them, Ysize, Yoffs, PixelF, TapG, Flags,
// StreamID, SourceTag), Ysize lines, each line word for word equal to the
// golden packing (padding included) and, for an arbitrary image, each line
// marker's Xsize / Xoffs / DsizeL.  pixfmt: the format the image must be
// packed in.  Returns true when all matched; reports one FAIL line naming
// the first difference otherwise.
bool judgeImage(Context& c, const ImageRec* im, const PixelImage& sent, uint32_t pixfmt, bool arbitrary,
                const std::string& what);

}  // namespace cxp::validation::checks::pix
