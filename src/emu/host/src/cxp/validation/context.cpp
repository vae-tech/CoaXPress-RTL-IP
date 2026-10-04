#include "cxp/validation/context.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <map>
#include <thread>

#include "cxp/image/reconstruct.h"
#include "cxp/protocol/crc.h"
#include "cxp/utils/log.h"

namespace cxp::validation {

namespace {

constexpr uint32_t MARKER_WORD = replicateByte(K28_3);
constexpr uint8_t HDR_TYPE_ARB = 0x03;   // Table 40 arbitrary image header
constexpr uint8_t LINE_TYPE_ARB = 0x04;  // Table 41 arbitrary line marker
constexpr size_t ARB_HDR_WORDS = 16;
constexpr size_t ARB_LINE_WORDS = 11;

std::string vformat(const char* fmt, va_list ap) {
    va_list ap2;
    va_copy(ap2, ap);
    int n = std::vsnprintf(nullptr, 0, fmt, ap2);
    va_end(ap2);
    std::string s(n > 0 ? size_t(n) : 0, '\0');
    if (n > 0) std::vsnprintf(s.data(), size_t(n) + 1, fmt, ap);
    return s;
}

// Majority-vote a replicated byte; -1 when no 3-of-4 majority exists.
int voted(uint32_t word) {
    Majority m = majorityByte(word);
    return m.ok ? m.value : -1;
}

bool replicated(uint32_t word) { return word == replicateByte(uint8_t(word)); }

}  // namespace

// -- tables ---------------------------------------------------------------------
std::string ackName(int code) {
    const char* what = "reserved";
    switch (code) {
    case -1: return "(no majority)";
    case Ack::READ_OK: what = "final, read data"; break;
    case Ack::WRITE_OK: what = "final, write OK"; break;
    case Ack::RESET_OK: what = "final, control channel reset OK"; break;
    case Ack::WAIT: what = "wait"; break;
    case Ack::BAD_ADDRESS: what = "invalid address"; break;
    case Ack::BAD_DATA: what = "invalid data"; break;
    case Ack::BAD_OPCODE: what = "invalid operation code"; break;
    case Ack::RO_WRITE: what = "write to read-only"; break;
    case Ack::WO_READ: what = "read from write-only"; break;
    case Ack::SIZE_TOO_LARGE: what = "size too large"; break;
    case Ack::SIZE_MISMATCH: what = "size inconsistent"; break;
    case Ack::MALFORMED: what = "malformed packet"; break;
    case Ack::CRC_ERROR: what = "CRC error"; break;
    default: break;
    }
    return strprintf("0x%02X (%s)", code, what);
}

const std::vector<BootReg>& bootstrapTable() {
    static const std::vector<BootReg> t = {
        {"Standard", Reg::STANDARD, 4, Access::RO, false},
        {"Revision", Reg::REVISION, 4, Access::RO, false},
        {"XmlManifestSize", Reg::XML_MANIFEST_SIZE, 4, Access::RO, false},
        {"XmlManifestSelector", Reg::XML_MANIFEST_SELECTOR, 4, Access::RW, false},
        {"XmlVersion", Reg::XML_VERSION, 4, Access::RO, false},
        {"XmlSchemaVersion", Reg::XML_SCHEMA_VERSION, 4, Access::RO, false},
        {"XmlUrlAddress", Reg::XML_URL_ADDRESS, 4, Access::RO, false},
        {"Iidc2Address", Reg::IIDC2_ADDRESS, 4, Access::RO, false},
        {"DeviceVendorName", Reg::DEVICE_VENDOR_NAME, 32, Access::RO, true},
        {"DeviceModelName", Reg::DEVICE_MODEL_NAME, 32, Access::RO, true},
        {"DeviceManufacturerInfo", Reg::DEVICE_MANUFACTURER_INFO, 48, Access::RO, true},
        {"DeviceVersion", Reg::DEVICE_VERSION, 32, Access::RO, true},
        {"DeviceSerialNumber", Reg::DEVICE_SERIAL_NUMBER, 16, Access::RO, true},
        {"DeviceUserID", Reg::DEVICE_USER_ID, 16, Access::RW, true},
        {"WidthAddress", Reg::WIDTH_ADDRESS, 4, Access::RO, false},
        {"HeightAddress", Reg::HEIGHT_ADDRESS, 4, Access::RO, false},
        {"AcquisitionModeAddress", Reg::ACQ_MODE_ADDRESS, 4, Access::RO, false},
        {"AcquisitionStartAddress", Reg::ACQ_START_ADDRESS, 4, Access::RO, false},
        {"AcquisitionStopAddress", Reg::ACQ_STOP_ADDRESS, 4, Access::RO, false},
        {"PixelFormatAddress", Reg::PIXEL_FORMAT_ADDRESS, 4, Access::RO, false},
        {"DeviceTapGeometryAddress", Reg::TAP_GEOMETRY_ADDRESS, 4, Access::RO, false},
        {"Image1StreamIDAddress", Reg::IMAGE1_STREAM_ID_ADDRESS, 4, Access::RO, false},
        {"ConnectionReset", Reg::CONNECTION_RESET, 4, Access::RW, false},
        {"DeviceConnectionID", Reg::DEVICE_CONNECTION_ID, 4, Access::RO, false},
        {"MasterHostConnectionID", Reg::MASTER_HOST_CONNECTION_ID, 4, Access::RW, false},
        {"ControlPacketSizeMax", Reg::CONTROL_PACKET_SIZE_MAX, 4, Access::RO, false},
        {"StreamPacketSizeMax", Reg::STREAM_PACKET_SIZE_MAX, 4, Access::RW, false},
        {"ConnectionConfig", Reg::CONNECTION_CONFIG, 4, Access::RW, false},
        {"ConnectionConfigDefault", Reg::CONNECTION_CONFIG_DEFAULT, 4, Access::RO, false},
        {"TestMode", Reg::TEST_MODE, 4, Access::RW, false},
        {"TestErrorCountSelector", Reg::TEST_ERROR_COUNT_SELECTOR, 4, Access::RW, false},
        {"TestErrorCount", Reg::TEST_ERROR_COUNT, 4, Access::RW, false},
        {"TestPacketCountTx", Reg::TEST_PACKET_COUNT_TX, 8, Access::RW, false},
        {"TestPacketCountRx", Reg::TEST_PACKET_COUNT_RX, 8, Access::RW, false},
        {"HsUpconnection", Reg::HS_UPCONNECTION, 4, Access::RO, false},
    };
    return t;
}

bool isValidSpeedCode(uint32_t code) {
    return code == 0x28 || code == 0x30 || code == 0x38 || code == 0x40 || code == 0x48;
}

std::optional<uint32_t> pfncValue(const std::string& name) {
    // GenICam PFNC values of the monochrome formats (§11.2.1.6: the
    // PixelFormat feature holds these, not the Table 25 PixelF code).
    static const std::map<std::string, uint32_t> t = {{"Mono8", 0x01080001}, {"Mono10", 0x01100003},
                                                      {"Mono12", 0x01100005}, {"Mono14", 0x01100025},
                                                      {"Mono16", 0x01100007}};
    auto it = t.find(name);
    if (it == t.end()) return std::nullopt;
    return it->second;
}

std::optional<uint16_t> pfncTable25(const std::string& name) {
    static const std::map<std::string, uint16_t> t = [] {
        std::map<std::string, uint16_t> m;
        const int depth[5] = {8, 10, 12, 14, 16};
        const std::pair<const char*, uint16_t> fam[] = {
            {"Mono", 0x0100}, {"BayerGR", 0x0310}, {"BayerRG", 0x0320}, {"BayerGB", 0x0330},
            {"BayerBG", 0x0340}, {"RGB", 0x0400}, {"RGBa", 0x0500}};
        for (const auto& [prefix, base] : fam) {
            for (int i = 0; i < 5; ++i) {
                uint16_t lo = uint16_t(i + 1);
                m[prefix + std::to_string(depth[i])] = uint16_t(base | lo);
            }
        }
        m["Raw"] = 0x0000;
        return m;
    }();
    auto it = t.find(name);
    if (it == t.end()) return std::nullopt;
    return it->second;
}

std::optional<int> pixelBits(uint32_t pixel_f) {
    const int nib = pixel_f & 0x0F;
    if (nib < 1 || nib > 5) return std::nullopt;
    const int depth = 8 + 2 * (nib - 1);
    switch (pixel_f >> 8) {
    case 0x01: case 0x02: case 0x03: return depth;
    case 0x04: return depth * 3;
    case 0x05: return depth * 4;
    default: return std::nullopt;
    }
}

// -- raw commands -------------------------------------------------------------------
// Header layout mirrors CtrlCmdPacket::toWords (Table 21): SOP, 4xTYPE,
// Cmd+Size word, big-endian Addr word, data, CRC over Cmd..data, EOP.
// header_lane_word indexes the frame (0 = SOP, 1 = TYPE, -1 = EOP).
Words buildCmd(const CmdSpec& s) {
    Words w;
    w.push_back(SOP_WORD);
    w.push_back(replicateByte(s.type));
    w.push_back(uint32_t(s.opcode) | (s.size_bytes >> 16 & 0xFF) << 8 | (s.size_bytes >> 8 & 0xFF) << 16 |
                (s.size_bytes & 0xFF) << 24);
    w.push_back(bswap32(s.address));
    w.insert(w.end(), s.data.begin(), s.data.end());
    uint32_t crc = crcToWire(crc32Words(w.data() + 2, w.size() - 2));
    if (s.crc_flip_bit >= 0) {
        crc ^= 1u << s.crc_flip_bit;
    } else if (s.corrupt_crc) {
        crc ^= 0xFFFFFFFFu;
    }
    w.push_back(crc);
    w.push_back(EOP_WORD);
    if (s.data_flip_bit >= 0 && !s.data.empty()) w[4] ^= 1u << s.data_flip_bit;
    if (s.header_lane_hit >= 0) {
        size_t idx = s.header_lane_word < 0 ? w.size() - 1 : size_t(s.header_lane_word);
        w[idx] ^= 0x5Au << (8 * (s.header_lane_hit & 3));
    }
    return w;
}

Words readCmd(uint32_t address, uint32_t size_bytes) {
    CmdSpec s;
    s.opcode = 0x00;
    s.size_bytes = size_bytes;
    s.address = address;
    return buildCmd(s);
}

Words writeCmd(uint32_t address, const std::vector<uint32_t>& values) {
    CmdSpec s;
    s.opcode = 0x01;
    s.size_bytes = uint32_t(values.size() * 4);
    s.address = address;
    for (uint32_t v : values) s.data.push_back(bswap32(v));
    return buildCmd(s);
}

Words resetCmd() {
    CmdSpec s;
    s.opcode = 0xFF;
    s.size_bytes = 0;
    s.address = 0;
    return buildCmd(s);
}

// -- decoders -------------------------------------------------------------------------
std::vector<uint32_t> RawAck::values() const {
    std::vector<uint32_t> out;
    for (uint32_t w : data) out.push_back(bswap32(w));
    return out;
}

std::vector<uint8_t> RawAck::bytes() const {
    std::vector<uint8_t> out;
    for (uint32_t v : values()) {
        out.push_back(uint8_t(v >> 24));
        out.push_back(uint8_t(v >> 16));
        out.push_back(uint8_t(v >> 8));
        out.push_back(uint8_t(v));
    }
    return out;
}

// Short form: SOP 4xTYPE 4xCODE EOP.  Long form: SOP 4xTYPE 4xCODE
// Size (big-endian) data... CRC EOP (Table 22).
RawAck decodeAck(const Words& f) {
    RawAck a;
    a.n_words = f.size();
    if (f.size() < 4) {
        a.defects.push_back(strprintf("ack frame only %zu words", f.size()));
        return a;
    }
    if (f.front() != SOP_WORD) a.defects.push_back(strprintf("SOP word 0x%08X", f.front()));
    if (f.back() != EOP_WORD) a.defects.push_back(strprintf("EOP word 0x%08X", f.back()));
    if (!replicated(f[1]) || uint8_t(f[1]) != 0x03) a.defects.push_back(strprintf("TYPE word 0x%08X", f[1]));
    a.code = voted(f[2]);
    if (!replicated(f[2])) a.defects.push_back(strprintf("CODE word 0x%08X not 4x replicated", f[2]));
    if (f.size() == 4) return a;
    a.long_form = true;
    if (f.size() < 6) {
        a.defects.push_back(strprintf("long ack only %zu words", f.size()));
        return a;
    }
    a.size_field = bswap32(f[3]);
    if (a.size_field > 0xFFFFFF) a.defects.push_back(strprintf("Size word 0x%08X over 24 bits", f[3]));
    a.data.assign(f.begin() + 4, f.end() - 2);
    const uint32_t crc = crc32Words(f.data() + 2, f.size() - 4);
    a.crc_ok = wireToCrc(f[f.size() - 2]) == crc;
    if (!a.crc_ok) a.defects.push_back("ack CRC mismatch");
    return a;
}

std::vector<StreamPkt> streamPackets(const std::vector<Captured>& cap) {
    std::vector<StreamPkt> out;
    for (const Captured& c : cap) {
        const Words& f = c.frame;
        if (f.size() < 3 || voted(f[1]) != 0x01) continue;
        StreamPkt p;
        p.t_ms = c.t_ms;
        p.total_words = f.size();
        if (f.front() != SOP_WORD) p.defects.push_back("SOP word not 4xK27.7");
        if (f.back() != EOP_WORD) p.defects.push_back("EOP word not 4xK29.7");
        if (!replicated(f[1])) p.defects.push_back("TYPE word not 4x replicated");
        if (f.size() < 8) {
            p.defects.push_back(strprintf("stream frame only %zu words", f.size()));
            out.push_back(std::move(p));
            continue;
        }
        for (size_t i = 2; i <= 5; ++i) {
            if (!replicated(f[i])) p.defects.push_back(strprintf("header word %zu 0x%08X not 4x replicated", i, f[i]));
        }
        p.stream_id = uint8_t(std::max(voted(f[2]), 0));
        p.tag = uint8_t(std::max(voted(f[3]), 0));
        p.dsize_p = uint32_t(std::max(voted(f[4]), 0)) << 8 | uint32_t(std::max(voted(f[5]), 0));
        p.payload.assign(f.begin() + 6, f.end() - 2);
        if (p.dsize_p != p.payload.size()) {
            p.defects.push_back(strprintf("DsizeP %u but %zu payload words", p.dsize_p, p.payload.size()));
        }
        const uint32_t crc = crc32Words(f.data() + 6, f.size() - 8);  // payload only (Table 19)
        p.crc_ok = wireToCrc(f[f.size() - 2]) == crc;
        if (!p.crc_ok) p.defects.push_back("stream CRC mismatch");
        out.push_back(std::move(p));
    }
    return out;
}

// Walk each stream's concatenated payload for §9.4 markers: a K28.3 word
// followed by 0x01 opens a 25-word rectangular header, 0x02 a 2-word line
// marker, 0x03 a 16-word arbitrary header and 0x04 an 11-word arbitrary
// line marker.  Line data runs to the next marker pair (the FIFO link
// carries no K flags, so a marker is recognised by its two-word pattern).
std::vector<ImageRec> walkImages(const std::vector<StreamPkt>& pkts) {
    struct Tagged { uint32_t w; double t; };
    std::map<uint8_t, std::vector<Tagged>> streams;
    for (const StreamPkt& p : pkts) {
        auto& v = streams[p.stream_id];
        for (uint32_t w : p.payload) v.push_back({w, p.t_ms});
    }
    std::vector<ImageRec> out;
    for (auto& [sid, words] : streams) {
        const size_t n = words.size();
        auto isMarker = [&](size_t i, uint8_t type) {
            return i + 1 < n && words[i].w == MARKER_WORD && words[i + 1].w == replicateByte(type);
        };
        auto anyMarker = [&](size_t j) {
            return isMarker(j, 0x01) || isMarker(j, 0x02) || isMarker(j, HDR_TYPE_ARB) || isMarker(j, LINE_TYPE_ARB);
        };
        ImageRec* cur = nullptr;
        size_t i = 0;
        while (i < n) {
            if (isMarker(i, HDR_TYPE_ARB)) {
                out.emplace_back();
                cur = &out.back();
                cur->arbitrary = true;
                cur->packet_stream_id = sid;
                cur->t_first_ms = words[i].t;
                if (i + ARB_HDR_WORDS > n) {
                    cur->header_complete = false;
                    break;
                }
                auto b = [&](int k) {
                    const uint32_t w = words[i + size_t(k)].w;
                    if (!replicated(w)) cur->replicas_ok = false;
                    return uint32_t(std::max(voted(w), 0));
                };
                cur->stream_id = b(2);
                cur->source_tag = b(3) << 8 | b(4);
                cur->ysize = b(5) << 16 | b(6) << 8 | b(7);
                cur->yoffs = b(8) << 16 | b(9) << 8 | b(10);
                cur->pixel_f = b(11) << 8 | b(12);
                cur->tap_g = b(13) << 8 | b(14);
                cur->flags = b(15);
                cur->t_last_ms = words[i + ARB_HDR_WORDS - 1].t;
                i += ARB_HDR_WORDS;
                continue;
            }
            if (isMarker(i, LINE_TYPE_ARB)) {
                if (i + ARB_LINE_WORDS > n) break;
                size_t j = i + ARB_LINE_WORDS;
                while (j < n && !anyMarker(j)) ++j;
                if (cur) {
                    auto b = [&](int k) {
                        const uint32_t w = words[i + size_t(k)].w;
                        if (!replicated(w)) cur->replicas_ok = false;
                        return uint32_t(std::max(voted(w), 0));
                    };
                    cur->line_xsize.push_back(b(2) << 16 | b(3) << 8 | b(4));
                    cur->line_xoffs.push_back(b(5) << 16 | b(6) << 8 | b(7));
                    cur->line_dsize_l.push_back(b(8) << 16 | b(9) << 8 | b(10));
                    Words line;
                    for (size_t k = i + ARB_LINE_WORDS; k < j; ++k) line.push_back(words[k].w);
                    cur->line_words.push_back(line.size());
                    cur->lines.push_back(std::move(line));
                    cur->t_last_ms = words[j - 1].t;
                }
                i = j;
                continue;
            }
            if (isMarker(i, 0x01)) {
                out.emplace_back();
                cur = &out.back();
                cur->packet_stream_id = sid;
                cur->t_first_ms = words[i].t;
                if (i + REC_HDR_WORDS > n) {
                    cur->header_complete = false;
                    break;
                }
                auto b = [&](int k) {
                    const uint32_t w = words[i + size_t(k)].w;
                    if (!replicated(w)) cur->replicas_ok = false;
                    return uint32_t(std::max(voted(w), 0));
                };
                cur->stream_id = b(2);
                cur->source_tag = b(3) << 8 | b(4);
                cur->xsize = b(5) << 16 | b(6) << 8 | b(7);
                cur->xoffs = b(8) << 16 | b(9) << 8 | b(10);
                cur->ysize = b(11) << 16 | b(12) << 8 | b(13);
                cur->yoffs = b(14) << 16 | b(15) << 8 | b(16);
                cur->dsize_l = b(17) << 16 | b(18) << 8 | b(19);
                cur->pixel_f = b(20) << 8 | b(21);
                cur->tap_g = b(22) << 8 | b(23);
                cur->flags = b(24);
                cur->t_last_ms = words[i + REC_HDR_WORDS - 1].t;
                i += REC_HDR_WORDS;
                continue;
            }
            if (isMarker(i, 0x02)) {
                size_t j = i + 2;
                while (j < n && !anyMarker(j)) ++j;
                if (cur) {
                    Words line;
                    for (size_t k = i + 2; k < j; ++k) line.push_back(words[k].w);
                    cur->line_words.push_back(line.size());
                    cur->lines.push_back(std::move(line));
                    cur->t_last_ms = words[j - 1].t;
                }
                i = j;
                continue;
            }
            ++i;
        }
    }
    return out;
}

// -- options -------------------------------------------------------------------------
#define CXP_OPT(field) \
    [](const Options& o) { return double(o.field); }, [](Options& o, double v) { o.field = decltype(o.field)(v); }

const std::vector<OptionSpec>& optionSpecs() {
    static const std::vector<OptionSpec> specs = {
        {"soak_seconds", "--soak", "Soak", "s", 5, 86400, 0, "duration of the PERF-003 streaming soak",
         "How long PERF-003 streams without a break while it keeps reading registers and counts lost "
         "images, CRC errors and ack latencies. Longer soaks catch rare drops and slow leaks; on the RTL "
         "simulation even a few seconds of device time take minutes of wall-clock time, so keep it short "
         "there.",
         CXP_OPT(soak_seconds)},
        {"perf_seconds", "--perf-seconds", "Perf window", "s", 1, 86400, 0,
         "measurement window of PERF-001 and PERF-002",
         "Wall-clock time PERF-001 (sustained stream) and PERF-002 (throughput) capture before they "
         "compute the rate. A longer window averages out start-up and host jitter; it is measured time, "
         "so the timeout scale does not stretch it.",
         CXP_OPT(perf_seconds)},
        {"host_spsm", "--host-spsm", "Host SPSM", "bytes", 36, 0xFFFFFFFC, 0,
         "StreamPacketSizeMax the host programs when it needs a stream (whole packet, a multiple of 4)",
         "The value a check writes to StreamPacketSizeMax before it streams: always in INIT-004, PERF-001 "
         "and some UVM mirrors, otherwise only when the device reads 0. BOOT-001 derives its probe value "
         "from it. It bounds each stream packet, header and CRC included: smaller values mean more "
         "packets per image and more header overhead. 36 is the smallest the host accepts.",
         CXP_OPT(host_spsm)},
        {"ack_timeout_ms", "--ack-timeout", "Ack wait", "ms", 1, 600000, 0,
         "wait for a raw command's acknowledgment (register calls through the XML wait the global --timeout)",
         "How long a check that sends hand-built command packets (malformed, corrupted CRC, wrong size, "
         "resets) waits for the device's acknowledgment before it counts \"no ack\". Feature reads and "
         "writes through the GenICam XML wait the global --timeout instead. Multiplied by the timeout "
         "scale.",
         CXP_OPT(ack_timeout_ms)},
        {"timeout_scale", "--timeout-scale", "Waits", "x", 0.1, 10000, 1,
         "multiply every host-side wait (slow RTL simulation); combines with the catalogue's per-case "
         "timeout_scale; spec limits a check measures never change",
         "Every host-side wait (acks, first image, quiet link, image capture deadlines) is multiplied by "
         "this, then by the case's own scale from the catalogue. Use 1 for a real or in-process camera "
         "and 20-100 for the RTL simulation. It never changes a limit a check judges the device against, "
         "such as the ack latency limit.",
         CXP_OPT(timeout_scale)},
        {"quiet_ms", "--quiet-ms", "Quiet link", "ms", 10, 60000, 0,
         "a link without stream packets this long counts as quiet (scaled)",
         "After AcquisitionStop a check waits until no stream packet has arrived for this long before it "
         "treats the stream as ended and starts the next step. Too short and a slow device's last packets "
         "leak into the next step; too long only costs time. Multiplied by the timeout scale.",
         CXP_OPT(quiet_ms)},
        {"quiet_timeout_ms", "--quiet-timeout", "Quiet wait", "ms", 100, 600000, 0,
         "longest wait for a quiet link after a stop (scaled)",
         "Upper bound on the wait for a quiet link after a stop. When it runs out the check goes on "
         "anyway: BOOT-007 and GEN-005, which judge the stop, fail it; the others may then see stray "
         "packets in their next step. Multiplied by the timeout scale.",
         CXP_OPT(quiet_timeout_ms)},
        {"first_image_timeout_ms", "--first-image-timeout", "First image wait", "ms", 100, 600000, 0,
         "wait for the first image header after AcquisitionStart (scaled)",
         "How long a check waits for the first image header after AcquisitionStart before it reports that "
         "streaming did not start. Used by the CT, CTRL, IMG, INIT, PERF and REC streaming checks. "
         "Multiplied by the timeout scale.",
         CXP_OPT(first_image_timeout_ms)},
        {"ack_latency_ms", "--ack-latency", "Ack latency limit", "ms", 1, 600000, 0,
         "the plan's command-to-acknowledgment limit the latency checks judge; a verdict limit, never "
         "scaled: relax it only on a slow simulator",
         "The largest command-to-acknowledgment time CT-003, CTRL-003, CTRL-010, INIT-004 and PERF-003 "
         "accept. This is a pass/fail limit, so the timeout scale does not touch it. The host measures "
         "wall-clock time, so a slow simulator can exceed it; raise it there and keep the plan's value on "
         "hardware.",
         CXP_OPT(ack_latency_ms)},
        {"seed", "--seed", "Seed", "", 0, 4294967295.0, 0, "random seed of every check; 0 = each check's own default",
         "Seeds the random stimulus (register values, trigger kinds, image sizes, command sequences). 0 "
         "lets each check use its own fixed default, so runs repeat exactly. Any other value replaces "
         "every check's seed; the seed in effect is written to each case log, so a failing run can be "
         "replayed.",
         CXP_OPT(seed)},
        {"max_reported", "--max-reported", "FAIL lines per item", "", 1, 100000, 0,
         "FAIL lines a check prints per kind of item before it only counts",
         "A check that finds many bad items of one kind (images, packets, registers, acks) prints a FAIL "
         "line for the first N, then only counts the rest in its summary. The verdict is the same either "
         "way; raise it when you need every failing item in the log.",
         CXP_OPT(max_reported)},
        {"keep_logs", "--keep-logs", "Runs kept", "", 1, 100000, 0,
         "timestamped run directories kept under the log folder",
         "Each run writes a timestamped directory with one log per case and results.json. When a new run "
         "starts, the oldest run directories beyond this count, the new one included, are deleted from "
         "the log folder; a directory named with --log-dir is never pruned.",
         CXP_OPT(keep_logs)},
    };
    return specs;
}

#undef CXP_OPT

const OptionSpec* findOption(const std::string& key) {
    for (const OptionSpec& s : optionSpecs()) {
        if (key == s.key) return &s;
    }
    return nullptr;
}

void setOption(Options& o, const OptionSpec& s, double v) {
    if (!(v >= s.min && v <= s.max)) {
        throw std::invalid_argument(strprintf("%s must be in %g..%g", s.flag, s.min, s.max));
    }
    if (s.decimals == 0 && v != std::floor(v)) throw std::invalid_argument(std::string(s.flag) + " must be a whole number");
    if (std::string(s.key) == "host_spsm" && uint64_t(v) % 4) {
        throw std::invalid_argument(std::string(s.flag) + " must be a multiple of 4 (bytes of the whole packet)");
    }
    s.set(o, v);
}

std::string describeOptions(const Options& o) {
    std::string out;
    for (const OptionSpec& s : optionSpecs()) {
        out += strprintf("%s%s=%.*f", out.empty() ? "" : " ", s.key, s.decimals, s.get(o));
    }
    return out;
}

// -- context -------------------------------------------------------------------------
Context::Context(std::shared_ptr<CameraControl> cam, const std::atomic<bool>& cancel, Sink sink,
                 Options opt)
    : cam_(std::move(cam)),
      tree_(cam_->xmlTree()),
      cancel_(cancel),
      sink_(std::move(sink)),
      opt_(opt),
      t0_(std::chrono::steady_clock::now()) {
    if (tree_ && !tree_->accessor()) tree_->setAccessor(std::make_shared<CameraRegisterAccessor>(cam_));
    // Register calls through the camera (GenICam features included) wait
    // as long as the case's other waits: a slow device (an RTL sim) answers
    // late while the stream fills the link.
    base_ack_ms_ = cam_->ackTimeoutMs();
    cam_->setAckTimeoutMs(wait(base_ack_ms_));
    tap_ = cam_->addRxTap([this](const Words& f) { onFrame(f); });
    side_tap_ = cam_->addSideTap([this](uint32_t magic, const Words& b) { onSide(magic, b); });
}

Context::~Context() {
    runCleanup();
    cam_->setAckTimeoutMs(base_ack_ms_);
    cam_->removeSideTap(side_tap_);
    cam_->removeRxTap(tap_);
}

void Context::onSide(uint32_t magic, const Words& b) {
    const double t = nowMs();
    if (magic == CHARS_MAGIC) {
        std::lock_guard<std::mutex> lk(side_mu_);
        shorts_.push_back({t, decodeShortPacket(wordsToChars(b))});
    } else if (magic == bench::MAGIC && !b.empty()) {
        if (b[0] == bench::PIN_EDGE && b.size() >= 5) {
            std::lock_guard<std::mutex> lk(side_mu_);
            edges_.push_back({t, b[1], b[2], uint64_t(b[4]) << 32 | b[3]});
        } else if (b[0] == bench::SHORT_DL && b.size() >= 7) {
            // The CXC1 frame of this packet came just before: the latest short
            // packet without a time whose characters are these two words.
            std::lock_guard<std::mutex> lk(side_mu_);
            const Chars want = [&] {
                Chars c;
                for (int w = 1; w <= 2; ++w) {
                    for (int l = 0; l < 4; ++l) c.push_back({uint8_t(b[size_t(w)] >> (8 * l)), false});
                }
                return c;
            }();
            for (auto it = shorts_.rbegin(); it != shorts_.rend(); ++it) {
                if (it->inside >= 0 || it->pkt.chars.size() != 8) continue;
                bool same = true;
                for (size_t i = 0; i < 8; ++i) same &= it->pkt.chars[i].v == want[i].v;
                if (!same) continue;
                it->device_ps = uint64_t(b[4]) << 32 | b[3];
                it->inside = int(b[5]);
                it->word_index = int(b[6]);
                break;
            }
        } else if (b[0] == bench::FRAME_DL && b.size() >= 7) {
            std::lock_guard<std::mutex> lk(side_mu_);
            frame_times_.push_back({t, b[1], uint64_t(b[3]) << 32 | b[2], uint64_t(b[5]) << 32 | b[4], b[6]});
        } else if (b[0] == bench::UPLINK_MARK && b.size() >= 5) {
            std::lock_guard<std::mutex> lk(side_mu_);
            marks_.push_back({t, b[1], uint64_t(b[3]) << 32 | b[2], b[4]});
        } else if (b[0] & bench::REPLY) {
            std::lock_guard<std::mutex> lk(side_mu_);
            bench_replies_.push_back(b);
        }
    } else {
        return;
    }
    side_cv_.notify_all();
}

// -- bench ---------------------------------------------------------------------------
uint32_t Context::benchCaps() { return cam_->benchCaps(wait(500)); }

void Context::needBench(uint32_t caps, const std::string& what) {
    const uint32_t have = benchCaps();
    if ((have & caps) == caps) return;
    throw Skip(have ? strprintf("the device's bench cannot %s (capabilities 0x%04X, needs 0x%04X)", what.c_str(),
                                have, caps)
                    : "the device has no test bench (no answer to the bench HELLO); needed to " + what);
}

std::optional<Words> Context::waitBenchReply(uint32_t op, int timeout_ms,
                                             const std::function<bool(const Words&)>& match) {
    std::unique_lock<std::mutex> lk(side_mu_);
    std::optional<Words> got;
    side_cv_.wait_for(lk, std::chrono::milliseconds(wait(timeout_ms)), [&] {
        for (auto it = bench_replies_.begin(); it != bench_replies_.end(); ++it) {
            if (!it->empty() && (*it)[0] == (op | bench::REPLY) && (!match || match(*it))) {
                got = std::move(*it);
                bench_replies_.erase(it);
                return true;
            }
        }
        return bool(cancel_);
    });
    return got;
}

std::optional<uint32_t> Context::benchPins(int timeout_ms) {
    if (!(benchCaps() & bench::CAP_SYNC)) return std::nullopt;
    {
        // A reply that came after its query timed out answers nothing now.
        std::lock_guard<std::mutex> lk(side_mu_);
        const auto late = [](const Words& r) { return !r.empty() && r[0] == (bench::GET_PINS | bench::REPLY); };
        bench_replies_.erase(std::remove_if(bench_replies_.begin(), bench_replies_.end(), late), bench_replies_.end());
    }
    benchSend({bench::GET_PINS});
    const auto r = waitBenchReply(bench::GET_PINS, timeout_ms);
    if (!r || r->size() < 2) return std::nullopt;
    return (*r)[1];
}

void Context::benchPin(uint32_t pin, uint32_t value) {
    const auto pins = benchPins();
    // Without GET_PINS the input is taken to have been at the other level.
    const uint32_t restore = pins ? (*pins >> (pin - 1)) & 1u : (value ? 0u : 1u);
    benchSend({bench::PIN, pin, value});
    onExit([this, pin, restore] {
        benchSend({bench::PIN, pin, restore});
        benchSync();
    });
}

bool Context::benchReset(uint32_t inputs, int timeout_ms, uint32_t domains) {
    if (!(benchCaps() & bench::CAP_RESET)) return false;
    if (domains && !(benchCaps() & bench::CAP_RESET_DOMAINS)) return false;
    const auto before = benchPins();
    benchSend(domains ? Words{bench::RESET, inputs, domains} : Words{bench::RESET, inputs});
    if (!waitBenchReply(bench::RESET, timeout_ms)) return false;
    if (tree_) tree_->invalidate();
    if (before) {
        onExit([this, was = *before] {
            for (uint32_t p = bench::TRIG_IN; p <= bench::ARBITRARY; ++p) benchSend({bench::PIN, p, was >> (p - 1) & 1u});
            benchSync();
        });
    }
    // The receiver locks again on the host's IDLE; a command sent before
    // that is lost, so ask until one is answered.
    const double end = nowMs() + wait(timeout_ms);
    while (nowMs() < end) {
        if (exchange(readCmd(Reg::STANDARD, 4), 300)) return true;
    }
    return false;
}

void Context::benchSend(const Words& body) {
    checkpoint();
    cam_->sendBench(body);
}

bool Context::benchSync(int timeout_ms) {
    if (!(benchCaps() & bench::CAP_SYNC)) return false;
    static std::atomic<uint32_t> token{1};
    const uint32_t tok = token++;
    cam_->sendBench({bench::SYNC, tok});
    return waitBenchReply(bench::SYNC, timeout_ms, [tok](const Words& r) { return r.size() >= 2 && r[1] == tok; })
        .has_value();
}

std::optional<Context::BenchTime> Context::benchTime(int timeout_ms) {
    if (!(benchCaps() & bench::CAP_TIMES)) return std::nullopt;
    static std::atomic<uint32_t> token{0x40000000};
    const uint32_t tok = token++;
    cam_->sendBench({bench::SYNC, tok});
    const auto r = waitBenchReply(bench::SYNC, timeout_ms, [tok](const Words& w) { return w.size() >= 2 && w[1] == tok; });
    if (!r || r->size() < 4) return std::nullopt;
    BenchTime t;
    t.now_ps = uint64_t((*r)[3]) << 32 | (*r)[2];
    if (r->size() >= 6 && ((*r)[4] || (*r)[5])) t.ms_ps = uint64_t((*r)[5]) << 32 | (*r)[4];
    return t;
}

std::optional<Words> Context::benchRequest(const Words& op, int timeout_ms) {
    if (op.empty()) return std::nullopt;
    {
        // A reply that came after its request timed out answers nothing now.
        std::lock_guard<std::mutex> lk(side_mu_);
        const uint32_t want = op[0] | bench::REPLY;
        const auto late = [want](const Words& r) { return !r.empty() && r[0] == want; };
        bench_replies_.erase(std::remove_if(bench_replies_.begin(), bench_replies_.end(), late), bench_replies_.end());
    }
    benchSend(op);
    auto r = waitBenchReply(op[0], timeout_ms);
    if (!r) return std::nullopt;
    return Words(r->begin() + 1, r->end());
}

int64_t Context::pixelFrame(const PixelImage& img, int timeout_ms) {
    sendPixelFrame(img);
    return waitPixelFrame(timeout_ms);
}

void Context::sendPixelFrame(const PixelImage& img) {
    Words b = {bench::PIXEL_FRAME, img.xsize, img.ysize, img.xoffs, img.yoffs, img.pixfmt, img.tapg,
               img.streamid, img.sourcetag, img.flags, img.valid_permille, uint32_t(img.pixels.size())};
    for (size_t i = 0; i < img.pixels.size(); i += 2) {
        b.push_back(uint32_t(img.pixels[i]) | (i + 1 < img.pixels.size() ? uint32_t(img.pixels[i + 1]) << 16 : 0u));
    }
    benchSend(b);
}

void Context::sendPixelBeats(const PixelImage& img, const std::vector<uint32_t>& beats) {
    Words b = {bench::PIXEL_BEATS, img.xsize, img.ysize, img.xoffs, img.yoffs, img.pixfmt, img.tapg,
               img.streamid, img.sourcetag, img.flags, img.valid_permille, uint32_t(beats.size())};
    b.insert(b.end(), beats.begin(), beats.end());
    benchSend(b);
}

int64_t Context::waitPixelFrame(int timeout_ms) {
    checkpoint();
    const auto r = waitBenchReply(bench::PIXEL_FRAME, timeout_ms);
    checkpoint();
    return r && r->size() >= 2 ? int64_t((*r)[1]) : -1;
}

std::vector<RawAck> Context::exchangeChars(const Chars& chars, size_t max_acks, int timeout_ms) {
    checkpoint();
    timeout_ms = wait(timeout_ms < 0 ? opt_.ack_timeout_ms : timeout_ms);
    std::vector<RawAck> out;
    cam_->rawSession([&](const CameraControl::RawSender&) {
        dropLateAcks();
        const double t_send = nowMs();
        cam_->sendChars(chars);
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (out.size() < max_acks) {
            const auto left =
                std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now());
            Captured c;
            if (left.count() <= 0 || !acks_.pop(c, int(left.count()))) break;
            RawAck a = decodeAck(c.frame);
            a.latency_ms = c.t_ms - t_send;
            out.push_back(std::move(a));
        }
    });
    return out;
}

void Context::sendChars(const Chars& chars) {
    checkpoint();
    cam_->sendChars(chars);
}

std::vector<TimedShort> Context::shortPackets(double since_ms) const {
    std::lock_guard<std::mutex> lk(side_mu_);
    std::vector<TimedShort> out;
    for (const auto& s : shorts_) {
        if (s.t_ms >= since_ms) out.push_back(s);
    }
    return out;
}

std::vector<PinEdge> Context::pinEdges(uint32_t pin, double since_ms) const {
    std::lock_guard<std::mutex> lk(side_mu_);
    std::vector<PinEdge> out;
    for (const auto& e : edges_) {
        if (e.pin == pin && e.t_ms >= since_ms) out.push_back(e);
    }
    return out;
}

std::vector<UplinkMark> Context::uplinkMarks(double since_ms) const {
    std::lock_guard<std::mutex> lk(side_mu_);
    std::vector<UplinkMark> out;
    for (const auto& m : marks_) {
        if (m.t_ms >= since_ms) out.push_back(m);
    }
    return out;
}

std::vector<FrameTime> Context::frameTimes(double since_ms) const {
    std::lock_guard<std::mutex> lk(side_mu_);
    std::vector<FrameTime> out;
    for (const auto& f : frame_times_) {
        if (f.t_ms >= since_ms) out.push_back(f);
    }
    return out;
}

bool Context::waitShort(ShortPacket::Kind kind, size_t n, double since_ms, int timeout_ms) {
    std::unique_lock<std::mutex> lk(side_mu_);
    return side_cv_.wait_for(lk, std::chrono::milliseconds(wait(timeout_ms)), [&] {
        size_t k = 0;
        for (const auto& s : shorts_) k += s.t_ms >= since_ms && s.pkt.kind == kind;
        return k >= n || cancel_;
    });
}

bool Context::waitEdges(uint32_t pin, size_t n, double since_ms, int timeout_ms) {
    std::unique_lock<std::mutex> lk(side_mu_);
    return side_cv_.wait_for(lk, std::chrono::milliseconds(wait(timeout_ms)), [&] {
        size_t k = 0;
        for (const auto& e : edges_) k += e.pin == pin && e.t_ms >= since_ms;
        return k >= n || cancel_;
    });
}

// -- parameters ------------------------------------------------------------------------
const ParamValue& Context::param(const std::string& key) const {
    const size_t dot = key.find('.');
    const auto it = params_.find(key.substr(0, dot));
    const ParamValue* v = it == params_.end() ? nullptr : &it->second;
    if (v && dot != std::string::npos) {
        const ParamValue* in = nullptr;
        for (const auto& [k, x] : v->group) {
            if (k == key.substr(dot + 1)) in = &x;
        }
        v = in;
    }
    if (!v) throw std::logic_error("the check reads parameter '" + key + "', which the catalogue does not declare");
    return *v;
}

namespace {

// A number, or a "0x..." string (the catalogue writes addresses and register
// patterns in hex).
int64_t whole(const ParamValue& v, const std::string& key) {
    if (v.kind == ParamValue::Kind::String && v.str.rfind("0x", 0) == 0) {
        size_t pos = 0;
        const int64_t x = int64_t(std::stoull(v.str, &pos, 16));
        if (pos == v.str.size()) return x;
    }
    if (v.kind != ParamValue::Kind::Number || v.num != std::floor(v.num)) {
        throw std::logic_error("parameter '" + key + "' is not a whole number");
    }
    return int64_t(v.num);
}

const std::vector<ParamValue>& listOf(const ParamValue& v, const std::string& key) {
    if (v.kind != ParamValue::Kind::List) throw std::logic_error("parameter '" + key + "' is not a list");
    return v.list;
}

}  // namespace

int Context::iparam(const std::string& key) const { return int(whole(param(key), key)); }

std::string Context::sparam(const std::string& key) const {
    const ParamValue& v = param(key);
    if (v.kind != ParamValue::Kind::String) throw std::logic_error("parameter '" + key + "' is not a string");
    return v.str;
}

std::vector<int64_t> Context::ilist(const std::string& key) const {
    std::vector<int64_t> out;
    for (const auto& x : listOf(param(key), key)) out.push_back(whole(x, key));
    return out;
}

std::vector<std::string> Context::slist(const std::string& key) const {
    std::vector<std::string> out;
    for (const auto& x : listOf(param(key), key)) {
        if (x.kind != ParamValue::Kind::String) throw std::logic_error("parameter '" + key + "' is not a list of strings");
        out.push_back(x.str);
    }
    return out;
}

std::vector<uint32_t> Context::spsmList(const std::string& key) const {
    std::vector<uint32_t> out;
    for (const auto& x : listOf(param(key), key)) {
        if (x.kind == ParamValue::Kind::String && x.str == "host_spsm") {
            out.push_back(opt_.host_spsm);
        } else {
            out.push_back(uint32_t(whole(x, key)));
        }
    }
    return out;
}

std::vector<std::vector<int64_t>> Context::rows(const std::string& key) const {
    std::vector<std::vector<int64_t>> out;
    for (const auto& r : listOf(param(key), key)) {
        std::vector<int64_t> row;
        for (const auto& x : listOf(r, key)) row.push_back(whole(x, key));
        out.push_back(std::move(row));
    }
    return out;
}

uint32_t Context::seed(uint32_t dflt) {
    const uint32_t s = opt_.seed ? opt_.seed : dflt;
    info("random seed %u (%s)", s, opt_.seed ? "--seed" : "the check's default");
    return s;
}

double Context::nowMs() const {
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0_).count();
}

void Context::onFrame(const Words& f) {
    Captured c{nowMs(), f};
    const int type = f.size() >= 3 ? voted(f[1]) : -1;
    if (type == 0x03) acks_.push(c);
    if (type == 0x01 && f.size() >= 8) {
        ++stream_seen_;
        last_stream_ms_ = c.t_ms;
        uint32_t prev = prev_tail_;
        size_t hdrs = 0;
        for (size_t i = 6; i < f.size() - 2; ++i) {  // payload: before CRC and EOP
            if (prev == MARKER_WORD && (f[i] == replicateByte(HDR_TYPE_REC) || f[i] == replicateByte(HDR_TYPE_ARB))) ++hdrs;
            prev = f[i];
        }
        prev_tail_ = prev;
        headers_seen_ += hdrs;
    }
    std::lock_guard<std::mutex> lk(rec_mu_);
    if (recording_) rec_.push_back(std::move(c));
}

Feature* Context::feature(const std::string& name) const {
    return tree_ ? tree_->find(name) : nullptr;
}

Feature& Context::need(const std::string& name) const {
    Feature* f = feature(name);
    if (!f) throw Skip("the device XML has no '" + name + "' feature");
    return *f;
}

void Context::vlog(LineKind k, const char* fmt, va_list ap) {
    if (sink_) sink_({k, vformat(fmt, ap)});
}

void Context::info(const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vlog(LineKind::Info, fmt, ap);
    va_end(ap);
}

void Context::note(const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vlog(LineKind::Note, fmt, ap);
    va_end(ap);
}

void Context::warn(const char* fmt, ...) {
    ++warnings_;
    va_list ap;
    va_start(ap, fmt);
    vlog(LineKind::Warn, fmt, ap);
    va_end(ap);
}

bool Context::expect(bool ok, const char* fmt, ...) {
    ok ? ++passes_ : ++failures_;
    va_list ap;
    va_start(ap, fmt);
    vlog(ok ? LineKind::Pass : LineKind::Fail, fmt, ap);
    va_end(ap);
    return ok;
}

void Context::skip(const std::string& why) { throw Skip(why); }
void Context::abort(const std::string& why) { throw Abort(why); }

void Context::checkpoint() const {
    if (cancel_ && !in_cleanup_) throw Cancelled();
}

void Context::sleepMs(int ms) const { sleepRawMs(wait(ms)); }

void Context::sleepRawMs(int ms) const {
    const auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(ms);
    while (std::chrono::steady_clock::now() < end) {
        checkpoint();
        std::this_thread::sleep_for(std::chrono::milliseconds(std::min(ms, 10)));
    }
}

void Context::runCleanup() {
    in_cleanup_ = true;  // restores must run even after a cancel
    while (!cleanup_.empty()) {
        auto fn = std::move(cleanup_.back());
        cleanup_.pop_back();
        try {
            fn();
        } catch (const std::exception& e) {
            if (sink_) sink_({LineKind::Warn, std::string("cleanup: ") + e.what()});
        }
    }
    in_cleanup_ = false;
}

// -- control -----------------------------------------------------------------------------
std::optional<RawAck> Context::exchange(const Words& cmd, int timeout_ms) {
    auto acks = exchangeMany({cmd}, 1, timeout_ms);
    if (acks.empty()) return std::nullopt;
    return acks.front();
}

// Every acknowledgment reaches both the camera session and this queue.
// One for a command the session sent (a feature access) can be queued a
// moment after that command returned, i.e. after the next raw exchange
// cleared the queue, and would then be taken for the raw command's answer
// — every later answer one command late.  Give it time to arrive and drop
// it before sending.
void Context::dropLateAcks() {
    acks_.clear();
    std::this_thread::sleep_for(std::chrono::milliseconds(kLateAckMs));
    acks_.clear();
}

std::vector<RawAck> Context::exchangeMany(const std::vector<Words>& frames, size_t max_acks,
                                          int timeout_ms) {
    checkpoint();
    timeout_ms = wait(timeout_ms < 0 ? opt_.ack_timeout_ms : timeout_ms);
    std::vector<RawAck> out;
    cam_->rawSession([&](const CameraControl::RawSender& send) {
        dropLateAcks();
        const double t_send = nowMs();
        for (const Words& f : frames) send(f);
        const auto deadline =
            std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (out.size() < max_acks) {
            const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
                deadline - std::chrono::steady_clock::now());
            Captured c;
            if (left.count() <= 0 || !acks_.pop(c, int(left.count()))) break;
            RawAck a = decodeAck(c.frame);
            a.latency_ms = c.t_ms - t_send;
            out.push_back(std::move(a));
        }
    });
    return out;
}

std::vector<RawAck> Context::exchangeFinal(const Words& cmd, int timeout_ms, int extra_ms) {
    checkpoint();
    std::vector<RawAck> out;
    cam_->rawSession([&](const CameraControl::RawSender& send) {
        dropLateAcks();
        const double t_send = nowMs();
        send(cmd);
        auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(wait(timeout_ms));
        bool final_seen = false;
        for (;;) {
            const auto left =
                std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now());
            Captured c;
            if (left.count() <= 0 || !acks_.pop(c, int(left.count()))) break;
            RawAck a = decodeAck(c.frame);
            a.latency_ms = c.t_ms - t_send;
            const bool fin = a.code != Ack::WAIT;
            out.push_back(std::move(a));
            if (fin && !final_seen) {
                final_seen = true;
                deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(wait(extra_ms));
            }
        }
    });
    return out;
}

void Context::sendOnly(const std::vector<Words>& frames, int settle_ms) {
    checkpoint();
    // §8.7: at least one IDLE word between packets (and §8.2.5.1: one every
    // 10 000 words on the low-speed link).  A link that takes characters
    // gets it after every frame; otherwise the PHY idles only when the
    // queue runs dry, and a burst of test packets would reach the device
    // as one run of words it may take for a lost link.
    static const Chars kIdle = {{0xBC, true}, {0x3C, true}, {0x3C, true}, {0xB5, false}};
    const bool idle = (benchCaps() & bench::CAP_CHARS) != 0;
    cam_->rawSession([&](const CameraControl::RawSender& send) {
        for (const Words& f : frames) {
            send(f);
            if (idle) cam_->sendChars(kIdle);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(wait(settle_ms)));
        acks_.clear();
    });
}

std::optional<RawAck> Context::readRaw(uint32_t addr, uint32_t bytes) {
    return exchange(readCmd(addr, bytes));
}

std::optional<RawAck> Context::writeRaw(uint32_t addr, const std::vector<uint32_t>& values) {
    return exchange(writeCmd(addr, values));
}

std::optional<uint32_t> Context::tryRd32(uint32_t addr, int* code) {
    auto a = readRaw(addr, 4);
    if (code) *code = a ? a->code : -1;
    if (!a || a->code != Ack::READ_OK || a->data.empty() || !a->crc_ok) return std::nullopt;
    return a->values()[0];
}

uint32_t Context::rd32(uint32_t addr) {
    int code = -1;
    auto v = tryRd32(addr, &code);
    if (!v) {
        abort(code < 0 ? strprintf("no acknowledgment to a read of 0x%04X", addr)
                       : strprintf("read of 0x%04X answered %s", addr, ackName(code).c_str()));
    }
    return *v;
}

uint64_t Context::rd64(uint32_t addr) {
    auto a = readRaw(addr, 8);
    if (!a || a->code != Ack::READ_OK || a->data.size() < 2) {
        abort(strprintf("8-byte read of 0x%04X answered %s", addr,
                        a ? ackName(a->code).c_str() : "nothing"));
    }
    auto v = a->values();
    return uint64_t(v[0]) << 32 | v[1];
}

void Context::wr32(uint32_t addr, uint32_t value) {
    auto a = writeRaw(addr, {value});
    if (!a || (a->code != Ack::WRITE_OK && a->code != Ack::READ_OK)) {
        abort(strprintf("write 0x%08X to 0x%04X answered %s", value, addr,
                        a ? ackName(a->code).c_str() : "nothing"));
    }
}

std::vector<uint8_t> Context::readBlock(uint32_t addr, size_t bytes, size_t chunk) {
    std::vector<uint8_t> out;
    while (out.size() < bytes) {
        const uint32_t n = uint32_t(std::min(chunk, bytes - out.size()));
        auto a = readRaw(uint32_t(addr + out.size()), n);
        if (!a || a->code != Ack::READ_OK) {
            abort(strprintf("read of %u bytes at 0x%08zX answered %s", n, addr + out.size(),
                            a ? ackName(a->code).c_str() : "nothing"));
        }
        auto b = a->bytes();
        if (b.size() < n) abort(strprintf("read of %u bytes returned %zu", n, b.size()));
        out.insert(out.end(), b.begin(), b.begin() + n);
    }
    return out;
}

std::string Context::readString(uint32_t addr, size_t bytes) {
    std::string s;
    for (uint8_t c : readBlock(addr, bytes)) {
        if (c == 0) break;
        s += char(c);
    }
    return s;
}

void Context::preserve(uint32_t addr) {
    if (auto v = tryRd32(addr)) {
        const uint32_t old = *v;
        onExit([this, addr, old] { writeRaw(addr, {old}); });
    }
}

// -- recording ------------------------------------------------------------------------------
void Context::startRecording() {
    std::lock_guard<std::mutex> lk(rec_mu_);
    rec_.clear();
    recording_ = true;
}

std::vector<Captured> Context::stopRecording() {
    std::lock_guard<std::mutex> lk(rec_mu_);
    recording_ = false;
    return std::move(rec_);
}

std::vector<Captured> Context::takeRecording() {
    std::lock_guard<std::mutex> lk(rec_mu_);
    std::vector<Captured> out;
    out.swap(rec_);
    return out;
}

double Context::lastStreamMs() const { return last_stream_ms_; }

std::vector<Captured> Context::record(int ms) {
    startRecording();
    try {
        sleepMs(ms);
    } catch (...) {
        stopRecording();
        throw;
    }
    return stopRecording();
}

// -- acquisition ------------------------------------------------------------------------------
void Context::setFeature(const std::string& name, const Value& v) {
    Feature& f = need(name);
    f.setValue(v);
    if (tree_) tree_->invalidate();
}

Value Context::getFeature(const std::string& name) {
    Feature& f = need(name);
    if (tree_) tree_->invalidate();
    return f.getValue();
}

void Context::prepareStreaming() {
    if (prepared_) return;
    prepared_ = true;
    need("AcquisitionStart");
    need("AcquisitionStop");
    auto spsm = tryRd32(Reg::STREAM_PACKET_SIZE_MAX);
    if (spsm && *spsm == 0) {
        info("StreamPacketSizeMax is 0; host programs %u bytes", opt_.host_spsm);
        preserve(Reg::STREAM_PACKET_SIZE_MAX);
        wr32(Reg::STREAM_PACKET_SIZE_MAX, opt_.host_spsm);
    }
    if (Feature* m = feature("AcquisitionMode"); m && m->isWritable()) {
        try {
            m->setValue(Value::ofString("Continuous"));
        } catch (const std::exception& e) {
            note("AcquisitionMode=Continuous not accepted: %s", e.what());
        }
    }
    // Vendor free-run switch of the reference camera (RTL cfg_run).
    if (Feature* run = feature("TpgRun"); run && run->isWritable()) {
        if (tree_) tree_->invalidate();
        const Value old = run->getValue();
        run->setValue(Value::ofInt(1));
        onExit([run, old] { run->setValue(old); });
    }
    onExit([this] {
        try {
            acqStop();
            waitQuiet();
        } catch (...) {
        }
    });
}

void Context::acqStart() {
    checkpoint();
    need("AcquisitionStart").execute();
    if (tree_) tree_->invalidate();
}

void Context::acqStop() {
    need("AcquisitionStop").execute();
    if (tree_) tree_->invalidate();
}

bool Context::waitHeaders(size_t n, int timeout_ms) {
    const double end = nowMs() + wait(timeout_ms);
    while (headers_seen_ < n) {
        if (nowMs() > end) return false;
        sleepRawMs(5);
    }
    return true;
}

bool Context::waitQuiet(int quiet_ms, int timeout_ms) {
    quiet_ms = wait(quiet_ms < 0 ? opt_.quiet_ms : quiet_ms);
    const double end = nowMs() + wait(timeout_ms < 0 ? opt_.quiet_timeout_ms : timeout_ms);
    while (nowMs() < end) {
        const double last = last_stream_ms_;
        if (last < 0 || nowMs() - last >= quiet_ms) return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    return false;
}

void Context::waitTail(size_t n_headers) {
    const double end = nowMs() + wait(2000);
    while (headers_seen_ < n_headers + 1 && nowMs() < end) {
        if (nowMs() - last_stream_ms_ >= wait(opt_.quiet_ms)) break;
        sleepRawMs(5);
    }
}

std::vector<Captured> Context::acquire(size_t n_images, int timeout_ms) {
    prepareStreaming();
    const size_t base = headers_seen_;
    startRecording();
    try {
        acqStart();
        // n headers and then either the next header or a quiet link, so the
        // n-th image's tail is in the recording.
        const bool got = waitHeaders(base + n_images, timeout_ms);
        if (got) waitTail(base + n_images);
        acqStop();
        waitQuiet();
        auto cap = stopRecording();
        if (!got) {
            info("only %zu of %zu image headers within %d ms", headers_seen_ - base, n_images,
                 wait(timeout_ms));
        }
        return cap;
    } catch (...) {
        stopRecording();
        throw;
    }
}

std::vector<Captured> Context::acquirePackets(size_t n_packets, int timeout_ms) {
    prepareStreaming();
    const size_t base = stream_seen_;
    startRecording();
    try {
        acqStart();
        const double end = nowMs() + wait(timeout_ms);
        while (stream_seen_ < base + n_packets && nowMs() < end) sleepRawMs(5);
        acqStop();
        waitQuiet();
        auto cap = stopRecording();
        if (stream_seen_ < base + n_packets) {
            info("only %zu of %zu stream packets within %d ms", stream_seen_ - base, n_packets,
                 wait(timeout_ms));
        }
        return cap;
    } catch (...) {
        stopRecording();
        throw;
    }
}

}  // namespace cxp::validation
