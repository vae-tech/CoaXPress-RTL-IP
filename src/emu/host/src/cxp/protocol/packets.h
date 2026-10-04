// CXP packet codecs, word-stream model (cxp/protocol/packets.py).
//
// A *frame* is a list of 32-bit words.  Long packets are delimited by an
// SOP word (K27.7 x4) and an EOP word (K29.7 x4).  The wire layout is
// CXP-001-2015 Tables 19-23 as encoded by the golden cxp_protocol package
// (the RTL device speaks it; tests/test_golden_vectors.cpp checks it).
// Data words are wire words: a register value goes out big-endian
// (§8.2.1), so callers bswap32() values in and out.
//
// Every codec exposes:
//
//     pkt.toWords()            -> Words   (full SOP..EOP frame)
//     Class::fromBody(body)    -> Class   (body = between SOP and EOP)
//     decodePacket(frame)      -> Packet  (auto-dispatch on TYPE)
//
// fromBody throws PacketDecodeError (with a machine-readable reason())
// on any structural / CRC fault so the compliance checker and parser can
// categorise failures.
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <variant>
#include <vector>

#include "cxp/protocol/constants.h"

namespace cxp {

// Non-owning view over a run of words (C++17 stand-in for std::span).
struct WordSpan {
    const uint32_t* ptr = nullptr;
    size_t len = 0;

    WordSpan() = default;
    WordSpan(const uint32_t* p, size_t n) : ptr(p), len(n) {}
    WordSpan(const Words& w) : ptr(w.data()), len(w.size()) {}  // NOLINT

    size_t size() const { return len; }
    bool empty() const { return len == 0; }
    uint32_t operator[](size_t i) const { return ptr[i]; }
    uint32_t front() const { return ptr[0]; }
    uint32_t back() const { return ptr[len - 1]; }
    const uint32_t* begin() const { return ptr; }
    const uint32_t* end() const { return ptr + len; }
    // [from, size()-drop_tail)
    WordSpan slice(size_t from, size_t drop_tail = 0) const {
        if (from + drop_tail >= len) return {ptr + len, 0};
        return {ptr + from, len - from - drop_tail};
    }
    Words toVector() const { return Words(begin(), end()); }
};

// Raised when a word frame cannot be decoded.  reason() is a stable token
// ("crc", "short", "bad_type", "replica", ...) consumed by the compliance
// checker and parser.
class PacketDecodeError : public std::runtime_error {
public:
    PacketDecodeError(std::string reason, const std::string& message = "")
        : std::runtime_error(message.empty() ? reason : message),
          reason_(std::move(reason)) {}
    const std::string& reason() const { return reason_; }

private:
    std::string reason_;
};

// Serialise a word frame as little-endian bytes.
std::vector<uint8_t> wordsToBytesLe(const Words& words);

// --------------------------------------------------------------------------
// Control command  (host -> device, Table 21)
//   SOP | 4xTYPE | Cmd,Size[23:16],Size[15:8],Size[7:0] | Addr (big-endian)
//       | N data words (writes) | CRC32 | EOP
// CRC32 covers the Cmd/Size word through the last data word.
// --------------------------------------------------------------------------
struct CtrlCmdPacket {
    CtrlOpcode opcode = CtrlOpcode::Read;
    uint32_t address = 0;
    uint32_t nwords = 1;  // words to read (READ) or data.size()
    Words data;
    bool corrupt_crc = false;  // test hook

    uint32_t sizeBytes() const;
    Words toWords() const;
    static CtrlCmdPacket fromBody(WordSpan body);
};

// --------------------------------------------------------------------------
// Control acknowledge  (device -> host, Table 22)
//
// Wire layout (Table 22):
//   long form  : SOP | 4xTYPE | 4xCODE | Size (big-endian, bytes)
//                | N data words | CRC32 | EOP
//   short form : SOP | 4xTYPE | 4xCODE | EOP
// The short form is mandatory for every ack code other than 0x00 (Final OK
// + reply data) and 0x04 (Wait).  Long-form CRC32 covers CODE..last-data;
// the TYPE word, the CRC word and the K27.7/K29.7 framing are not covered.
// --------------------------------------------------------------------------
struct CtrlAckPacket {
    AckCode code = AckCode::Ok;
    Words data;
    bool corrupt_crc = false;

    uint32_t sizeBytes() const { return static_cast<uint32_t>(data.size() * 4); }
    Words toWords() const;
    static CtrlAckPacket fromBody(WordSpan body);
};

// --------------------------------------------------------------------------
// Stream packet  (device -> host, Table 19)
//
//   SOP | 4xTYPE | 4xStreamID | 4xPacketTag | 4xDsizeP[15:8]
//       | 4xDsizeP[7:0] | N data words (raw) | CRC32 | EOP
// DsizeP is the payload size *in 32-bit words*.  CRC32 covers the payload
// only.
// --------------------------------------------------------------------------
struct StreamPacket {
    uint8_t stream_id = 0;
    uint8_t tag = 0;  // rolling packet counter (loss detection)
    Words payload;
    bool corrupt_crc = false;

    Words toWords() const;
    static StreamPacket fromBody(WordSpan body);
};

// --------------------------------------------------------------------------
// Event packet  (device -> host, §12.2)
// --------------------------------------------------------------------------
struct EventPacket {
    uint32_t event_id = 0;
    uint64_t timestamp = 0;
    Words data;
    bool corrupt_crc = false;

    Words toWords() const;
    static EventPacket fromBody(WordSpan body);
};

// --------------------------------------------------------------------------
// Link-test packet (Table 23) — counter pattern, no CRC; every body word
// is a test word.
// --------------------------------------------------------------------------
// Trailing word the pre-CXP-1.1.1 Python simulator appended; stripped by
// the GUI link statistics.
inline constexpr uint32_t LINKTEST_FILLER = 0xDEADBEEFu;

// Expected counter word for sequence byte `seq` (P0 in the LSB lane).
constexpr uint32_t linkTestWord(uint8_t seq) {
    return uint32_t(uint8_t(seq + 3)) << 24 | uint32_t(uint8_t(seq + 2)) << 16 |
           uint32_t(uint8_t(seq + 1)) << 8 | uint32_t(seq);
}

struct LinkTestPacket {
    uint32_t n_data = 64;  // multiple of 64 so the seq wraps to 0
    std::vector<uint32_t> error_indices;

    Words toWords() const;
    static LinkTestPacket fromBody(WordSpan body);
};

// --------------------------------------------------------------------------
// Discovery / heartbeat  (host extension, TYPE 0x06)
// --------------------------------------------------------------------------
struct DiscoveryPacket {
    uint32_t nonce = 0;
    bool is_heartbeat = false;

    Words toWords() const;
    static DiscoveryPacket fromBody(WordSpan body);
};

using Packet = std::variant<CtrlCmdPacket, CtrlAckPacket, StreamPacket,
                            EventPacket, LinkTestPacket, DiscoveryPacket>;

// Decode a full SOP..EOP frame, dispatching on the TYPE word.
Packet decodePacket(WordSpan frame);

// Return a frame's TYPE without validating its CRC.  Used to route ACK vs
// stream/event frames: a CRC-corrupted *stream* packet must still reach
// the HS parser.  nullopt if the frame is not a well-formed SOP..EOP frame.
std::optional<PacketType> peekType(WordSpan frame);

// Split a word stream into SOP..EOP frames, skipping IDLE/garbage.
std::vector<Words> findFrames(WordSpan words);

}  // namespace cxp
