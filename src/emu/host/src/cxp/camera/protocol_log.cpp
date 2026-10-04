#include "cxp/camera/protocol_log.h"

#include <chrono>
#include <ctime>
#include <stdexcept>

#include "cxp/protocol/bench.h"
#include "cxp/protocol/chars.h"
#include "cxp/protocol/crc.h"
#include "cxp/protocol/packets.h"
#include "cxp/utils/log.h"

namespace cxp {

namespace {

constexpr uint32_t MARKER_WORD = 0x7C7C7C7Cu;  // 4 x K28.3 (§9.4)

// Majority byte of a 4x replicated word, flagged when the lanes differ.
std::string rep(uint32_t w) {
    const Majority m = majorityByte(w);
    if (!m.ok) return strprintf("?(0x%08X)", w);
    if (w != replicateByte(m.value)) return strprintf("0x%02X(!0x%08X)", m.value, w);
    return strprintf("0x%02X", m.value);
}

int voted(uint32_t w) {
    const Majority m = majorityByte(w);
    return m.ok ? m.value : -1;
}

const char* opName(uint8_t op) {
    switch (op) {
    case 0x00: return "READ";
    case 0x01: return "WRITE";
    case 0xFF: return "RESET";
    default: return nullptr;
    }
}

std::string ackText(int code) {
    if (code < 0) return "code=?";
    if (auto c = toAckCode(uint8_t(code))) return strprintf("0x%02X %s", code, ackCodeName(*c));
    return strprintf("0x%02X (reserved)", code);
}

// CRC word at f[n-2] against the CRC of f[from .. n-2).
const char* crcText(const Words& f, size_t from) {
    if (f.size() < from + 2) return "crc=?";
    const uint32_t crc = crc32Words(f.data() + from, f.size() - 2 - from);
    return wireToCrc(f[f.size() - 2]) == crc ? "crc=ok" : "crc=BAD";
}

std::string values(const Words& f, size_t from, size_t to, size_t max) {
    std::string s;
    for (size_t i = from; i < to && i - from < max; ++i) {
        s += strprintf("%s%08X", s.empty() ? "" : " ", bswap32(f[i]));
    }
    if (to - from > max) s += strprintf(" ... (+%zu)", to - from - max);
    return s;
}

std::string timestamp() {
    using namespace std::chrono;
    const auto now = system_clock::now();
    const std::time_t t = system_clock::to_time_t(now);
    std::tm tm{};
    localtime_r(&t, &tm);
    const int ms = int(duration_cast<milliseconds>(now.time_since_epoch()).count() % 1000);
    return strprintf("%02d:%02d:%02d.%03d", tm.tm_hour, tm.tm_min, tm.tm_sec, ms);
}

}  // namespace

ProtocolLog::ProtocolLog(const std::string& path) { open(path); }

ProtocolLog::~ProtocolLog() { close(); }

void ProtocolLog::open(const std::string& path) {
    std::lock_guard<std::mutex> lk(mu_);
    if (fh_) std::fclose(fh_);
    fh_ = std::fopen(path.c_str(), "w");
    if (!fh_) {
        path_.clear();
        throw std::runtime_error("cannot open protocol log " + path);
    }
    std::setvbuf(fh_, nullptr, _IOFBF, 1 << 20);
    last_flush_ = std::chrono::steady_clock::now();
    path_ = path;
}

void ProtocolLog::close() {
    std::lock_guard<std::mutex> lk(mu_);
    if (fh_) std::fclose(fh_);
    fh_ = nullptr;
}

bool ProtocolLog::isOpen() const {
    std::lock_guard<std::mutex> lk(mu_);
    return fh_ != nullptr;
}

void ProtocolLog::tx(const Words& f) { frame("TX", f); }
void ProtocolLog::rx(const Words& f) { frame("RX", f); }

void ProtocolLog::side(const char* dir, uint32_t magic, const Words& body) {
    {
        std::lock_guard<std::mutex> lk(mu_);
        if (!fh_) return;
    }
    line(timestamp() + "  " + dir + "  " + describeSide(magic, body) + "\n");
}

std::string ProtocolLog::describeSide(uint32_t magic, const Words& b) {
    if (magic == CHARS_MAGIC) {
        const Chars ch = wordsToChars(b);
        if (ch.size() == 8) {
            const ShortPacket p = decodeShortPacket(ch);
            if (p.kind != ShortPacket::Kind::Unknown) return "CHARS " + describeShortPacket(p);
        }
        return strprintf("CHARS %zu  ", ch.size()) + describeChars(ch, 48);
    }
    if (magic != bench::MAGIC) return strprintf("frame magic 0x%08X, %zu words", magic, b.size());
    auto arg = [&](size_t i) { return i < b.size() ? b[i] : 0u; };
    const uint32_t op = arg(0);
    auto pin = [](uint32_t p) -> std::string {
        switch (p) {
        case bench::TRIG_IN: return "TRIG_IN";
        case bench::EXT_LINK: return "EXT_LINK";
        case bench::TRIG_POLARITY: return "TRIG_POLARITY";
        case bench::USE_TPG: return "USE_TPG";
        case bench::TPG_RUN: return "TPG_RUN";
        case bench::ARBITRARY: return "ARBITRARY";
        case bench::TRIG_OUT: return "TRIG_OUT";
        case bench::TRIG_GLITCH: return "TRIG_GLITCH";
        default: return strprintf("pin %u", p);
        }
    };
    switch (op) {
    case bench::HELLO: return "BENCH HELLO";
    case bench::HELLO | bench::REPLY: return strprintf("BENCH HELLO reply version %u caps 0x%04X", arg(1), arg(2));
    case bench::PIN: return strprintf("BENCH PIN %s = %u", pin(arg(1)).c_str(), arg(2));
    case bench::REG_ERR: return arg(1) ? strprintf("BENCH REG_ERR every access answers 0x%02X", arg(1))
                                       : std::string("BENCH REG_ERR off");
    case bench::UPLINK_PPM: return strprintf("BENCH UPLINK_PPM %d ppm", int32_t(arg(1)));
    case bench::PIXEL_FRAME:
        return strprintf("BENCH PIXEL_FRAME %ux%u offs %u,%u PixelF 0x%04X valid %u/1000, %u pixels", arg(1), arg(2),
                         arg(3), arg(4), arg(5), arg(10), arg(11));
    case bench::PIXEL_FRAME | bench::REPLY: return strprintf("BENCH PIXEL_FRAME done, %u pixels accepted", arg(1));
    case bench::SYNC: return strprintf("BENCH SYNC %u", arg(1));
    case bench::SYNC | bench::REPLY: return strprintf("BENCH SYNC reply %u", arg(1));
    case bench::GET_PINS: return "BENCH GET_PINS";
    case bench::GET_PINS | bench::REPLY: return strprintf("BENCH GET_PINS reply inputs 0x%02X", arg(1));
    case bench::RESET: return strprintf("BENCH RESET, inputs 0x%02X", arg(1));
    case bench::RESET | bench::REPLY: return "BENCH RESET done";
    case bench::PIN_EDGE:
        return strprintf("BENCH EDGE %s = %u at %llu ns", pin(arg(1)).c_str(), arg(2),
                         (unsigned long long)(uint64_t(arg(4)) << 32 | arg(3)));
    default: return strprintf("BENCH op 0x%02X, %zu words", op, b.size());
    }
}

void ProtocolLog::event(const std::string& text) { line(timestamp() + "  --  " + text + "\n", true); }

void ProtocolLog::line(const std::string& text, bool flush_now) {
    std::lock_guard<std::mutex> lk(mu_);
    if (!fh_) return;
    std::fputs(text.c_str(), fh_);
    const auto now = std::chrono::steady_clock::now();
    if (flush_now || now - last_flush_ >= std::chrono::milliseconds(FLUSH_MS)) {
        std::fflush(fh_);
        last_flush_ = now;
    }
}

void ProtocolLog::frame(const char* dir, const Words& f) {
    {
        std::lock_guard<std::mutex> lk(mu_);
        if (!fh_) return;
    }
    bool bulk = false;
    std::string out = timestamp() + "  " + dir + "  " + describe(f, &bulk) + "\n";
    // Raw words for control frames and anything that does not decode; a
    // clean stream packet is image data, a clean link-test packet a
    // counter pattern.
    if (!bulk) {
        for (size_t i = 0; i < f.size(); i += 8) {
            out += "                  ";
            for (size_t j = i; j < f.size() && j < i + 8; ++j) out += strprintf(" %08X", f[j]);
            out += "\n";
        }
    }
    line(out);
}

std::string ProtocolLog::describe(const Words& f, bool* clean_bulk) {
    const size_t n = f.size();
    if (clean_bulk) *clean_bulk = false;
    std::string framing;
    if (n == 0 || f.front() != SOP_WORD) framing += " no-SOP";
    if (n == 0 || f.back() != EOP_WORD) framing += " no-EOP";
    if (n < 3) return strprintf("FRAME %zu words%s", n, framing.c_str());
    const int type = voted(f[1]);
    std::string s;
    switch (type) {
    case 0x02: {  // Table 21
        if (n < 6) {
            s = "CTRL_CMD short frame";
            break;
        }
        const uint8_t op = uint8_t(f[2]);
        const uint32_t size = (f[2] >> 8 & 0xFF) << 16 | (f[2] >> 16 & 0xFF) << 8 | f[2] >> 24;
        const char* name = opName(op);
        s = strprintf("CTRL_CMD %s addr=0x%08X size=%u", name ? name : strprintf("op=0x%02X", op).c_str(),
                      bswap32(f[3]), size);
        if (n > 6) s += " data=[" + values(f, 4, n - 2, 16) + "]";
        s += std::string(" ") + crcText(f, 2);
        break;
    }
    case 0x03: {  // Table 22
        s = "CTRL_ACK " + ackText(voted(f[2]));
        if (!framing.empty() || n == 4) break;
        if (n < 6) {
            s += " long form too short";
            break;
        }
        s += strprintf(" size=%u", bswap32(f[3]));
        if (n > 6) s += " data=[" + values(f, 4, n - 2, 16) + "]";
        s += std::string(" ") + crcText(f, 2);
        break;
    }
    case 0x01: {  // Table 19
        if (n < 8) {
            s = "STREAM short frame";
            break;
        }
        const int hi = voted(f[4]), lo = voted(f[5]);
        s = "STREAM sid=" + rep(f[2]) + " tag=" + rep(f[3]) +
            strprintf(" dsizeP=%d payload=%zu", hi < 0 || lo < 0 ? -1 : hi << 8 | lo, n - 8);
        size_t hdr = 0, line = 0;
        for (size_t i = 6; i + 1 < n - 2; ++i) {
            if (f[i] != MARKER_WORD) continue;
            hdr += f[i + 1] == replicateByte(0x01);
            line += f[i + 1] == replicateByte(0x02);
        }
        if (hdr) s += strprintf(" image-header=%zu", hdr);
        if (line) s += strprintf(" line-markers=%zu", line);
        const char* crc = crcText(f, 6);
        s += std::string(" ") + crc;
        if (clean_bulk) *clean_bulk = framing.empty() && hi >= 0 && lo >= 0 && size_t(hi << 8 | lo) == n - 8 &&
                                      std::string(crc) == "crc=ok";
        break;
    }
    case 0x04: {  // Table 23
        size_t bad = 0;
        for (size_t i = 2; i + 1 < n; ++i) bad += f[i] != linkTestWord(uint8_t(4 * (i - 2)));
        s = strprintf("LINKTEST %zu words, %zu off-pattern", n - 3, bad);
        if (clean_bulk) *clean_bulk = framing.empty() && bad == 0;
        break;
    }
    case 0x05: s = strprintf("EVENT %zu words", n); break;
    case 0x06: s = strprintf("DISCOVERY %zu words", n); break;
    default: s = "FRAME type=" + rep(f[1]); break;
    }
    return s + strprintf(" (%zu w)", n) + framing;
}

}  // namespace cxp
