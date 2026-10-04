#include "cxp/protocol/packets.h"

#include <algorithm>
#include <set>

#include "cxp/protocol/crc.h"
#include "cxp/utils/log.h"

namespace cxp {

namespace {

// Majority-vote a 4x-replicated header byte, or fail decoding.
uint8_t vote(uint32_t word, const std::string& what) {
    Majority m = majorityByte(word);
    if (!m.ok) {
        throw PacketDecodeError("replica", "unrecoverable replica in " + what);
    }
    return m.value;
}

uint32_t crcWire(const Words& span, size_t from, size_t n, bool corrupt) {
    uint32_t crc = crc32Words(span.data() + from, n);
    return crcToWire(crc) ^ (corrupt ? 0xFFFFFFFFu : 0u);
}

bool crcMatches(WordSpan covered, uint32_t wire) {
    return wireToCrc(wire) == crc32Words(covered.begin(), covered.size());
}

}  // namespace

std::vector<uint8_t> wordsToBytesLe(const Words& words) {
    std::vector<uint8_t> out;
    out.reserve(words.size() * 4);
    for (uint32_t w : words) {
        for (int i = 0; i < 4; ++i) out.push_back(static_cast<uint8_t>(w >> (8 * i)));
    }
    return out;
}

// --------------------------------------------------------------------------
// CtrlCmdPacket
// --------------------------------------------------------------------------
uint32_t CtrlCmdPacket::sizeBytes() const {
    if (opcode == CtrlOpcode::Write) return static_cast<uint32_t>(data.size() * 4);
    if (opcode == CtrlOpcode::Read) return (nwords ? nwords : 1) * 4;
    return 0;  // RESET
}

Words CtrlCmdPacket::toWords() const {
    // Table 21: word 0 = Cmd (P0) + Size[23:16..7:0] (P1..P3), word 1 =
    // Addr big-endian, then N data words, CRC over words 0..N+1.
    uint32_t sz = sizeBytes();
    Words w;
    w.reserve(6 + data.size());
    w.push_back(SOP_WORD);
    w.push_back(replicateByte(static_cast<uint8_t>(PacketType::CtrlCmd)));
    w.push_back(uint32_t(static_cast<uint8_t>(opcode)) | (sz >> 16 & 0xFF) << 8 |
                (sz >> 8 & 0xFF) << 16 | (sz & 0xFF) << 24);
    w.push_back(bswap32(address));
    if (opcode == CtrlOpcode::Write) w.insert(w.end(), data.begin(), data.end());
    w.push_back(crcWire(w, 2, w.size() - 2, corrupt_crc));
    w.push_back(EOP_WORD);
    return w;
}

CtrlCmdPacket CtrlCmdPacket::fromBody(WordSpan body) {
    // body excludes SOP/EOP. [0]=TYPE, [1]=Cmd+Size, [2]=Addr, [..]=data, [-1]=CRC
    if (body.size() < 4) throw PacketDecodeError("short", "ctrl-cmd frame too short");
    if (vote(body[0], "TYPE") != static_cast<uint8_t>(PacketType::CtrlCmd)) {
        throw PacketDecodeError("bad_type", "not a ctrl-cmd");
    }
    const uint32_t w0 = body[1];
    const uint8_t opb = static_cast<uint8_t>(w0);
    const uint32_t size = (w0 >> 8 & 0xFF) << 16 | (w0 >> 16 & 0xFF) << 8 | w0 >> 24;
    if (!crcMatches(body.slice(1, 1), body.back())) {
        throw PacketDecodeError("crc", "ctrl-cmd CRC mismatch");
    }
    auto op = toCtrlOpcode(opb);
    if (!op) {
        throw PacketDecodeError("bad_opcode",
                                strprintf("%u is not a valid CtrlOpcode", opb));
    }
    CtrlCmdPacket p;
    p.opcode = *op;
    p.address = bswap32(body[2]);
    p.data = body.slice(3, 1).toVector();
    p.nwords = (*op == CtrlOpcode::Read) ? (size + 3) / 4
                                         : static_cast<uint32_t>(p.data.size());
    return p;
}

// --------------------------------------------------------------------------
// CtrlAckPacket
// --------------------------------------------------------------------------
Words CtrlAckPacket::toWords() const {
    uint32_t type_word = replicateByte(static_cast<uint8_t>(PacketType::CtrlAck));
    uint32_t code_word = replicateByte(static_cast<uint8_t>(code));
    // Table 22 short form: every code outside {OK, Wait} omits the
    // Size/Data/CRC trailer; EOP follows CODE directly.
    uint8_t c = static_cast<uint8_t>(code);
    if (c != 0x00 && c != 0x04) return {SOP_WORD, type_word, code_word, EOP_WORD};
    // Table 22 long form: one big-endian Size word (B, bytes).
    Words w{SOP_WORD, type_word, code_word, bswap32(sizeBytes())};
    w.insert(w.end(), data.begin(), data.end());
    // Table 22: CRC span = words 0..N+1 (CODE..last-data); TYPE excluded.
    w.push_back(crcWire(w, 2, w.size() - 2, corrupt_crc));
    w.push_back(EOP_WORD);
    return w;
}

CtrlAckPacket CtrlAckPacket::fromBody(WordSpan body) {
    if (body.size() < 2) throw PacketDecodeError("short", "ctrl-ack frame too short");
    if (vote(body[0], "TYPE") != static_cast<uint8_t>(PacketType::CtrlAck)) {
        throw PacketDecodeError("bad_type", "not a ctrl-ack");
    }
    uint8_t code = vote(body[1], "ACK");
    CtrlAckPacket p;
    p.code = toAckCode(code).value_or(AckCode::InvalidOperation);
    // Short form: TYPE + CODE only (no Size/Data/CRC per Table 22).
    if (body.size() == 2) return p;
    if (body.size() < 4) throw PacketDecodeError("short", "ctrl-ack frame too short");
    // body[2] = Size, big-endian (B, bytes)
    p.data = body.slice(3, 1).toVector();
    if (!crcMatches(body.slice(1, 1), body.back())) {
        throw PacketDecodeError("crc", "ctrl-ack CRC mismatch");
    }
    return p;
}

// --------------------------------------------------------------------------
// StreamPacket
// --------------------------------------------------------------------------
Words StreamPacket::toWords() const {
    uint32_t dsize = static_cast<uint32_t>(payload.size()) & 0xFFFF;  // word count
    Words w;
    w.reserve(payload.size() + 8);
    w.push_back(SOP_WORD);
    w.push_back(replicateByte(static_cast<uint8_t>(PacketType::Stream)));
    w.push_back(replicateByte(stream_id));
    w.push_back(replicateByte(tag));
    w.push_back(replicateByte(uint8_t(dsize >> 8)));  // DsizeP[15:8]
    w.push_back(replicateByte(uint8_t(dsize)));       // DsizeP[7:0]
    w.insert(w.end(), payload.begin(), payload.end());
    // Table 19: CRC covers the payload only.
    w.push_back(crcWire(w, 6, w.size() - 6, corrupt_crc));
    w.push_back(EOP_WORD);
    return w;
}

StreamPacket StreamPacket::fromBody(WordSpan body) {
    // body excludes SOP/EOP: [0]=4xTYPE [1]=4xStreamID [2]=4xTag
    // [3]=4xDsizeP[15:8] [4]=4xDsizeP[7:0] [5:-1]=data [-1]=CRC32
    if (body.size() < 6) throw PacketDecodeError("short", "stream frame too short");
    if (vote(body[0], "TYPE") != static_cast<uint8_t>(PacketType::Stream)) {
        throw PacketDecodeError("bad_type", "not a stream packet");
    }
    StreamPacket p;
    p.stream_id = vote(body[1], "StreamID");
    p.tag = vote(body[2], "Tag");
    uint32_t dsize = uint32_t(vote(body[3], "DsizeP_H")) << 8 | vote(body[4], "DsizeP_L");
    size_t n_payload = body.size() - 6;
    if (dsize != n_payload) {
        throw PacketDecodeError(
            "dsize", strprintf("DsizeP != payload length (DsizeP=%u payload=%zu "
                               "body=%zu stream_id=%u tag=%u)",
                               dsize, n_payload, body.size(), p.stream_id, p.tag));
    }
    if (!crcMatches(body.slice(5, 1), body.back())) {
        throw PacketDecodeError("crc", "stream CRC mismatch");
    }
    p.payload = body.slice(5, 1).toVector();
    return p;
}

// --------------------------------------------------------------------------
// EventPacket
// --------------------------------------------------------------------------
Words EventPacket::toWords() const {
    Words w{SOP_WORD,
            replicateByte(static_cast<uint8_t>(PacketType::Event)),
            event_id,
            static_cast<uint32_t>(timestamp >> 32),
            static_cast<uint32_t>(timestamp)};
    w.insert(w.end(), data.begin(), data.end());
    w.push_back(crcWire(w, 1, w.size() - 1, corrupt_crc));
    w.push_back(EOP_WORD);
    return w;
}

EventPacket EventPacket::fromBody(WordSpan body) {
    if (body.size() < 5) throw PacketDecodeError("short", "event frame too short");
    if (vote(body[0], "TYPE") != static_cast<uint8_t>(PacketType::Event)) {
        throw PacketDecodeError("bad_type", "not an event");
    }
    if (!crcMatches(body.slice(0, 1), body.back())) {
        throw PacketDecodeError("crc", "event CRC mismatch");
    }
    EventPacket p;
    p.event_id = body[1];
    p.timestamp = uint64_t(body[2]) << 32 | body[3];
    p.data = body.slice(4, 1).toVector();
    return p;
}

// --------------------------------------------------------------------------
// LinkTestPacket
// --------------------------------------------------------------------------
Words LinkTestPacket::toWords() const {
    Words w{SOP_WORD, replicateByte(static_cast<uint8_t>(PacketType::LinkTest))};
    std::set<uint32_t> errs(error_indices.begin(), error_indices.end());
    uint8_t seq = 0;
    for (uint32_t i = 0; i < n_data; ++i) {
        uint32_t v = linkTestWord(seq);
        if (errs.count(i)) v ^= 0x00000001u;
        w.push_back(v);
        seq = static_cast<uint8_t>(seq + 4);
    }
    w.push_back(EOP_WORD);
    return w;
}

LinkTestPacket LinkTestPacket::fromBody(WordSpan body) {
    if (body.size() < 2) throw PacketDecodeError("short", "linktest frame too short");
    if (vote(body[0], "TYPE") != static_cast<uint8_t>(PacketType::LinkTest)) {
        throw PacketDecodeError("bad_type", "not a linktest");
    }
    WordSpan data = body.slice(1);
    LinkTestPacket p;
    p.n_data = static_cast<uint32_t>(data.size());
    uint8_t seq = 0;
    for (size_t i = 0; i < data.size(); ++i) {
        if (data[i] != linkTestWord(seq)) p.error_indices.push_back(static_cast<uint32_t>(i));
        seq = static_cast<uint8_t>(seq + 4);
    }
    return p;
}

// --------------------------------------------------------------------------
// DiscoveryPacket
// --------------------------------------------------------------------------
Words DiscoveryPacket::toWords() const {
    Words w{SOP_WORD,
            replicateByte(static_cast<uint8_t>(PacketType::Discovery)),
            replicateByte(is_heartbeat ? 1 : 0),
            nonce};
    w.push_back(crcWire(w, 1, w.size() - 1, false));
    w.push_back(EOP_WORD);
    return w;
}

DiscoveryPacket DiscoveryPacket::fromBody(WordSpan body) {
    if (body.size() < 4) throw PacketDecodeError("short", "discovery frame too short");
    if (vote(body[0], "TYPE") != static_cast<uint8_t>(PacketType::Discovery)) {
        throw PacketDecodeError("bad_type", "not a discovery packet");
    }
    if (!crcMatches(body.slice(0, 1), body.back())) {
        throw PacketDecodeError("crc", "discovery CRC mismatch");
    }
    DiscoveryPacket p;
    p.nonce = body[2];
    p.is_heartbeat = vote(body[1], "HB") != 0;
    return p;
}

// --------------------------------------------------------------------------
// Auto-dispatch
// --------------------------------------------------------------------------
Packet decodePacket(WordSpan frame) {
    if (frame.size() < 3) {
        throw PacketDecodeError("short", "frame shorter than SOP+TYPE+EOP");
    }
    if (frame.front() != SOP_WORD) {
        throw PacketDecodeError("no_sop", "frame does not start with SOP");
    }
    if (frame.back() != EOP_WORD) {
        throw PacketDecodeError("no_eop", "frame does not end with EOP");
    }
    WordSpan body = frame.slice(1, 1);
    Majority m = majorityByte(body[0]);
    if (!m.ok) throw PacketDecodeError("replica", "TYPE word replica corrupt");
    auto t = toPacketType(m.value);
    if (!t) throw PacketDecodeError("bad_type", strprintf("unknown TYPE 0x%02X", m.value));
    switch (*t) {
    case PacketType::CtrlCmd:   return CtrlCmdPacket::fromBody(body);
    case PacketType::CtrlAck:   return CtrlAckPacket::fromBody(body);
    case PacketType::Stream:    return StreamPacket::fromBody(body);
    case PacketType::Event:     return EventPacket::fromBody(body);
    case PacketType::LinkTest:  return LinkTestPacket::fromBody(body);
    case PacketType::Discovery: return DiscoveryPacket::fromBody(body);
    }
    throw PacketDecodeError("bad_type", "unreachable");
}

std::optional<PacketType> peekType(WordSpan frame) {
    if (frame.size() < 3 || frame.front() != SOP_WORD || frame.back() != EOP_WORD) {
        return std::nullopt;
    }
    Majority m = majorityByte(frame[1]);
    if (!m.ok) return std::nullopt;
    return toPacketType(m.value);
}

std::vector<Words> findFrames(WordSpan words) {
    std::vector<Words> frames;
    Words cur;
    bool in_frame = false;
    for (uint32_t w : words) {
        if (w == SOP_WORD) {
            cur.assign(1, w);
            in_frame = true;
        } else if (in_frame) {
            cur.push_back(w);
            if (w == EOP_WORD) {
                frames.push_back(std::move(cur));
                cur.clear();
                in_frame = false;
            }
        }
    }
    return frames;
}

}  // namespace cxp
