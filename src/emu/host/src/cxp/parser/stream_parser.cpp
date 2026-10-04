#include "cxp/parser/stream_parser.h"

#include "cxp/protocol/crc.h"

namespace cxp {

namespace {

// CXP §9.4 in-band stream markers in fully-replicated wire form; the marker
// walker matches these without majority voting.
constexpr uint32_t K28_3_REP = replicateByte(K28_3);
constexpr uint32_t HDR_TYPE_REC_REP = replicateByte(HDR_TYPE_REC);
constexpr uint32_t HDR_TYPE_ARB_REP = replicateByte(HDR_TYPE_ARB);
constexpr uint32_t LINE_TYPE_REC_REP = replicateByte(LINE_TYPE_REC);
constexpr uint32_t LINE_TYPE_ARB_REP = replicateByte(LINE_TYPE_ARB);

// Marker lengths mirror cxp_app_image_header.sv / cxp_app_line_marker.sv.
constexpr size_t kRecHdrWords = 25;
constexpr size_t kArbHdrWords = 16;
constexpr size_t kArbLmWords = 11;

std::vector<uint8_t> voteBytes(const Words& words) {
    std::vector<uint8_t> out;
    out.reserve(words.size());
    for (uint32_t w : words) out.push_back(majorityByte(w).value);
    return out;
}

}  // namespace

std::string ParseStats::str() const {
    std::string reasons;
    for (const auto& kv : decode_reasons) {
        if (!reasons.empty()) reasons += ", ";
        reasons += strprintf("'%s': %llu", kv.first.c_str(), (unsigned long long)kv.second);
    }
    return strprintf(
        "ParseStats(frames_seen=%llu, stream_pkts=%llu, event_pkts=%llu, "
        "ack_pkts=%llu, crc_errors=%llu, malformed=%llu, sync_losses=%llu, "
        "missing_packets=%llu, missing_frames=%llu, images_started=%llu, "
        "images_completed=%llu, decode_reasons={%s})",
        (unsigned long long)frames_seen, (unsigned long long)stream_pkts,
        (unsigned long long)event_pkts, (unsigned long long)ack_pkts,
        (unsigned long long)crc_errors, (unsigned long long)malformed,
        (unsigned long long)sync_losses, (unsigned long long)missing_packets,
        (unsigned long long)missing_frames, (unsigned long long)images_started,
        (unsigned long long)images_completed, reasons.c_str());
}

StreamParser::StreamParser() : log_("parser") {}

// -- ingest ---------------------------------------------------------------
std::vector<FramePtr> StreamParser::feedWords(WordSpan words) {
    std::vector<FramePtr> done;
    for (uint32_t w : words) {
        if (!in_frame_) {
            if (w == SOP_WORD) {
                in_frame_ = true;
                buf_.assign(1, w);
            } else if (w != IDLE_WORD) {
                // Stray non-SOP word between frames.
                ++stats.sync_losses;
            }
            continue;
        }
        buf_.push_back(w);
        if (w == EOP_WORD) {
            in_frame_ = false;
            handleFrame(buf_, done);
        } else if (buf_.size() > (1u << 20)) {
            // runaway: never saw EOP -> drop and resync
            ++stats.sync_losses;
            in_frame_ = false;
            buf_.clear();
        }
    }
    return done;
}

std::vector<FramePtr> StreamParser::feedFrame(WordSpan frame) {
    std::vector<FramePtr> done;
    handleFrame(frame, done);
    return done;
}

// -- frame handling -------------------------------------------------------
void StreamParser::handleFrame(WordSpan frame, std::vector<FramePtr>& done) {
    ++stats.frames_seen;
    Packet pkt;
    try {
        pkt = decodePacket(frame);
    } catch (const PacketDecodeError& exc) {
        stats.note(exc.reason());
        if (exc.reason() == "crc") {
            ++stats.crc_errors;
        } else if (exc.reason() == "no_sop" || exc.reason() == "no_eop") {
            ++stats.sync_losses;
        } else {
            ++stats.malformed;
        }
        log_.warning("malformed frame: %s (%s)", exc.what(), exc.reason().c_str());
        return;
    }
    if (auto* ev = std::get_if<EventPacket>(&pkt)) {
        ++stats.event_pkts;
        events.push_back(std::move(*ev));
    } else if (auto* ack = std::get_if<CtrlAckPacket>(&pkt)) {
        ++stats.ack_pkts;
        acks.push_back(std::move(*ack));
    } else if (auto* sp = std::get_if<StreamPacket>(&pkt)) {
        ++stats.stream_pkts;
        assemble(*sp, done);
    }
}

// -- image reassembly -----------------------------------------------------
void StreamParser::assemble(const StreamPacket& pkt, std::vector<FramePtr>& done) {
    // Rolling-tag continuity check (8-bit wrap).
    if (expect_tag_ && pkt.tag != *expect_tag_) {
        uint8_t gap = static_cast<uint8_t>(pkt.tag - *expect_tag_);
        stats.missing_packets += gap;
        if (cur_) cur_->missing_packets += gap;
        log_.warning("stream tag gap: expected %u got %u (%u lost)", *expect_tag_,
                     pkt.tag, gap);
    }
    expect_tag_ = static_cast<uint8_t>(pkt.tag + 1);
    // Both the virtual camera and the RTL DUT emit the CXP §9.4 in-band
    // header (Table 38) and line markers (Table 39) inline in the pixels.
    consumeCxpPayload(pkt.payload, done);
}

// Pixel-stream extractor for CXP §9.4 in-band markers:
//
// * K28.3 + HDR_TYPE_REC  -> 25-word Table 38 header; finalises the in-flight
//   frame and opens a fresh one with the decoded geometry.
// * K28.3 + HDR_TYPE_ARB  -> 16-word §9.4 arbitrary header.
// * K28.3 + LINE_TYPE_REC -> 2-word Table 39 line marker (skipped).
// * K28.3 + LINE_TYPE_ARB -> 11-word §9.4 arbitrary line marker (skipped).
//
// Anything else is pixel data.  A bare K28.3 followed by an unrecognised
// type word is rolled back into the pixel buffer (uniform 0x7C grey must not
// desync the parser).
void StreamParser::consumeCxpPayload(const Words& payload,
                                     std::vector<FramePtr>& done) {
    Words pixel_run;
    pixel_run.reserve(payload.size());
    auto flushPixels = [&] {
        if (!pixel_run.empty() && cur_) {
            wordsToBytesBe(pixel_run.data(), pixel_run.size(), cur_->data);
        }
        pixel_run.clear();
    };
    auto closeAndStart = [&](const ImageHeader& hdr) {
        flushPixels();
        if (FramePtr closed = finalizeIfDue(true)) done.push_back(closed);
        startFrame(hdr);
        mk_buf_.clear();
        mk_state_ = MkState::Hunt;
    };

    for (uint32_t w : payload) {
        switch (mk_state_) {
        case MkState::Hunt:
            if (w == K28_3_REP) {
                mk_buf_.assign(1, w);
                mk_state_ = MkState::Type;
            } else {
                pixel_run.push_back(w);
            }
            break;
        case MkState::Type:
            mk_buf_.push_back(w);
            if (w == HDR_TYPE_REC_REP) {
                mk_state_ = MkState::HdrRec;
            } else if (w == HDR_TYPE_ARB_REP) {
                mk_state_ = MkState::HdrArb;
            } else if (w == LINE_TYPE_REC_REP) {
                mk_buf_.clear();  // 2-word marker complete; drop
                mk_state_ = MkState::Hunt;
            } else if (w == LINE_TYPE_ARB_REP) {
                mk_state_ = MkState::LmArb;
            } else {
                pixel_run.insert(pixel_run.end(), mk_buf_.begin(), mk_buf_.end());
                mk_buf_.clear();
                mk_state_ = MkState::Hunt;
            }
            break;
        case MkState::HdrRec:
            mk_buf_.push_back(w);
            if (mk_buf_.size() >= kRecHdrWords) closeAndStart(decodeRecHeader(mk_buf_));
            break;
        case MkState::HdrArb:
            mk_buf_.push_back(w);
            if (mk_buf_.size() >= kArbHdrWords) closeAndStart(decodeArbHeader(mk_buf_));
            break;
        case MkState::LmArb:
            mk_buf_.push_back(w);
            if (mk_buf_.size() >= kArbLmWords) {
                mk_buf_.clear();
                mk_state_ = MkState::Hunt;
            }
            break;
        }
    }
    flushPixels();
    // Pixel data arriving before the first header is silently dropped
    // (mid-stream tap-in is normal).
    if (FramePtr f = finalizeIfDue(false)) done.push_back(f);
}

uint32_t StreamParser::nextFrameId() { return cxp_frame_counter_++; }

PixelFormat StreamParser::safePixfmt(uint32_t raw) {
    if (auto f = toPixelFormat(raw)) return *f;
    log_.warning("unknown PixelFormat 0x%04X, defaulting MONO8", raw);
    return PixelFormat::Mono8;
}

ImageHeader StreamParser::decodeRecHeader(const Words& words) {
    // words[0]=4xK28.3, words[1]=4xHDR_TYPE_REC; Table 38 field bytes from 2.
    auto b = voteBytes(words);
    ImageHeader h;
    h.frame_id = nextFrameId();
    h.width = uint32_t(b[5]) << 16 | uint32_t(b[6]) << 8 | b[7];
    h.x_offs = uint32_t(b[8]) << 16 | uint32_t(b[9]) << 8 | b[10];
    h.height = uint32_t(b[11]) << 16 | uint32_t(b[12]) << 8 | b[13];
    h.y_offs = uint32_t(b[14]) << 16 | uint32_t(b[15]) << 8 | b[16];
    h.pixel_format = safePixfmt(uint32_t(b[20]) << 8 | b[21]);
    return h;
}

ImageHeader StreamParser::decodeArbHeader(const Words& words) {
    // §9.4 arbitrary form: 16 words, no xsize/xoffs (DsizeL is per-line and
    // carried in the line marker); width is reported as 0.
    auto b = voteBytes(words);
    ImageHeader h;
    h.frame_id = nextFrameId();
    h.height = uint32_t(b[5]) << 16 | uint32_t(b[6]) << 8 | b[7];
    h.y_offs = uint32_t(b[8]) << 16 | uint32_t(b[9]) << 8 | b[10];
    h.pixel_format = safePixfmt(uint32_t(b[11]) << 8 | b[12]);
    return h;
}

void StreamParser::startFrame(const ImageHeader& hdr) {
    if (expect_frame_id_ && hdr.frame_id != *expect_frame_id_) {
        uint32_t gap = hdr.frame_id - *expect_frame_id_;
        stats.missing_frames += gap ? gap : 1;
        log_.warning("frame_id gap: expected %u got %u", *expect_frame_id_, hdr.frame_id);
    }
    expect_frame_id_ = hdr.frame_id + 1;
    cur_ = std::make_shared<ReconstructedFrame>();
    cur_->header = hdr;
    cur_->data.reserve(hdr.frameBytes() + 4);
    ++stats.images_started;
}

FramePtr StreamParser::finalizeIfDue(bool force) {
    if (!cur_) return nullptr;
    const uint64_t need = cur_->header.frameBytes();
    if (!force && cur_->data.size() < need) return nullptr;
    FramePtr f = std::move(cur_);
    cur_.reset();
    if (f->data.size() >= need) {
        f->data.resize(need);
        ++stats.images_completed;
    }
    return f;
}

FramePtr StreamParser::flush() { return finalizeIfDue(true); }

}  // namespace cxp
