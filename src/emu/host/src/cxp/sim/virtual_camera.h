// Virtual CoaXPress camera (cxp/sim/virtual_camera.py).
//
// A reference device the host stack can be developed and validated against.
// It owns the mirror side of the FIFO pair (reads h2c, writes c2h),
// serves the device's register map, replies to every control command with
// a spec-shaped acknowledge, echoes heartbeats, and streams test-pattern
// frames when acquisition is started.
//
// Registers: the generated map (cxp::reg, src/regmap/cxp_regmap.yaml), the
// same table the RTL register file follows.  Power-on values, identity
// strings and the Table 22 answer to every access (0x40 nothing there,
// 0x41 a value the register does not take, 0x43 read-only, 0x44
// write-only) come from it.  The TPG-facing features (Width .. TpgRun) power
// up at VirtualCameraConfig, which defaults to the map's values; a bench may
// set them otherwise, as the RTL's elaboration parameters do.
//
// Stream packetisation reads StreamPacketSizeMax as the RTL does: bytes of
// the whole packet, DsizeP = SPSM / 4 - 8 payload words; below 36 bytes, and
// at 0 (its power-on value), no stream packet is sent.
//
// Bench (protocol/bench.h, virtual_camera_bench.cpp): the camera answers
// HELLO and carries out what a model without clocks can — CXC1 character
// frames with Table 15 triggers (recreated on TRIG_OUT, acknowledged with a
// Table 17 I/O ack), the TRIG_IN input (Table 16 trigger packets, one
// outstanding until the host acknowledges it or 10 ms pass, §8.3.3), the
// extension-link strap (writes answer 0x43), a faulting register bus, and
// the pixel port (PIXEL_FRAME with USE_TPG = 0, rectangular or arbitrary
// images).  UPLINK_PPM is not carried out.
#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "cxp/image/pixel_formats.h"
#include "cxp/image/test_pattern.h"
#include "cxp/protocol/chars.h"
#include "cxp/protocol/packets.h"
#include "cxp/transport/fifo.h"
#include "cxp/utils/log.h"

namespace cxp {

// The manufacturer-window features the camera acts on (cxp::reg names).
inline constexpr uint32_t R_WIDTH = reg::WIDTH_ALIAS;
inline constexpr uint32_t R_HEIGHT = reg::HEIGHT_ALIAS;
inline constexpr uint32_t R_PIXFMT = reg::PIXEL_FORMAT_ALIAS;
inline constexpr uint32_t R_ACQ_START = reg::ACQUISITION_START_ALIAS;
inline constexpr uint32_t R_ACQ_STOP = reg::ACQUISITION_STOP_ALIAS;
inline constexpr uint32_t R_FRAMECNT = reg::FRAME_COUNT;
inline constexpr uint32_t R_TESTPAT = reg::TEST_PATTERN;
inline constexpr uint32_t R_OFFSET_X = reg::OFFSET_X;
inline constexpr uint32_t R_OFFSET_Y = reg::OFFSET_Y;
inline constexpr uint32_t R_TAPG = reg::TAP_GEOMETRY_ALIAS;
inline constexpr uint32_t R_STREAMID = reg::IMAGE1_STREAM_ID_ALIAS;
inline constexpr uint32_t R_SOURCETAG = reg::SOURCE_TAG;
inline constexpr uint32_t R_FLAGS = reg::STREAM_FLAGS;
inline constexpr uint32_t R_TPG_RUN = reg::TPG_RUN;
inline constexpr uint32_t XML_BASE = reg::XML_BLOB_ADDR;

struct VirtualCameraConfig {
    // Identity and the TPG-facing features' power-on values: the map's
    // (cxp::reg), so the camera answers like the RTL device unless a bench
    // sets them otherwise.
    std::string vendor = reg::DEVICE_VENDOR_NAME_STR;
    std::string model = reg::DEVICE_MODEL_NAME_STR;
    uint32_t width = reg::WIDTH_RESET;
    uint32_t height = reg::HEIGHT_RESET;
    uint32_t pixel_format = reg::PIXEL_FORMAT_RESET;  // PixelFormat, a PFNC value (§11.2.1.6)
    uint32_t frames_per_start = reg::FRAME_COUNT_RESET;  // FrameCount (0 = 1)
    uint32_t test_pattern = reg::TEST_PATTERN_RESET;
    uint32_t x_offs = reg::OFFSET_X_RESET;
    uint32_t y_offs = reg::OFFSET_Y_RESET;
    uint32_t tap_geometry = reg::TAP_GEOMETRY_RESET;  // 0 = Geometry_1X_1Y
    uint32_t stream_id = reg::IMAGE1_STREAM_ID_RESET;
    uint32_t source_tag = reg::SOURCE_TAG_RESET;
    uint32_t flags = reg::STREAM_FLAGS_RESET;         // StreamFlags (low byte)
    bool free_run = reg::TPG_RUN_RESET != 0;          // TpgRun: stream until stopped
    std::vector<uint8_t> xml;        // GenICam manifest served at XML_BASE
    uint32_t inject_crc_every = 0;   // >0: corrupt every Nth stream pkt
    uint32_t drop_packet_every = 0;  // >0: drop every Nth stream pkt
};

class VirtualCamera {
public:
    VirtualCamera(std::shared_ptr<FifoEndpoint> endpoint,
                  VirtualCameraConfig config = VirtualCameraConfig());
    ~VirtualCamera();
    VirtualCamera(const VirtualCamera&) = delete;
    VirtualCamera& operator=(const VirtualCamera&) = delete;

    void start();
    void stop();
    // Serve until *stop_flag becomes true.
    void runUntil(const std::atomic<bool>& stop_flag);

    const VirtualCameraConfig& config() const { return cfg_; }

    // Register memory (big-endian bytes), thread-safe.
    uint32_t r32(uint32_t addr) const;
    void w32(uint32_t addr, uint32_t val);

private:
    void initRegs();
    void serve();
    void handleCmd(const CtrlCmdPacket& cmd);
    AckCode readAccess(uint32_t addr, uint32_t nwords) const;
    AckCode writeAccess(uint32_t addr, const std::vector<uint32_t>& values);
    void connectionReset();
    void onWrite(uint32_t addr, const Words& data);
    void resetDevice();
    void sendAck(AckCode code, Words data = {});
    void startAcquisition();
    void stopAcquisition();
    void acquire();
    uint32_t streamChunkWords() const;
    void sendFrame();
    void emitStream(const uint32_t* payload, size_t n);
    void emitImage(Words words, bool acquisition);

    // -- bench (virtual_camera_bench.cpp) ---------------------------------
    void handleFrame(const Words& frame);
    void handleChars(const Chars& chars);
    void handleBench(const Words& body);
    void hostTrigger(bool rising, int delay);
    void setTrigIn(bool level);
    void trigLoop();
    void pixelFrame(const Words& body);
    void sendSide(uint32_t magic, const Words& body);
    void benchEvent(uint32_t pin, uint32_t value);

    std::vector<uint8_t> memRead(uint64_t addr, size_t length) const;
    void memWrite(uint64_t addr, const uint8_t* data, size_t n);
    void memWriteStr(uint64_t addr, const std::string& s, size_t length);

    std::shared_ptr<FifoEndpoint> ep_;
    VirtualCameraConfig cfg_;
    Logger log_;

    mutable std::mutex mem_mu_;
    std::unordered_map<uint64_t, uint8_t> mem_;

    uint8_t stream_tag_ = 0;
    uint64_t stream_pkt_n_ = 0;
    uint32_t frame_id_ = 0;

    std::atomic<bool> running_{false};
    std::thread rx_thr_;
    std::mutex acq_mu_;
    std::thread acq_thr_;
    std::atomic<bool> acq_active_{false};
    std::atomic<bool> acq_cancel_{false};
    std::mutex tx_mu_;  // one downlink frame at a time (stream, acks, bench)

    // Bench state.
    std::chrono::steady_clock::time_point t0_ = std::chrono::steady_clock::now();
    std::atomic<uint32_t> reg_err_{0};
    std::atomic<bool> ext_link_{false};
    std::atomic<bool> trig_polarity_{false};
    std::atomic<bool> use_tpg_{true};
    std::atomic<bool> arbitrary_{false};
    bool trig_in_ = false;
    bool trig_out_ = false;
    uint32_t pix_sourcetag_ = 0;
    std::mutex trig_mu_;
    std::condition_variable trig_cv_;
    std::deque<bool> trig_pending_;  // device -> host trigger packets to send (rising?)
    bool trig_acked_ = true;
    std::thread trig_thr_;
};

}  // namespace cxp
