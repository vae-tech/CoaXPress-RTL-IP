// One reachable (or unreachable) device plus open/close/scan helpers
// (cxp/gui/device_model.py).  Widget-free: the tabs include this, never
// the other way around.
//
// Threading: session fields are only mutated by openSession/closeSession,
// which run on the IoBridge worker during a scan or at teardown.  The
// explorer clears every widget's device pointer before starting either, so
// the GUI thread never reads a Device while it is being mutated.
#pragma once

#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include <QString>

#include "cxp/camera/client.h"
#include "cxp/camera/session.h"
#include "cxp/genicam/sfnc.h"
#include "cxp/sim/virtual_camera.h"
#include "link_stats.h"

// Probe budget while scanning vs. the longer budget once a device is the
// active session (bulk XML reads need the headroom).
inline constexpr int PROBE_ACK_MS = 800;
inline constexpr int PROBE_RETRIES = 2;
inline constexpr int SESSION_ACK_MS = 5000;

struct Device {
    enum class Kind { Sim, Fifo };

    QString label;
    Kind kind = Kind::Fifo;
    std::string fifo_dir;
    std::string h2c, c2h;  // Fifo kind only

    // -- live session ---------------------------------------------------------
    std::optional<cxp::DeviceInfo> info;
    std::shared_ptr<cxp::HostSession> session;  // owns host and vcam
    std::shared_ptr<cxp::CameraControl> host;
    std::shared_ptr<cxp::VirtualCamera> vcam;
    std::shared_ptr<cxp::NodeTree> tree;
    std::map<std::string, cxp::ParamResult> params;
    std::shared_ptr<LinkStats> stats;
    std::string error;
};

using DevicePtr = std::shared_ptr<Device>;

// One entry per discoverable device, always starting with the in-process
// virtual camera, then every <name>.h2c/<name>.c2h pair in fifo_dir.
std::vector<DevicePtr> candidates(const std::string& fifo_dir);

// Bring dev online: spawn the simulator if needed, connect, bind a live
// register accessor to the SFNC tree and start the stream-drain thread.
void openSession(Device& dev, int ack_ms, int retries);
void closeSession(Device& dev);
