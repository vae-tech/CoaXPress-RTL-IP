#include "cxp/transport/fifo.h"

#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstring>
#include <fcntl.h>
#include <poll.h>
#include <sys/stat.h>
#include <unistd.h>

namespace cxp {

namespace {

void mkdirs(const std::string& path) {
    std::string cur;
    size_t pos = 0;
    while (pos != std::string::npos) {
        pos = path.find('/', pos + 1);
        cur = path.substr(0, pos);
        if (!cur.empty()) ::mkdir(cur.c_str(), 0777);
    }
}

uint32_t le32(const uint8_t* p) {
    return uint32_t(p[0]) | uint32_t(p[1]) << 8 | uint32_t(p[2]) << 16 |
           uint32_t(p[3]) << 24;
}

}  // namespace

bool knownMagic(uint32_t magic) {
    return magic == FRAME_MAGIC || magic == 0x43584331u /* CXC1 */ || magic == 0x43584231u /* CXB1 */;
}

std::pair<std::string, std::string> makeFifoPair(const std::string& base_dir,
                                                 const std::string& name) {
    mkdirs(base_dir);
    std::string h2c = base_dir + "/" + name + ".h2c";
    std::string c2h = base_dir + "/" + name + ".c2h";
    for (const auto& p : {h2c, c2h}) {
        struct stat st{};
        if (::stat(p.c_str(), &st) != 0) {
            if (::mkfifo(p.c_str(), 0666) != 0 && errno != EEXIST) {
                throw std::runtime_error("mkfifo " + p + ": " + std::strerror(errno));
            }
        }
    }
    return {h2c, c2h};
}

std::vector<uint8_t> encodeFrame(const Words& words, uint32_t magic) {
    std::vector<uint8_t> out;
    out.reserve(8 + words.size() * 4);
    auto put = [&out](uint32_t v) {
        for (int i = 0; i < 4; ++i) out.push_back(static_cast<uint8_t>(v >> (8 * i)));
    };
    put(magic);
    put(static_cast<uint32_t>(words.size()));
    for (uint32_t w : words) put(w);
    return out;
}

FifoEndpoint::FifoEndpoint(std::string tx_path, std::string rx_path, bool recover,
                           const std::string& name)
    : tx_path_(std::move(tx_path)),
      rx_path_(std::move(rx_path)),
      recover_(recover),
      log_("transport." + name) {}

FifoEndpoint::~FifoEndpoint() { close(); }

// -- lifecycle ------------------------------------------------------------
void FifoEndpoint::open() {
    std::lock_guard<std::mutex> lk(life_mu_);
    if (opened_) return;
    // A vanished reader must surface as EPIPE, not kill the process.
    std::signal(SIGPIPE, SIG_IGN);
    stop_ = false;
    tx_done_ = false;
    txq_.clear();
    rxq_.clear();
    rx_thr_ = std::thread(&FifoEndpoint::rxLoop, this);
    tx_thr_ = std::thread(&FifoEndpoint::txLoop, this);
    opened_ = true;
    log_.info("link open  tx=%s rx=%s", tx_path_.c_str(), rx_path_.c_str());
}

void FifoEndpoint::close() {
    std::lock_guard<std::mutex> lk(life_mu_);
    if (!opened_) return;
    opened_ = false;
    stop_ = true;
    txq_.push(std::nullopt);
    // Unblock a writer thread still parked in O_WRONLY open() by briefly
    // opening the read end ourselves; repeat until it notices the stop.
    while (!tx_done_) {
        int fd = ::open(tx_path_.c_str(), O_RDONLY | O_NONBLOCK);
        if (fd >= 0) ::close(fd);
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    if (tx_thr_.joinable()) tx_thr_.join();
    if (rx_thr_.joinable()) rx_thr_.join();
    log_.info("link closed (tx=%llu rx=%llu resync=%llu reopen=%llu)",
              (unsigned long long)tx_frames, (unsigned long long)rx_frames,
              (unsigned long long)resyncs, (unsigned long long)reopens);
}

// -- public API -----------------------------------------------------------
void FifoEndpoint::sendFrame(const Words& words, uint32_t magic) {
    if (!opened_) throw LinkClosed("endpoint not open");
    txq_.push(encodeFrame(words, magic));
    ++tx_frames;
}

bool FifoEndpoint::recv(LinkFrame& out, int timeout_ms) {
    std::optional<LinkFrame> item;
    if (!rxq_.pop(item, timeout_ms)) return false;
    if (!item) {
        rxq_.pushFront(std::nullopt);  // keep the sentinel for later callers
        throw LinkClosed("peer closed link");
    }
    ++rx_frames;
    out = std::move(*item);
    return true;
}

bool FifoEndpoint::recvFrame(Words& out, int timeout_ms) {
    const auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    LinkFrame f;
    while (true) {
        int left = -1;
        if (timeout_ms >= 0) {
            left = int(std::chrono::duration_cast<std::chrono::milliseconds>(end - std::chrono::steady_clock::now()).count());
            if (left < 0) return false;
        }
        if (!recv(f, left)) return false;
        if (f.magic == FRAME_MAGIC) {
            out = std::move(f.words);
            return true;
        }
    }
}

bool FifoEndpoint::sleepUnlessStopped(int ms) {
    for (int t = 0; t < ms && !stop_; t += 10) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return stop_;
}

// -- writer thread --------------------------------------------------------
bool FifoEndpoint::writeAll(int fd, const std::vector<uint8_t>& chunk) {
    size_t off = 0;
    while (off < chunk.size()) {
        ssize_t n = ::write(fd, chunk.data() + off, chunk.size() - off);
        if (n > 0) {
            off += static_cast<size_t>(n);
            continue;
        }
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 && errno == EAGAIN) {
            // Pipe full: wait for the reader, but give up once stopping so a
            // stalled peer cannot hang close().
            pollfd p{fd, POLLOUT, 0};
            int r = ::poll(&p, 1, 200);
            if (r == 0 && stop_) return false;
            if (r > 0 && (p.revents & (POLLERR | POLLHUP))) return false;
            continue;
        }
        return false;  // EPIPE / other error
    }
    return true;
}

void FifoEndpoint::txLoop() {
    while (!stop_) {
        int fd = ::open(tx_path_.c_str(), O_WRONLY);  // blocks until a reader
        if (fd < 0) {
            if (sleepUnlessStopped(50)) break;
            continue;
        }
        ::fcntl(fd, F_SETFL, ::fcntl(fd, F_GETFL) | O_NONBLOCK);
        bool quit = false;
        while (true) {
            std::optional<std::vector<uint8_t>> chunk;
            txq_.pop(chunk);
            if (!chunk) {
                quit = true;
                break;
            }
            if (!writeAll(fd, *chunk)) {
                if (!stop_) txq_.pushFront(std::move(chunk));  // retry on reopen
                break;
            }
        }
        ::close(fd);
        if (quit || !recover_) break;
    }
    tx_done_ = true;
}

// -- reader thread --------------------------------------------------------
void FifoEndpoint::rxLoop() {
    std::vector<uint8_t> buf;
    size_t head = 0;
    std::vector<uint8_t> tmp(1 << 16);
    while (!stop_) {
        int fd = ::open(rx_path_.c_str(), O_RDONLY | O_NONBLOCK);
        if (fd < 0) {
            if (sleepUnlessStopped(50)) break;
            continue;
        }
        bool got_data = false;
        while (!stop_) {
            pollfd p{fd, POLLIN, 0};
            int r = ::poll(&p, 1, 200);
            if (r <= 0) continue;
            ssize_t n = ::read(fd, tmp.data(), tmp.size());
            if (n < 0) {
                if (errno == EAGAIN || errno == EINTR) continue;
                break;
            }
            if (n == 0) break;  // peer EOF
            got_data = true;
            buf.insert(buf.end(), tmp.data(), tmp.data() + n);
            drain(buf, head);
        }
        ::close(fd);
        if (!recover_ || stop_) break;
        if (got_data) {
            ++reopens;
        } else if (sleepUnlessStopped(20)) {
            break;  // no writer yet: don't spin on repeated EOF
        }
        buf.clear();  // a restarted peer starts a fresh frame stream
        head = 0;
    }
    rxq_.push(std::nullopt);
}

void FifoEndpoint::drain(std::vector<uint8_t>& buf, size_t& head) {
    while (true) {
        size_t avail = buf.size() - head;
        if (avail < 4) break;
        const uint8_t* p = buf.data() + head;
        if (!knownMagic(le32(p))) {
            // Synchronisation loss: skip to the next plausible magic.
            size_t idx = std::string::npos;
            for (size_t i = 1; i + 4 <= avail; ++i) {
                if (knownMagic(le32(p + i))) {
                    idx = i;
                    break;
                }
            }
            if (idx == std::string::npos) {
                head += avail - 3;
                break;
            }
            head += idx;
            ++resyncs;
            log_.warning("resync: skipped %zu bytes", idx);
            continue;
        }
        if (avail < 8) break;
        uint32_t nwords = le32(p + 4);
        if (nwords > FRAME_MAX_WORDS) {
            head += 4;  // corrupt length — treat magic as false positive
            ++resyncs;
            continue;
        }
        size_t need = 8 + size_t(nwords) * 4;
        if (avail < need) break;
        LinkFrame f;
        f.magic = le32(p);
        f.words.resize(nwords);
        for (uint32_t i = 0; i < nwords; ++i) f.words[i] = le32(p + 8 + 4 * i);
        head += need;
        rxq_.push(std::move(f));
    }
    // Compact the consumed prefix once it dominates the buffer.
    if (head > 0 && (head == buf.size() || head > (1u << 16))) {
        buf.erase(buf.begin(), buf.begin() + static_cast<std::ptrdiff_t>(head));
        head = 0;
    }
}

}  // namespace cxp
