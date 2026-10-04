// CRC, replication and packet codec round-trips (tests/test_protocol.py).

#include <QtTest>

#include "cxp/camera/protocol_log.h"
#include "cxp/protocol/crc.h"
#include "cxp/protocol/packets.h"
#include "cxp/utils/log.h"

using namespace cxp;

class TestProtocol : public QObject {
    Q_OBJECT

private slots:
    void replicateMajorityRoundtrip() {
        for (uint8_t b : {0x00, 0x55, 0xAB, 0xFF}) {
            Majority m = majorityByte(replicateByte(b));
            QCOMPARE(m.value, b);
            QVERIFY(m.ok);
        }
    }

    void majorityToleratesOneCorruptLane() {
        Majority m = majorityByte(replicateByte(0x3C) ^ 0x00FF0000u);
        QCOMPARE(m.value, uint8_t(0x3C));
        QVERIFY(m.ok);
    }

    void majorityFailsOnTwoCorruptLanes() {
        QVERIFY(!majorityByte(replicateByte(0x3C) ^ 0x00FFFF00u).ok);
    }

    void crcMatchesZlib() {
        // zlib.crc32(b"123456789") == 0xCBF43926
        const uint8_t s[] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};
        QCOMPARE(crc32Bytes(s, sizeof s), 0xCBF43926u);
        const uint32_t w[] = {0x34333231u, 0x38373635u};
        const uint8_t b[] = {'1', '2', '3', '4', '5', '6', '7', '8'};
        QCOMPARE(crc32Words(w, 2), crc32Bytes(b, sizeof b));
    }

    void crcWireRoundtrip() {
        const uint32_t w[] = {0x11223344u, 0xDEADBEEFu};
        uint32_t crc = crc32Words(w, 2);
        QCOMPARE(wireToCrc(crcToWire(crc)), crc);
    }

    void ctrlCmdRoundtrip_data() {
        QTest::addColumn<int>("op");
        QTest::addColumn<QVector<uint>>("data");
        QTest::newRow("read") << int(CtrlOpcode::Read) << QVector<uint>{};
        QTest::newRow("write") << int(CtrlOpcode::Write) << QVector<uint>{0xCAFEBABE, 0x12345678};
        QTest::newRow("reset") << int(CtrlOpcode::Reset) << QVector<uint>{};
    }

    void ctrlCmdRoundtrip() {
        QFETCH(int, op);
        QFETCH(QVector<uint>, data);
        CtrlCmdPacket cmd;
        cmd.opcode = static_cast<CtrlOpcode>(op);
        cmd.address = reg::CONNECTION_CONFIG;
        cmd.nwords = 2;
        cmd.data.assign(data.begin(), data.end());
        Packet dec = decodePacket(cmd.toWords());
        QVERIFY(std::holds_alternative<CtrlCmdPacket>(dec));
        const auto& d = std::get<CtrlCmdPacket>(dec);
        QCOMPARE(int(d.opcode), op);
        QCOMPARE(d.address, reg::CONNECTION_CONFIG);
        if (cmd.opcode == CtrlOpcode::Write) QVERIFY(d.data == cmd.data);
    }

    void ctrlAckRoundtrip() {
        CtrlAckPacket ack;
        ack.code = AckCode::Ok;
        ack.data = {0xC0A79AE5u};
        const auto d = std::get<CtrlAckPacket>(decodePacket(ack.toWords()));
        QVERIFY(d.code == AckCode::Ok);
        QCOMPARE(d.sizeBytes(), uint32_t(4));
        QVERIFY(d.data == Words{0xC0A79AE5u});
    }

    void ctrlAckShortForm() {
        CtrlAckPacket ack;
        ack.code = AckCode::WriteOk;
        Words w = ack.toWords();
        QCOMPARE(w.size(), size_t(4));
        QVERIFY(std::get<CtrlAckPacket>(decodePacket(w)).code == AckCode::WriteOk);
    }

    void streamRoundtripAndDsize() {
        StreamPacket pkt;
        pkt.tag = 42;
        pkt.payload = {1, 2, 3, 4};
        const auto d = std::get<StreamPacket>(decodePacket(pkt.toWords()));
        QCOMPARE(d.tag, uint8_t(42));
        QVERIFY(d.payload == (Words{1, 2, 3, 4}));
    }

    void eventAndDiscoveryRoundtrip() {
        EventPacket ev;
        ev.event_id = 0x8001;
        ev.timestamp = 0x100000002ull;
        ev.data = {9};
        const auto d = std::get<EventPacket>(decodePacket(ev.toWords()));
        QCOMPARE(d.event_id, uint32_t(0x8001));
        QCOMPARE(d.timestamp, uint64_t(0x100000002ull));

        DiscoveryPacket hb;
        hb.nonce = 99;
        hb.is_heartbeat = true;
        const auto dh = std::get<DiscoveryPacket>(decodePacket(hb.toWords()));
        QVERIFY(dh.is_heartbeat);
        QCOMPARE(dh.nonce, uint32_t(99));
    }

    void linktestDetectsInjectedErrors() {
        LinkTestPacket lt;
        lt.n_data = 64;
        lt.error_indices = {3, 10, 50};
        const auto d = std::get<LinkTestPacket>(decodePacket(lt.toWords()));
        QVERIFY(d.error_indices == (std::vector<uint32_t>{3, 10, 50}));
    }

    void crcCorruptionThrows() {
        StreamPacket pkt;
        pkt.tag = 1;
        pkt.payload = {0xAA, 0xBB};
        pkt.corrupt_crc = true;
        try {
            decodePacket(pkt.toWords());
            QFAIL("expected PacketDecodeError");
        } catch (const PacketDecodeError& e) {
            QCOMPARE(QString::fromStdString(e.reason()), QString("crc"));
        }
    }

    void framingErrors() {
        try {
            decodePacket(Words{0xDEADBEEFu, 0x0, 0x1});
            QFAIL("expected PacketDecodeError");
        } catch (const PacketDecodeError& e) {
            QCOMPARE(QString::fromStdString(e.reason()), QString("no_sop"));
        }
        QCOMPARE(int(PacketType::Stream), 0x01);
    }

    void peekAndFindFrames() {
        StreamPacket pkt;
        pkt.payload = {7};
        pkt.corrupt_crc = true;
        Words f = pkt.toWords();
        QVERIFY(peekType(f) == PacketType::Stream);  // CRC not validated
        Words stream{IDLE_WORD, 0x1234};
        stream.insert(stream.end(), f.begin(), f.end());
        stream.push_back(IDLE_WORD);
        auto frames = findFrames(stream);
        QCOMPARE(frames.size(), size_t(1));
        QVERIFY(frames[0] == f);
    }

    void protocolLogDescribe() {
        CtrlCmdPacket rd;
        rd.address = reg::STREAM_PACKET_SIZE_MAX;
        const std::string cmd = ProtocolLog::describe(rd.toWords());
        const std::string want = strprintf("CTRL_CMD READ addr=0x%08X size=4 crc=ok", rd.address);
        QVERIFY2(cmd.find(want) == 0, cmd.c_str());

        CtrlAckPacket ack;
        ack.data = {bswap32(0xC0A79AE5u)};
        const std::string a = ProtocolLog::describe(ack.toWords());
        QVERIFY2(a.find("CTRL_ACK 0x00 OK size=4 data=[C0A79AE5] crc=ok") == 0, a.c_str());
        CtrlAckPacket wr;
        wr.code = AckCode::WriteOk;
        QCOMPARE(QString::fromStdString(ProtocolLog::describe(wr.toWords())), QString("CTRL_ACK 0x01 WRITE_OK (4 w)"));

        StreamPacket pkt;
        pkt.stream_id = 1;
        pkt.payload = {1, 2, 3};
        bool clean = false;
        std::string st = ProtocolLog::describe(pkt.toWords(), &clean);
        QVERIFY2(st.find("STREAM sid=0x01 tag=0x00 dsizeP=3 payload=3 crc=ok") == 0, st.c_str());
        QVERIFY(clean);
        pkt.corrupt_crc = true;
        st = ProtocolLog::describe(pkt.toWords(), &clean);
        QVERIFY2(st.find("crc=BAD") != std::string::npos, st.c_str());
        QVERIFY(!clean);  // a broken stream packet is dumped word by word

        Words no_eop = rd.toWords();
        no_eop.pop_back();
        st = ProtocolLog::describe(no_eop);
        QVERIFY2(st.find("no-EOP") != std::string::npos, st.c_str());
    }
};

QTEST_GUILESS_MAIN(TestProtocol)
#include "test_protocol.moc"
