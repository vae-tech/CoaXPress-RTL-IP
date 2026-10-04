#include "cxp/camera/client.h"

#include <algorithm>
#include <cctype>
#include <chrono>

#include "cxp/protocol/bench.h"
#include "cxp/protocol/crc.h"
#include "cxp/utils/hexdump.h"

namespace cxp {

std::optional<std::pair<uint64_t, uint64_t>> parseGenicamUrl(const std::string& url) {
    size_t colon = url.find(':');
    if (colon == std::string::npos) return std::nullopt;
    std::string scheme = url.substr(0, colon);
    std::transform(scheme.begin(), scheme.end(), scheme.begin(),
                   [](unsigned char c) { return std::tolower(c); });
    if (scheme != "local" && scheme != "async") return std::nullopt;
    std::vector<std::string> parts;
    std::string body = url.substr(colon + 1);
    size_t start = 0;
    while (true) {
        size_t semi = body.find(';', start);
        parts.push_back(body.substr(start, semi - start));
        if (semi == std::string::npos) break;
        start = semi + 1;
    }
    if (parts.size() < 3) return std::nullopt;
    try {
        size_t p1 = 0, p2 = 0;
        uint64_t addr = std::stoull(parts[1], &p1, 16);
        uint64_t len = std::stoull(parts[2], &p2, 16);
        if (p1 != parts[1].size() || p2 != parts[2].size()) return std::nullopt;
        return std::make_pair(addr, len);
    } catch (const std::exception&) {
        return std::nullopt;
    }
}

CameraControl::CameraControl(std::shared_ptr<FifoEndpoint> endpoint, int ack_timeout_ms,
                             int retries, const std::string& pcap_path)
    : ep_(std::move(endpoint)),
      ack_timeout_ms_(ack_timeout_ms),
      retries_(retries),
      log_("camera") {
    if (!pcap_path.empty()) pcap_ = std::make_unique<PcapWriter>(pcap_path);
}

CameraControl::~CameraControl() { disconnect(); }

// -- lifecycle ----------------------------------------------------------------
DeviceInfo CameraControl::connect() {
    {
        std::lock_guard<std::mutex> lk(bench_mu_);
        bench_caps_.reset();  // a reconnect may face another device
    }
    ep_->open();
    if (!rx_thr_.joinable()) {
        rx_stop_ = false;
        rx_thr_ = std::thread(&CameraControl::rxLoop, this);
    }
    // Cold-start hygiene: a freshly opened c2h FIFO may still carry acks from
    // a previous run, and the device's word-aligner / soft-sampler warm-up
    // emits a corrupted leading ack on the first exchange.  Settle briefly and
    // discard anything queued so discover() starts from a clean state.
    drainStale();
    DeviceInfo info = discover();
    connected = true;
    return info;
}

void CameraControl::drainStale(int settle_ms) {
    std::this_thread::sleep_for(std::chrono::milliseconds(settle_ms));
    size_t n_ack = ack_q_.clear();
    size_t n_strm = stream_q_.clear();
    if (n_ack || n_strm) {
        log_.info("drained stale frames on connect (ack=%zu stream=%zu)", n_ack, n_strm);
    }
}

void CameraControl::stopThreads() {
    hb_stop_ = true;
    hb_cv_.notify_all();
    if (hb_thr_.joinable()) hb_thr_.join();
    rx_stop_ = true;
    if (rx_thr_.joinable()) rx_thr_.join();
}

void CameraControl::disconnect() {
    connected = false;
    stopThreads();
    ep_->close();
    if (pcap_) pcap_->close();
}

DeviceInfo CameraControl::reconnect() {
    log_.warning("reconnecting link");
    stopThreads();
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    return connect();
}

// -- discovery / link init ----------------------------------------------------
DeviceInfo CameraControl::discover() {
    DeviceInfo info;
    info.magic = readReg(Bootstrap::STANDARD)[0];
    if (info.magic != CXP_MAGIC) {
        throw CameraError(strprintf("not a CXP device: STANDARD=0x%08X (expected 0x%08X)",
                                    info.magic, CXP_MAGIC));
    }
    info.revision = readReg(Bootstrap::REVISION)[0];
    info.vendor_name = readString(Bootstrap::VENDOR_NAME, 32);
    info.model_name = readString(Bootstrap::MODEL_NAME, 32);
    // XmlUrlAddress (0x0018) is a *pointer* per §10.3.11; the 64-byte URL
    // string lives at the address it returns.
    uint32_t url_ptr = readReg(Bootstrap::XML_URL_ADDRESS)[0];
    info.xml_url = readString(url_ptr, 64);
    if (!info.xml_url.empty()) {
        log_.info("XML found via read path: %s (ptr=0x%08X)", info.xml_url.c_str(), url_ptr);
    } else {
        log_.warning("no XML URL found at ptr=0x%08X", url_ptr);
    }
    info.device_link_id = readReg(Bootstrap::DEVICE_LINK_ID)[0];
    log_.info("discovered %s %s rev=%u link_id=%u", info.vendor_name.c_str(),
              info.model_name.c_str(), info.revision, info.device_link_id);
    discoverXml(info);
    return info;
}

// Discovery continues into the GenICam manifest.  Best-effort: a device with
// an absent or malformed manifest is still a discovered device.
void CameraControl::discoverXml(const DeviceInfo& info) {
    {
        std::lock_guard<std::mutex> lk(state_mu_);
        xml_tree_.reset();
        parameters_.clear();
    }
    try {
        auto [tree, values] = readAllParameters(&info);
        std::lock_guard<std::mutex> lk(state_mu_);
        xml_tree_ = std::move(tree);
        parameters_ = std::move(values);
    } catch (const CameraError& exc) {
        log_.warning("XML discovery skipped: %s", exc.what());
    }
}

void CameraControl::linkInit(uint32_t host_link_id) {
    // ControlPacketSizeMax is read-only (Table 45): nothing else to set.
    writeReg(Bootstrap::MASTER_HOST_LINK_ID, {host_link_id});
    log_.info("link initialised (host_link_id=%u)", host_link_id);
}

void CameraControl::reset() {
    CtrlCmdPacket cmd;
    cmd.opcode = CtrlOpcode::Reset;
    {
        std::lock_guard<std::mutex> lk(cmd_mu_);
        txn(cmd);
    }
    log_.warning("device reset issued");
}

// -- register access ----------------------------------------------------------
// CXP §8.2.1 transmits 32-bit values big-endian (MSByte in character P0).  The
// FIFO bridge packs P0 into the LSByte of each uint32 (src/emu/bridge/dpi/cxp_fifo_dpi.c),
// so a data word on the link is the byte-reversal of the logical register
// value; readReg/writeReg convert so callers see spec-correct values.
std::vector<uint32_t> CameraControl::readReg(uint32_t address, uint32_t nwords) {
    CtrlCmdPacket cmd;
    cmd.opcode = CtrlOpcode::Read;
    cmd.address = address;
    cmd.nwords = nwords;
    CtrlAckPacket ack;
    {
        std::lock_guard<std::mutex> lk(cmd_mu_);
        ack = txn(cmd);
    }
    if (ack.code != AckCode::Ok) {
        throw CameraError(strprintf("read 0x%X failed: %s", address, ackCodeName(ack.code)));
    }
    std::vector<uint32_t> out;
    if (ack.data.empty()) return {0};
    for (uint32_t w : ack.data) out.push_back(bswap32(w));
    return out;
}

void CameraControl::writeReg(uint32_t address, const std::vector<uint32_t>& data) {
    CtrlCmdPacket cmd;
    cmd.opcode = CtrlOpcode::Write;
    cmd.address = address;
    for (uint32_t d : data) cmd.data.push_back(bswap32(d));
    CtrlAckPacket ack;
    {
        std::lock_guard<std::mutex> lk(cmd_mu_);
        ack = txn(cmd);
    }
    // CXP Table 22: a successful write acks 0x01 (WRITE_OK); accept 0x00 too.
    if (ack.code != AckCode::Ok && ack.code != AckCode::WriteOk) {
        throw CameraError(strprintf("write 0x%X failed: %s", address, ackCodeName(ack.code)));
    }
}

std::vector<uint8_t> CameraControl::readBytes(uint32_t address, size_t length) {
    uint32_t nwords = static_cast<uint32_t>((length + 3) / 4);
    // A control READ is capped at the device's command-buffer depth; bulk
    // reads like the XML manifest are split or the device NAKs BAD_SIZE.
    std::vector<uint8_t> out;
    out.reserve(nwords * 4);
    uint32_t done = 0;
    while (done < nwords) {
        uint32_t n = std::min(CXP_MAX_CTRL_XFER_WORDS, nwords - done);
        for (uint32_t w : readReg(address + done * 4, n)) {
            out.push_back(uint8_t(w >> 24));
            out.push_back(uint8_t(w >> 16));
            out.push_back(uint8_t(w >> 8));
            out.push_back(uint8_t(w));
        }
        done += n;
    }
    out.resize(std::min(out.size(), length));
    return out;
}

void CameraControl::writeBytes(uint32_t address, std::vector<uint8_t> data) {
    data.resize((data.size() + 3) / 4 * 4, 0);
    std::vector<uint32_t> words;
    for (size_t i = 0; i < data.size(); i += 4) {
        words.push_back(uint32_t(data[i]) << 24 | uint32_t(data[i + 1]) << 16 |
                        uint32_t(data[i + 2]) << 8 | data[i + 3]);
    }
    for (size_t off = 0; off < words.size(); off += CXP_MAX_CTRL_XFER_WORDS) {
        size_t end = std::min(words.size(), off + CXP_MAX_CTRL_XFER_WORDS);
        writeReg(static_cast<uint32_t>(address + off * 4),
                 std::vector<uint32_t>(words.begin() + off, words.begin() + end));
    }
}

std::string CameraControl::readString(uint32_t address, size_t length) {
    std::string s;
    for (uint8_t c : readBytes(address, length)) {
        if (c == 0) break;
        s += (c < 0x80) ? static_cast<char>(c) : '?';
    }
    return s;
}

// -- XML / SFNC ---------------------------------------------------------------
// Does the camera advertise a readable XML?  Returns (addr, length) of the
// manifest in device register space or throws CameraError with the reason.
std::pair<uint64_t, uint64_t> CameraControl::checkXml(const DeviceInfo* info) {
    DeviceInfo local;
    if (info == nullptr) {
        local = discover();
        info = &local;
    }
    if (info->xml_url.empty()) {
        throw CameraError("no XML: XmlUrlAddress points at an empty string "
                          "(camera advertises no manifest)");
    }
    auto loc = parseGenicamUrl(info->xml_url);
    if (!loc) {
        throw CameraError("XML not device-resident (url='" + info->xml_url +
                          "'); only Local:/Async: schemes can be read over the link");
    }
    auto [addr, length] = *loc;
    if (length == 0) {
        throw CameraError("XML manifest length is 0 (url='" + info->xml_url + "')");
    }
    try {
        uint32_t declared = readReg(Bootstrap::XML_MFST_SIZE)[0];
        if (declared && declared != length) {
            log_.warning("XmlManifestSize=%u disagrees with URL length %llu; trusting URL",
                         declared, (unsigned long long)length);
        }
    } catch (const CameraError& exc) {
        log_.debug("XmlManifestSize read skipped: %s", exc.what());
    }
    log_.info("XML present: %llu bytes @ 0x%08llX (%s)", (unsigned long long)length,
              (unsigned long long)addr, info->xml_url.c_str());
    return {addr, length};
}

std::vector<uint8_t> CameraControl::fetchXml(const DeviceInfo* info) {
    auto [addr, length] = checkXml(info);
    log_.info("fetching XML: %llu bytes @ 0x%08llX", (unsigned long long)length,
              (unsigned long long)addr);
    return readBytes(static_cast<uint32_t>(addr), static_cast<size_t>(length));
}

// Full pipeline: read XmlUrl, check it exists, fetch+parse it, then read
// every register the XML defines.  One bad register does not abort the sweep.
std::pair<std::shared_ptr<NodeTree>, std::map<std::string, ParamResult>>
CameraControl::readAllParameters(const DeviceInfo* info) {
    DeviceInfo local;
    if (info == nullptr) {
        local = discover();
        info = &local;
    }
    std::vector<uint8_t> raw = fetchXml(info);
    std::shared_ptr<NodeTree> tree;
    try {
        tree = NodeTree::fromString(raw);
    } catch (const std::exception& exc) {
        throw CameraError(strprintf("failed to parse GenICam XML (%zu bytes): %s",
                                    raw.size(), exc.what()));
    }
    std::map<std::string, ParamResult> values;
    for (Feature* feat : tree->features()) {
        if (!feat->isReadable() || !feat->isAvailable()) continue;
        if (feat->const_value) {
            values[feat->name].value = *feat->const_value;
            continue;
        }
        if (!feat->reg) continue;
        try {
            // Prime the tree cache with a live read so the value decode
            // (sign/enum/string) is reused.
            tree->primeCache(feat->reg->address,
                             readBytes(static_cast<uint32_t>(feat->reg->address),
                                       feat->reg->length));
            values[feat->name].value = feat->getValue();
        } catch (const std::exception& exc) {
            log_.warning("read %s failed: %s", feat->name.c_str(), exc.what());
            values[feat->name].error = exc.what();
        }
    }
    log_.info("read %zu parameters from %s %s", values.size(), info->vendor_name.c_str(),
              info->model_name.c_str());
    return {tree, values};
}

std::shared_ptr<NodeTree> CameraControl::xmlTree() const {
    std::lock_guard<std::mutex> lk(state_mu_);
    return xml_tree_;
}

std::map<std::string, ParamResult> CameraControl::parameters() const {
    std::lock_guard<std::mutex> lk(state_mu_);
    return parameters_;
}

// -- transaction core (retry + timeout) ----------------------------------------
CtrlAckPacket CameraControl::txn(const CtrlCmdPacket& cmd) {
    std::string last = "timeout";
    for (int attempt = 1; attempt <= retries_; ++attempt) {
        // Single-outstanding invariant: anything queued now is a stale/corrupt
        // ack from a prior attempt, not the reply to this command.
        if (size_t stale = ack_q_.clear()) {
            log_.debug("flushed %zu stale ack(s) before attempt %d", stale, attempt);
        }
        Words words = cmd.toWords();
        if (log_.enabled(LogLevel::Debug)) {
            log_.debug("TX CtrlCmdPacket\n%s", wordsHex(words).c_str());
        }
        try {
            send(words);
        } catch (const LinkClosed& exc) {
            last = exc.what();
            break;
        }
        CtrlAckPacket ack;
        const int wait_ms = ack_timeout_ms_;
        if (ack_q_.pop(ack, wait_ms)) return ack;
        log_.warning("ack timeout (attempt %d/%d) addr=0x%X", attempt, retries_, cmd.address);
        plogEvent(strprintf("no ack within %d ms (attempt %d/%d) addr=0x%X", wait_ms, attempt,
                            retries_, cmd.address));
    }
    plogEvent("control transaction failed: " + last);
    throw CameraError("control transaction failed: " + last);
}

// -- receive dispatcher ---------------------------------------------------------
void CameraControl::rxLoop() {
    while (!rx_stop_) {
        LinkFrame lf;
        try {
            if (!ep_->recv(lf, 100)) continue;
        } catch (const LinkClosed&) {
            return;
        }
        if (lf.magic != FRAME_MAGIC) {
            if (auto plog = protocolLog()) plog->side("RX", lf.magic, lf.words);
            std::lock_guard<std::mutex> lk(tap_mu_);
            for (const auto& [id, fn] : side_taps_) fn(lf.magic, lf.words);
            continue;
        }
        Words frame = std::move(lf.words);
        if (pcap_) pcap_->writeWords(frame);
        if (auto plog = protocolLog()) plog->rx(frame);
        // The tap sees a frame only after its ack is queued, so a raw session
        // that collected the ack through the tap also clears it from ack_q_.
        auto tap = [&] {
            std::lock_guard<std::mutex> lk(tap_mu_);
            for (const auto& [id, fn] : rx_taps_) fn(frame);
        };
        // Route by TYPE only.  A CRC-corrupted *stream* packet must still reach
        // the HS parser (it owns stream-CRC accounting).
        auto ptype = peekType(frame);
        if (ptype == PacketType::CtrlAck) {
            try {
                CtrlAckPacket ack = std::get<CtrlAckPacket>(decodePacket(frame));
                if (log_.enabled(LogLevel::Debug)) {
                    std::string data;
                    for (uint32_t d : ack.data) data += strprintf("%s0x%x", data.empty() ? "" : ", ", d);
                    log_.debug("RX ACK code=%s size=%uB data=[%s]", ackCodeName(ack.code),
                               ack.sizeBytes(), data.c_str());
                }
                ack_q_.push(std::move(ack));
            } catch (const PacketDecodeError& exc) {
                // Bad ACK -> command times out and retries (§9.6).
                log_.error("bad control-ack dropped: %s", exc.what());
                plogEvent(std::string("bad control-ack dropped: ") + exc.what());
            }
            tap();
        } else if (ptype == PacketType::Discovery) {
            try {
                auto d = std::get<DiscoveryPacket>(decodePacket(frame));
                if (d.is_heartbeat) heartbeat_alive = true;
            } catch (const PacketDecodeError&) {
            }
            tap();
        } else {
            tap();
            stream_q_.push(std::move(frame));
        }
    }
}

// -- raw access -----------------------------------------------------------------
CameraControl::TapId CameraControl::addRxTap(RxTap tap) {
    std::lock_guard<std::mutex> lk(tap_mu_);
    const TapId id = next_tap_++;
    rx_taps_.emplace_back(id, std::move(tap));
    return id;
}

void CameraControl::removeRxTap(TapId id) {
    std::lock_guard<std::mutex> lk(tap_mu_);
    rx_taps_.erase(std::remove_if(rx_taps_.begin(), rx_taps_.end(),
                                  [id](const auto& t) { return t.first == id; }),
                   rx_taps_.end());
}

CameraControl::TapId CameraControl::addSideTap(SideTap tap) {
    std::lock_guard<std::mutex> lk(tap_mu_);
    const TapId id = next_tap_++;
    side_taps_.emplace_back(id, std::move(tap));
    return id;
}

void CameraControl::removeSideTap(TapId id) {
    std::lock_guard<std::mutex> lk(tap_mu_);
    side_taps_.erase(std::remove_if(side_taps_.begin(), side_taps_.end(),
                                    [id](const auto& t) { return t.first == id; }),
                     side_taps_.end());
}

void CameraControl::sendChars(const Chars& chars) {
    const Words body = charsToWords(chars);
    if (auto plog = protocolLog()) plog->side("TX", CHARS_MAGIC, body);
    ep_->sendFrame(body, CHARS_MAGIC);
}

void CameraControl::sendBench(const Words& body) {
    if (auto plog = protocolLog()) plog->side("TX", bench::MAGIC, body);
    ep_->sendFrame(body, bench::MAGIC);
}

uint32_t CameraControl::benchCaps(int timeout_ms) {
    std::lock_guard<std::mutex> lk(bench_mu_);
    if (bench_caps_) return *bench_caps_;
    BlockingQueue<uint32_t> reply;
    const TapId tap = addSideTap([&reply](uint32_t magic, const Words& b) {
        if (magic == bench::MAGIC && b.size() >= 3 && b[0] == (bench::HELLO | bench::REPLY)) reply.push(b[2]);
    });
    uint32_t caps = 0;
    try {
        sendBench({bench::HELLO});
        if (!reply.pop(caps, timeout_ms)) caps = 0;
    } catch (...) {
        removeSideTap(tap);
        throw;
    }
    removeSideTap(tap);
    bench_caps_ = caps;
    return caps;
}

void CameraControl::rawSession(const std::function<void(const RawSender& send)>& body) {
    std::lock_guard<std::mutex> lk(cmd_mu_);
    ack_q_.clear();
    body([this](const Words& frame) { send(frame); });
    ack_q_.clear();
}

void CameraControl::send(const Words& frame) {
    if (auto plog = protocolLog()) plog->tx(frame);
    ep_->sendFrame(frame);
}

void CameraControl::setProtocolLog(std::shared_ptr<ProtocolLog> plog) {
    std::lock_guard<std::mutex> lk(plog_mu_);
    plog_ = std::move(plog);
}

std::shared_ptr<ProtocolLog> CameraControl::protocolLog() const {
    std::lock_guard<std::mutex> lk(plog_mu_);
    return plog_;
}

void CameraControl::plogEvent(const std::string& text) const {
    if (auto plog = protocolLog()) plog->event(text);
}

// -- heartbeat ------------------------------------------------------------------
void CameraControl::startHeartbeat(int period_ms) {
    if (hb_thr_.joinable()) return;
    hb_stop_ = false;
    hb_thr_ = std::thread(&CameraControl::hbLoop, this, period_ms);
}

void CameraControl::hbLoop(int period_ms) {
    while (!hb_stop_) {
        heartbeat_alive = false;
        DiscoveryPacket hb;
        hb.nonce = ++hb_nonce_;
        hb.is_heartbeat = true;
        try {
            send(hb.toWords());
        } catch (const LinkClosed&) {
            return;
        }
        std::unique_lock<std::mutex> lk(hb_mu_);
        if (hb_cv_.wait_for(lk, std::chrono::milliseconds(period_ms),
                            [this] { return hb_stop_.load(); })) {
            return;
        }
        if (!heartbeat_alive) log_.error("heartbeat lost (nonce=%u)", hb_nonce_);
    }
}

// -- accessor adapter -----------------------------------------------------------
std::vector<uint8_t> CameraRegisterAccessor::read(uint64_t address, size_t length) {
    auto cam = cam_.lock();
    if (!cam) throw CameraError("camera session closed");
    return cam->readBytes(static_cast<uint32_t>(address), length);
}

void CameraRegisterAccessor::write(uint64_t address, const std::vector<uint8_t>& data) {
    auto cam = cam_.lock();
    if (!cam) throw CameraError("camera session closed");
    cam->writeBytes(static_cast<uint32_t>(address), data);
}

}  // namespace cxp
