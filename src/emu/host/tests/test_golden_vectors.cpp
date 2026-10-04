// The C++ codecs against the golden vectors emitted by cxp_protocol
// (python3 -m cxp_protocol.vectors).  The C++ stack speaks the wire
// convention of the RTL device, so it is checked against the vectors of
// the DEVICE quirk profile.  Packet data words here are wire words.

#include <QtTest>

#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>

#include "cxp/protocol/crc.h"
#include "cxp/protocol/packets.h"

using namespace cxp;

namespace {

Words toWords(const QJsonArray& a) {
    Words w;
    for (const auto& v : a) w.push_back(static_cast<uint32_t>(v.toDouble()));
    return w;
}

}  // namespace

class TestGoldenVectors : public QObject {
    Q_OBJECT

    QJsonObject root_;
    QJsonObject dev_;

private slots:
    void initTestCase() {
        const QByteArray path = qgetenv("CXP_GOLDEN_VECTORS");
        QVERIFY2(!path.isEmpty(), "CXP_GOLDEN_VECTORS not set");
        QFile f(QString::fromLocal8Bit(path));
        QVERIFY2(f.open(QIODevice::ReadOnly), path.constData());
        root_ = QJsonDocument::fromJson(f.readAll()).object();
        dev_ = root_.value("device_packets").toObject();
        QVERIFY(!dev_.isEmpty());
    }

    void crcRegister() {
        for (const auto& c : root_.value("crc").toArray()) {
            Words w = toWords(c.toObject().value("words").toArray());
            // crc32Words returns the zlib value (register ^ 0xFFFFFFFF).
            uint32_t reg = crc32Words(w.data(), w.size()) ^ 0xFFFFFFFFu;
            QCOMPARE(reg, static_cast<uint32_t>(c.toObject().value("reg").toDouble()));
        }
    }

    void ctrlCommands() {
        CtrlCmdPacket rd;
        rd.opcode = CtrlOpcode::Read;
        rd.address = 0;
        rd.nwords = 1;
        QCOMPARE(rd.toWords(), toWords(dev_.value("ctrl_read").toArray()));

        CtrlCmdPacket wr;
        wr.opcode = CtrlOpcode::Write;
        wr.address = reg::CONNECTION_CONFIG;  // cxp_protocol.vectors writes ConnectionConfig
        wr.data = toWords(dev_.value("ctrl_write_data_wire").toArray());
        QCOMPARE(wr.toWords(), toWords(dev_.value("ctrl_write").toArray()));

        CtrlCmdPacket rst;
        rst.opcode = CtrlOpcode::Reset;
        QCOMPARE(rst.toWords(), toWords(dev_.value("ctrl_reset").toArray()));
    }

    void acknowledgments() {
        CtrlAckPacket rd;
        rd.code = AckCode::Ok;
        rd.data = toWords(dev_.value("ack_read_data_wire").toArray());
        QCOMPARE(rd.toWords(), toWords(dev_.value("ack_read").toArray()));

        CtrlAckPacket wr;
        wr.code = AckCode::WriteOk;
        QCOMPARE(wr.toWords(), toWords(dev_.value("ack_write").toArray()));
    }

    void streamPacket() {
        StreamPacket s;
        s.stream_id = 1;
        s.tag = 7;
        for (uint32_t i = 0; i < 8; ++i) s.payload.push_back(i);
        QCOMPARE(s.toWords(), toWords(dev_.value("stream").toArray()));
    }
};

QTEST_GUILESS_MAIN(TestGoldenVectors)
#include "test_golden_vectors.moc"
