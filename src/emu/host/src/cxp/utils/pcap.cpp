#include "cxp/utils/pcap.h"

#include <stdexcept>
#include <sys/time.h>

namespace cxp {

namespace {

void putLe32(std::vector<uint8_t>& b, uint32_t v) {
    for (int i = 0; i < 4; ++i) b.push_back(static_cast<uint8_t>(v >> (8 * i)));
}

void putLe16(std::vector<uint8_t>& b, uint16_t v) {
    b.push_back(static_cast<uint8_t>(v));
    b.push_back(static_cast<uint8_t>(v >> 8));
}

}  // namespace

PcapWriter::PcapWriter(const std::string& path) {
    fh_ = std::fopen(path.c_str(), "wb");
    if (!fh_) throw std::runtime_error("cannot open pcap file " + path);
    // magic, ver_major, ver_minor, thiszone, sigfigs, snaplen, network
    std::vector<uint8_t> hdr;
    putLe32(hdr, 0xA1B2C3D4);
    putLe16(hdr, 2);
    putLe16(hdr, 4);
    putLe32(hdr, 0);
    putLe32(hdr, 0);
    putLe32(hdr, 0x40000);
    putLe32(hdr, DLT_USER0);
    std::fwrite(hdr.data(), 1, hdr.size(), fh_);
}

PcapWriter::~PcapWriter() { close(); }

void PcapWriter::writeWords(const std::vector<uint32_t>& words, double ts) {
    std::vector<uint8_t> payload;
    payload.reserve(words.size() * 4);
    for (uint32_t w : words) putLe32(payload, w);
    writeBytes(payload, ts);
}

void PcapWriter::writeBytes(const std::vector<uint8_t>& payload, double ts) {
    uint32_t sec, usec;
    if (ts < 0) {
        timeval tv{};
        gettimeofday(&tv, nullptr);
        sec = static_cast<uint32_t>(tv.tv_sec);
        usec = static_cast<uint32_t>(tv.tv_usec);
    } else {
        sec = static_cast<uint32_t>(ts);
        usec = static_cast<uint32_t>((ts - sec) * 1e6);
    }
    std::vector<uint8_t> rec;
    putLe32(rec, sec);
    putLe32(rec, usec);
    putLe32(rec, static_cast<uint32_t>(payload.size()));
    putLe32(rec, static_cast<uint32_t>(payload.size()));
    std::lock_guard<std::mutex> lk(mu_);
    if (!fh_) return;
    std::fwrite(rec.data(), 1, rec.size(), fh_);
    std::fwrite(payload.data(), 1, payload.size(), fh_);
}

void PcapWriter::close() {
    std::lock_guard<std::mutex> lk(mu_);
    if (fh_) {
        std::fclose(fh_);
        fh_ = nullptr;
    }
}

}  // namespace cxp
