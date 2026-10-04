#include "device_model.h"

#include <set>

#include <QDir>
#include <QFileInfo>

using namespace cxp;

std::vector<DevicePtr> candidates(const std::string& fifo_dir) {
    std::vector<DevicePtr> devs;
    auto sim = std::make_shared<Device>();
    sim->label = "Virtual Camera (in-process simulator)";
    sim->kind = Device::Kind::Sim;
    sim->fifo_dir = fifo_dir;
    devs.push_back(sim);

    QDir dir(QString::fromStdString(fifo_dir));
    const auto entries = dir.entryInfoList({"*.h2c"}, QDir::System | QDir::Files, QDir::Name);
    std::set<QString> seen;
    for (const QFileInfo& fi : entries) {
        const QString base = fi.absoluteFilePath().chopped(4);
        const QString c2h = base + ".c2h";
        if (base.endsWith("gui_sim") || seen.count(c2h) || !QFileInfo::exists(c2h)) continue;
        seen.insert(c2h);
        auto d = std::make_shared<Device>();
        d->label = QFileInfo(base).fileName();
        d->kind = Device::Kind::Fifo;
        d->fifo_dir = fifo_dir;
        d->h2c = fi.absoluteFilePath().toStdString();
        d->c2h = c2h.toStdString();
        devs.push_back(d);
    }
    return devs;
}

void openSession(Device& dev, int ack_ms, int retries) {
    SessionConfig cfg;
    cfg.fifo_dir = dev.fifo_dir;
    if (dev.kind == Device::Kind::Sim) {
        cfg.name = "gui_sim";
        cfg.spawn_vcam = true;
    } else {
        cfg.h2c = dev.h2c;
        cfg.c2h = dev.c2h;
    }
    cfg.ack_timeout_ms = ack_ms;
    cfg.retries = retries;
    cfg.host_name = "gui-host";
    cfg.vcam_name = "gui-vcam";
    // Before connect, so closeSession can tear it down on failure.
    dev.session = std::make_shared<HostSession>(cfg);
    dev.vcam = dev.session->vcam();
    auto host = dev.session->host();
    dev.host = host;
    dev.info = host->connect();
    dev.params = host->parameters();
    dev.tree = host->xmlTree();
    if (dev.tree) dev.tree->setAccessor(std::make_shared<CameraRegisterAccessor>(host));
    dev.stats = std::make_shared<LinkStats>(host);
    dev.stats->start();
}

void closeSession(Device& dev) {
    if (dev.stats) dev.stats->stop();
    dev.stats.reset();
    dev.session.reset();  // disconnects the host, stops the camera
    dev.tree.reset();
    dev.host.reset();
    dev.vcam.reset();
    dev.info.reset();
    dev.params.clear();
}
