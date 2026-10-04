// CXP-EMU-PIX-108.  See cases/_common.h.
//
// Malformed pixel-port framing (bench PIXEL_BEATS): a frame whose SOF / EOL /
// EOF break the port's rules, then a well-formed frame.  What the device does
// with the broken frame is its choice (drop it, cut it, pad it).  A streaming
// device may already have sent its header when a later pixel proves invalid,
// so judge packet framing and recovery, not the broken image's geometry.
// The good frame after it must come back whole and bit for bit.  The last sub-case
// sends well-formed frames back to back whose metadata changes the cycle after
// each frame's last pixel, as the port allows (metadata sampled with SOF).

#include "cxp/validation/cases/pix/_helpers.h"

namespace cxp::validation::checks::pix {

namespace {

// The beats of a Mono8 frame whose line y has lens[y] pixels (EOL on each
// line's last pixel, SOF on the first, EOF on the last unless !eof).
std::vector<uint32_t> lineBeats(const std::vector<uint32_t>& lens, uint32_t seed, bool eof = true) {
    std::vector<uint32_t> b;
    uint32_t v = seed;
    for (size_t y = 0; y < lens.size(); ++y) {
        for (uint32_t x = 0; x < lens[y]; ++x) {
            uint32_t w = (v++ * 37u + 11u) & 0xFFu;
            if (b.empty()) w |= kSof;
            if (x + 1 == lens[y]) w |= kEol;
            b.push_back(w);
        }
    }
    if (eof && !b.empty()) b.back() |= kEof;
    return b;
}

PixelImage meta(uint32_t w, uint32_t h, uint32_t tag) {
    PixelImage m;
    m.xsize = w;
    m.ysize = h;
    m.sourcetag = tag;
    return m;
}

PixelImage goodFrame(uint32_t w, uint32_t h, uint32_t tag) {
    PixelImage m = meta(w, h, tag);
    m.pixels = uniquePixels(size_t(w) * h, 8, tag);
    return m;
}

void pix108(Context& c) {
    c.needBench(bench::CAP_PIXEL | bench::CAP_PIXEL_BEATS, "send pixel-port frames with malformed framing");
    uvm::usePixelPort(c);
    const uint32_t w = uint32_t(c.iparam("frame.width")), h = uint32_t(c.iparam("frame.height"));
    uint32_t tag = 10;
    size_t good_ok = 0, good_n = 0;
    for (const std::string& kind : c.slist("kinds")) {
        const std::string what = kind + ": ";
        std::vector<PortFrame> frames;
        PortFrame bad;
        bad.img = meta(w, h, 0x100 + tag);
        std::vector<uint32_t> lens(h, w);
        if (kind == "sof_mid_frame") {
            // Line 0 and a quarter of line 1, then SOF again: the frame starts over and completes.
            bad.beats = lineBeats({w, w / 4}, 1, false);
            bad.beats.back() &= ~kEol;
            const auto again = lineBeats(lens, 2);
            bad.beats.insert(bad.beats.end(), again.begin(), again.end());
        } else if (kind == "short_line") {
            lens[1] = w - w / 4;
            bad.beats = lineBeats(lens, 3);
        } else if (kind == "long_line") {
            lens[1] = w + w / 4;
            bad.beats = lineBeats(lens, 4);
        } else if (kind == "stray_pixels") {
            // Beats outside any frame: no SOF, one of them with EOL + EOF.
            for (uint32_t i = 0; i < w; ++i) bad.beats.push_back(i | (i == w - 1 ? kEol | kEof : 0));
        } else if (kind == "missing_eof") {
            bad.beats = lineBeats(lens, 5, false);
        } else if (kind == "early_eof") {
            // EOF at the end of line 1, the rest of the frame after it.
            bad.beats = lineBeats(lens, 6, false);
            bad.beats[2 * w - 1] |= kEof;
        } else if (kind == "metadata_back_to_back") {
            bad.beats.clear();
        } else {
            c.expect(false, "unknown kind '%s'", kind.c_str());
            continue;
        }
        std::vector<PixelImage> goods;
        if (kind == "metadata_back_to_back") {
            // Short well-formed frames, back to back, each with its own
            // metadata (SourceTag, Xsize).
            for (int k = 0; k < c.iparam("short_frames"); ++k) {
                goods.push_back(goodFrame(uint32_t(1 + k % 3), 1 + uint32_t(k % 2), tag++));
            }
        } else {
            frames.push_back(bad);
            goods.push_back(goodFrame(w, h, tag++));
        }
        for (const auto& g : goods) frames.push_back({g, {}});
        const auto pk = streamPackets(sendPortFrames(c, frames));
        size_t bad_packets = 0, tag_breaks = 0;
        const StreamPkt* previous = nullptr;
        for (const auto& p : pk) {
            bad_packets += !p.defects.empty() || p.total_words != p.payload.size() + 8;
            if (previous && p.stream_id == previous->stream_id &&
                p.tag != uint8_t(previous->tag + 1)) ++tag_breaks;
            previous = &p;
        }
        c.expect(!pk.empty() && bad_packets == 0, "%s%zu packets, %zu Table 19 defects", what.c_str(),
                 pk.size(), bad_packets);
        c.expect(tag_breaks == 0, "%s%zu PacketTag breaks", what.c_str(), tag_breaks);
        const auto ims = walkImages(pk);
        std::string seen;
        for (const auto& im : ims) {
            seen += strprintf(" [tag 0x%X %ux%u, %zu lines]", im.source_tag, im.xsize, im.ysize, im.lines.size());
        }
        c.info("%s%zu beats sent, then %zu good frame(s); on the link:%s", what.c_str(), bad.beats.size(), goods.size(),
               seen.empty() ? " nothing" : seen.c_str());
        for (const auto& g : goods) {
            ++good_n;
            good_ok += judgeImage(c, imageByTag(ims, g.sourcetag), g, 0x0101, false,
                                  strprintf("%sgood frame SourceTag %u (%ux%u)", what.c_str(), g.sourcetag, g.xsize, g.ysize));
        }
        const auto a = c.readRaw(Reg::STANDARD, 4);
        c.expect(is(a, Ack::READ_OK), "%sthe device answers a read after it (%s)", what.c_str(), ackStr(a).c_str());
    }
    c.expect(good_ok == good_n, "%zu of %zu good frames after the malformed ones came back whole and bit for bit",
             good_ok, good_n);
}
CXP_CHECK("CXP-EMU-PIX-108", pix108);

}  // namespace

}  // namespace cxp::validation::checks::pix
