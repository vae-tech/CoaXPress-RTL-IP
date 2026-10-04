#include "cxp/validation/cases/pix/_helpers.h"

namespace cxp::validation::checks::pix {

std::vector<uint32_t> wellFormedBeats(const PixelImage& im) {
    std::vector<uint32_t> b;
    const size_t n = im.pixels.size();
    for (size_t i = 0; i < n; ++i) {
        uint32_t w = im.pixels[i];
        if (i == 0) w |= kSof;
        if (im.xsize && i % im.xsize == im.xsize - 1) w |= kEol;
        if (i + 1 == n) w |= kEof;
        b.push_back(w);
    }
    return b;
}

std::vector<uint16_t> uniquePixels(size_t n, int bits, uint32_t seed) {
    const uint32_t mask = (1u << bits) - 1;
    std::vector<uint16_t> px(n);
    uint32_t v = seed;
    for (size_t i = 0; i < n; ++i) {
        // A step with the top and bottom bit set walks every container bit.
        px[i] = uint16_t(v & mask);
        v += (1u << (bits - 1)) + 0x2F + 2 * uint32_t(i);
    }
    if (n) px[0] = uint16_t(mask);  // full scale: the MSB and the LSB of the container
    return px;
}

std::vector<Captured> sendPortFrames(Context& c, const std::vector<PortFrame>& frames,
                                     const std::function<void()>& during, std::vector<double>* reply_ms) {
    c.prepareStreaming();
    c.startRecording();
    try {
        c.acqStart();
        for (const auto& f : frames) {
            if (f.beats.empty()) {
                c.sendPixelFrame(f.img);
            } else {
                c.sendPixelBeats(f.img, f.beats);
            }
        }
        if (during) during();
        for (size_t i = 0; i < frames.size(); ++i) {
            const size_t want = frames[i].beats.empty() ? frames[i].img.pixels.size() : frames[i].beats.size();
            const int64_t n = c.waitPixelFrame(20000);
            if (reply_ms) reply_ms->push_back(c.nowMs());
            c.expect(n == int64_t(want), "pixel port took %lld of %zu beats of frame %zu (SourceTag %u)", (long long)n,
                     want, i, frames[i].img.sourcetag);
        }
        const double t_in = c.nowMs();
        const double end = t_in + c.wait(c.opt().first_image_timeout_ms);
        while (c.nowMs() < end && c.nowMs() - std::max(t_in, c.lastStreamMs()) < c.wait(c.opt().quiet_ms)) {
            c.sleepRawMs(5);
        }
        c.acqStop();
        return c.stopRecording();
    } catch (...) {
        c.stopRecording();
        throw;
    }
}

Words goldenLine(const uint16_t* px, uint32_t n, int bits) {
    Words out((size_t(n) * size_t(bits) + 31) / 32, 0);
    size_t bit = 0;
    for (uint32_t x = 0; x < n; ++x) {
        const uint32_t v = px[x] & ((1u << bits) - 1);
        for (int b = bits - 1; b >= 0; --b, ++bit) {
            if (v >> b & 1) out[bit / 32] |= 1u << (8 * (bit / 8 % 4) + 7 - bit % 8);
        }
    }
    return out;
}

const ImageRec* imageByTag(const std::vector<ImageRec>& ims, uint32_t tag) {
    for (const auto& im : ims) {
        if (im.source_tag == tag) return &im;
    }
    return nullptr;
}

bool judgeImage(Context& c, const ImageRec* im, const PixelImage& s, uint32_t pixfmt, bool arbitrary,
                const std::string& what) {
    if (!im) return c.expect(false, "%s: the frame (SourceTag %u) never came back", what.c_str(), s.sourcetag);
    const int bits = pixelBits(pixfmt).value_or(8);
    const uint32_t dsize_l = uint32_t((uint64_t(s.xsize) * uint64_t(bits) + 31) / 32);
    std::string why;
    if (im->arbitrary != arbitrary) {
        why = arbitrary ? "rectangular header, arbitrary expected" : "arbitrary header, rectangular expected";
    } else if (!im->replicas_ok || !im->header_complete) {
        why = "header replicas disagree or header cut";
    } else if (im->ysize != s.ysize || im->yoffs != s.yoffs) {
        why = strprintf("Ysize/Yoffs %u/%u (%u/%u)", im->ysize, im->yoffs, s.ysize, s.yoffs);
    } else if (!arbitrary && (im->xsize != s.xsize || im->xoffs != s.xoffs || im->dsize_l != dsize_l)) {
        why = strprintf("Xsize/Xoffs/DsizeL %u/%u/%u (%u/%u/%u)", im->xsize, im->xoffs, im->dsize_l, s.xsize, s.xoffs,
                        dsize_l);
    } else if (im->pixel_f != pixfmt || im->tap_g != s.tapg || im->flags != s.flags) {
        why = strprintf("PixelF/TapG/Flags 0x%04X/0x%04X/0x%02X (0x%04X/0x%04X/0x%02X)", im->pixel_f, im->tap_g,
                        im->flags, pixfmt, s.tapg, s.flags);
    } else if (im->stream_id != s.streamid) {
        why = strprintf("StreamID %u (%u)", im->stream_id, s.streamid);
    } else if (im->lines.size() != s.ysize) {
        why = strprintf("%zu lines (Ysize %u)", im->lines.size(), s.ysize);
    } else if (arbitrary && im->line_xsize.size() != im->lines.size()) {
        why = strprintf("%zu of %zu lines behind an arbitrary line marker", im->line_xsize.size(), im->lines.size());
    } else if (!arbitrary && !im->line_xsize.empty()) {
        why = strprintf("%zu lines behind an arbitrary line marker in a rectangular image", im->line_xsize.size());
    }
    for (uint32_t y = 0; why.empty() && y < s.ysize; ++y) {
        if (arbitrary && (im->line_xsize[y] != s.xsize || im->line_xoffs[y] != s.xoffs || im->line_dsize_l[y] != dsize_l)) {
            why = strprintf("line %u marker Xsize/Xoffs/DsizeL %u/%u/%u (%u/%u/%u)", y, im->line_xsize[y],
                            im->line_xoffs[y], im->line_dsize_l[y], s.xsize, s.xoffs, dsize_l);
            break;
        }
        const Words gold = goldenLine(s.pixels.data() + size_t(y) * s.xsize, s.xsize, bits);
        const Words& got = im->lines[y];
        if (got != gold) {
            size_t k = 0;
            while (k < got.size() && k < gold.size() && got[k] == gold[k]) ++k;
            why = strprintf("line %u: %zu words (%zu), word %zu is 0x%08X, golden 0x%08X", y, got.size(), gold.size(), k,
                            k < got.size() ? got[k] : 0, k < gold.size() ? gold[k] : 0);
        }
    }
    if (!why.empty()) return c.expect(false, "%s: %s", what.c_str(), why.c_str());
    return true;
}

}  // namespace cxp::validation::checks::pix
