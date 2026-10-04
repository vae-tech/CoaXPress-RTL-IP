#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

const char* regName(uint32_t addr) {
    switch (addr) {
    case Reg::MASTER_HOST_CONNECTION_ID: return "MasterHostConnectionID";
    case Reg::STREAM_PACKET_SIZE_MAX: return "StreamPacketSizeMax";
    case Reg::CONNECTION_CONFIG: return "ConnectionConfig";
    case Reg::TEST_ERROR_COUNT_SELECTOR: return "TestErrorCountSelector";
    default: return "register";
    }
}

void stopAcqIfPossible(Context& c) {
    if (Feature* f = c.feature("AcquisitionStop")) {
        try {
            f->execute();
        } catch (const std::exception&) {
        }
        c.waitQuiet();
    }
}

bool hasEntry(const Feature* f, const std::string& name) {
    return f && std::any_of(f->enum_entries.begin(), f->enum_entries.end(),
                            [&](const auto& e) { return e.first == name; });
}

void runTestPattern(Context& c) {
    if (!(c.benchCaps() & bench::CAP_PIXEL)) return;
    c.benchPin(bench::USE_TPG, 1);
    c.benchPin(bench::TPG_RUN, 0);
    c.benchSync();
    c.waitQuiet();
}

int selectBars(Context& c) {
    runTestPattern(c);
    preserveFeature(c, "PixelFormat");
    preserveFeature(c, "TestPattern");
    if (c.feature("PixelFormat")) trySet(c, "PixelFormat", Value::ofString("Mono8"));
    const std::string bars = testPatternName(TPG_BARS);
    if (hasEntry(c.feature("TestPattern"), bars) && trySet(c, "TestPattern", Value::ofString(bars))) {
        return int(TPG_BARS);
    }
    c.note("no Bars test pattern: pixels are not compared with the golden model");
    return -1;
}

std::vector<Captured> streamAround(Context& c, size_t more, const std::function<void()>& during,
                                   int timeout_ms) {
    c.prepareStreaming();
    c.startRecording();
    try {
        const size_t h0 = c.headersSeen();
        c.acqStart();
        if (!c.waitHeaders(h0 + 1, timeout_ms)) c.info("no image header within %d ms", c.wait(timeout_ms));
        during();
        const size_t h1 = c.headersSeen();
        if (c.waitHeaders(h1 + more, timeout_ms)) c.waitTail(h1 + more);
        auto cap = c.stopRecording();
        try {
            c.acqStop();
            c.waitQuiet();
        } catch (const Cancelled&) {
            throw;
        } catch (const std::exception& e) {
            c.expect(false, "AcquisitionStop after the traffic: %s", e.what());
        }
        return cap;
    } catch (...) {
        c.stopRecording();
        throw;
    }
}

StreamSb streamScoreboard(Context& c, const std::vector<StreamPkt>& pk, int pattern, uint32_t spsm,
                          const std::vector<double>& tag_resets, const char* what) {
    StreamSb sb;
    sb.packets = pk.size();
    size_t bad = 0, over = 0;
    for (const auto& p : pk) {
        const bool ok = p.defects.empty() && p.total_words == p.payload.size() + 8;
        if (!ok && ++bad <= size_t(c.opt().max_reported)) {
            c.expect(false, "%spacket tag %u: %s", what, p.tag,
                     p.defects.empty() ? "total length != N + 8" : p.defects.front().c_str());
        }
        over += spsm && uint64_t(p.total_words) * 4 > spsm;
    }
    if (pk.empty()) {
        c.expect(false, "%sno stream packet in the recording", what);
        return sb;
    }
    c.expect(bad == 0, "%s%zu of %zu stream packets match Table 19 (DsizeP = payload words, CRC)", what,
             pk.size() - bad, pk.size());
    if (spsm) c.expect(over == 0, "%s%zu packets longer than StreamPacketSizeMax = %u bytes", what, over, spsm);

    std::set<uint8_t> sids;
    for (const auto& p : pk) sids.insert(p.stream_id);
    for (uint8_t sid : sids) {
        const StreamPkt* prev = nullptr;
        size_t breaks = 0, restarts = 0, n = 0;
        for (const auto& p : pk) {
            if (p.stream_id != sid) continue;
            ++n;
            if (prev && p.tag != ((prev->tag + 1) & 0xFF)) {
                const bool reset = p.tag == 0 && std::any_of(tag_resets.begin(), tag_resets.end(), [&](double t) {
                    return t >= prev->t_ms && t <= p.t_ms;
                });
                if (reset) {
                    ++restarts;
                } else if (++breaks <= size_t(c.opt().max_reported)) {
                    c.expect(false, "%sstream %u: tag %u followed by %u", what, sid, prev->tag, p.tag);
                }
            }
            prev = &p;
        }
        c.expect(breaks == 0, "%sstream %u: packet tags +1 mod 256 over %zu packets (%zu breaks)", what, sid, n, breaks);
        if (restarts) c.info("%sstream %u: %zu tag restarts at 0 after a ConnectionConfig write", what, sid, restarts);
    }

    const auto ims = walkImages(pk);
    size_t framed = 0, framing_bad = 0;
    for (size_t i = 0; i < ims.size(); ++i) {
        const ImageRec& im = ims[i];
        if (i + 1 == ims.size() && !imageComplete(im) && im.lines.size() < im.ysize) continue;  // cut by the stop
        ++framed;
        if (!imageComplete(im) && ++framing_bad <= size_t(c.opt().max_reported)) {
            c.expect(false, "%simage SourceTag %u: %zu of %u lines, first line %zu words (DsizeL %u, spec %zu)", what,
                     im.source_tag, im.lines.size(), im.ysize, im.line_words.empty() ? 0 : im.line_words.front(),
                     im.dsize_l, specLineWords(im));
        }
    }
    if (framed) {
        c.expect(framing_bad == 0, "%s%zu of %zu images framed: Ysize line markers of DsizeL = ceil(Xsize x bpp / 32) words",
                 what, framed - framing_bad, framed);
    } else {
        c.info("%sno image to judge the framing of", what);
    }

    const auto done = completeImages(ims);
    sb.complete = done.size();
    if (pattern >= 0 && testPatternStatic(uint32_t(pattern))) {
        size_t checked = 0, mismatched = 0, rev_ok = 0;
        for (const auto& im : done) {
            if (im.pixel_f != 0x0101) continue;
            const auto gold = renderTestPattern(PixelFormat::Mono8, im.xsize, im.ysize, uint32_t(pattern), 0);
            bool ok = true, ok_rev = true;
            for (uint32_t y = 0; y < im.ysize; ++y) {
                const auto a = lineBytesP0(im.lines[y]), b = lineBytesP3(im.lines[y]);
                const auto g0 = gold.begin() + y * im.xsize, g1 = g0 + im.xsize;
                ok &= a.size() >= im.xsize && std::equal(g0, g1, a.begin());
                ok_rev &= b.size() >= im.xsize && std::equal(g0, g1, b.begin());
            }
            ++checked;
            rev_ok += !ok && ok_rev;
            if (!ok && ++mismatched <= size_t(c.opt().max_reported)) c.expect(false, "%simage SourceTag %u differs from the golden model", what, im.source_tag);
        }
        c.expect(checked > 0 && mismatched == 0, "%s%zu of %zu complete Mono8 images equal the golden %s model bit for bit",
                 what, checked - mismatched, checked, testPatternName(uint32_t(pattern)));
        if (rev_ok) c.note("%s%zu images match only with bytes read MSB-first (first pixel in P3, not P0)", what, rev_ok);
    }
    return sb;
}

RegModel readModel(Context& c) {
    RegModel m;
    for (uint32_t a : kUvmRw) m[a] = c.rd32(a);
    return m;
}

uint32_t legalValue(Context& c, uint32_t addr, std::mt19937& rng, const RegModel& m) {
    switch (addr) {
    case Reg::MASTER_HOST_CONNECTION_ID: return uint32_t(rng());
    case Reg::STREAM_PACKET_SIZE_MAX: {
        const auto r = c.ilist("spsm_range");
        return 4 * std::uniform_int_distribution<uint32_t>(uint32_t(r[0] / 4), uint32_t(r[1] / 4))(rng);
    }
    case Reg::CONNECTION_CONFIG: return m.at(addr);
    default: return 0;
    }
}

void ctrlTraffic(Context& c, RegModel& m, int n, int write, std::mt19937& rng, std::vector<double>* cc_writes) {
    size_t bad = 0;
    for (int i = 0; i < n; ++i) {
        const uint32_t addr = kUvmRw[rng() % 4];
        const bool w = write < 0 ? (rng() & 1) != 0 : write != 0;
        std::string why;
        if (w) {
            const uint32_t v = legalValue(c, addr, rng, m);
            if (addr == Reg::CONNECTION_CONFIG && cc_writes) cc_writes->push_back(c.nowMs());
            auto a = c.writeRaw(addr, {v});
            if (!is(a, Ack::WRITE_OK)) why = strprintf("write %s: %s", hex(v).c_str(), ackStr(a).c_str());
            else if (a->long_form) why = strprintf("write %s: 0x01 in the long form", hex(v).c_str());
            else m[addr] = v;
        } else {
            auto a = c.readRaw(addr, 4);
            if (!is(a, Ack::READ_OK) || a->data.size() != 1) {
                why = "read: " + ackStr(a);
            } else if (a->values()[0] != m[addr]) {
                why = strprintf("read %s, model %s", hex(a->values()[0]).c_str(), hex(m[addr]).c_str());
            }
        }
        if (!why.empty() && ++bad <= size_t(c.opt().max_reported)) c.expect(false, "%s %s", regName(addr), why.c_str());
    }
    c.expect(bad == 0, "%zu of %d commands answered as the register model predicts", n - bad, n);
}

void resetTestCounters(Context& c) {
    c.wr32(Reg::TEST_ERROR_COUNT_SELECTOR, 0);
    c.wr32(Reg::TEST_ERROR_COUNT, 0);
    write64(c, Reg::TEST_PACKET_COUNT_RX, 0);
}

void expectCounters(Context& c, uint64_t want_rx, uint32_t want_err) {
    const uint32_t err = c.rd32(Reg::TEST_ERROR_COUNT);
    const uint64_t rx = c.rd64(Reg::TEST_PACKET_COUNT_RX);
    c.expect(rx == want_rx && err == want_err, "TestPacketCountRx %llu, TestErrorCount %u (%llu, %u)",
             (unsigned long long)rx, err, (unsigned long long)want_rx, want_err);
}

bool readsStandard(const OptAck& a) {
    return is(a, Ack::READ_OK) && a->data.size() == 1 && a->values()[0] == CXP_MAGIC;
}

void uvmIdleConfig(Context& c) {
    const uint32_t caps = c.benchCaps();
    if (caps & bench::CAP_RESET) {
        if (c.benchReset(0)) {
            c.info("device reset through the bench, every input at 0 (as UVM starts each test)");
        } else {
            c.expect(false, "the device did not answer a read after a bench reset");
        }
        c.waitQuiet();
        return;
    }
    if (!(caps & bench::CAP_PIXEL)) return;
    c.benchPin(bench::TPG_RUN, 0);
    c.benchSync();
    c.waitQuiet();
}

bool usePixelPort(Context& c) {
    if (!(c.benchCaps() & bench::CAP_PIXEL)) {
        c.note("no bench pixel port: the device's own test pattern stands in for the UVM video agent");
        return false;
    }
    uvmIdleConfig(c);      // as in UVM, the port is switched with the generator stopped
    c.prepareStreaming();  // StreamPacketSizeMax non-zero, restored on exit
    c.benchPin(bench::USE_TPG, 0);
    c.benchSync();
    c.info("images go in through the bench pixel port (USE_TPG = 0)");
    return true;
}

PixelImage randomImage(Context& c, std::mt19937& rng, uint32_t sourcetag) {
    const auto xs = c.ilist("frame_xsizes"), ys = c.ilist("frame_ysizes"), valid = c.ilist("valid_percent_range");
    PixelImage im;
    im.xsize = uint32_t(xs[rng() % xs.size()]);
    im.ysize = uint32_t(ys[rng() % ys.size()]);
    im.sourcetag = sourcetag;
    im.valid_permille = uint32_t(10 * valid[0]) + rng() % uint32_t(10 * (valid[1] - valid[0]) + 1);
    im.pixels.resize(size_t(im.xsize) * im.ysize);
    for (auto& p : im.pixels) p = uint16_t(rng() & 0xFF);
    return im;
}

PixelImage rampImage(uint32_t xsize, uint32_t ysize) {
    PixelImage im;
    im.xsize = xsize;
    im.ysize = ysize;
    im.pixels.resize(size_t(xsize) * ysize);
    for (size_t i = 0; i < im.pixels.size(); ++i) im.pixels[i] = uint16_t(i & 0xFF);
    return im;
}

std::vector<uint16_t> unpackLine(const Words& line, uint32_t n, int bits) {
    const auto bytes = lineBytesP0(line);
    std::vector<uint16_t> out;
    size_t bit = 0;
    for (uint32_t x = 0; x < n; ++x) {
        uint32_t v = 0;
        for (int b = 0; b < bits; ++b, ++bit) {
            const bool one = bit / 8 < bytes.size() && (bytes[bit / 8] >> (7 - bit % 8) & 1);
            v = v << 1 | uint32_t(one);
        }
        out.push_back(uint16_t(v));
    }
    return out;
}

void injectedScoreboard(Context& c, const std::vector<StreamPkt>& pk, const std::vector<PixelImage>& sent,
                        bool arbitrary, uint32_t spsm, const char* what) {
    streamScoreboard(c, pk, -1, spsm, {}, what);
    const auto all = walkImages(pk);
    std::vector<ImageRec> ims;
    std::vector<PixelImage> want;
    for (const PixelImage& s : sent) {
        auto it = std::find_if(all.begin(), all.end(), [&](const ImageRec& im) { return im.source_tag == s.sourcetag; });
        if (it == all.end()) {
            c.expect(false, "%sframe with SourceTag %u (%ux%u) never came back", what, s.sourcetag, s.xsize, s.ysize);
            continue;
        }
        ims.push_back(*it);
        want.push_back(s);
    }
    c.expect(ims.size() == sent.size() && all.size() == sent.size(),
             "%s%zu of %zu frames sent into the pixel port came back (%zu images on the link)", what, ims.size(),
             sent.size(), all.size());
    const std::vector<PixelImage>& sent_ = want;
    size_t hdr_bad = 0, px_bad = 0, rev_ok = 0;
    for (size_t i = 0; i < ims.size(); ++i) {
        const ImageRec& im = ims[i];
        const PixelImage& s = sent_[i];
        std::string why;
        if (im.arbitrary != arbitrary) why = arbitrary ? "rectangular header, arbitrary expected" : "arbitrary header";
        else if (im.ysize != s.ysize || im.yoffs != s.yoffs) why = strprintf("Ysize/Yoffs %u/%u", im.ysize, im.yoffs);
        else if (!arbitrary && (im.xsize != s.xsize || im.xoffs != s.xoffs)) why = strprintf("Xsize/Xoffs %u/%u", im.xsize, im.xoffs);
        else if (im.pixel_f != s.pixfmt || im.tap_g != s.tapg || im.flags != s.flags) {
            why = strprintf("PixelF/TapG/Flags 0x%04X/0x%04X/0x%02X", im.pixel_f, im.tap_g, im.flags);
        } else if (im.source_tag != s.sourcetag || im.stream_id != s.streamid) {
            why = strprintf("SourceTag/StreamID %u/%u (%u/%u)", im.source_tag, im.stream_id, s.sourcetag, s.streamid);
        }
        if (arbitrary && why.empty()) {
            for (size_t y = 0; y < im.line_xsize.size(); ++y) {
                if (im.line_xsize[y] != s.xsize || im.line_xoffs[y] != s.xoffs) {
                    why = strprintf("line %zu marker Xsize/Xoffs %u/%u", y, im.line_xsize[y], im.line_xoffs[y]);
                    break;
                }
            }
        }
        if (!why.empty() && ++hdr_bad <= size_t(c.opt().max_reported)) c.expect(false, "%sframe %zu (%ux%u): %s", what, i, s.xsize, s.ysize, why.c_str());
        const int bits = pixelBits(s.pixfmt).value_or(8);
        bool ok = im.lines.size() == s.ysize, ok_rev = ok;
        std::string where = ok ? "" : strprintf("%zu of %u lines", im.lines.size(), s.ysize);
        for (uint32_t y = 0; y < s.ysize && y < im.lines.size(); ++y) {
            const auto got = unpackLine(im.lines[y], s.xsize, bits);
            const auto g0 = s.pixels.begin() + size_t(y) * s.xsize;
            const auto miss = std::mismatch(got.begin(), got.end(), g0);
            if (miss.first != got.end() && where.find("pixel") == std::string::npos) {
                where += strprintf("%sline %u pixel %zu is 0x%02X, sent 0x%02X (line %zu words)", where.empty() ? "" : "; ",
                                   y, size_t(miss.first - got.begin()), *miss.first, *miss.second, im.lines[y].size());
            }
            ok &= miss.first == got.end();
            if (bits == 8) {
                const auto rev = lineBytesP3(im.lines[y]);
                ok_rev &= rev.size() >= s.xsize &&
                          std::equal(s.pixels.begin() + size_t(y) * s.xsize, s.pixels.begin() + size_t(y + 1) * s.xsize,
                                     rev.begin(), [](uint16_t a, uint8_t b) { return a == b; });
            } else {
                ok_rev = false;
            }
        }
        rev_ok += !ok && ok_rev;
        if (!ok && ++px_bad <= size_t(c.opt().max_reported)) {
            c.expect(false, "%sframe %zu (%ux%u): pixels differ from those sent: %s", what, i, s.xsize, s.ysize,
                     where.c_str());
        }
    }
    const size_t n = ims.size();
    if (n) c.expect(hdr_bad == 0, "%s%zu of %zu images carry the header and line markers of the frame sent", what, n - hdr_bad, n);
    c.expect(n > 0 && px_bad == 0, "%s%zu of %zu images equal the pixels sent, bit for bit (first pixel in P0)", what,
             n - px_bad, n);
    if (rev_ok) c.note("%s%zu images match only with bytes read MSB-first (first pixel in P3, not P0)", what, rev_ok);
}

std::vector<Captured> injectImages(Context& c, const std::vector<PixelImage>& imgs,
                                   const std::function<void()>& during) {
    // A camera takes sensor images only while an acquisition runs.
    c.prepareStreaming();
    c.startRecording();
    try {
        c.acqStart();
        const size_t h0 = c.headersSeen();
        for (const auto& im : imgs) c.sendPixelFrame(im);
        if (during) during();
        for (size_t i = 0; i < imgs.size(); ++i) {
            const int64_t n = c.waitPixelFrame(20000);
            c.expect(n == int64_t(imgs[i].pixels.size()), "pixel port took %lld of %zu pixels of frame %zu", (long long)n,
                     imgs[i].pixels.size(), i);
        }
        const double t_in = c.nowMs();
        const double end = t_in + c.wait(c.opt().first_image_timeout_ms);
        c.waitHeaders(h0 + imgs.size(), 2000);
        while (c.nowMs() < end && c.nowMs() - std::max(t_in, c.lastStreamMs()) < c.wait(c.opt().quiet_ms)) c.sleepRawMs(5);
        c.acqStop();
        return c.stopRecording();
    } catch (...) {
        c.stopRecording();
        throw;
    }
}

Chars trigChars(bool rising) { return lsTrigger(rising, 0); }

void settleTrigOut(Context& c) {
    c.sendChars(trigChars(false));
    c.sleepMs(100);
}

HostTrigRun hostTriggers(Context& c, const std::vector<bool>& kinds, int spacing_ms) {
    HostTrigRun r;
    settleTrigOut(c);
    const double since = c.nowMs();
    uint32_t level = 0;
    for (bool rising : kinds) {
        c.sendChars(trigChars(rising));
        ++r.sent;
        r.rising += rising;
        if (uint32_t(rising) != level) r.want_edges.push_back(level = uint32_t(rising));
        c.waitShort(ShortPacket::Kind::IoAck, r.sent, since, 500);
        c.sleepRawMs(spacing_ms);
    }
    c.waitShort(ShortPacket::Kind::IoAck, r.sent, since, 500);
    for (const auto& s : c.shortPackets(since)) {
        if (s.pkt.kind == ShortPacket::Kind::IoAck) r.acks.push_back(s);
    }
    r.edges = c.pinEdges(bench::TRIG_OUT, since);
    return r;
}

std::vector<bool> randomKinds(size_t n, unsigned seed) {
    std::mt19937 rng(seed);
    std::vector<bool> k;
    for (size_t i = 0; i < n; ++i) k.push_back((rng() & 1) != 0);
    return k;
}

void judgeAcks(Context& c, const HostTrigRun& r, const char* what) {
    size_t good = 0;
    for (const auto& a : r.acks) good += a.pkt.clean && a.pkt.value == IOACK_OK;
    c.expect(r.acks.size() == r.sent && good == r.sent,
             "%s%zu triggers sent, %zu I/O acknowledgments, %zu of them 4 x K28.6 + 4 x 0x01", what, r.sent,
             r.acks.size(), good);
}

// Rises of the host's level: a rising trigger with no falling one since is
// a resend, not a new event.
static size_t levelRises(const HostTrigRun& r) {
    size_t n = 0;
    for (uint32_t v : r.want_edges) n += v != 0;
    return n;
}

TrigOut trigOutReading(const HostTrigRun& r, std::string* seen) {
    std::vector<uint32_t> got;
    for (const auto& e : r.edges) got.push_back(e.value);
    seen->clear();
    for (uint32_t v : got) *seen += v ? "1" : "0";
    if (seen->empty()) *seen = "none";
    if (got == r.want_edges) return TrigOut::Level;
    std::vector<uint32_t> strobe;
    for (size_t i = 0; i < levelRises(r); ++i) strobe.insert(strobe.end(), {1, 0});
    return got == strobe ? TrigOut::Strobe : TrigOut::Neither;
}

void judgeEdges(Context& c, const HostTrigRun& r, const char* what) {
    std::string seen, w;
    for (uint32_t v : r.want_edges) w += v ? "1" : "0";
    const TrigOut k = trigOutReading(r, &seen);
    c.expect(k != TrigOut::Neither, "%srecreated trigger edges %s: %s (level: %s, or a strobe per rise of the level: %zu)",
             what, seen.c_str(), k == TrigOut::Level ? "follows the trigger level" : k == TrigOut::Strobe ? "one strobe per rise of the level" : "neither",
             w.empty() ? "none" : w.c_str(), levelRises(r));
}

size_t countTriggers(Context& c, double since) {
    size_t k = 0;
    for (const auto& s : c.shortPackets(since)) k += s.pkt.kind != ShortPacket::Kind::IoAck;
    return k;
}

std::vector<TimedShort> deviceTriggers(Context& c, size_t n, int gap_ms) {
    c.benchPin(bench::TRIG_IN, 0);
    c.benchSync();
    c.sleepMs(50);
    for (const auto& s : c.shortPackets()) {
        if (s.pkt.kind != ShortPacket::Kind::IoAck) c.sendChars(ioAck());  // acknowledge leftovers
    }
    const double since = c.nowMs();
    uint32_t level = 0;
    for (size_t i = 0; i < n; ++i) {
        level ^= 1;
        c.benchSend({bench::PIN, bench::TRIG_IN, level});
        const double end = c.nowMs() + c.wait(500);
        while (countTriggers(c, since) < i + 1 && c.nowMs() < end) c.sleepRawMs(2);
        c.sendChars(ioAck());  // §8.3.3: the Host acknowledges every trigger
        c.sleepRawMs(gap_ms);
    }
    if (level) {  // leave the input de-asserted, as IoTriggerSeq does
        c.benchSend({bench::PIN, bench::TRIG_IN, 0});
        c.sleepMs(50);
        c.sendChars(ioAck());
    }
    std::vector<TimedShort> out;
    for (const auto& s : c.shortPackets(since)) {
        if (s.pkt.kind != ShortPacket::Kind::IoAck) out.push_back(s);
    }
    return out;
}

void judgeDeviceTriggers(Context& c, const std::vector<TimedShort>& got, size_t edges, const char* what) {
    const size_t want = edges + (edges % 2);  // an odd count ends with a falling edge back to low
    size_t bad = 0;
    for (size_t i = 0; i < got.size(); ++i) {
        const auto& p = got[i].pkt;
        const auto kind = i % 2 == 0 ? ShortPacket::Kind::TriggerRise : ShortPacket::Kind::TriggerFall;
        const bool ok = p.kind == kind && p.clean && p.value >= 0 && p.value <= 3;
        if (!ok && ++bad <= size_t(c.opt().max_reported)) c.expect(false, "%strigger packet %zu: %s", what, i, describeShortPacket(p).c_str());
    }
    c.expect(got.size() == want && bad == 0,
             "%s%zu edges on the trigger input, %zu trigger packets (%zu expected), %zu well formed", what, want,
             got.size(), want, got.size() - bad);
}

std::vector<PixelImage> randomImages(Context& c, std::mt19937& rng, size_t n) {
    std::vector<PixelImage> out;
    for (size_t i = 0; i < n; ++i) out.push_back(randomImage(c, rng, uint32_t(i)));
    return out;
}

void preemptRound(Context& c, int pat, bool pixel, unsigned seed, const char* what) {
    resetTestCounters(c);
    RegModel m = readModel(c);
    std::mt19937 rng(seed);
    std::vector<double> cc_writes;
    const bool trig = (c.benchCaps() & bench::CAP_CHARS) != 0;
    const double since = c.nowMs();
    size_t trig_sent = 0;
    const int packets = c.iparam("test_packets");
    auto traffic = [&] {
        ctrlTraffic(c, m, c.iparam("commands"), -1, rng, &cc_writes);
        c.sendOnly(std::vector<Words>(size_t(packets), hostTestPacket(0)), 100);
    };
    if (pixel) {
        const std::vector<PixelImage> sent = randomImages(c, rng, size_t(c.iparam("frames")));
        const auto pk = streamPackets(injectImages(c, sent, traffic));
        injectedScoreboard(c, pk, sent, false, 0, what);
    } else {
        auto cap = streamAround(c, size_t(c.iparam("images")), traffic);
        const auto sb = streamScoreboard(c, streamPackets(cap), pat, 0, cc_writes, what);
        c.expect(sb.complete >= size_t(c.iparam("min_complete")), "%s%zu complete images (>= %d)", what, sb.complete,
                 c.iparam("min_complete"));
    }
    expectCounters(c, uint64_t(packets), 0);
    // The two host triggers last: a device that loses the uplink to a Table 15
    // trigger has shown everything else by then.
    if (trig) {
        for (bool rising : {true, false}) {
            c.sendChars(lsTrigger(rising, 0));
            ++trig_sent;
        }
        c.waitShort(ShortPacket::Kind::IoAck, trig_sent, since, 500);
        size_t io = 0;
        for (const auto& s : c.shortPackets(since)) io += s.pkt.kind == ShortPacket::Kind::IoAck;
        c.expect(io == trig_sent, "%s%zu host triggers, %zu I/O acknowledgments", what, trig_sent, io);
    } else {
        c.note("%sno bench character link: the host triggers are not sent", what);
    }
}

void shortTailImage(Context& c) {
    const bool pixel = usePixelPort(c);  // first: a bench reset clears the registers
    const uint32_t spsm = uint32_t(c.iparam("spsm"));
    const uint32_t w = uint32_t(c.iparam("frame.width")), h = uint32_t(c.iparam("frame.height"));
    c.prepareStreaming();
    c.preserve(Reg::STREAM_PACKET_SIZE_MAX);
    c.wr32(Reg::STREAM_PACKET_SIZE_MAX, spsm);
    std::vector<StreamPkt> pk;
    if (pixel) {
        const std::vector<PixelImage> sent = {rampImage(w, h)};
        pk = streamPackets(injectImages(c, sent));
        injectedScoreboard(c, pk, sent, false, spsm);
    } else {
        const int pat = selectBars(c);
        preserveFeature(c, "Width");
        preserveFeature(c, "Height");
        if (!trySet(c, "Width", Value::ofInt(w)) || !trySet(c, "Height", Value::ofInt(h))) {
            c.abort(strprintf("cannot set the %u x %u geometry", w, h));
        }
        pk = streamPackets(c.acquire(c.iparam("images")));
        const auto sb = streamScoreboard(c, pk, pat, spsm);
        c.expect(sb.complete >= 1, "%zu complete %u x %u images", sb.complete, w, h);
    }
    if (!pk.empty()) {
        std::map<size_t, size_t> sizes;
        for (const auto& p : pk) ++sizes[p.payload.size()];
        std::string hist;
        for (auto [n, k] : sizes) hist += strprintf(" %zu words x%zu", n, k);
        c.info("packet payloads:%s", hist.c_str());
    }
}

}  // namespace cxp::validation::checks::uvm
