#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks {

std::string ackStr(const OptAck& a) { return a ? ackName(a->code) : "no acknowledgment"; }

bool is(const OptAck& a, int code) { return a && a->code == code; }

bool replicatedWord(uint32_t w) { return w == replicateByte(uint8_t(w)); }

std::string hex(uint32_t v) { return strprintf("0x%08X", v); }

bool isDiscoveryConfig(uint32_t cc) {
    return (cc >> 16) == 1 && ((cc & 0xFFFF) == 0x28 || (cc & 0xFFFF) == 0x38);
}

void preserveLink(Context& c) {
    c.preserve(Reg::CONNECTION_CONFIG);
    c.preserve(Reg::STREAM_PACKET_SIZE_MAX);
    c.preserve(Reg::MASTER_HOST_CONNECTION_ID);
}

void connectionReset(Context& c) {
    c.sendOnly({writeCmd(Reg::CONNECTION_RESET, {1})}, 0);
    c.sleepMs(200);
}

bool streamContinues(Context& c, int ms) {
    const size_t s0 = c.streamPacketsSeen();
    const double end = c.nowMs() + c.wait(ms);
    while (c.streamPacketsSeen() == s0 && c.nowMs() < end) c.sleepRawMs(5);
    return c.streamPacketsSeen() > s0;
}

void preserveFeature(Context& c, const std::string& name) {
    Feature* f = c.feature(name);
    if (!f || !f->isReadable() || !f->isWritable()) return;
    if (c.tree()) c.tree()->invalidate();
    if (f->reg) {
        const uint32_t addr = uint32_t(f->reg->address);
        try {
            const std::vector<uint8_t> old = c.cam().readBytes(addr, f->reg->length);
            c.onExit([&c, addr, old] {
                c.cam().writeBytes(addr, old);
                if (c.tree()) c.tree()->invalidate();
            });
        } catch (const std::exception&) {
        }
        return;
    }
    try {
        Value old = f->getValue();
        c.onExit([&c, f, old] {
            f->setValue(old);
            if (c.tree()) c.tree()->invalidate();
        });
    } catch (const std::exception&) {
    }
}

bool trySet(Context& c, const std::string& name, const Value& v) {
    try {
        c.setFeature(name, v);
        return true;
    } catch (const Skip&) {
        throw;
    } catch (const std::exception& e) {
        c.note("%s = %s not accepted: %s", name.c_str(), v.str().c_str(), e.what());
        return false;
    }
}

std::optional<int64_t> featInt(Context& c, const std::string& name) {
    Feature* f = c.feature(name);
    if (!f || !f->isReadable()) return std::nullopt;
    try {
        if (c.tree()) c.tree()->invalidate();
        Value v = f->getValue();
        if (v.type == Value::Type::Int) return v.i;
    } catch (const std::exception&) {
    }
    return std::nullopt;
}

Feature* featureOrAlias(Context& c, const std::string& sfnc, const std::string& alias,
                        std::string* used) {
    if (Feature* f = c.feature(sfnc)) {
        if (used) *used = sfnc;
        return f;
    }
    if (Feature* f = c.feature(alias)) {
        if (used) *used = alias;
        return f;
    }
    return nullptr;
}

std::vector<ImageRec> imagesOf(const std::vector<Captured>& cap) {
    return walkImages(streamPackets(cap));
}

size_t specLineWords(const ImageRec& im) {
    auto bits = pixelBits(im.pixel_f);
    return bits ? (size_t(im.xsize) * size_t(*bits) + 31) / 32 : 0;
}

bool imageComplete(const ImageRec& im) {
    if (!im.header_complete || im.lines.size() != im.ysize || im.ysize == 0) return false;
    if (im.arbitrary) {  // each Table 41 marker states its own line
        auto bits = pixelBits(im.pixel_f);
        for (size_t y = 0; y < im.lines.size(); ++y) {
            const size_t want = bits ? (size_t(im.line_xsize[y]) * size_t(*bits) + 31) / 32 : 0;
            if (im.line_words[y] != im.line_dsize_l[y] || (want && im.line_words[y] != want)) return false;
        }
        return true;
    }
    const size_t want = specLineWords(im);
    for (size_t w : im.line_words) {
        if (want ? w != want : w == 0) return false;
    }
    return true;
}

std::vector<ImageRec> completeImages(const std::vector<ImageRec>& ims) {
    std::vector<ImageRec> out;
    for (const auto& im : ims) {
        if (imageComplete(im)) out.push_back(im);
    }
    return out;
}

std::vector<uint8_t> lineBytesP0(const Words& line) {
    std::vector<uint8_t> out;
    for (uint32_t w : line) {
        for (int k = 0; k < 4; ++k) out.push_back(uint8_t(w >> (8 * k)));
    }
    return out;
}

std::vector<uint8_t> lineBytesP3(const Words& line) {
    std::vector<uint8_t> out;
    for (uint32_t w : line) {
        for (int k = 3; k >= 0; --k) out.push_back(uint8_t(w >> (8 * k)));
    }
    return out;
}

uint32_t imageRequired(Context& c, size_t want, size_t got, const char* what) {
    c.expect(got >= want, "%zu of %zu %s received", got, want, what);
    if (got == 0) c.abort(strprintf("no %s received; cannot continue", what));
    return uint32_t(got);
}

std::vector<uint32_t> pktTags(const std::vector<StreamPkt>& pk, uint8_t sid) {
    std::vector<uint32_t> t;
    for (const auto& p : pk) {
        if (p.stream_id == sid) t.push_back(p.tag);
    }
    return t;
}

size_t tagBreaks(const std::vector<uint32_t>& tags, int* first_bad) {
    size_t n = 0;
    for (size_t i = 1; i < tags.size(); ++i) {
        if (tags[i] != ((tags[i - 1] + 1) & 0xFF)) {
            if (n == 0 && first_bad) *first_bad = int(i);
            ++n;
        }
    }
    return n;
}

Words hostTestPacket(int corrupt_words) {
    Words w;
    w.push_back(SOP_WORD);
    w.push_back(replicateByte(0x04));
    for (uint32_t k = 0; k < 1024; ++k) w.push_back(linkTestWord(uint8_t(4 * k)));
    for (int i = 0; i < corrupt_words; ++i) w[2 + size_t(i) * 7] ^= 0x00FF0000u;
    w.push_back(EOP_WORD);
    return w;
}

int linkTestErrors(const Words& f) {
    if (f.size() != 1027) return -1;
    int bad = 0;
    for (uint32_t k = 0; k < 1024; ++k) bad += f[2 + k] != linkTestWord(uint8_t(4 * k));
    return bad;
}

std::vector<Captured> framesOfType(const std::vector<Captured>& cap, uint8_t type) {
    std::vector<Captured> out;
    for (const auto& c : cap) {
        if (c.frame.size() >= 3 && majorityByte(c.frame[1]).ok &&
            majorityByte(c.frame[1]).value == type) {
            out.push_back(c);
        }
    }
    return out;
}

double percentile(std::vector<double> v, double p) {
    if (v.empty()) return 0;
    std::sort(v.begin(), v.end());
    size_t i = size_t(std::ceil(p / 100.0 * double(v.size()))) - 1;
    return v[std::min(i, v.size() - 1)];
}

uint32_t usableCpsm(Context& c) {
    const uint32_t cpsm = c.rd32(Reg::CONTROL_PACKET_SIZE_MAX);
    const bool ok = cpsm >= 128 && cpsm % 4 == 0 && cpsm <= 0x10000;
    c.expect(ok, "ControlPacketSizeMax = %u (>= 128, multiple of 4)", cpsm);
    if (!ok) c.abort(strprintf("ControlPacketSizeMax = %u is not a usable limit; boundary cases not run", cpsm));
    return cpsm;
}

void write64(Context& c, uint32_t addr, uint64_t v) {
    auto a = c.writeRaw(addr, {uint32_t(v >> 32), uint32_t(v)});
    if (!a || (a->code != Ack::WRITE_OK && a->code != Ack::READ_OK)) {
        c.abort(strprintf("8-byte write to 0x%04X answered %s", addr, ackStr(a).c_str()));
    }
}

bool waitBenchMs(Context& c, double ms, int timeout_ms) {
    const auto t0 = c.benchTime();
    if (!t0) {
        c.sleepMs(int(ms));
        return true;
    }
    const double end = c.nowMs() + c.wait(timeout_ms);
    while (c.nowMs() < end) {
        const auto t = c.benchTime();
        if (t && double(t->now_ps - t0->now_ps) / double(t0->ms_ps) >= ms) return true;
        c.sleepRawMs(20);
    }
    return false;
}

TimedExchange timedExchange(Context& c, const Words& cmd, int timeout_ms, int extra_ms) {
    TimedExchange r;
    const bool timed = (c.benchCaps() & bench::CAP_TIMES) != 0;
    const double t0 = c.nowMs();
    r.acks = c.exchangeFinal(cmd, timeout_ms, extra_ms);
    if (!timed) return r;
    const auto bt = c.benchTime();  // every event the bench sent before its reply is in
    if (bt) r.ms_ps = bt->ms_ps;
    const auto marks = c.uplinkMarks(t0);
    for (const auto& m : marks) {
        if (m.chr == (0x100u | K27_7) && !r.cmd_start_ps) r.cmd_start_ps = m.time_ps;
        if (m.chr == (0x100u | K29_7) && r.cmd_start_ps && !r.cmd_end_ps) {
            r.cmd_end_ps = m.time_ps + uint64_t(40) * m.bit_ps;  // 4 characters of 10 bits
        }
    }
    // The FRAME_DL event of an acknowledgment that arrived just before t0
    // (the previous exchange's) can land after t0: only packets that started
    // after this command did belong to it.
    std::vector<FrameTime> acks;
    for (const auto& f : c.frameTimes(t0)) {
        if (f.type == 0x03 && f.sop_ps >= r.cmd_start_ps) acks.push_back(f);
    }
    r.times.assign(acks.begin(), acks.begin() + std::min(acks.size(), r.acks.size()));
    r.timed = r.cmd_end_ps != 0 && r.times.size() == r.acks.size();
    return r;
}

}  // namespace cxp::validation::checks
