// Minimal libpcap writer for Wireshark-compatible packet export.
//
// Each CXP frame is stored as one pcap record with DLT_USER0 (147); the
// record payload is the raw 32-bit little-endian word frame.
#pragma once

#include <cstdint>
#include <cstdio>
#include <mutex>
#include <string>
#include <vector>

namespace cxp {

inline constexpr uint32_t DLT_USER0 = 147;

class PcapWriter {
public:
    explicit PcapWriter(const std::string& path);  // throws std::runtime_error
    ~PcapWriter();
    PcapWriter(const PcapWriter&) = delete;
    PcapWriter& operator=(const PcapWriter&) = delete;

    // ts < 0 means "now".
    void writeWords(const std::vector<uint32_t>& words, double ts = -1.0);
    void writeBytes(const std::vector<uint8_t>& payload, double ts = -1.0);
    void close();

private:
    std::mutex mu_;
    FILE* fh_ = nullptr;
};

}  // namespace cxp
