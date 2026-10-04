// What a validation check sees: the device session, raw control access,
// a downlink recorder, and wire decoders independent of the host codecs.
//
// The decoders here deliberately do not reuse CtrlAckPacket/StreamPacket
// ::fromBody: those collapse unknown ack codes and throw on the first
// defect, while a conformance check needs the raw code byte, every
// field and a list of everything that is wrong.  Wire *framing* follows
// the host stack (Tables 19-23 as cxp_protocol encodes them, CRC per
// crc.h), because that is what this FIFO link carries.
#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdarg>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "cxp/camera/client.h"
#include "cxp/genicam/sfnc.h"
#include "cxp/protocol/bench.h"
#include "cxp/protocol/chars.h"
#include "cxp/protocol/constants.h"
#include "cxp/transport/blocking_queue.h"
#include "cxp/validation/cases.h"

namespace cxp::validation {

// -- CXP 1.1.1 Table 22 acknowledgment codes ---------------------------------
namespace Ack {
inline constexpr uint8_t READ_OK = 0x00;
inline constexpr uint8_t WRITE_OK = 0x01;
inline constexpr uint8_t RESET_OK = 0x03;
inline constexpr uint8_t WAIT = 0x04;
inline constexpr uint8_t BAD_ADDRESS = 0x40;
inline constexpr uint8_t BAD_DATA = 0x41;
inline constexpr uint8_t BAD_OPCODE = 0x42;
inline constexpr uint8_t RO_WRITE = 0x43;
inline constexpr uint8_t WO_READ = 0x44;
inline constexpr uint8_t SIZE_TOO_LARGE = 0x45;
inline constexpr uint8_t SIZE_MISMATCH = 0x46;
inline constexpr uint8_t MALFORMED = 0x47;
inline constexpr uint8_t CRC_ERROR = 0x80;
}  // namespace Ack

// "0x43 (write to read-only)"
std::string ackName(int code);

// -- CXP 1.1.1 Table 45 bootstrap registers ----------------------------------
// The device's addresses are the generated map (cxp::reg, from
// src/regmap/cxp_regmap.yaml); Reg adds the slot names the checks use and
// the spec's manufacturer-space boundary.
namespace Reg {
using namespace cxp::reg;
inline constexpr uint32_t WIDTH_ADDRESS = WIDTH_SLOT;
inline constexpr uint32_t HEIGHT_ADDRESS = HEIGHT_SLOT;
inline constexpr uint32_t ACQ_MODE_ADDRESS = ACQUISITION_MODE_SLOT;
inline constexpr uint32_t ACQ_START_ADDRESS = ACQUISITION_START_SLOT;
inline constexpr uint32_t ACQ_STOP_ADDRESS = ACQUISITION_STOP_SLOT;
inline constexpr uint32_t PIXEL_FORMAT_ADDRESS = PIXEL_FORMAT_SLOT;
inline constexpr uint32_t TAP_GEOMETRY_ADDRESS = TAP_GEOMETRY_SLOT;
inline constexpr uint32_t IMAGE1_STREAM_ID_ADDRESS = IMAGE1_STREAM_ID_SLOT;
// Image<n>StreamIDAddress for n = 1..16 (§10.3.27).
constexpr uint32_t imageStreamIdAddress(int n) { return IMAGE1_STREAM_ID_SLOT + 4 * uint32_t(n - 1); }
// Table 45: the manufacturer-specific register space starts here.
inline constexpr uint32_t MANUFACTURER_SPACE = 0x6000;
}  // namespace Reg

enum class Access { RO, RW, WO };

struct BootReg {
    const char* name;
    uint32_t addr;
    uint32_t bytes;
    Access access;
    bool is_string;
};

const std::vector<BootReg>& bootstrapTable();

// CXP 1.1.1 Table 46 ConnectionConfig speed codes (bits 15:0).
bool isValidSpeedCode(uint32_t code);
// Table 25 PixelF codes (monochrome / Bayer / RGB subset the checks name).
std::optional<uint16_t> pfncTable25(const std::string& pfnc_name);
// GenICam PFNC value of a monochrome format name (Mono8 .. Mono16).
std::optional<uint32_t> pfncValue(const std::string& pfnc_name);

// -- raw control commands ------------------------------------------------------
struct CmdSpec {
    uint8_t opcode = 0x00;
    uint32_t size_bytes = 4;  // the Size field, as sent
    uint32_t address = 0;
    Words data;               // write data, already on-wire (bswap'd) words
    uint8_t type = 0x02;
    bool corrupt_crc = false;
    int crc_flip_bit = -1;    // >= 0: flip this CRC bit instead of inverting
    int data_flip_bit = -1;   // >= 0: flip this bit of data word 0 after the CRC
    int header_lane_hit = -1; // >= 0: corrupt one byte lane of header word (see .cpp)
    int header_lane_word = 0;
};

Words buildCmd(const CmdSpec& spec);
Words readCmd(uint32_t address, uint32_t size_bytes);
Words writeCmd(uint32_t address, const std::vector<uint32_t>& values);  // logical values
Words resetCmd();

// -- decoded downlink --------------------------------------------------------------
struct RawAck {
    int code = -1;           // raw code byte, -1 when the replicas disagree
    bool long_form = false;  // carries Size / Data / CRC
    uint32_t size_field = 0;
    Words data;              // raw wire words
    bool crc_ok = true;
    std::vector<std::string> defects;
    double latency_ms = 0.0;
    size_t n_words = 0;

    bool ok() const { return defects.empty(); }
    // Data words as logical big-endian register values.
    std::vector<uint32_t> values() const;
    std::vector<uint8_t> bytes() const;
};

RawAck decodeAck(const Words& frame);

struct Captured {
    double t_ms;  // since the context started
    Words frame;
};

struct StreamPkt {
    double t_ms = 0;
    uint8_t stream_id = 0;
    uint8_t tag = 0;
    uint32_t dsize_p = 0;  // DsizeP field, words
    Words payload;
    size_t total_words = 0;
    bool crc_ok = true;
    std::vector<std::string> defects;
};

std::vector<StreamPkt> streamPackets(const std::vector<Captured>& cap);

// One image walked out of the concatenated stream payload: a rectangular
// image (Table 38 header, Table 39 line markers) or an arbitrary one
// (Table 40 header, Table 41 line markers carrying each line's geometry).
struct ImageRec {
    bool arbitrary = false;
    uint8_t packet_stream_id = 0;
    uint32_t stream_id = 0;
    uint32_t source_tag = 0;
    uint32_t xsize = 0, xoffs = 0, ysize = 0, yoffs = 0, dsize_l = 0;
    uint32_t pixel_f = 0, tap_g = 0, flags = 0;
    bool replicas_ok = true;
    std::vector<size_t> line_words;  // words between each line marker and the next marker
    std::vector<Words> lines;        // raw line words
    // Arbitrary images: each line marker's Xsize, Xoffs and DsizeL.
    std::vector<uint32_t> line_xsize, line_xoffs, line_dsize_l;
    bool header_complete = true;
    double t_first_ms = 0, t_last_ms = 0;
};

std::vector<ImageRec> walkImages(const std::vector<StreamPkt>& pkts);

// Bits per pixel of a Table 25 code (container width for the packed modes).
std::optional<int> pixelBits(uint32_t pixel_f);

// -- bench observations -----------------------------------------------------------
// A short packet the device inserted into the downlink (Table 16 trigger,
// Table 17 I/O acknowledgment), with the host time it arrived.
struct TimedShort {
    double t_ms = 0;
    ShortPacket pkt;
    // From the bench's SHORT_DL event (CAP_TIMES), else 0 / -1: the bench
    // time of the first word, whether the packet was inserted into another
    // one, and how many words of that one went before it.
    uint64_t device_ps = 0;
    int inside = -1;
    int word_index = -1;
};

// A Table 15 trigger or Table 17 acknowledgment the bench put on the uplink
// (bench::UPLINK_MARK): its first character, the bench time of that
// character's first bit and the bit period.
struct UplinkMark {
    double t_ms = 0;
    uint32_t chr = 0;     // character | K << 8
    uint64_t time_ps = 0;
    uint32_t bit_ps = 0;
};

// A downlink packet other than a stream packet (bench::FRAME_DL): its TYPE
// and the bench times of its SOP and EOP words.  In arrival order, so the
// n-th control acknowledgment a case received is the n-th of TYPE 0x03.
struct FrameTime {
    double t_ms = 0;
    uint32_t type = 0;
    uint64_t sop_ps = 0, eop_ps = 0;
    uint32_t words = 0;
};

// A device output edge the bench reported (bench::PIN_EDGE).
struct PinEdge {
    double t_ms = 0;          // host time the event arrived
    uint32_t pin = 0;
    uint32_t value = 0;
    uint64_t device_ns = 0;   // the bench's own time stamp
};

// One image for the device's pixel port (bench::PIXEL_FRAME).
struct PixelImage {
    uint32_t xsize = 0, ysize = 0, xoffs = 0, yoffs = 0;
    uint32_t pixfmt = 0x0101, tapg = 0, streamid = 1, sourcetag = 0, flags = 0;
    uint32_t valid_permille = 1000;  // share of cycles the port offers a pixel
    std::vector<uint16_t> pixels;    // xsize * ysize, row by row
};

// -- the context ------------------------------------------------------------------
// What a user may set for a whole run.  optionSpecs() describes every field
// once; the CLI flags, the GUI controls and the logged settings come from it.
struct Options {
    int soak_seconds = 60;      // PERF-003 duration
    int perf_seconds = 5;       // PERF-001/002 measurement window
    uint32_t host_spsm = 4096;  // StreamPacketSizeMax the host programs (bytes)
    // The wait for a raw command's acknowledgment.  Register calls through
    // the camera (GenICam features) wait the session's own ack timeout
    // (global --timeout) instead; both are multiplied by timeout_scale.
    int ack_timeout_ms = 1000;
    // Multiplies every host-side wait (ack, image, quiet link), never a
    // spec limit a check measures against.  The catalogue's per-case
    // emulator.timeout_scale and the CLI --timeout-scale multiply in.
    double timeout_scale = 1.0;
    // A link without stream packets for quiet_ms counts as quiet; a check
    // waits at most quiet_timeout_ms for that after a stop.  Both scaled.
    int quiet_ms = 150;
    int quiet_timeout_ms = 3000;
    // Wait for the first image header after AcquisitionStart (scaled).
    int first_image_timeout_ms = 5000;
    // The plan's command-to-acknowledgment limit the latency checks judge.
    // A verdict limit, so timeout_scale does not touch it; a user on a slow
    // simulator may relax it (the host measures wall-clock time).
    int ack_latency_ms = 200;
    // Random seed of every check; 0 = each check's own default.
    uint32_t seed = 0;
    // FAIL lines a check prints per kind of item before it only counts.
    int max_reported = 3;
    // Timestamped run directories kept under the log folder.
    int keep_logs = 10;
};

struct OptionSpec {
    const char* key;    // field name, "host_spsm"
    const char* flag;   // CLI flag, "--host-spsm"
    const char* label;  // GUI label
    const char* unit;   // "s", "ms", "bytes", "x" or ""
    double min, max;
    int decimals;
    const char* help;     // one line: CLI --help, GUI label tooltip lead
    const char* details;  // a few sentences for the GUI pop-up help
    double (*get)(const Options&);
    void (*set)(Options&, double);
};

const std::vector<OptionSpec>& optionSpecs();
const OptionSpec* findOption(const std::string& key);
// Set one field; throws std::invalid_argument when the value is out of
// range or not a legal value (host_spsm: a multiple of 4).
void setOption(Options& o, const OptionSpec& s, double v);
// "soak_seconds=60 perf_seconds=5 ..." with every field.
std::string describeOptions(const Options& o);

enum class LineKind { Info, Pass, Fail, Warn, Note };

struct LogLine {
    LineKind kind;
    std::string text;
};

class Skip : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};
class Cancelled : public std::runtime_error {
public:
    Cancelled() : std::runtime_error("cancelled") {}
};
// A precondition the device did not meet; ends the case as FAIL.
class Abort : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

class Context {
public:
    using Sink = std::function<void(const LogLine&)>;

    Context(std::shared_ptr<CameraControl> cam, const std::atomic<bool>& cancel, Sink sink,
            Options opt = Options());
    ~Context();
    Context(const Context&) = delete;
    Context& operator=(const Context&) = delete;

    CameraControl& cam() { return *cam_; }
    const Options& opt() const { return opt_; }
    // A host-side wait scaled by Options::timeout_scale.
    int wait(int ms) const { return int(ms * opt_.timeout_scale + 0.5); }
    // Clear the raw ack queue, wait kLateAckMs, clear it again (see .cpp).
    void dropLateAcks();
    static constexpr int kLateAckMs = 3;
    // The random seed a check uses: Options::seed, or the check's own
    // default when that is 0.  Logs the value in effect.
    uint32_t seed(uint32_t dflt);

    // -- per-case parameters (catalogue emulator.params) ---------------------
    // The check's stimulus counts and lists live in the catalogue, next to
    // the prose that describes them.  A key the case does not declare is a
    // check bug (std::logic_error, verdict ERROR); validation/check_params.py
    // matches the keys each check reads with the declared ones.
    void setParams(Params p) { params_ = std::move(p); }
    // "images", or "test_packets.clean" inside a group.
    const ParamValue& param(const std::string& key) const;
    int iparam(const std::string& key) const;
    std::string sparam(const std::string& key) const;
    std::vector<int64_t> ilist(const std::string& key) const;
    std::vector<std::string> slist(const std::string& key) const;
    // StreamPacketSizeMax values in bytes; "host_spsm" is Options::host_spsm.
    std::vector<uint32_t> spsmList(const std::string& key) const;
    // A list of number lists (roi_list: [Width, Height, OffsetX, OffsetY]).
    std::vector<std::vector<int64_t>> rows(const std::string& key) const;
    NodeTree* tree() const { return tree_.get(); }
    // Feature or nullptr.  need() skips the case when the XML lacks it.
    Feature* feature(const std::string& name) const;
    Feature& need(const std::string& name) const;

    // -- verdict lines -------------------------------------------------------
    void info(const char* fmt, ...);
    void note(const char* fmt, ...);
    void warn(const char* fmt, ...);
    bool expect(bool ok, const char* fmt, ...);
    [[noreturn]] void skip(const std::string& why);
    [[noreturn]] void abort(const std::string& why);
    int failures() const { return failures_; }
    int passes() const { return passes_; }
    int warnings() const { return warnings_; }

    void checkpoint() const;
    // A host-side pause, scaled like every wait (settle time, a window to
    // record in).  sleepRawMs is wall-clock: for stimulus timing and for a
    // window whose length is itself measured, and for poll ticks.
    void sleepMs(int ms) const;
    void sleepRawMs(int ms) const;
    double nowMs() const;

    // Run on exit, last registered first (restore registers, stop streams).
    void onExit(std::function<void()> fn) { cleanup_.push_back(std::move(fn)); }
    void runCleanup();

    // -- control access ----------------------------------------------------
    std::optional<RawAck> exchange(const Words& cmd, int timeout_ms = -1);
    // Send several frames back to back, collect up to max_acks.
    std::vector<RawAck> exchangeMany(const std::vector<Words>& frames, size_t max_acks,
                                     int timeout_ms);
    // Send one command and collect its acknowledgments up to the first final
    // one (not 0x04 Wait), waiting at most timeout_ms (scaled) for it; then
    // extra_ms (scaled) more for any that follow it.
    std::vector<RawAck> exchangeFinal(const Words& cmd, int timeout_ms, int extra_ms);
    // Send without waiting for anything (acks that do arrive are discarded);
    // one IDLE word follows each frame when the link takes characters (§8.7).
    void sendOnly(const std::vector<Words>& frames, int settle_ms = 50);
    std::optional<RawAck> readRaw(uint32_t addr, uint32_t bytes);
    std::optional<RawAck> writeRaw(uint32_t addr, const std::vector<uint32_t>& values);
    // Strict helpers: abort() when the device does not answer as a spec
    // device must for a plain access.
    uint32_t rd32(uint32_t addr);
    uint64_t rd64(uint32_t addr);
    void wr32(uint32_t addr, uint32_t value);
    std::optional<uint32_t> tryRd32(uint32_t addr, int* code = nullptr);
    std::vector<uint8_t> readBlock(uint32_t addr, size_t bytes, size_t chunk = 64);
    std::string readString(uint32_t addr, size_t bytes);
    // Read a register now and put it back on exit.
    void preserve(uint32_t addr);

    // -- downlink recording ------------------------------------------------
    void startRecording();
    std::vector<Captured> stopRecording();
    // Hand over what was recorded so far and keep recording (no gap).
    std::vector<Captured> takeRecording();
    size_t headersSeen() const { return headers_seen_; }
    size_t streamPacketsSeen() const { return stream_seen_; }
    double lastStreamMs() const;
    // Record the downlink for ms (scaled, like sleepMs).
    std::vector<Captured> record(int ms);

    // -- acquisition through the XML (Width, AcquisitionStart, ...) -----------
    // Makes sure StreamPacketSizeMax is non-zero, selects continuous/free-run
    // mode where the XML offers it, and restores both on exit.
    void prepareStreaming();
    void acqStart();
    void acqStop();
    bool waitHeaders(size_t n, int timeout_ms);
    // -1: Options::quiet_ms / quiet_timeout_ms.
    bool waitQuiet(int quiet_ms = -1, int timeout_ms = -1);
    // After the n-th image header: wait for the next header or a quiet link,
    // so the n-th image's tail is in the recording.
    void waitTail(size_t n_headers);
    // prepare, record, start, wait for n image headers (+ the n-th image's
    // tail), stop, wait until quiet.  Returns the recording.
    std::vector<Captured> acquire(size_t n_images, int timeout_ms = 10000);
    // Same, but until n stream packets have arrived.
    std::vector<Captured> acquirePackets(size_t n_packets, int timeout_ms = 15000);
    void setFeature(const std::string& name, const Value& v);
    Value getFeature(const std::string& name);

    // -- the device's bench (protocol/bench.h) -------------------------------
    // Capability bits of the bench, 0 without one.
    uint32_t benchCaps();
    // Skip the case unless the bench has every bit of `caps`; `what` says
    // what the case needs it for.
    void needBench(uint32_t caps, const std::string& what);
    // Drive a device input; it goes back to its level before (GET_PINS) on
    // exit.
    void benchPin(uint32_t pin, uint32_t value);
    // The bench's input levels, bit (pin - 1); nullopt without an answer.
    std::optional<uint32_t> benchPins(int timeout_ms = 2000);
    // Power-on reset through the bench with the inputs at `inputs` (bit
    // pin - 1), then wait until the device answers a read again.  The inputs
    // go back to their levels before on exit; registers the case preserved
    // are restored as usual, everything else stays at its reset value.
    // domains: the reset inputs pulsed (bench::CAP_RESET_DOMAINS), bit 0 app,
    // 1 tx, 2 rx; 0 = all.
    bool benchReset(uint32_t inputs, int timeout_ms = 5000, uint32_t domains = 0);
    // REG_ERR / UPLINK_PPM etc. as they are; restored by the caller.
    void benchSend(const Words& body);
    // Round trip through the bench: every earlier op has taken effect.
    bool benchSync(int timeout_ms = 2000);
    // The bench's time (bench::CAP_TIMES) in ps, from a SYNC round trip, and
    // the length of the device's millisecond in ps (its control time limits;
    // a simulated device may run them faster than real time).
    struct BenchTime {
        uint64_t now_ps = 0;
        uint64_t ms_ps = 1000000000ull;
    };
    std::optional<BenchTime> benchTime(int timeout_ms = 2000);
    // An op with a reply (DL_STATS ...): the reply's words after the op, or
    // nullopt without one within timeout_ms.
    std::optional<Words> benchRequest(const Words& op, int timeout_ms = 2000);
    // One image through the pixel port; returns the pixels the bench reported
    // accepted (-1: no reply within timeout_ms).
    int64_t pixelFrame(const PixelImage& img, int timeout_ms = 10000);
    // The same in two halves, so traffic can run while the port is busy:
    // send, then collect the reply of the oldest outstanding frame.
    void sendPixelFrame(const PixelImage& img);
    int64_t waitPixelFrame(int timeout_ms = 10000);
    // A frame with the framing as given (bench::PIXEL_BEATS): img supplies the
    // metadata and the valid density, beats the pixels with their SOF / EOL /
    // EOF bits (16, 17, 18).  The reply is waited for with waitPixelFrame.
    void sendPixelBeats(const PixelImage& img, const std::vector<uint32_t>& beats);

    // Characters on the uplink (one CXC1 frame), collecting up to max_acks
    // control acknowledgments like exchangeMany.
    std::vector<RawAck> exchangeChars(const Chars& chars, size_t max_acks, int timeout_ms = -1);
    void sendChars(const Chars& chars);

    // Everything the bench and the downlink short packets said since the
    // context started (or since since_ms).
    std::vector<TimedShort> shortPackets(double since_ms = 0) const;
    std::vector<PinEdge> pinEdges(uint32_t pin, double since_ms = 0) const;
    std::vector<UplinkMark> uplinkMarks(double since_ms = 0) const;
    std::vector<FrameTime> frameTimes(double since_ms = 0) const;
    // Wait until at least n short packets of `kind` arrived after since_ms.
    bool waitShort(ShortPacket::Kind kind, size_t n, double since_ms, int timeout_ms);
    bool waitEdges(uint32_t pin, size_t n, double since_ms, int timeout_ms);

private:
    void vlog(LineKind k, const char* fmt, va_list ap);
    void onFrame(const Words& frame);
    void onSide(uint32_t magic, const Words& body);
    // The first bench reply `op | REPLY` that `match` accepts, waiting up to
    // timeout_ms; replies to other requests stay for their own waiter.
    std::optional<Words> waitBenchReply(uint32_t op, int timeout_ms,
                                        const std::function<bool(const Words&)>& match = {});

    std::shared_ptr<CameraControl> cam_;
    CameraControl::TapId tap_ = 0;
    int base_ack_ms_ = 0;  // the camera's ack timeout before this case
    CameraControl::TapId side_tap_ = 0;
    std::shared_ptr<NodeTree> tree_;
    const std::atomic<bool>& cancel_;
    Sink sink_;
    Options opt_;
    Params params_;
    std::chrono::steady_clock::time_point t0_;
    int failures_ = 0, passes_ = 0, warnings_ = 0;
    std::vector<std::function<void()>> cleanup_;
    bool prepared_ = false;
    bool in_cleanup_ = false;

    BlockingQueue<Captured> acks_;
    mutable std::mutex rec_mu_;
    bool recording_ = false;
    std::vector<Captured> rec_;
    std::atomic<size_t> headers_seen_{0};
    std::atomic<size_t> stream_seen_{0};
    std::atomic<double> last_stream_ms_{-1.0};
    uint32_t prev_tail_ = 0;  // last payload word of the previous stream packet

    mutable std::mutex side_mu_;
    std::condition_variable side_cv_;
    std::vector<TimedShort> shorts_;
    std::vector<PinEdge> edges_;
    std::vector<UplinkMark> marks_;
    std::vector<FrameTime> frame_times_;
    std::deque<Words> bench_replies_;
};

}  // namespace cxp::validation
