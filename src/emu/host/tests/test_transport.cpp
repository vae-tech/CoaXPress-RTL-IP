// FIFO transport: framing round-trip and resynchronisation
// (tests/test_transport.py).

#include <QTemporaryDir>
#include <QtTest>

#include <fcntl.h>
#include <unistd.h>

#include "cxp/transport/fifo.h"

using namespace cxp;

class TestTransport : public QObject {
    Q_OBJECT

private slots:
    void encodeFrameLayout() {
        auto f = encodeFrame({0x11223344u});
        QCOMPARE(f.size(), size_t(12));
        const uint8_t magic[] = {'1', 'P', 'X', 'C'};  // b'1PXC' on the wire
        QVERIFY(std::equal(magic, magic + 4, f.begin()));
        QCOMPARE(int(f[4]), 1);
        QCOMPARE(int(f[8]), 0x44);
        QCOMPARE(int(f[11]), 0x11);
    }

    void fifoRoundtripAndResync() {
        QTemporaryDir tmp;
        QVERIFY(tmp.isValid());
        auto [h2c, c2h] = makeFifoPair(tmp.path().toStdString());
        FifoEndpoint a(h2c, c2h, true, "A");
        FifoEndpoint b(c2h, h2c, true, "B");
        a.open();
        b.open();

        a.sendFrame({1, 2, 3});
        Words got;
        QVERIFY(b.recvFrame(got, 2000));
        QVERIFY(got == (Words{1, 2, 3}));

        // Inject garbage ahead of a good frame through the real pipe: B must resync.
        std::vector<uint8_t> raw{0xDE, 0xAD};
        auto good = encodeFrame({0xAA, 0xBB});
        raw.insert(raw.end(), good.begin(), good.end());
        int fd = ::open(h2c.c_str(), O_WRONLY);
        QVERIFY(fd >= 0);
        QCOMPARE(::write(fd, raw.data(), raw.size()), ssize_t(raw.size()));
        ::close(fd);

        QVERIFY(b.recvFrame(got, 2000));
        QVERIFY(got == (Words{0xAA, 0xBB}));
        QVERIFY(b.resyncs >= 1);

        a.close();
        b.close();
    }

    // Character (CXC1) and bench (CXB1) frames share the pipe with CXP1
    // frames: recv() hands over every kind with its magic, recvFrame() only
    // CXP1 frames.
    void frameKinds() {
        QTemporaryDir tmp;
        QVERIFY(tmp.isValid());
        auto [h2c, c2h] = makeFifoPair(tmp.path().toStdString());
        FifoEndpoint a(h2c, c2h, true, "A");
        FifoEndpoint b(c2h, h2c, true, "B");
        a.open();
        b.open();
        a.sendFrame({0x1BC}, 0x43584331u);  // CXC1
        a.sendFrame({7, 8}, 0x43584231u);   // CXB1
        a.sendFrame({9});
        LinkFrame f;
        QVERIFY(b.recv(f, 2000));
        QCOMPARE(f.magic, 0x43584331u);
        QVERIFY(f.words == (Words{0x1BC}));
        QVERIFY(b.recv(f, 2000));
        QCOMPARE(f.magic, 0x43584231u);
        a.sendFrame({0x100}, 0x43584331u);
        a.sendFrame({10});
        Words got;
        QVERIFY(b.recvFrame(got, 2000));
        QVERIFY(got == (Words{9}));
        QVERIFY(b.recvFrame(got, 2000));  // the CXC1 frame in between is skipped
        QVERIFY(got == (Words{10}));
        QCOMPARE(b.resyncs.load(), uint64_t(0));
        a.close();
        b.close();
    }

    void recvTimesOutAndCloseSignals() {
        QTemporaryDir tmp;
        auto [h2c, c2h] = makeFifoPair(tmp.path().toStdString());
        FifoEndpoint a(h2c, c2h, true, "A");
        a.open();
        Words got;
        QVERIFY(!a.recvFrame(got, 50));
        a.close();
        QVERIFY_EXCEPTION_THROWN(a.recvFrame(got, 500), LinkClosed);
        QVERIFY_EXCEPTION_THROWN(a.sendFrame({1}), LinkClosed);
    }
};

QTEST_GUILESS_MAIN(TestTransport)
#include "test_transport.moc"
