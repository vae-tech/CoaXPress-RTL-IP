#include "cxp/sim/virtual_camera.h"

#include <algorithm>
#include <chrono>

#include "cxp/image/reconstruct.h"
#include "cxp/protocol/bench.h"
#include "cxp/protocol/crc.h"

namespace cxp {

VirtualCamera::VirtualCamera(std::shared_ptr<FifoEndpoint> endpoint,
                             VirtualCameraConfig config)
    : ep_(std::move(endpoint)), cfg_(std::move(config)), log_("vcam") {
    initRegs();
}

VirtualCamera::~VirtualCamera() { stop(); }

// -- register memory ----------------------------------------------------------
std::vector<uint8_t> VirtualCamera::memRead(uint64_t addr, size_t length) const {
    std::lock_guard<std::mutex> lk(mem_mu_);
    std::vector<uint8_t> out(length, 0);
    for (size_t i = 0; i < length; ++i) {
        auto it = mem_.find(addr + i);
        if (it != mem_.end()) out[i] = it->second;
    }
    return out;
}

void VirtualCamera::memWrite(uint64_t addr, const uint8_t* data, size_t n) {
    std::lock_guard<std::mutex> lk(mem_mu_);
    for (size_t i = 0; i < n; ++i) mem_[addr + i] = data[i];
}

void VirtualCamera::memWriteStr(uint64_t addr, const std::string& s, size_t length) {
    std::vector<uint8_t> b(length, 0);
    std::copy_n(s.begin(), std::min(s.size(), length), b.begin());
    memWrite(addr, b.data(), b.size());
}

uint32_t VirtualCamera::r32(uint32_t addr) const {
    auto b = memRead(addr, 4);
    return uint32_t(b[0]) << 24 | uint32_t(b[1]) << 16 | uint32_t(b[2]) << 8 | b[3];
}

void VirtualCamera::w32(uint32_t addr, uint32_t val) {
    const uint8_t b[4] = {uint8_t(val >> 24), uint8_t(val >> 16), uint8_t(val >> 8),
                          uint8_t(val)};
    memWrite(addr, b, 4);
}

void VirtualCamera::initRegs() {
    const auto& c = cfg_;
    {
        std::lock_guard<std::mutex> lk(mem_mu_);
        mem_.clear();
    }
    // The map's power-on state, as the RTL register file's.
    for (const reg::Row& r : reg::BOOTSTRAP) {
        if (r.kind == reg::Kind::RO) w32(r.addr, r.value);
        if (r.kind == reg::Kind::RW || r.kind == reg::Kind::CRST) w32(r.addr, r.reset);
        if (r.kind == reg::Kind::STR) memWriteStr(r.addr, r.text, r.nbytes);
    }
    for (const reg::Feature& d : reg::DEVICE) {
        w32(d.slot, d.alias);  // §10.3.19-27: the slot reads the feature's address
        w32(d.alias, d.reset);
    }
    for (const reg::Word& w : reg::MANUFACTURER) w32(w.addr, w.reset);
    // This camera's identity and the XML it serves (the XmlUrl states its size).
    memWriteStr(reg::DEVICE_VENDOR_NAME, c.vendor, reg::DEVICE_VENDOR_NAME_LEN);
    memWriteStr(reg::DEVICE_MODEL_NAME, c.model, reg::DEVICE_MODEL_NAME_LEN);
    memWriteStr(reg::XML_URL, strprintf("Local:%s;%08X;%zX", reg::XML_FILE_NAME, XML_BASE, c.xml.size()),
                reg::XML_URL_LEN);
    if (!c.xml.empty()) memWrite(XML_BASE, c.xml.data(), c.xml.size());
    // The TPG-facing features power up at this camera's configuration.
    w32(R_WIDTH, c.width);
    w32(R_HEIGHT, c.height);
    w32(R_PIXFMT, c.pixel_format);
    w32(R_FRAMECNT, c.frames_per_start);
    w32(R_TESTPAT, c.test_pattern);
    w32(R_OFFSET_X, c.x_offs);
    w32(R_OFFSET_Y, c.y_offs);
    w32(R_TAPG, c.tap_geometry);
    w32(R_STREAMID, c.stream_id);
    w32(R_SOURCETAG, c.source_tag);
    w32(R_FLAGS, c.flags & 0xFF);
    w32(R_TPG_RUN, c.free_run ? 1 : 0);
}

// -- lifecycle ------------------------------------------------------------------
void VirtualCamera::start() {
    if (running_) return;
    ep_->open();
    running_ = true;
    rx_thr_ = std::thread(&VirtualCamera::serve, this);
    trig_thr_ = std::thread(&VirtualCamera::trigLoop, this);
    log_.info("virtual camera online: %s %s", cfg_.vendor.c_str(), cfg_.model.c_str());
}

void VirtualCamera::stop() {
    bool was_running = running_.exchange(false);
    stopAcquisition();
    trig_cv_.notify_all();
    if (trig_thr_.joinable()) trig_thr_.join();
    if (rx_thr_.joinable()) rx_thr_.join();
    if (was_running) ep_->close();
}

void VirtualCamera::runUntil(const std::atomic<bool>& stop_flag) {
    start();
    while (running_ && !stop_flag) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
    stop();
}

// -- command service ----------------------------------------------------------------
void VirtualCamera::serve() {
    while (running_) {
        LinkFrame lf;
        try {
            if (!ep_->recv(lf, 100)) continue;
        } catch (const LinkClosed&) {
            return;
        }
        if (lf.magic == CHARS_MAGIC) {
            handleChars(wordsToChars(lf.words));
        } else if (lf.magic == bench::MAGIC) {
            handleBench(lf.words);
        } else {
            handleFrame(lf.words);
        }
    }
}

void VirtualCamera::handleFrame(const Words& frame) {
    // Test Receiver (§8.7.1, §8.7.3): a Table 23 packet from the Host counts
    // in TestPacketCountRx, and every word that differs from the sequence
    // (or is missing) in TestErrorCount.  Host writes of 0 clear them.
    if (frame.size() >= 3 && frame[0] == SOP_WORD && frame[1] == replicateByte(0x04)) {
        uint32_t bad = 0;
        const size_t n = frame.size() >= 3 ? frame.size() - 3 : 0;  // words between TYPE and EOP
        for (uint32_t k = 0; k < 1024; ++k) bad += k >= n || frame[2 + k] != linkTestWord(uint8_t(4 * k));
        if (n > 1024) bad += uint32_t(n - 1024);
        constexpr uint32_t RX = reg::TEST_PACKET_COUNT_RX;  // 8 bytes, high word first
        w32(reg::TEST_ERROR_COUNT, r32(reg::TEST_ERROR_COUNT) + bad);
        const uint64_t rx = (uint64_t(r32(RX)) << 32 | r32(RX + 4)) + 1;
        w32(RX, uint32_t(rx >> 32));
        w32(RX + 4, uint32_t(rx));
        return;
    }
    Packet pkt;
    try {
        pkt = decodePacket(frame);
    } catch (const PacketDecodeError& exc) {
        if (exc.reason() == "crc") {
            sendAck(AckCode::CrcError);
        } else {
            log_.warning("dropping bad uplink frame: %s", exc.what());
        }
        return;
    }
    if (auto* cmd = std::get_if<CtrlCmdPacket>(&pkt)) {
        handleCmd(*cmd);
    } else if (auto* d = std::get_if<DiscoveryPacket>(&pkt)) {
        if (d->is_heartbeat) {
            try {
                ep_->sendFrame(d->toWords());
            } catch (const LinkClosed&) {
            }
        }
    }
}

void VirtualCamera::handleCmd(const CtrlCmdPacket& cmd) {
    // Bench: a faulting register bus answers every access with its code and
    // carries none of them out.
    if (const uint32_t err = reg_err_; err && cmd.opcode != CtrlOpcode::Reset) {
        sendAck(static_cast<AckCode>(err));
        return;
    }
    // Bench: on an extension connection the control channel is read-only
    // (§10.3.30); a write is refused like one to a read-only register, except
    // that ConnectionReset and MasterHostConnectionID writes, which a Host
    // sends to every connection during discovery (§10.3.28, §10.3.30 notes),
    // are ignored and acknowledged 0x01 (decision D3, as the RTL device).
    if (ext_link_ && cmd.opcode == CtrlOpcode::Write) {
        const bool ignored = cmd.address == reg::CONNECTION_RESET ||
                             cmd.address == reg::MASTER_HOST_CONNECTION_ID;
        sendAck(ignored ? AckCode::WriteOk : AckCode::WriteProtect);
        return;
    }
    if (cmd.opcode == CtrlOpcode::Reset) {
        log_.warning("RESET received");
        resetDevice();
        sendAck(AckCode::ResetOk);
        return;
    }
    // On-link data words are the bridge uint32 (P0 in LSByte) per CXP §8.2.1
    // big-endian wire ordering; memory stores logical 32-bit values.
    if (cmd.opcode == CtrlOpcode::Read) {
        const uint32_t n = cmd.nwords ? cmd.nwords : 1;
        if (const AckCode code = readAccess(cmd.address, n); code != AckCode::Ok) {
            sendAck(code);
            return;
        }
        Words data;
        for (uint32_t i = 0; i < n; ++i) data.push_back(bswap32(r32(cmd.address + 4 * i)));
        sendAck(AckCode::Ok, std::move(data));
        return;
    }
    if (cmd.opcode == CtrlOpcode::Write) {
        std::vector<uint32_t> values;
        for (uint32_t w : cmd.data) values.push_back(bswap32(w));
        // Table 22: 0x01 when the write is carried out, else the refusal.
        const AckCode code = writeAccess(cmd.address, values);
        sendAck(code);
        if (code == AckCode::WriteOk) onWrite(cmd.address, cmd.data);
        return;
    }
    sendAck(AckCode::InvalidOperation);
}

void VirtualCamera::onWrite(uint32_t addr, const Words& data) {
    // ConnectionReset (§10.3.28): besides the registers (connectionReset),
    // the Device de-asserts the trigger it recreates from the Host's
    // (§8.3.2, as a falling edge would).
    if (addr == reg::CONNECTION_RESET && !data.empty() && (bswap32(data[0]) & 1u)) {
        bool edge = false;
        {
            std::lock_guard<std::mutex> lk(trig_mu_);
            edge = trig_out_;
            trig_out_ = false;
        }
        if (edge) benchEvent(bench::TRIG_OUT, 0);
    }
    if (addr == R_ACQ_START && !data.empty() && data[0]) {
        startAcquisition();
    } else if (addr == R_ACQ_STOP && !data.empty() && data[0]) {
        stopAcquisition();
    }
}

// -- register decode (cxp::reg; cxp_protocol.regref in C++) ---------------------------
namespace {

const reg::Row* bootstrapRow(uint32_t a) {
    for (const reg::Row& r : reg::BOOTSTRAP) {
        if (a >= r.addr && a < r.addr + std::max<uint32_t>(4, r.nbytes)) return &r;
    }
    return nullptr;
}

const reg::Feature* deviceFeature(uint32_t a) {
    for (const reg::Feature& d : reg::DEVICE) {
        if (a == d.slot || a == d.alias) return &d;
    }
    return nullptr;
}

bool inMfrWindow(uint32_t a) { return a >= reg::MFR_BASE && a < reg::MFR_BASE + 4 * reg::MFR_WORDS; }

}  // namespace

AckCode VirtualCamera::readAccess(uint32_t addr, uint32_t nwords) const {
    if (addr & 3) return AckCode::InvalidAddress;
    for (uint32_t i = 0; i < nwords; ++i) {
        const uint32_t a = addr + 4 * i;
        if (bootstrapRow(a)) continue;
        if (const reg::Feature* d = deviceFeature(a)) {
            if (a == d->alias && d->write_only) return AckCode::ReadProtect;
            continue;
        }
        if (inMfrWindow(a)) continue;
        if (a >= XML_BASE && a - XML_BASE < cfg_.xml.size()) continue;
        return AckCode::InvalidAddress;
    }
    return AckCode::Ok;
}

AckCode VirtualCamera::writeAccess(uint32_t addr, const std::vector<uint32_t>& values) {
    if (addr & 3) return AckCode::InvalidAddress;
    // Decide the whole access before changing anything: a refused write
    // changes nothing.
    std::vector<std::pair<uint32_t, uint32_t>> plan;  // (address, value)
    std::vector<const reg::Row*> clears;              // counters written 0
    for (size_t i = 0; i < values.size(); ++i) {
        const uint32_t a = addr + 4 * uint32_t(i), v = values[i];
        if (const reg::Row* r = bootstrapRow(a)) {
            switch (r->kind) {
            case reg::Kind::RO:
            case reg::Kind::STR:
            case reg::Kind::ZERO:
                return AckCode::WriteProtect;
            case reg::Kind::CNT:
                if (v != 0) return AckCode::InvalidData;  // only a write of 0 clears it
                clears.push_back(r);
                continue;
            case reg::Kind::RW:
            case reg::Kind::CRST:
                if (reg::refused(v, r->min, r->max, r->allowed, r->n_allowed, r->multiple))
                    return AckCode::InvalidData;
                plan.emplace_back(r->addr, v);
                continue;
            case reg::Kind::NVSTR:
                plan.emplace_back(a, v);
                continue;
            }
        }
        if (const reg::Feature* d = deviceFeature(a)) {
            if (a == d->slot) return AckCode::WriteProtect;  // the slot is read-only
            if (reg::refused(v, d->min, d->max, d->allowed, d->n_allowed)) return AckCode::InvalidData;
            plan.emplace_back(a, v);
            continue;
        }
        if (inMfrWindow(a)) {
            for (const reg::Word& w : reg::MANUFACTURER) {
                if (w.addr == a && reg::refused(v, w.min, w.max, w.allowed, w.n_allowed))
                    return AckCode::InvalidData;
            }
            plan.emplace_back(a, v);
            continue;
        }
        if (a >= XML_BASE && a - XML_BASE < cfg_.xml.size()) return AckCode::WriteProtect;
        return AckCode::InvalidAddress;
    }
    for (const auto& [a, v] : plan) w32(a, v);
    for (const reg::Row* r : clears) {
        for (uint32_t off = 0; off < r->nbytes; off += 4) w32(r->addr + off, 0);
    }
    // A write of 1 to ConnectionReset applies §10.3.28 and reads 0 again.
    if (addr == reg::CONNECTION_RESET && !values.empty() && (values[0] & 1u)) connectionReset();
    return AckCode::WriteOk;
}

void VirtualCamera::connectionReset() {
    for (const reg::Row& r : reg::BOOTSTRAP) {
        if (r.kind == reg::Kind::RW && r.conn_reset != reg::NONE) w32(r.addr, uint32_t(r.conn_reset));
        if (r.kind == reg::Kind::CRST) w32(r.addr, 0);
        if (r.kind == reg::Kind::CNT) {
            for (uint32_t off = 0; off < r.nbytes; off += 4) w32(r.addr + off, 0);
        }
    }
}

void VirtualCamera::resetDevice() {
    stopAcquisition();
    initRegs();
    stream_tag_ = 0;
    stream_pkt_n_ = 0;
    frame_id_ = 0;
}

void VirtualCamera::sendAck(AckCode code, Words data) {
    // CXP 1.1.1 control acks carry a Size field, not a tag.
    CtrlAckPacket ack;
    ack.code = code;
    ack.data = std::move(data);
    try {
        ep_->sendFrame(ack.toWords());
    } catch (const LinkClosed&) {
    }
}

// -- acquisition / streaming ----------------------------------------------------------
void VirtualCamera::startAcquisition() {
    std::lock_guard<std::mutex> lk(acq_mu_);
    if (acq_active_) return;
    if (acq_thr_.joinable()) acq_thr_.join();  // previous burst finished
    acq_cancel_ = false;
    acq_active_ = true;
    acq_thr_ = std::thread(&VirtualCamera::acquire, this);
}

void VirtualCamera::stopAcquisition() {
    std::lock_guard<std::mutex> lk(acq_mu_);
    acq_cancel_ = true;
    if (acq_thr_.joinable()) acq_thr_.join();
}

void VirtualCamera::acquire() {
    // TpgRun (RTL cfg_run) selects free-run: stream frames until
    // AcquisitionStop / RESET.  Otherwise emit the FrameCount burst (0 = 1).
    const bool free_run = r32(R_TPG_RUN) != 0;
    uint32_t n = r32(R_FRAMECNT);
    if (n == 0) n = 1;
    for (uint32_t i = 0; (free_run || i < n) && !acq_cancel_; ++i) {
        if (use_tpg_) sendFrame();  // USE_TPG = 0: the pixel port is the source
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    // Acquisition-end event; the link may already be torn down.
    try {
        ep_->sendFrame(EventPacket{}.toWords());
    } catch (const LinkClosed&) {
    }
    acq_active_ = false;
}

uint32_t VirtualCamera::streamChunkWords() const {
    // StreamPacketSizeMax (§10.3.32) is bytes of the whole packet, as the RTL
    // takes it (cxp_device_top): DsizeP = SPSM / 4 - 8 payload words (SOP,
    // type, 4 header words, CRC and EOP are the other 8), 1 .. 0xFFFF.  Below
    // 36 bytes no packet fits; at 0 (its power-on value, "not initialized",
    // Table 44) nothing streams.  0 = send no stream packet.
    const uint32_t spsm = r32(reg::STREAM_PACKET_SIZE_MAX);
    if (spsm < SPSM_MIN_BYTES) return 0;
    const uint32_t words = spsm / 4;
    return std::min<uint32_t>(words - 8, 0xFFFF);
}

// Build one frame's word stream the way the RTL does, then chop it into
// stream packets of DsizeP words (§9.6.2):
//
//     <25-word rectangular image header (Table 38)>
//     for each of H lines:
//         <2-word rectangular line marker (Table 39)>
//         <W bytes of pixel data, big-endian-packed>
void VirtualCamera::sendFrame() {
    const uint32_t w = r32(R_WIDTH);
    const uint32_t h = r32(R_HEIGHT);
    // PixelFormat holds a PFNC value; the header carries its PixelF code.
    uint32_t fmt_raw = pixelFFromPfnc(r32(R_PIXFMT));
    auto fmt_opt = toPixelFormat(fmt_raw);
    if (!fmt_opt) {
        log_.warning("unsupported PixelFormat 0x%08X (PixelF 0x%04X), streaming MONO8", r32(R_PIXFMT), fmt_raw);
    }
    const PixelFormat fmt = fmt_opt.value_or(PixelFormat::Mono8);
    const std::vector<uint8_t> pixels =
        renderTestPattern(fmt, w, h, r32(R_TESTPAT), frame_id_);
    const uint64_t bpl = bytesPerFrame(fmt, w, 1);  // DsizeL: pixel bytes per line

    Words words = cxpImageHeaderRect(1, 0, w, r32(R_OFFSET_X), h, r32(R_OFFSET_Y),
                                     static_cast<uint32_t>(bpl),
                                     static_cast<uint32_t>(fmt), 0, 0);
    const Words lm = cxpLineMarkerRect();
    words.reserve(words.size() + h * (lm.size() + (bpl + 3) / 4));
    for (uint32_t y = 0; y < h; ++y) {
        words.insert(words.end(), lm.begin(), lm.end());
        const uint64_t off = uint64_t(y) * bpl;
        const uint64_t len = std::min<uint64_t>(bpl, pixels.size() - std::min<uint64_t>(off, pixels.size()));
        Words line = bytesToWordsBe(pixels.data() + off, len);
        words.insert(words.end(), line.begin(), line.end());
    }

    emitImage(std::move(words), true);
    ++frame_id_;
}

// Chop one image's words into stream packets of DsizeP words (§9.6.2).
// An acquisition image stops at AcquisitionStop; a pixel-port image does not.
void VirtualCamera::emitImage(Words words, bool acquisition) {
    std::lock_guard<std::mutex> lk(tx_mu_);
    const size_t chunk = streamChunkWords();
    if (chunk == 0) return;  // StreamPacketSizeMax 0 or below one packet
    for (size_t i = 0; i < words.size() && !(acquisition && acq_cancel_); i += chunk) {
        emitStream(words.data() + i, std::min(chunk, words.size() - i));
    }
}

void VirtualCamera::emitStream(const uint32_t* payload, size_t n) {
    ++stream_pkt_n_;
    if (cfg_.drop_packet_every && stream_pkt_n_ % cfg_.drop_packet_every == 0) {
        ++stream_tag_;  // gap on purpose
        return;
    }
    StreamPacket pkt;
    pkt.stream_id = 0;
    pkt.tag = stream_tag_;
    pkt.payload.assign(payload, payload + n);
    pkt.corrupt_crc = cfg_.inject_crc_every && stream_pkt_n_ % cfg_.inject_crc_every == 0;
    try {
        ep_->sendFrame(pkt.toWords());
    } catch (const LinkClosed&) {
        acq_cancel_ = true;
        return;
    }
    ++stream_tag_;
}

}  // namespace cxp
