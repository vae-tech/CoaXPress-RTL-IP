// CXP-CAM-IMG-007.  See cases/_common.h.
//
// Arbitrary image stream (§9.4.7): the bench's ARBITRARY strap selects the
// Table 40 header and Table 41 line markers; frames go in at the pixel port.
// The port carries one Xsize / Xoffs per frame (its metadata), so every line of
// an image has the frame's geometry: an arbitrary shape with lines of
// different lengths needs per-line metadata the bench does not drive.

#include "cxp/validation/cases/pix/_helpers.h"

namespace cxp::validation::checks::img {

namespace {

using pix::judgeImage;
using pix::imageByTag;

// Every image on the link has one form: the header's.
size_t mixedImages(Context& c, const std::vector<ImageRec>& ims, const std::string& what) {
    size_t mixed = 0;
    for (const auto& im : ims) {
        const bool ok = im.arbitrary ? im.line_xsize.size() == im.lines.size() : im.line_xsize.empty();
        if (!ok && ++mixed <= size_t(c.opt().max_reported)) {
            c.expect(false, "%simage SourceTag %u: %s header, %zu of %zu lines behind an arbitrary line marker",
                     what.c_str(), im.source_tag, im.arbitrary ? "arbitrary" : "rectangular", im.line_xsize.size(),
                     im.lines.size());
        }
    }
    return mixed;
}

void img007(Context& c) {
    c.needBench(bench::CAP_PIXEL | bench::CAP_ARBITRARY, "drive the pixel port with the ARBITRARY strap");
    uvm::usePixelPort(c);
    preserveFeature(c, "PixelFormat");
    trySet(c, "PixelFormat", Value::ofString("Mono8"));
    c.benchPin(bench::ARBITRARY, 1);
    c.benchSync();
    uint32_t tag = 300;

    // 1. Arbitrary images of several geometries.
    size_t ok = 0, n = 0;
    for (const auto& g : c.rows("geometries")) {
        PixelImage im;
        im.xsize = uint32_t(g[0]);
        im.ysize = uint32_t(g[1]);
        im.xoffs = uint32_t(g[2]);
        im.yoffs = uint32_t(g[3]);
        im.sourcetag = tag++;
        im.pixels = pix::uniquePixels(size_t(im.xsize) * im.ysize, 8, im.sourcetag);
        const auto pk = streamPackets(pix::sendPortFrames(c, {{im, {}}}));
        const std::string what = strprintf("arbitrary %ux%u at (%u,%u): ", im.xsize, im.ysize, im.xoffs, im.yoffs);
        uvm::streamScoreboard(c, pk, -1, 0, {}, what.c_str());
        ++n;
        ok += judgeImage(c, imageByTag(walkImages(pk), im.sourcetag), im, 0x0101, true, what + "image");
    }
    c.expect(ok == n, "%zu of %zu arbitrary images: Table 40 header, a Table 41 marker with the line's Xsize, Xoffs and "
             "DsizeL before every line, pixels bit for bit", ok, n);

    // 2. The form switched between images: each image in the form of its time.
    ok = 0;
    const auto seq = c.ilist("toggle_sequence");
    for (int64_t arb : seq) {
        c.benchSend({bench::PIN, bench::ARBITRARY, uint32_t(arb)});
        c.benchSync();
        PixelImage im;
        im.xsize = uint32_t(c.iparam("frame.width"));
        im.ysize = uint32_t(c.iparam("frame.height"));
        im.xoffs = 3;
        im.sourcetag = tag++;
        im.pixels = pix::uniquePixels(size_t(im.xsize) * im.ysize, 8, im.sourcetag);
        const auto pk = streamPackets(pix::sendPortFrames(c, {{im, {}}}));
        ok += judgeImage(c, imageByTag(walkImages(pk), im.sourcetag), im, 0x0101, arb != 0,
                         strprintf("switched to %s: image", arb ? "arbitrary" : "rectangular"));
    }
    c.expect(ok == seq.size(), "%zu of %zu images after a switch between frames in the form of the strap", ok, seq.size());

    // 3. The form switched while an image goes in: the image on the link keeps
    //    one form (the one its header announces), or starts over cleanly.
    for (int64_t to : {0, 1}) {
        c.benchSend({bench::PIN, bench::ARBITRARY, uint32_t(1 - to)});
        c.benchSync();
        PixelImage im;
        im.xsize = uint32_t(c.iparam("slow_frame.width"));
        im.ysize = uint32_t(c.iparam("slow_frame.height"));
        im.valid_permille = uint32_t(c.iparam("slow_frame.valid_permille"));
        im.sourcetag = tag++;
        im.pixels = pix::uniquePixels(size_t(im.xsize) * im.ysize, 8, im.sourcetag);
        PixelImage next = im;
        next.valid_permille = 1000;
        next.sourcetag = tag++;
        const std::string what = strprintf("switched to %s during an image: ", to ? "arbitrary" : "rectangular");
        double t_toggle = 0;
        std::vector<double> reply;
        const double t_send = c.nowMs();
        auto cap = pix::sendPortFrames(c, {{im, {}}}, [&] {
            c.sleepRawMs(c.iparam("toggle_after_ms"));
            c.benchSend({bench::PIN, bench::ARBITRARY, uint32_t(to)});
            t_toggle = c.nowMs();
        }, &reply);
        if (!reply.empty() && reply[0] < t_toggle) {
            c.warn("%sthe frame was all in %.0f ms after the send, before the switch at %.0f ms: not a switch during "
                   "an image", what.c_str(), reply[0] - t_send, t_toggle - t_send);
        } else if (!reply.empty()) {
            c.info("%sstrap switched %.0f ms after the send, the frame all in at %.0f ms", what.c_str(),
                   t_toggle - t_send, reply[0] - t_send);
        }
        const auto more = pix::sendPortFrames(c, {{next, {}}});
        cap.insert(cap.end(), more.begin(), more.end());
        const auto pk = streamPackets(cap);
        const auto ims = walkImages(pk);
        uvm::streamScoreboard(c, pk, -1, 0, {}, what.c_str());
        const size_t mixed = mixedImages(c, ims, what);
        c.expect(mixed == 0, "%s%zu images on the link, %zu mixing the two forms", what.c_str(), ims.size(), mixed);
        const ImageRec* a = imageByTag(ims, im.sourcetag);
        if (a) {
            c.info("%sthe image in flight went out with a%s header, %zu of %u lines", what.c_str(),
                   a->arbitrary ? "n arbitrary" : " rectangular", a->lines.size(), im.ysize);
        }
        judgeImage(c, imageByTag(ims, next.sourcetag), next, 0x0101, to != 0, what + "the next image");
    }
    c.note("every line of an image has the frame's Xsize and Xoffs (the port's metadata is per frame); REQ-IMG-013 "
           "says a rectangular image should not use the arbitrary form, so this is a stimulus choice");
}
CXP_CHECK("CXP-CAM-IMG-007", img007);

}  // namespace

}  // namespace cxp::validation::checks::img
