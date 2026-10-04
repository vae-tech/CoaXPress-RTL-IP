// CoaXPress host control client (cxp/camera/client.py).
//
// Owns one FifoEndpoint and runs a background receive dispatcher thread
// that demultiplexes the downlink into:
//
// * control acknowledges  -> correlated to the outstanding command,
// * stream / event frames -> pushed to streamQueue() for the parser.
//
// Public operations cover the CXP control plane: discovery, link
// initialisation, register read/write, command execution, heartbeat, device
// reset and reconnect, each with retry + timeout.  All calls are
// synchronous and thread-safe (one outstanding command at a time).
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "cxp/camera/protocol_log.h"
#include "cxp/genicam/sfnc.h"
#include "cxp/protocol/chars.h"
#include "cxp/protocol/packets.h"
#include "cxp/transport/blocking_queue.h"
#include "cxp/transport/fifo.h"
#include "cxp/utils/log.h"
#include "cxp/utils/pcap.h"

namespace cxp {

class CameraError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct DeviceInfo {
    uint32_t magic = 0;
    uint32_t revision = 0;
    std::string vendor_name;
    std::string model_name;
    std::string xml_url;
    uint32_t device_link_id = 0;
};

// Result of reading one XML-defined parameter: a value or the error text.
struct ParamResult {
    std::optional<Value> value;
    std::string error;
    bool ok() const { return value.has_value(); }
};

// Parse a GenICam XmlUrl: "Local:<name>;<addr-hex>;<len-hex>" (also
// "Async:") -> (addr, length).  Other schemes are not device-resident.
std::optional<std::pair<uint64_t, uint64_t>> parseGenicamUrl(const std::string& url);

class CameraControl {
public:
    CameraControl(std::shared_ptr<FifoEndpoint> endpoint,
                  int ack_timeout_ms = CTRL_ACK_TIMEOUT_MS, int retries = 3,
                  const std::string& pcap_path = "");
    ~CameraControl();
    CameraControl(const CameraControl&) = delete;
    CameraControl& operator=(const CameraControl&) = delete;

    // -- lifecycle ---------------------------------------------------------
    DeviceInfo connect();
    void disconnect();
    DeviceInfo reconnect();

    // -- discovery / link init --------------------------------------------
    DeviceInfo discover();
    void linkInit(uint32_t host_link_id = 1);
    void reset();

    // -- register access (logical, spec-order values) ---------------------
    std::vector<uint32_t> readReg(uint32_t address, uint32_t nwords = 1);
    void writeReg(uint32_t address, const std::vector<uint32_t>& data);
    std::vector<uint8_t> readBytes(uint32_t address, size_t length);
    void writeBytes(uint32_t address, std::vector<uint8_t> data);
    std::string readString(uint32_t address, size_t length);

    // -- XML / SFNC --------------------------------------------------------
    std::pair<uint64_t, uint64_t> checkXml(const DeviceInfo* info = nullptr);
    std::vector<uint8_t> fetchXml(const DeviceInfo* info = nullptr);
    std::pair<std::shared_ptr<NodeTree>, std::map<std::string, ParamResult>>
    readAllParameters(const DeviceInfo* info = nullptr);

    // -- heartbeat ---------------------------------------------------------
    void startHeartbeat(int period_ms = HEARTBEAT_PERIOD_MS);

    // -- raw access (conformance tests) -----------------------------------
    // Observe every downlink frame as received, before routing (acks, stream,
    // events alike, CRC unchecked).  Any number of taps, called in the order
    // they were added.  Taps run on the receive thread, so they must be quick.
    // Once removeRxTap returns, that tap is not running and never runs again.
    using RxTap = std::function<void(const Words& frame)>;
    using TapId = uint64_t;
    TapId addRxTap(RxTap tap);
    void removeRxTap(TapId id);
    // Run `body` under the command lock, so no register call interleaves;
    // body sends frames verbatim through the function it is handed and
    // collects the answers through its tap.  Acks the frames provoke never
    // reach a later readReg/writeReg.
    using RawSender = std::function<void(const Words& frame)>;
    void rawSession(const std::function<void(const RawSender& send)>& body);

    // -- character and bench frames (protocol/chars.h, protocol/bench.h) ---
    // Observe every received CXC1 / CXB1 frame (downlink short packets,
    // bench replies and events), on the receive thread, like an RxTap.
    using SideTap = std::function<void(uint32_t magic, const Words& body)>;
    TapId addSideTap(SideTap tap);
    void removeSideTap(TapId id);
    // One CXC1 frame: these characters on the uplink, in order.  Safe inside
    // rawSession (it does not take the command lock).
    void sendChars(const Chars& chars);
    // One CXB1 frame to the device's bench.
    void sendBench(const Words& body);
    // The bench's HELLO capability bits (bench::Cap), 0 when no bench answers
    // within timeout_ms.  Asked once per connection, then cached.
    uint32_t benchCaps(int timeout_ms = 500);

    // -- protocol log --------------------------------------------------------
    // Every frame sent and received (commands, acks, stream, heartbeat),
    // plus retries and timeouts.  nullptr stops logging.
    void setProtocolLog(std::shared_ptr<ProtocolLog> plog);
    std::shared_ptr<ProtocolLog> protocolLog() const;

    // -- state -------------------------------------------------------------
    // Wait for each ack attempt of a register call (readReg, writeReg, ...).
    int ackTimeoutMs() const { return ack_timeout_ms_; }
    void setAckTimeoutMs(int ms) { ack_timeout_ms_ = ms; }
    FifoEndpoint& endpoint() { return *ep_; }
    BlockingQueue<Words>& streamQueue() { return stream_q_; }
    std::shared_ptr<NodeTree> xmlTree() const;
    std::map<std::string, ParamResult> parameters() const;

    std::atomic<bool> heartbeat_alive{true};
    std::atomic<bool> connected{false};

private:
    CtrlAckPacket txn(const CtrlCmdPacket& cmd);
    void send(const Words& frame);  // sendFrame + protocol log
    void plogEvent(const std::string& text) const;
    void rxLoop();
    void hbLoop(int period_ms);
    void stopThreads();
    void drainStale(int settle_ms = 250);
    void discoverXml(const DeviceInfo& info);

    std::shared_ptr<FifoEndpoint> ep_;
    std::atomic<int> ack_timeout_ms_;
    int retries_;
    Logger log_;
    std::unique_ptr<PcapWriter> pcap_;

    std::mutex cmd_mu_;
    BlockingQueue<CtrlAckPacket> ack_q_;
    BlockingQueue<Words> stream_q_;

    std::atomic<bool> rx_stop_{false};
    std::thread rx_thr_;
    std::atomic<bool> hb_stop_{false};
    std::mutex hb_mu_;
    std::condition_variable hb_cv_;
    std::thread hb_thr_;
    uint32_t hb_nonce_ = 0;

    std::mutex tap_mu_;
    std::vector<std::pair<TapId, RxTap>> rx_taps_;
    std::vector<std::pair<TapId, SideTap>> side_taps_;
    TapId next_tap_ = 1;
    std::mutex bench_mu_;
    std::optional<uint32_t> bench_caps_;

    mutable std::mutex plog_mu_;
    std::shared_ptr<ProtocolLog> plog_;

    mutable std::mutex state_mu_;
    std::shared_ptr<NodeTree> xml_tree_;
    std::map<std::string, ParamResult> parameters_;
};

// Adapts CameraControl to the GenICam RegisterAccessor interface.
class CameraRegisterAccessor : public RegisterAccessor {
public:
    explicit CameraRegisterAccessor(std::shared_ptr<CameraControl> cam) : cam_(std::move(cam)) {}
    std::vector<uint8_t> read(uint64_t address, size_t length) override;
    void write(uint64_t address, const std::vector<uint8_t>& data) override;

private:
    std::weak_ptr<CameraControl> cam_;
};

}  // namespace cxp
