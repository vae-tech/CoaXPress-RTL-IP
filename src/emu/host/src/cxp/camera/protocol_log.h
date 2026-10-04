// Protocol activity log: every frame the host sends or receives, decoded.
//
// One line per frame, timestamped, with the direction and the fields of
// the CXP 1.1.1 packet (Tables 19-23); control frames (commands, acks and
// anything that does not decode) are followed by their raw words, so a
// malformed exchange can be read back word by word.  Well-formed stream
// and link-test packets get a summary line only (image data, counter
// pattern); a broken one is dumped too.
// Free-text event lines (retries, timeouts, test-case banners, check
// verdicts) interleave with the frames in time order.
//
// The file is block-buffered: frame lines reach it at most every
// FLUSH_MS, event lines at once.  A per-line flush costs one write per
// frame, which on a slow filesystem throttles the receive thread that
// logs each frame before it dispatches it, and control acks then wait
// behind queued stream packets.
//
// The decoder here is deliberately tolerant: it never throws, reports each
// field as found and flags what is wrong, because a conformance run
// sends and receives broken frames on purpose.
#pragma once

#include <chrono>
#include <cstdio>
#include <mutex>
#include <string>

#include "cxp/protocol/constants.h"

namespace cxp {

class ProtocolLog {
public:
    ProtocolLog() = default;
    explicit ProtocolLog(const std::string& path);  // throws std::runtime_error
    ~ProtocolLog();
    ProtocolLog(const ProtocolLog&) = delete;
    ProtocolLog& operator=(const ProtocolLog&) = delete;

    // Close the current file (if any) and start a new one, truncating it.
    void open(const std::string& path);  // throws std::runtime_error
    void close();
    bool isOpen() const;
    const std::string& path() const { return path_; }

    void tx(const Words& frame);
    void rx(const Words& frame);
    // A character (CXC1) or bench (CXB1) frame, one decoded line.
    void side(const char* dir, uint32_t magic, const Words& body);
    void event(const std::string& text);

    static std::string describeSide(uint32_t magic, const Words& body);

    // One-line decode of a SOP..EOP frame ("CTRL_ACK 0x00 OK size=4 ...").
    // clean_bulk (optional) is set for a well-formed stream or link-test
    // packet, whose raw words the log leaves out.
    static std::string describe(const Words& frame, bool* clean_bulk = nullptr);

private:
    void frame(const char* dir, const Words& frame);
    void line(const std::string& text, bool flush_now = false);

    static constexpr int FLUSH_MS = 250;

    mutable std::mutex mu_;
    FILE* fh_ = nullptr;
    std::string path_;
    std::chrono::steady_clock::time_point last_flush_{};
};

}  // namespace cxp
