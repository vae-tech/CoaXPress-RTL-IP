// High-Speed downlink stream parser (cxp/parser/stream_parser.py).
//
// Consumes the device->host word stream and:
//
// * detects packet boundaries (SOP..EOP), tolerating IDLE / garbage,
// * decodes stream / event / ctrl-ack packets and validates their CRC,
// * flags malformed packets and synchronisation loss,
// * tracks the rolling stream-packet tag to detect missing packets,
// * validates the per-frame frame_id to detect missing frames,
// * reassembles pixel payload into ReconstructedFrame objects.
//
// Transport-agnostic: feed raw words (feedWords) or already-split frames
// (feedFrame).  Not thread-safe; callers serialise access.
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "cxp/image/reconstruct.h"
#include "cxp/protocol/packets.h"
#include "cxp/utils/log.h"

namespace cxp {

struct ParseStats {
    // frames_seen counts stream PACKETS off the wire (one per SOP..EOP), not
    // image frames.  Use images_started / images_completed for images.
    uint64_t frames_seen = 0;
    uint64_t stream_pkts = 0;
    uint64_t event_pkts = 0;
    uint64_t ack_pkts = 0;
    uint64_t crc_errors = 0;
    uint64_t malformed = 0;
    uint64_t sync_losses = 0;
    uint64_t missing_packets = 0;
    uint64_t missing_frames = 0;
    uint64_t images_started = 0;    // CXP image headers detected
    uint64_t images_completed = 0;  // frames whose pixel payload finished
    std::map<std::string, uint64_t> decode_reasons;

    void note(const std::string& reason) { ++decode_reasons[reason]; }
    std::string str() const;
};

using FramePtr = std::shared_ptr<ReconstructedFrame>;

class StreamParser {
public:
    StreamParser();

    ParseStats stats;
    std::vector<EventPacket> events;
    std::vector<CtrlAckPacket> acks;

    // 1 if a frame is mid-reassembly (header seen, payload still arriving).
    int imagesInFlight() const { return cur_ ? 1 : 0; }

    // Feed a raw word stream; returns any images completed.
    std::vector<FramePtr> feedWords(WordSpan words);
    // Feed one already-delimited SOP..EOP frame.
    std::vector<FramePtr> feedFrame(WordSpan frame);
    // Return a partially-assembled trailing frame, if any.
    FramePtr flush();

private:
    enum class MkState { Hunt, Type, HdrRec, HdrArb, LmArb };

    void handleFrame(WordSpan frame, std::vector<FramePtr>& done);
    void assemble(const StreamPacket& pkt, std::vector<FramePtr>& done);
    void consumeCxpPayload(const Words& payload, std::vector<FramePtr>& done);
    ImageHeader decodeRecHeader(const Words& words);
    ImageHeader decodeArbHeader(const Words& words);
    PixelFormat safePixfmt(uint32_t raw);
    uint32_t nextFrameId();
    void startFrame(const ImageHeader& hdr);
    FramePtr finalizeIfDue(bool force);

    Logger log_;
    Words buf_;
    bool in_frame_ = false;
    FramePtr cur_;
    std::optional<uint8_t> expect_tag_;
    std::optional<uint32_t> expect_frame_id_;
    // CXP §9.4 in-band marker walker; persists across stream packets because
    // a marker is free to straddle a packet boundary.
    MkState mk_state_ = MkState::Hunt;
    Words mk_buf_;
    uint32_t cxp_frame_counter_ = 0;
};

}  // namespace cxp
