#include "cxp/camera/session.h"

#include <cstdlib>
#include <fstream>
#include <iterator>

namespace cxp {

std::vector<uint8_t> defaultCameraXml() {
    const char* env = std::getenv("CXP_XML");
    std::ifstream f(env ? env : CXP_DEFAULT_XML, std::ios::binary);
    if (!f) return {};
    return {std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>()};
}

HostSession::HostSession(SessionConfig cfg) : cfg_(std::move(cfg)) {
    std::string h2c = cfg_.h2c, c2h = cfg_.c2h;
    if (h2c.empty() || c2h.empty()) std::tie(h2c, c2h) = makeFifoPair(cfg_.fifo_dir, cfg_.name);
    if (cfg_.spawn_vcam) {
        VirtualCameraConfig vc = cfg_.vcam;
        if (vc.xml.empty()) vc.xml = defaultCameraXml();
        vcam_ = std::make_shared<VirtualCamera>(std::make_shared<FifoEndpoint>(c2h, h2c, true, cfg_.vcam_name),
                                                vc);
        vcam_->start();
    }
    host_ = std::make_shared<CameraControl>(std::make_shared<FifoEndpoint>(h2c, c2h, true, cfg_.host_name),
                                            cfg_.ack_timeout_ms, cfg_.retries, cfg_.pcap);
    if (!cfg_.protocol_log.empty()) host_->setProtocolLog(std::make_shared<ProtocolLog>(cfg_.protocol_log));
}

HostSession::~HostSession() {
    stopDrain();
    if (host_) host_->disconnect();
    if (vcam_) vcam_->stop();
}

void HostSession::startDrain() {
    if (drain_.joinable()) return;
    drain_stop_ = false;
    drain_ = std::thread([this] {
        Words f;
        while (!drain_stop_) host_->streamQueue().pop(f, 100);
    });
}

void HostSession::stopDrain() {
    drain_stop_ = true;
    if (drain_.joinable()) drain_.join();
}

}  // namespace cxp
