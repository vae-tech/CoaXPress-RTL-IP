// One host session on a FIFO pair: the pipes, an optional in-process
// reference camera on the far end, the CameraControl, and an optional
// thread that drains the stream queue.
//
// The CLI, the GUI and the tests all bring a device up this way.  A
// session is not connected until connect(); the destructor stops the
// drain, disconnects the host and stops the camera, in that order.
#pragma once

#include <atomic>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "cxp/camera/client.h"
#include "cxp/sim/virtual_camera.h"

namespace cxp {

struct SessionConfig {
    std::string fifo_dir = "/tmp/cxp";
    std::string name = "cxp";       // FIFO pair <fifo_dir>/<name>.h2c / .c2h
    std::string h2c, c2h;           // an existing pair; overrides fifo_dir / name
    bool spawn_vcam = false;        // serve the pair with the reference camera
    VirtualCameraConfig vcam;       // its configuration (xml empty: defaultCameraXml())
    int ack_timeout_ms = CTRL_ACK_TIMEOUT_MS;
    int retries = 3;
    std::string pcap;               // capture file, "" = none
    std::string protocol_log;       // protocol log file opened before connect, "" = none
    std::string host_name = "host"; // endpoint names in the transport log
    std::string vcam_name = "vcam";
};

// The camera XML: $CXP_XML, else the build's CXP_DEFAULT_XML.  Empty when
// the file cannot be read.
std::vector<uint8_t> defaultCameraXml();

class HostSession {
public:
    explicit HostSession(SessionConfig cfg);  // makes the pipes, starts the camera
    ~HostSession();
    HostSession(const HostSession&) = delete;
    HostSession& operator=(const HostSession&) = delete;

    DeviceInfo connect() { return host_->connect(); }

    // Pop and discard stream frames so the queue does not grow while no
    // one parses the stream (a validation run records through its tap).
    void startDrain();
    void stopDrain();

    const std::shared_ptr<CameraControl>& host() const { return host_; }
    const std::shared_ptr<VirtualCamera>& vcam() const { return vcam_; }
    const SessionConfig& config() const { return cfg_; }

private:
    SessionConfig cfg_;
    std::shared_ptr<VirtualCamera> vcam_;
    std::shared_ptr<CameraControl> host_;
    std::atomic<bool> drain_stop_{false};
    std::thread drain_;
};

}  // namespace cxp
