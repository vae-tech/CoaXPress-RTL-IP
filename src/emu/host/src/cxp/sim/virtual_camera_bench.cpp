// The virtual camera's bench: character frames, triggers, the pixel port and
// the straps a lab would wire (protocol/bench.h).  See virtual_camera.h.

#include "cxp/sim/virtual_camera.h"

#include "cxp/image/reconstruct.h"
#include "cxp/protocol/bench.h"
#include "cxp/protocol/crc.h"

namespace cxp {

namespace {

// Host-side trigger acknowledgment wait before the next Device trigger may
// go out anyway (§8.3.3 transmission timeout; the camera's default).
constexpr int kTrigAckTimeoutMs = 10;

constexpr uint32_t kMarker = replicateByte(K28_3);  // §9.2 stream marker

constexpr uint32_t caps() {
    using namespace bench;
    return CAP_CHARS | CAP_TRIG_IN | CAP_TRIG_OUT | CAP_EXT_LINK | CAP_TRIG_POLARITY | CAP_PIXEL | CAP_ARBITRARY |
           CAP_REG_ERR | CAP_SYNC | CAP_RESET;
}

// A Table 15 leader at i: two of the three characters in place are enough
// (§8.2.2, one damaged copy is out-voted); the two leaders differ in every
// position, so at most one of them matches.
bool isLsTrigger(const Chars& c, size_t i, bool* rising) {
    if (i + 6 > c.size()) return false;
    const auto hits = [&](uint8_t a, uint8_t b, uint8_t d) {
        const uint8_t want[3] = {a, b, d};
        int n = 0;
        for (size_t k = 0; k < 3; ++k) n += c[i + k].k && c[i + k].v == want[k];
        return n;
    };
    const bool r = hits(K28_2, K28_4, K28_4) >= 2;
    const bool f = hits(K28_4, K28_2, K28_2) >= 2;
    *rising = r;
    return r || f;
}

// The Delay of a Table 15 packet: two data copies alike and within 0..239,
// else -1 (a glitch: no trigger, no acknowledgment).
int lsDelay(const Chars& c, size_t i) {
    const Char a = c[i + 3], b = c[i + 4], d = c[i + 5];
    int v = -1;
    if (!a.k && ((!b.k && a.v == b.v) || (!d.k && a.v == d.v))) v = a.v;
    else if (!b.k && !d.k && b.v == d.v) v = b.v;
    return v > 239 ? -1 : v;
}

bool isIoAck(const Chars& c, size_t i) {
    if (i + 8 > c.size()) return false;
    for (size_t k = 0; k < 4; ++k) {
        if (!c[i + k].k || c[i + k].v != K28_6 || c[i + 4 + k].k) return false;
    }
    return true;
}

// Bits per pixel of the Table 25 monochrome codes the camera lists.
int pixelBitsOf(uint32_t pixfmt) {
    switch (pixfmt) {
    case 0x0102: return 10;
    case 0x0103: return 12;
    case 0x0104: return 14;
    case 0x0105: return 16;
    default: return 8;
    }
}

// One line of pixels (§9.4.2, Figures 27-31): MSB first, the MSB of the
// first pixel in P0 bit 7; P0 is bits 7:0 of a word.  No packing across
// lines; the unused bits of the last word are 0.
Words packLine(const uint16_t* px, uint32_t n, int bits) {
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

void put24(Words& w, uint32_t v) {
    w.push_back(replicateByte(uint8_t(v >> 16)));
    w.push_back(replicateByte(uint8_t(v >> 8)));
    w.push_back(replicateByte(uint8_t(v)));
}

}  // namespace

void VirtualCamera::sendSide(uint32_t magic, const Words& body) {
    try {
        ep_->sendFrame(body, magic);
    } catch (const LinkClosed&) {
    }
}

void VirtualCamera::benchEvent(uint32_t pin, uint32_t value) {
    const uint64_t ns = uint64_t(
        std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - t0_).count());
    sendSide(bench::MAGIC, {bench::PIN_EDGE, pin, value, uint32_t(ns), uint32_t(ns >> 32)});
}

// A CXC1 frame: Table 15 triggers and Table 17 acknowledgments may sit at
// any character boundary, also inside a command (§8.2.4).  They are taken
// out; what is left goes back together into words and is served like CXP1
// frames, one SOP..EOP packet at a time.
void VirtualCamera::handleChars(const Chars& chars) {
    Chars rest;
    for (size_t i = 0; i < chars.size();) {
        bool rising = false;
        if (isLsTrigger(chars, i, &rising)) {
            hostTrigger(rising, lsDelay(chars, i));
            i += 6;
            continue;
        }
        if (isIoAck(chars, i)) {
            {
                std::lock_guard<std::mutex> lk(trig_mu_);
                trig_acked_ = true;
            }
            trig_cv_.notify_all();
            i += 8;
            continue;
        }
        rest.push_back(chars[i++]);
    }
    if (rest.size() % 4) {
        log_.warning("%zu characters left over after the short packets; the last %zu dropped", rest.size(),
                     rest.size() % 4);
    }
    Words frame;
    for (size_t i = 0; i + 4 <= rest.size(); i += 4) {
        uint32_t w = 0;
        for (int l = 0; l < 4; ++l) w |= uint32_t(rest[i + size_t(l)].v) << (8 * l);
        if (w == IDLE_WORD) continue;
        if (w == SOP_WORD) frame.clear();
        frame.push_back(w);
        if (w == EOP_WORD) {
            handleFrame(frame);
            frame.clear();
        }
    }
}

void VirtualCamera::hostTrigger(bool rising, int delay) {
    // §8.3: the I/O channel is defined for the Master connection only.
    if (ext_link_) {
        log_.info("host trigger on an extension connection: ignored");
        return;
    }
    if (delay < 0) {
        log_.info("host trigger %s with no valid Delay: glitch, not acknowledged", rising ? "rising" : "falling");
        benchEvent(bench::TRIG_GLITCH, 1);
        return;
    }
    log_.info("host trigger %s, delay %d", rising ? "rising" : "falling", delay);
    bool edge = false;
    {
        std::lock_guard<std::mutex> lk(trig_mu_);
        edge = trig_out_ != rising;
        trig_out_ = rising;
    }
    if (edge) benchEvent(bench::TRIG_OUT, rising ? 1 : 0);
    sendSide(CHARS_MAGIC, charsToWords(ioAck(IOACK_OK)));
}

void VirtualCamera::setTrigIn(bool level) {
    std::lock_guard<std::mutex> lk(trig_mu_);
    if (level == trig_in_) return;
    trig_in_ = level;
    trig_pending_.push_back(level != trig_polarity_);  // rising trigger packet?
    trig_cv_.notify_all();
}

// Device -> host triggers (Table 16), one outstanding at a time (§8.3.3).
void VirtualCamera::trigLoop() {
    std::unique_lock<std::mutex> lk(trig_mu_);
    while (running_) {
        trig_cv_.wait_for(lk, std::chrono::milliseconds(100), [this] { return !running_ || !trig_pending_.empty(); });
        if (!running_ || trig_pending_.empty()) continue;
        if (!trig_acked_) {
            trig_cv_.wait_for(lk, std::chrono::milliseconds(kTrigAckTimeoutMs), [this] { return trig_acked_; });
        }
        const bool rising = trig_pending_.front();
        trig_pending_.pop_front();
        trig_acked_ = false;
        lk.unlock();
        sendSide(CHARS_MAGIC, charsToWords(hsTrigger(rising, 0)));
        lk.lock();
    }
}

void VirtualCamera::handleBench(const Words& b) {
    if (b.empty()) return;
    auto arg = [&](size_t i) { return i < b.size() ? b[i] : 0u; };
    switch (b[0]) {
    case bench::HELLO:
        sendSide(bench::MAGIC, {bench::HELLO | bench::REPLY, bench::VERSION, caps()});
        break;
    case bench::PIN:
        switch (arg(1)) {
        case bench::TRIG_IN: setTrigIn(arg(2) != 0); break;
        case bench::EXT_LINK: ext_link_ = arg(2) != 0; break;
        case bench::TRIG_POLARITY: trig_polarity_ = arg(2) != 0; break;
        case bench::USE_TPG: use_tpg_ = arg(2) != 0; break;
        case bench::TPG_RUN: w32(R_TPG_RUN, arg(2) != 0); break;
        case bench::ARBITRARY: arbitrary_ = arg(2) != 0; break;
        default: log_.warning("bench: no pin %u", arg(1));
        }
        break;
    case bench::REG_ERR:
        reg_err_ = arg(1) & 0xFF;
        break;
    case bench::PIXEL_FRAME:
        pixelFrame(b);
        break;
    case bench::SYNC:
        sendSide(bench::MAGIC, {bench::SYNC | bench::REPLY, arg(1)});
        break;
    case bench::GET_PINS: {
        uint32_t v = 0;
        {
            std::lock_guard<std::mutex> lk(trig_mu_);
            v |= trig_in_ ? 1u << (bench::TRIG_IN - 1) : 0u;
        }
        v |= ext_link_ ? 1u << (bench::EXT_LINK - 1) : 0u;
        v |= trig_polarity_ ? 1u << (bench::TRIG_POLARITY - 1) : 0u;
        v |= use_tpg_ ? 1u << (bench::USE_TPG - 1) : 0u;
        v |= r32(R_TPG_RUN) ? 1u << (bench::TPG_RUN - 1) : 0u;
        v |= arbitrary_ ? 1u << (bench::ARBITRARY - 1) : 0u;
        sendSide(bench::MAGIC, {bench::GET_PINS | bench::REPLY, v});
        break;
    }
    case bench::RESET: {
        // Power-on state (registers, stream tags) with the inputs as asked.
        auto pin = [&](uint32_t p) { return (arg(1) >> (p - 1) & 1u) != 0; };
        resetDevice();
        reg_err_ = 0;
        {
            // Out of reset the trigger is de-asserted (§8.3.2) and nothing is
            // pending: the input's level is taken, not sent as an edge.
            std::lock_guard<std::mutex> lk(trig_mu_);
            trig_polarity_ = pin(bench::TRIG_POLARITY);
            trig_in_ = pin(bench::TRIG_IN);
            trig_pending_.clear();
            trig_acked_ = true;
        }
        ext_link_ = pin(bench::EXT_LINK);
        use_tpg_ = pin(bench::USE_TPG);
        w32(R_TPG_RUN, pin(bench::TPG_RUN));
        arbitrary_ = pin(bench::ARBITRARY);
        sendSide(bench::MAGIC, {bench::RESET | bench::REPLY});
        break;
    }
    default:
        log_.warning("bench: op %u not carried out", b[0]);
    }
}

// PIXEL_FRAME: one image through the pixel port.  With USE_TPG = 1 the port
// is not the source and the pixels are not taken.  As on the RTL bench, a
// pixel is a sample of the image's format width (b[5]); the image goes out
// in the PixelFormat register's format, the sample MSB-aligned into it
// (§9.4.2, Figure 32), and the header names that format.
void VirtualCamera::pixelFrame(const Words& b) {
    if (b.size() < 12) return;
    const uint32_t xsize = b[1], ysize = b[2], xoffs = b[3], yoffs = b[4], tapg = b[6],
                   streamid = b[7], sourcetag = b[8], flags = b[9], npix = b[11];
    const uint32_t reg_fmt = pixelFFromPfnc(r32(R_PIXFMT));
    const uint32_t pixfmt = reg_fmt ? reg_fmt : b[5];
    const int in_bits = pixelBitsOf(b[5]);
    std::vector<uint16_t> px(npix);
    for (uint32_t i = 0; i < npix && 12 + i / 2 < b.size(); ++i) px[i] = uint16_t(b[12 + i / 2] >> (16 * (i % 2)));
    uint32_t accepted = 0;
    if (!use_tpg_ && xsize && ysize && uint64_t(xsize) * ysize <= npix) {
        const int bits = pixelBitsOf(pixfmt);
        for (uint16_t& v : px) {
            const uint32_t s = v & ((1u << in_bits) - 1);
            v = uint16_t(bits >= in_bits ? s << (bits - in_bits) : s >> (in_bits - bits));
        }
        const uint32_t dsize_l = uint32_t((uint64_t(xsize) * uint64_t(bits) + 31) / 32);
        Words words;
        if (arbitrary_) {  // Table 40 header, Table 41 marker before every line
            words = {kMarker, replicateByte(0x03), replicateByte(uint8_t(streamid)),
                     replicateByte(uint8_t(sourcetag >> 8)), replicateByte(uint8_t(sourcetag))};
            put24(words, ysize);
            put24(words, yoffs);
            for (uint32_t v : {pixfmt >> 8, pixfmt, tapg >> 8, tapg, flags}) words.push_back(replicateByte(uint8_t(v)));
        } else {
            words = cxpImageHeaderRect(streamid, sourcetag, xsize, xoffs, ysize, yoffs, dsize_l, pixfmt, tapg, flags);
        }
        for (uint32_t y = 0; y < ysize; ++y) {
            if (arbitrary_) {
                words.push_back(kMarker);
                words.push_back(replicateByte(0x04));
                put24(words, xsize);
                put24(words, xoffs);
                put24(words, dsize_l);
            } else {
                const Words lm = cxpLineMarkerRect();
                words.insert(words.end(), lm.begin(), lm.end());
            }
            const Words line = packLine(px.data() + size_t(y) * xsize, xsize, bits);
            words.insert(words.end(), line.begin(), line.end());
        }
        emitImage(std::move(words), false);
        accepted = xsize * ysize;
    }
    sendSide(bench::MAGIC, {bench::PIXEL_FRAME | bench::REPLY, accepted});
}

}  // namespace cxp
