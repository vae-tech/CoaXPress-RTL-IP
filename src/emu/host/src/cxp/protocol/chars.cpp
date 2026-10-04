#include "cxp/protocol/chars.h"

#include <algorithm>

#include "cxp/utils/log.h"

namespace cxp {

Words charsToWords(const Chars& chars) {
    Words out;
    out.reserve(chars.size());
    for (const Char& c : chars) out.push_back(uint32_t(c.v) | (c.k ? 0x100u : 0u));
    return out;
}

Chars wordsToChars(const Words& words) {
    Chars out;
    out.reserve(words.size());
    for (uint32_t w : words) out.push_back({uint8_t(w), (w & 0x100u) != 0});
    return out;
}

namespace {

unsigned lanesEqual(uint32_t w, uint8_t k) {
    unsigned m = 0;
    for (int i = 0; i < 4; ++i) m |= (uint8_t(w >> (8 * i)) == k) ? 1u << i : 0u;
    return m;
}

int popcount4(unsigned m) { return int(m & 1) + int(m >> 1 & 1) + int(m >> 2 & 1) + int(m >> 3 & 1); }

}  // namespace

Chars frameChars(const Words& frame) {
    Chars out;
    out.reserve(frame.size() * 4);
    for (size_t i = 0; i < frame.size(); ++i) {
        unsigned km = 0;
        if (i == 0) km = lanesEqual(frame[i], K27_7);
        else if (i + 1 == frame.size()) km = lanesEqual(frame[i], K29_7);
        if (popcount4(km) < 3) km = 0;
        for (int l = 0; l < 4; ++l) out.push_back({uint8_t(frame[i] >> (8 * l)), ((km >> l) & 1) != 0});
    }
    return out;
}

Chars lsTrigger(bool rising, uint8_t delay) {
    const uint8_t a = rising ? K28_2 : K28_4, b = rising ? K28_4 : K28_2;
    return {{a, true}, {b, true}, {b, true}, {delay, false}, {delay, false}, {delay, false}};
}

Chars hsTrigger(bool rising, uint8_t delay) {
    const uint8_t k = rising ? K28_4 : K28_2;
    return {{k, true}, {k, true}, {k, true}, {k, true}, {delay, false}, {delay, false}, {delay, false}, {delay, false}};
}

Chars ioAck(uint8_t code) {
    return {{K28_6, true}, {K28_6, true}, {K28_6, true}, {K28_6, true},
            {code, false}, {code, false}, {code, false}, {code, false}};
}

Chars insertAt(const Chars& inner, const Chars& outer, size_t at) {
    Chars out(inner.begin(), inner.begin() + std::min(at, inner.size()));
    out.insert(out.end(), outer.begin(), outer.end());
    if (at < inner.size()) out.insert(out.end(), inner.begin() + at, inner.end());
    return out;
}

ShortPacket decodeShortPacket(const Chars& chars) {
    ShortPacket p;
    p.chars = chars;
    if (chars.size() != 8) {
        p.clean = false;
        return p;
    }
    // Majority over the 4 K characters, then over the 4 value characters.
    auto vote = [&](size_t from, bool k, int& agree) {
        int best = -1, best_n = 0;
        for (size_t i = from; i < from + 4; ++i) {
            if (chars[i].k != k) continue;
            int n = 0;
            for (size_t j = from; j < from + 4; ++j) n += chars[j].k == k && chars[j].v == chars[i].v;
            if (n > best_n) best_n = n, best = chars[i].v;
        }
        agree = best_n;
        return best_n >= 3 ? best : -1;
    };
    int ka = 0, va = 0;
    const int k = vote(0, true, ka);
    p.value = vote(4, false, va);
    p.clean = ka == 4 && va == 4;
    if (k == K28_4) p.kind = ShortPacket::Kind::TriggerRise;
    else if (k == K28_2) p.kind = ShortPacket::Kind::TriggerFall;
    else if (k == K28_6) p.kind = ShortPacket::Kind::IoAck;
    return p;
}

std::string describeShortPacket(const ShortPacket& p) {
    const char* what = "UNKNOWN short packet";
    switch (p.kind) {
    case ShortPacket::Kind::TriggerRise: what = "TRIGGER rising"; break;
    case ShortPacket::Kind::TriggerFall: what = "TRIGGER falling"; break;
    case ShortPacket::Kind::IoAck: what = "IO_ACK"; break;
    default: break;
    }
    std::string s = what;
    if (p.kind == ShortPacket::Kind::IoAck) s += strprintf(" code=0x%02X", p.value & 0xFF);
    else if (p.kind != ShortPacket::Kind::Unknown) s += strprintf(" delay=%d", p.value);
    if (!p.clean) s += " (replicas disagree)";
    return s + "  " + describeChars(p.chars);
}

std::string describeChars(const Chars& chars, size_t max) {
    std::string s;
    for (size_t i = 0; i < chars.size() && i < max; ++i) {
        if (i) s += ' ';
        s += chars[i].k ? strprintf("K.%02X", chars[i].v) : strprintf("%02X", chars[i].v);
    }
    if (chars.size() > max) s += strprintf(" ... (%zu chars)", chars.size());
    return s;
}

}  // namespace cxp
