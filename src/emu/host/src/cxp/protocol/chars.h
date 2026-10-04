// Character-level link frames: what the word envelope cannot say.
//
// A CXP1 link frame carries whole 32-bit words and leaves the K flags to
// the receiver (SOP / EOP words, §8.2.2).  Two things on a real link are
// not whole SOP..EOP packets, and a conformance host has to send and see
// them:
//
//   * the §8.3.2 trigger and §8.3.3 I/O-acknowledgment packets, which carry
//     K28.2 / K28.4 / K28.6 characters and no SOP, and may be inserted into
//     another packet (§8.2.4) — at any character boundary for the Table 15
//     low-speed trigger;
//   * characters that do not fill whole words.
//
// A CXC1 frame (magic 'CXC1') carries characters one per word:
//
//     bits 7:0  the character          bit 8  K flag          bits 31:9  0
//
// Host -> device, the receiver puts the characters on the wire in order and
// fills the gap after them with IDLE as usual.  Device -> host, the
// transmitter hands over each short packet (trigger, I/O acknowledgment)
// it takes out of the downlink as one CXC1 frame, with the packet it was
// inserted into delivered intact in a CXP1 frame.
//
// A device that does not know CXC1 skips the frame as garbage (the CXP1
// reader resynchronises on its magic), so the frame is safe to send to any
// device; whether it understood it is the bench's HELLO (bench.h).
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "cxp/protocol/constants.h"

namespace cxp {

inline constexpr uint32_t CHARS_MAGIC = 0x43584331u;  // b'1CXC' on the wire: 'CXC1'

inline constexpr uint8_t K28_6 = 0xDC;  // I/O acknowledgment (Table 17)
inline constexpr uint8_t IOACK_OK = 0x01;

struct Char {
    uint8_t v = 0;
    bool k = false;
    bool operator==(const Char& o) const { return v == o.v && k == o.k; }
};
using Chars = std::vector<Char>;

// CXC1 frame body <-> characters.
Words charsToWords(const Chars& chars);
Chars wordsToChars(const Words& words);

// The characters a CXP1 word frame puts on the wire, P0 first: the first
// word's K27.7 lanes and the last word's K29.7 lanes are K characters when
// at least 3 of the 4 are (a corrupted delimiter keeps its good three),
// everything else is data.
Chars frameChars(const Words& frame);

// Table 15: 3 characters K28.2 K28.4 K28.4 (rising) or K28.4 K28.2 K28.2
// (falling), then the delay 3 times.
Chars lsTrigger(bool rising, uint8_t delay);
// Table 16: 4 x K28.4 (rising) or 4 x K28.2 (falling), then 4 x delay.
Chars hsTrigger(bool rising, uint8_t delay);
// Table 17: 4 x K28.6, then 4 x code.
Chars ioAck(uint8_t code = IOACK_OK);

// `inner` with `outer` inserted after its first `at` characters (§8.2.4).
Chars insertAt(const Chars& inner, const Chars& outer, size_t at);

// A short packet taken off the downlink.
struct ShortPacket {
    enum class Kind { TriggerRise, TriggerFall, IoAck, Unknown };
    Kind kind = Kind::Unknown;
    int value = -1;      // delay (trigger) or code (I/O ack), -1 when the replicas disagree
    bool clean = true;   // 4 identical K characters and 4 identical value characters
    Chars chars;
};

// Decode a Table 16 / Table 17 short packet (8 characters).
ShortPacket decodeShortPacket(const Chars& chars);
std::string describeShortPacket(const ShortPacket& p);
std::string describeChars(const Chars& chars, size_t max = 16);

}  // namespace cxp
