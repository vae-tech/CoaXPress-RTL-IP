// Linux FIFO (named-pipe) transport (cxp/transport/fifo.py).
//
// The host and the camera communicate over two named pipes:
//
//     h2c   host  -> camera   (control commands, discovery, heartbeat)
//     c2h   camera -> host    (acks, stream, events)
//
// A *link frame* envelopes one CXP word-frame so boundaries survive the
// byte-oriented pipe and the receiver can resynchronise after corruption
// or a peer restart:
//
//     +-----------+-----------+--------------------+
//     | MAGIC u32 | NWORDS u32| NWORDS * u32 (LE)  |
//     +-----------+-----------+--------------------+
//
// MAGIC is 'CXP1' for a word frame.  The same envelope carries two more
// kinds, told apart by their magic: 'CXC1' character frames
// (protocol/chars.h) and 'CXB1' bench frames (protocol/bench.h).  A reader
// that knows only CXP1 skips the others as garbage.
//
// On an unknown magic the reader slides forward to the next plausible
// magic (synchronisation-loss recovery).  On EOF (peer closed its end) the
// reader transparently reopens the FIFO (link recovery).
//
// Pipe I/O runs on two dedicated threads per endpoint; the reader polls
// with a 200 ms timeout so it observes shutdown promptly.
#pragma once

#include <atomic>
#include <cstdint>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "cxp/protocol/constants.h"
#include "cxp/transport/blocking_queue.h"
#include "cxp/utils/log.h"

namespace cxp {

inline constexpr uint32_t FRAME_MAGIC = 0x43585031u;  // b'CXP1'
inline constexpr uint32_t FRAME_MAX_WORDS = 1u << 20;  // absurd NWORDS => desync

// Raised when the peer has closed the link and recovery is disabled, or the
// endpoint is not open.
class LinkClosed : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

// Create (h2c, c2h) FIFOs under base_dir, returning their paths.
std::pair<std::string, std::string> makeFifoPair(const std::string& base_dir,
                                                 const std::string& name = "cxp");

std::vector<uint8_t> encodeFrame(const Words& words, uint32_t magic = FRAME_MAGIC);

// One received link frame of any kind.
struct LinkFrame {
    uint32_t magic = FRAME_MAGIC;
    Words words;
};

// A magic this transport accepts: CXP1, CXC1 or CXB1.
bool knownMagic(uint32_t magic);

class FifoEndpoint {
public:
    // tx_path: path this endpoint *writes* to; rx_path: path it *reads* from.
    // recover: reopen the FIFO on peer EOF instead of signalling close.
    FifoEndpoint(std::string tx_path, std::string rx_path, bool recover = true,
                 const std::string& name = "link");
    ~FifoEndpoint();
    FifoEndpoint(const FifoEndpoint&) = delete;
    FifoEndpoint& operator=(const FifoEndpoint&) = delete;

    void open();
    void close();
    bool isOpen() const { return opened_; }

    // throws LinkClosed if not open
    void sendFrame(const Words& words, uint32_t magic = FRAME_MAGIC);

    // Next received CXP1 word-frame; frames of the other kinds are dropped.
    // Waits up to timeout_ms (< 0: forever) and returns false on timeout;
    // throws LinkClosed once the link shut down.
    bool recvFrame(Words& out, int timeout_ms = -1);
    // Next received frame of any kind.
    bool recv(LinkFrame& out, int timeout_ms = -1);

    const std::string& txPath() const { return tx_path_; }
    const std::string& rxPath() const { return rx_path_; }

    // Stats surfaced to the compliance checker / CLI / GUI.
    std::atomic<uint64_t> tx_frames{0};
    std::atomic<uint64_t> rx_frames{0};
    std::atomic<uint64_t> resyncs{0};
    std::atomic<uint64_t> reopens{0};

private:
    void rxLoop();
    void txLoop();
    void drain(std::vector<uint8_t>& buf, size_t& head);
    bool writeAll(int fd, const std::vector<uint8_t>& chunk);
    bool sleepUnlessStopped(int ms);

    std::string tx_path_;
    std::string rx_path_;
    bool recover_;
    Logger log_;

    std::mutex life_mu_;
    std::atomic<bool> opened_{false};
    std::atomic<bool> stop_{false};
    std::atomic<bool> tx_done_{true};
    std::thread rx_thr_;
    std::thread tx_thr_;
    BlockingQueue<std::optional<std::vector<uint8_t>>> txq_;
    BlockingQueue<std::optional<LinkFrame>> rxq_;  // nullopt = link closed
};

}  // namespace cxp
