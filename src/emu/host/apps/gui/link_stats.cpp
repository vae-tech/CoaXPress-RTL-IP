#include "link_stats.h"

#include <algorithm>

#include <QLocale>

#include "cxp/protocol/crc.h"
#include "cxp/protocol/packets.h"

using namespace cxp;

namespace {

constexpr uint32_t K28_3_REP = replicateByte(K28_3);
constexpr uint32_t HDR_TYPE_REC_REP = replicateByte(HDR_TYPE_REC);

const QLocale& numLocale() {
    static const QLocale l(QLocale::English, QLocale::UnitedStates);
    return l;
}

QString num(uint64_t v) { return numLocale().toString(qulonglong(v)); }
QString fixed(double v, int prec) { return numLocale().toString(v, 'f', prec); }
QString dash() { return QStringLiteral("—"); }

StatNode leaf(const QString& name, const QString& value) { return {name, value, {}}; }
StatNode leaf(const QString& name, uint64_t value) { return {name, QString::number(value), {}}; }

}  // namespace

LinkStats::LinkStats(std::shared_ptr<CameraControl> host)
    : host_(std::move(host)),
      start_(std::chrono::steady_clock::now()),
      fps_prev_t_(start_) {}

LinkStats::~LinkStats() { stop(); }

void LinkStats::start() {
    if (thr_.joinable()) return;
    stop_ = false;
    thr_ = std::thread(&LinkStats::drainLoop, this);
}

void LinkStats::stop() {
    stop_ = true;
    if (thr_.joinable()) thr_.join();
}

// Continuously feed the host downlink into the parser (so its RX counters
// stay live) and the statistics.  When the stream goes idle the parser is
// flushed so a fully-received trailing frame still reaches the viewer.
void LinkStats::drainLoop() {
    while (!stop_) {
        Words frame;
        bool got = host_->streamQueue().pop(frame, 500);
        std::lock_guard<std::mutex> lk(mu_);
        try {
            if (!got) {
                if (FramePtr img = parser_.flush()) noteVideoLocked(img);
                continue;
            }
            feedLocked(frame);
            for (auto& img : parser_.feedFrame(frame)) noteVideoLocked(img);
            if (peekType(frame) == PacketType::LinkTest) noteLinktestLocked(frame);
        } catch (const std::exception&) {
        }
    }
}

void LinkStats::feedLocked(const Words& frame) {
    ++frames_;
    bytes_ += frame.size() * 4;
    auto ptype = peekType(frame);
    ++by_type_[ptype ? packetTypeName(*ptype) : "UNKNOWN"];
    if (!ptype) {
        ++unknown_;
        return;
    }
    if (*ptype != PacketType::Stream) return;
    StreamPacket pkt;
    try {
        pkt = std::get<StreamPacket>(decodePacket(frame));
    } catch (const PacketDecodeError&) {
        return;
    }
    StreamStat& st = streams_[pkt.stream_id];
    ++st.packets;
    const uint64_t pl_bytes = pkt.payload.size() * 4;
    st.bytes += pl_bytes;
    // CXP §9.4 image headers (Table 38) sit inline anywhere in the payload, so
    // scan for the K28.3 + 0x01 pair and vote the replicated geometry fields.
    const Words& p = pkt.payload;
    std::optional<size_t> hdr_idx;
    for (size_t i = 0; i + 1 < p.size(); ++i) {
        if (p[i] == K28_3_REP && p[i + 1] == HDR_TYPE_REC_REP) {
            hdr_idx = i;
            break;
        }
    }
    if (hdr_idx && p.size() - *hdr_idx >= 22) {
        auto vb = [&](size_t off) { return uint32_t(majorityByte(p[*hdr_idx + off]).value); };
        uint32_t xsize = vb(5) << 16 | vb(6) << 8 | vb(7);
        uint32_t ysize = vb(11) << 16 | vb(12) << 8 | vb(13);
        ++st.frames;
        st.last_frame_id = st.last_frame_id.value_or(0) + 1;
        st.last_width = xsize;
        st.last_height = ysize;
        st.last_pixel_format = vb(20) << 8 | vb(21);
        st.lines += ysize;
    } else {
        st.last_payload_bytes = pl_bytes;
        st.max_payload_bytes = std::max(st.max_payload_bytes, pl_bytes);
        st.min_payload_bytes = std::min(st.min_payload_bytes.value_or(pl_bytes), pl_bytes);
    }
}

void LinkStats::noteVideoLocked(FramePtr frame) {
    last_video_ = std::move(frame);
    ++video_seq_;
}

// Decode a §8.7 connection-test frame: [SOP, TYPE, data..., (filler,) EOP].
void LinkStats::noteLinktestLocked(const Words& frame) {
    auto snap = std::make_shared<LinkTestSnapshot>();
    if (frame.size() > 3) snap->data_words.assign(frame.begin() + 2, frame.end() - 1);
    // The Python-sim filler word is stripped; the RTL generator emits none.
    if (!snap->data_words.empty() && snap->data_words.back() == LINKTEST_FILLER) {
        snap->data_words.pop_back();
    }
    uint8_t seq = 0;
    for (size_t i = 0; i < snap->data_words.size(); ++i) {
        if (snap->data_words[i] != linkTestWord(seq)) {
            snap->error_indices.push_back(static_cast<uint32_t>(i));
        }
        seq = static_cast<uint8_t>(seq + 4);
    }
    last_linktest_ = std::move(snap);
    ++linktest_seq_;
}

FramePtr LinkStats::lastVideo() const {
    std::lock_guard<std::mutex> lk(mu_);
    return last_video_;
}

std::shared_ptr<const LinkTestSnapshot> LinkStats::lastLinktest() const {
    std::lock_guard<std::mutex> lk(mu_);
    return last_linktest_;
}

size_t LinkStats::clearFrames() {
    size_t drained = host_->streamQueue().clear();
    std::lock_guard<std::mutex> lk(mu_);
    last_video_.reset();
    video_seq_ = 0;
    last_linktest_.reset();
    linktest_seq_ = 0;
    return drained;
}

std::vector<StatNode> LinkStats::tree() {
    std::lock_guard<std::mutex> lk(mu_);
    const auto now = std::chrono::steady_clock::now();
    const double up = std::max(std::chrono::duration<double>(now - start_).count(), 1e-6);
    const FifoEndpoint& ep = host_->endpoint();
    const ParseStats& ps = parser_.stats;
    uint64_t overflows = 0;
    for (const auto& e : parser_.events) overflows += e.event_id == EventCode::STREAM_OVERFLOW;
    const uint64_t imgs_done = ps.images_completed;

    const double dt = std::chrono::duration<double>(now - fps_prev_t_).count();
    if (dt >= 0.25) {  // avoid division noise
        fps_last_inst_ = (imgs_done - fps_prev_imgs_) / dt;
        fps_last_inst_mbps_ = (bytes_ - fps_prev_bytes_) / dt / 1e6;
        fps_prev_t_ = now;
        fps_prev_imgs_ = imgs_done;
        fps_prev_bytes_ = bytes_;
    }

    auto bt = [this](const char* k) {
        auto it = by_type_.find(k);
        return it == by_type_.end() ? uint64_t(0) : it->second;
    };
    auto reason = [&ps](const char* k) {
        auto it = ps.decode_reasons.find(k);
        return it == ps.decode_reasons.end() ? uint64_t(0) : it->second;
    };
    uint64_t violations = 0;
    for (const auto& kv : ps.decode_reasons) violations += kv.second;

    std::vector<StatNode> general = {
        leaf("Session uptime", fixed(up, 1) + " s"),
        leaf("Total received frames", ep.rx_frames.load()),
        leaf("Total transmitted frames", ep.tx_frames.load()),
        leaf("Stream-path packets", frames_),
        leaf("Stream-path bytes", num(bytes_)),
        leaf("Average packet rate", fixed(frames_ / up, 1) + " /s"),
        leaf("FPS (current)", fixed(fps_last_inst_, 2) + " fps"),
        leaf("FPS (average)", fixed(imgs_done / up, 2) + " fps"),
        leaf("Throughput (MB/s, current)", fixed(fps_last_inst_mbps_, 3)),
        leaf("Throughput (MB/s, average)", fixed(bytes_ / up / 1e6, 3)),
        leaf("Throughput (Gb/s, average)", fixed(bytes_ * 8 / up / 1e9, 4)),
        leaf("Active stream", ps.stream_pkts ? "yes" : "no"),
        leaf("Connected", host_->connected ? "yes" : "no"),
        leaf("Heartbeat", host_->heartbeat_alive ? "alive" : "lost"),
        leaf("Active streams", uint64_t(streams_.size())),
    };

    std::vector<StatNode> pkt_types = {
        leaf("Stream / data", ps.stream_pkts),
        leaf("Event", ps.event_pkts),
        leaf("Acknowledge", ps.ack_pkts),
        leaf("Command (TX)", ep.tx_frames.load()),
        leaf("Test (link-test)", bt("LINKTEST")),
        leaf("Heartbeat", dash() + " (host-consumed)"),
        leaf("Discovery", dash() + " (host-consumed)"),
        leaf("Control / idle", dash() + " (not framed)"),
        leaf("Unknown / unsupported", unknown_),
    };

    std::vector<StatNode> errors = {
        leaf("CRC errors", ps.crc_errors),
        leaf("Invalid packet headers", reason("bad_type")),
        leaf("Invalid packet length", reason("short") + reason("dsize")),
        leaf("Unsupported packet types", reason("bad_type")),
        leaf("Sequence discontinuities", ps.missing_packets),
        leaf("Packet loss (missing pkts)", ps.missing_packets),
        leaf("Missing / dropped frames", ps.missing_frames),
        leaf("Timeout detection", dash()),
        leaf("FIFO / buffer overflows", overflows),
        leaf("Transport resyncs", ep.resyncs.load()),
        leaf("Transport reopens", ep.reopens.load()),
        leaf("Parsing errors / malformed", ps.malformed),
        leaf("Stream sync losses", ps.sync_losses),
        leaf("Protocol violations", violations),
    };

    std::vector<StatNode> stream_nodes;
    for (const auto& [sid, st] : streams_) {
        QString res = (st.last_width && st.last_height)
                          ? QString("%1 x %2").arg(*st.last_width).arg(*st.last_height)
                          : dash();
        QString pf = dash();
        if (st.last_pixel_format) {
            auto f = toPixelFormat(*st.last_pixel_format);
            pf = f ? QString(pixelFormatName(*f))
                   : QString::asprintf("0x%04X", *st.last_pixel_format);
        }
        stream_nodes.push_back(
            {QString::asprintf("Stream 0x%02X", uint(sid)),
             num(st.packets) + " pkts",
             {
                 leaf("Detected resolution", res),
                 leaf("Pixel format", pf),
                 leaf("Packets", num(st.packets)),
                 leaf("Payload bytes", num(st.bytes)),
                 leaf("Bandwidth (MB/s)", fixed(st.bytes / up / 1e6, 3)),
                 leaf("Frames (complete)", st.frames),
                 leaf("Last frame id",
                      st.last_frame_id ? QString::number(*st.last_frame_id) : dash()),
                 leaf("FPS (average)", fixed(st.frames / up, 2) + " fps"),
                 leaf("Lines (sum of heights)", num(st.lines)),
                 leaf("Payload size last", num(st.last_payload_bytes)),
                 leaf("Payload size min",
                      st.min_payload_bytes ? num(*st.min_payload_bytes) : dash()),
                 leaf("Payload size max", num(st.max_payload_bytes)),
             }});
    }
    stream_nodes.push_back(leaf("Images started", ps.images_started));
    stream_nodes.push_back(leaf("Images completed", ps.images_completed));
    // "Incomplete" = header arrived but payload never finished; steady state
    // is 0 or 1 in flight, anything larger signals reassembly trouble.
    const uint64_t done_or_flight = ps.images_completed + parser_.imagesInFlight();
    stream_nodes.push_back(leaf("Images incomplete",
                                ps.images_started > done_or_flight
                                    ? ps.images_started - done_or_flight
                                    : 0));
    stream_nodes.push_back(leaf("Stream packets seen", ps.stream_pkts));

    return {
        {"General RX statistics", "", general},
        {"Packet type counters", "", pkt_types},
        {"Error statistics", "", errors},
        {"Stream and image statistics", "", stream_nodes},
    };
}
