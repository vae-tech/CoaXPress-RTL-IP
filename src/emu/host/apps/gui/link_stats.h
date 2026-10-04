// Live link / RX statistics accumulator plus the stream-drain thread that
// feeds it (cxp/gui/link_stats.py).
//
// The drain thread pulls every stream-path frame off the host, feeds the
// StreamParser and the counters under one mutex; the GUI takes the same
// mutex to render.  Counters the host stack does not surface on this path
// (heartbeat / discovery are consumed by CameraControl, IDLE is not framed)
// are reported as "—" rather than fabricated.
#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <thread>
#include <vector>

#include <QString>

#include "cxp/camera/client.h"
#include "cxp/parser/stream_parser.h"

// The most recent §8.7 connection-test packet, decoded for display.
struct LinkTestSnapshot {
    cxp::Words data_words;
    std::vector<uint32_t> error_indices;  // words that missed the counter ramp
};

// One row of the statistics tree.
struct StatNode {
    QString name;
    QString value;
    std::vector<StatNode> kids;
};

class LinkStats {
public:
    explicit LinkStats(std::shared_ptr<cxp::CameraControl> host);
    ~LinkStats();

    void start();
    void stop();

    std::vector<StatNode> tree();

    // -- image viewer feed ----------------------------------------------------
    uint64_t videoSeq() const { return video_seq_; }
    uint64_t linktestSeq() const { return linktest_seq_; }
    cxp::FramePtr lastVideo() const;
    std::shared_ptr<const LinkTestSnapshot> lastLinktest() const;
    // Drop the buffered frames and anything still queued on the link;
    // session counters are left intact.  Returns the dropped queue depth.
    size_t clearFrames();

private:
    struct StreamStat {
        uint64_t packets = 0;
        uint64_t bytes = 0;
        uint64_t frames = 0;  // image headers seen
        std::optional<uint64_t> last_frame_id;
        uint64_t lines = 0;  // sum of image-header heights
        uint64_t last_payload_bytes = 0;
        std::optional<uint64_t> min_payload_bytes;
        uint64_t max_payload_bytes = 0;
        std::optional<uint32_t> last_width;
        std::optional<uint32_t> last_height;
        std::optional<uint32_t> last_pixel_format;
    };

    void drainLoop();
    void feedLocked(const cxp::Words& frame);
    void noteLinktestLocked(const cxp::Words& frame);
    void noteVideoLocked(cxp::FramePtr frame);

    std::shared_ptr<cxp::CameraControl> host_;
    std::thread thr_;
    std::atomic<bool> stop_{false};

    mutable std::mutex mu_;
    cxp::StreamParser parser_;
    std::chrono::steady_clock::time_point start_;
    uint64_t frames_ = 0;
    uint64_t bytes_ = 0;
    std::map<std::string, uint64_t> by_type_;
    uint64_t unknown_ = 0;
    std::map<uint8_t, StreamStat> streams_;

    cxp::FramePtr last_video_;
    std::atomic<uint64_t> video_seq_{0};
    std::shared_ptr<const LinkTestSnapshot> last_linktest_;
    std::atomic<uint64_t> linktest_seq_{0};

    // Rolling-window FPS state sampled on every tree() call.
    std::chrono::steady_clock::time_point fps_prev_t_;
    uint64_t fps_prev_imgs_ = 0;
    uint64_t fps_prev_bytes_ = 0;
    double fps_last_inst_ = 0.0;
    double fps_last_inst_mbps_ = 0.0;
};
