// Host <-> virtual camera over real FIFOs: discovery, R/W, streaming
// (tests/test_end_to_end.py).

#include <QFileInfo>
#include <QTemporaryDir>
#include <QtTest>

#include <fstream>
#include <iterator>

#include "cxp/camera/client.h"
#include "cxp/compliance/checker.h"
#include "cxp/genicam/sfnc.h"
#include "cxp/parser/stream_parser.h"
#include "cxp/sim/virtual_camera.h"

using namespace cxp;

namespace {

const std::string kXml = CXP_DEFAULT_XML;

// StreamPacketSizeMax (§10.3.32) a host programs before it streams: bytes
// of the whole packet, here 256 payload words + the 8 framing words.  At
// its power-on value 0 the camera sends no stream packet (Table 44).
constexpr uint32_t kSpsm = 4 * (256 + 8);

struct Rig {
    std::unique_ptr<VirtualCamera> vcam;
    std::shared_ptr<CameraControl> host;

    ~Rig() {
        if (host) host->disconnect();
        if (vcam) vcam->stop();
    }
};

std::unique_ptr<Rig> bringUp(const QTemporaryDir& tmp, VirtualCameraConfig cfg) {
    auto [h2c, c2h] = makeFifoPair(tmp.path().toStdString());
    std::ifstream f(kXml, std::ios::binary);
    cfg.xml.assign(std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>());
    auto rig = std::make_unique<Rig>();
    rig->vcam = std::make_unique<VirtualCamera>(
        std::make_shared<FifoEndpoint>(c2h, h2c, true, "vcam"), cfg);
    rig->vcam->start();
    rig->host = std::make_shared<CameraControl>(
        std::make_shared<FifoEndpoint>(h2c, c2h, true, "host"));
    return rig;
}

}  // namespace

class TestEndToEnd : public QObject {
    Q_OBJECT

private slots:
    void initTestCase() { logging::setLevel(LogLevel::Warning); }

    void discoverAndRegisterRw() {
        QTemporaryDir tmp;
        auto rig = bringUp(tmp, VirtualCameraConfig());
        DeviceInfo info = rig->host->connect();
        QCOMPARE(QString::fromStdString(info.vendor_name), QString(reg::DEVICE_VENDOR_NAME_STR));
        QCOMPARE(QString::fromStdString(info.model_name), QString(reg::DEVICE_MODEL_NAME_STR));
        QVERIFY(rig->host->xmlTree() != nullptr);
        QVERIFY(rig->host->parameters().at("Width").ok());
        rig->host->linkInit(2);
        QCOMPARE(rig->host->readReg(Bootstrap::MASTER_HOST_LINK_ID)[0], uint32_t(2));
        rig->host->writeReg(R_WIDTH, {320});
        QCOMPARE(rig->host->readReg(R_WIDTH)[0], uint32_t(320));

        // Live SFNC access through the camera-backed accessor.
        auto tree = rig->host->xmlTree();
        tree->setAccessor(std::make_shared<CameraRegisterAccessor>(rig->host));
        tree->invalidate();
        QCOMPARE(tree->find("Width")->getValue().i, int64_t(320));
        tree->find("Height")->setValue(Value::ofInt(100));
        QCOMPARE(rig->host->readReg(R_HEIGHT)[0], uint32_t(100));
    }

    void streamingReconstructsFrames() {
        QTemporaryDir tmp;
        VirtualCameraConfig cfg;
        cfg.width = 32;
        cfg.height = 16;
        cfg.frames_per_start = 3;
        auto rig = bringUp(tmp, cfg);
        StreamParser parser;
        std::vector<FramePtr> imgs;
        rig->host->connect();
        rig->host->writeReg(R_FRAMECNT, {3});
        rig->host->writeReg(Bootstrap::STREAM_PACKET_DATA_SIZE, {kSpsm});
        rig->host->writeReg(R_ACQ_START, {1});
        for (int i = 0; i < 200 && imgs.size() < 3; ++i) {
            Words fr;
            if (!rig->host->streamQueue().pop(fr, 2000)) break;
            for (auto& img : parser.feedFrame(fr)) imgs.push_back(img);
        }
        QCOMPARE(imgs.size(), size_t(3));
        QCOMPARE(imgs[0]->header.width, uint32_t(32));
        QCOMPARE(imgs[0]->header.height, uint32_t(16));
        QCOMPARE(imgs[0]->data.size(), size_t(32 * 16));
        QCOMPARE(parser.stats.crc_errors, uint64_t(0));
        // Gradient pattern: pixel (x, y) of frame f is x + y + f.
        QCOMPARE(int(imgs[1]->data[3 * 32 + 5]), 3 + 5 + 1);
        std::string out = imgs[0]->save(tmp.filePath("f.pgm").toStdString());
        QVERIFY(QFileInfo(QString::fromStdString(out)).size() > 32 * 16);
        std::string png = imgs[0]->save(tmp.filePath("f.png").toStdString());
        QVERIFY(QImage(QString::fromStdString(png)).size() == QSize(32, 16));
    }

    void crcInjectionFlaggedByCompliance() {
        QTemporaryDir tmp;
        VirtualCameraConfig cfg;
        cfg.width = 32;
        cfg.height = 16;
        cfg.frames_per_start = 4;
        cfg.inject_crc_every = 3;
        auto rig = bringUp(tmp, cfg);
        StreamParser parser;
        DeviceInfo info = rig->host->connect();
        rig->host->writeReg(R_FRAMECNT, {4});
        rig->host->writeReg(Bootstrap::STREAM_PACKET_DATA_SIZE, {kSpsm});
        rig->host->writeReg(R_ACQ_START, {1});
        for (int i = 0; i < 300; ++i) {
            Words fr;
            if (!rig->host->streamQueue().pop(fr, 2000)) break;
            parser.feedFrame(fr);
            if (parser.stats.event_pkts) break;  // acquisition-end event
        }
        QVERIFY(parser.stats.crc_errors > 0);
        ComplianceChecker chk;
        chk.checkDevice(info);
        auto tree = NodeTree::fromFile(kXml);
        chk.checkSfnc(tree.get());
        chk.checkErrorHandling(parser.stats.crc_errors > 0);
        bool err_pass = false;
        for (const auto& r : chk.finalize().results) {
            err_pass |= r.rule == "CXP-ERR" && r.severity == Severity::Pass;
        }
        QVERIFY(err_pass);
    }

    void packetDropCountsMissing() {
        QTemporaryDir tmp;
        VirtualCameraConfig cfg;
        cfg.width = 64;
        cfg.height = 64;
        cfg.frames_per_start = 2;
        cfg.drop_packet_every = 5;
        auto rig = bringUp(tmp, cfg);
        StreamParser parser;
        rig->host->connect();
        rig->host->writeReg(Bootstrap::STREAM_PACKET_DATA_SIZE, {kSpsm});
        rig->host->writeReg(R_ACQ_START, {1});
        for (int i = 0; i < 300; ++i) {
            Words fr;
            if (!rig->host->streamQueue().pop(fr, 2000)) break;
            parser.feedFrame(fr);
            if (parser.stats.event_pkts) break;
        }
        QVERIFY(parser.stats.missing_packets > 0);
    }

    void noStreamWhileSpsmZero() {
        // Table 44: StreamPacketSizeMax 0 = not initialized, no stream
        // packet; below 36 bytes no packet fits either.  The camera reads
        // it as bytes of the whole packet, as the RTL does (cxp_device_top).
        QTemporaryDir tmp;
        VirtualCameraConfig cfg;
        cfg.width = 16;
        cfg.height = 4;
        auto rig = bringUp(tmp, cfg);
        rig->host->connect();
        QCOMPARE(rig->host->readReg(Bootstrap::STREAM_PACKET_DATA_SIZE)[0], uint32_t(0));
        rig->host->writeReg(R_FRAMECNT, {1});
        rig->host->writeReg(R_ACQ_START, {1});
        StreamParser parser;
        std::vector<FramePtr> imgs;
        for (int i = 0; i < 20; ++i) {
            Words fr;
            if (!rig->host->streamQueue().pop(fr, 200)) break;
            for (auto& img : parser.feedFrame(fr)) imgs.push_back(img);
        }
        QCOMPARE(imgs.size(), size_t(0));
        QCOMPARE(parser.stats.stream_pkts, uint64_t(0));
        // 36 bytes: one payload word per packet (DsizeP 1).
        rig->host->writeReg(Bootstrap::STREAM_PACKET_DATA_SIZE, {36});
        rig->host->writeReg(R_ACQ_START, {1});
        for (int i = 0; i < 400 && imgs.empty(); ++i) {
            Words fr;
            if (!rig->host->streamQueue().pop(fr, 2000)) break;
            for (auto& img : parser.feedFrame(fr)) imgs.push_back(img);
        }
        QCOMPARE(imgs.size(), size_t(1));
        QCOMPARE(parser.stats.crc_errors, uint64_t(0));
    }
};

QTEST_GUILESS_MAIN(TestEndToEnd)
#include "test_end_to_end.moc"
