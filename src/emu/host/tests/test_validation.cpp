// Validation catalogue, wire decoders and a few checks run against the
// in-process virtual camera.

#include <QDir>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTemporaryDir>
#include <QtTest>
#include <zlib.h>

#include <fstream>
#include <iterator>
#include <set>

#include "cxp/camera/client.h"
#include "cxp/camera/session.h"
#include "cxp/image/reconstruct.h"
#include "cxp/protocol/crc.h"
#include "cxp/sim/virtual_camera.h"
#include "cxp/validation/campaign.h"
#include "cxp/validation/cases/gen/_helpers.h"
#include "cxp/validation/runner.h"

using namespace cxp;
using namespace cxp::validation;

namespace {

std::unique_ptr<HostSession> bringUp(const QTemporaryDir& tmp) {
    SessionConfig cfg;
    cfg.fifo_dir = tmp.path().toStdString();
    cfg.spawn_vcam = true;
    auto rig = std::make_unique<HostSession>(cfg);
    rig->connect();
    rig->startDrain();
    return rig;
}

CaseResult run(const Catalogue& cat, HostSession& rig, const char* id) {
    const CaseDef* def = cat.find(id);
    if (!def) throw std::runtime_error(std::string("no case ") + id);
    std::atomic<bool> cancel{false};
    return runCase(*def, rig.host(), cancel);
}

// A one-member zip (local header, data, central directory, end record) with
// the member STOREd (method 0) or raw-DEFLATEd (method 8).
std::vector<uint8_t> makeZip(const std::string& name, const std::string& body, int method) {
    std::vector<uint8_t> data(body.begin(), body.end());
    if (method == 8) {
        z_stream zs{};
        deflateInit2(&zs, Z_BEST_COMPRESSION, Z_DEFLATED, -15, 8, Z_DEFAULT_STRATEGY);
        std::vector<uint8_t> out(deflateBound(&zs, uLong(body.size())));
        zs.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(body.data()));
        zs.avail_in = uInt(body.size());
        zs.next_out = out.data();
        zs.avail_out = uInt(out.size());
        deflate(&zs, Z_FINISH);
        out.resize(zs.total_out);
        deflateEnd(&zs);
        data = out;
    }
    const uint32_t crc = uint32_t(crc32(0, reinterpret_cast<const Bytef*>(body.data()), uInt(body.size())));
    std::vector<uint8_t> z;
    auto u16 = [&](uint32_t v) { z.push_back(uint8_t(v)); z.push_back(uint8_t(v >> 8)); };
    auto u32 = [&](uint32_t v) { u16(v & 0xFFFF); u16(v >> 16); };
    auto entry = [&](uint32_t sig, bool central) {
        u32(sig);
        if (central) u16(20);  // version made by
        u16(20); u16(0); u16(uint32_t(method)); u16(0); u16(0);
        u32(crc); u32(uint32_t(data.size())); u32(uint32_t(body.size()));
        u16(uint32_t(name.size())); u16(0);
        if (central) { u16(0); u16(0); u16(0); u32(0); u32(0); }  // comment, disk, attrs, local offset 0
        z.insert(z.end(), name.begin(), name.end());
    };
    entry(0x04034b50, false);
    z.insert(z.end(), data.begin(), data.end());
    const uint32_t cd = uint32_t(z.size());
    entry(0x02014b50, true);
    const uint32_t cd_len = uint32_t(z.size()) - cd;
    u32(0x06054b50); u16(0); u16(0); u16(1); u16(1); u32(cd_len); u32(cd); u16(0);
    return z;
}

}  // namespace

class TestValidation : public QObject {
    Q_OBJECT

private slots:
    void initTestCase() { logging::setLevel(LogLevel::Error); }

    void catalogueMatchesRegistry() {
        const Catalogue cat = loadCatalogue(defaultCataloguePath());
        // The counts come from the catalogue's own summary block, so adding a
        // case changes no number here.
        QFile f(QString::fromStdString(defaultCataloguePath()));
        QVERIFY(f.open(QIODevice::ReadOnly));
        const QJsonObject summary = QJsonDocument::fromJson(f.readAll()).object().value("summary").toObject();
        QVERIFY(!summary.isEmpty());
        QCOMPARE(int(cat.cases.size()), summary.value("cases").toInt());
        std::set<std::string> ids, runnable;
        size_t uvm = 0;
        for (const auto& c : cat.cases) {
            QVERIFY2(ids.insert(c.id).second, c.id.c_str());
            if (c.isUvm()) {
                ++uvm;
                QCOMPARE(c.id, "UVM-" + c.uvm.test);
                QVERIFY2(!c.uvm.source.empty() && !c.uvm.tiers.empty() && !c.objective.empty(), c.id.c_str());
            } else {
                QVERIFY2(!c.pass_criteria.empty() && !c.fail_criteria.empty(), c.id.c_str());
            }
            if (c.emulator.runnable) {
                runnable.insert(c.id);
                QVERIFY2(!c.emulator.pass_criteria.empty() && !c.emulator.fail_criteria.empty() &&
                             !c.emulator.procedure.empty(),
                         c.id.c_str());
            } else {
                QVERIFY2(!c.emulator.reason.empty(), c.id.c_str());
            }
        }
        QCOMPARE(int(uvm), summary.value("uvm_tests").toInt());
        QCOMPARE(int(runnable.size()), summary.value("runnable_on_emulator").toInt());
        // A plan case names only UVM tests the catalogue holds.
        for (const auto& c : cat.cases) {
            for (const auto& t : c.uvm_tests) QVERIFY2(cat.find("UVM-" + t), (c.id + " -> " + t).c_str());
        }
        // Checks register themselves from static initialisers; a linker that
        // dropped a check's object file would leave its case unimplemented.
        std::set<std::string> implemented;
        for (const auto& [id, check] : checkRegistry()) {
            implemented.insert(id);
            QCOMPARE(QFileInfo(QString::fromStdString(check.source)).completeBaseName().toStdString(), id);
        }
        QCOMPARE(implemented, runnable);
    }

    void buildCmdMatchesHostCodec() {
        CtrlCmdPacket rd;
        rd.opcode = CtrlOpcode::Read;
        rd.address = reg::DEVICE_VENDOR_NAME;
        rd.nwords = 3;
        QCOMPARE(readCmd(reg::DEVICE_VENDOR_NAME, 12), rd.toWords());
        CtrlCmdPacket wr;
        wr.opcode = CtrlOpcode::Write;
        wr.address = reg::MASTER_HOST_CONNECTION_ID;
        wr.data = {bswap32(0x11223344)};
        QCOMPARE(writeCmd(reg::MASTER_HOST_CONNECTION_ID, {0x11223344}), wr.toWords());
    }

    void decodeAckForms() {
        CtrlAckPacket longAck;
        longAck.code = AckCode::Ok;
        longAck.data = {bswap32(0xC0A79AE5)};
        RawAck a = decodeAck(longAck.toWords());
        QVERIFY(a.ok());
        QCOMPARE(a.code, 0x00);
        QVERIFY(a.long_form);
        QCOMPARE(a.size_field, 4u);
        QCOMPARE(a.values().front(), 0xC0A79AE5u);

        CtrlAckPacket shortAck;
        shortAck.code = AckCode::WriteOk;
        a = decodeAck(shortAck.toWords());
        QVERIFY(a.ok());
        QCOMPARE(a.code, 0x01);
        QVERIFY(!a.long_form);
        QCOMPARE(a.n_words, size_t(4));

        longAck.corrupt_crc = true;
        a = decodeAck(longAck.toWords());
        QVERIFY(!a.crc_ok);
        QVERIFY(!a.ok());
    }

    void walkImagesAcrossPackets() {
        // 5x3 Mono8: 2 words per line, chopped into 3-word packets so markers straddle.
        Words stream = cxpImageHeaderRect(7, 0x1234, 5, 1, 3, 2, 2, 0x0101);
        for (int y = 0; y < 3; ++y) {
            Words lm = cxpLineMarkerRect();
            stream.insert(stream.end(), lm.begin(), lm.end());
            stream.push_back(0x03020100u + uint32_t(y));
            stream.push_back(0x00000004u);
        }
        std::vector<Captured> cap;
        uint8_t tag = 0;
        for (size_t i = 0; i < stream.size(); i += 3) {
            StreamPacket p;
            p.stream_id = 7;
            p.tag = tag++;
            p.payload.assign(stream.begin() + i, stream.begin() + std::min(stream.size(), i + 3));
            cap.push_back({double(i), p.toWords()});
        }
        auto pk = streamPackets(cap);
        QCOMPARE(pk.size(), cap.size());
        for (const auto& p : pk) QVERIFY(p.defects.empty());
        auto ims = walkImages(pk);
        QCOMPARE(ims.size(), size_t(1));
        const ImageRec& im = ims.front();
        QCOMPARE(im.stream_id, 7u);
        QCOMPARE(im.source_tag, 0x1234u);
        QCOMPARE(im.xsize, 5u);
        QCOMPARE(im.xoffs, 1u);
        QCOMPARE(im.ysize, 3u);
        QCOMPARE(im.yoffs, 2u);
        QCOMPARE(im.pixel_f, 0x0101u);
        QCOMPARE(im.lines.size(), size_t(3));
        for (size_t w : im.line_words) QCOMPARE(w, size_t(2));
    }

    void checksAgainstVirtualCamera() {
        const Catalogue cat = loadCatalogue(defaultCataloguePath());
        QTemporaryDir tmp;
        auto rig = bringUp(tmp);
        // CRC-corrupted writes are NACKed 0x80 and never executed.
        CaseResult r = run(cat, *rig, "CXP-CAM-NEG-001");
        QCOMPARE(verdictName(r.verdict), "PASS");
        // The reference camera serves the map's bootstrap values (Revision 0x00010001).
        r = run(cat, *rig, "CXP-CAM-BOOT-002");
        QCOMPARE(verdictName(r.verdict), "PASS");
        // A non-runnable case never touches the device.
        r = run(cat, *rig, "CXP-CAM-INIT-003");
        QCOMPARE(verdictName(r.verdict), "NOT RUN");
        QVERIFY(r.log.empty());
        // Streaming checks complete (verdict aside) and leave the device usable.
        r = run(cat, *rig, "CXP-CAM-DATA-001");
        QVERIFY2(r.verdict == Verdict::Pass || r.verdict == Verdict::Fail, r.summary.c_str());
        QCOMPARE(rig->host()->readReg(Bootstrap::STANDARD)[0], CXP_MAGIC);
    }

    void cancelStopsARun() {
        const Catalogue cat = loadCatalogue(defaultCataloguePath());
        QTemporaryDir tmp;
        auto rig = bringUp(tmp);
        std::atomic<bool> cancel{true};
        CaseResult r = runCase(*cat.find("CXP-CAM-NEG-004"), rig->host(), cancel);
        QCOMPARE(verdictName(r.verdict), "STOPPED");
    }

    // Two contexts on one session both see the downlink; closing one leaves
    // the other's tap in place.
    void rxTapsCoexist() {
        QTemporaryDir tmp;
        auto rig = bringUp(tmp);
        std::atomic<bool> cancel{false};
        auto outer = std::make_unique<Context>(rig->host(), cancel, Context::Sink());
        {
            Context inner(rig->host(), cancel, Context::Sink());
            QVERIFY(inner.tryRd32(Reg::STANDARD).has_value());
            QVERIFY(outer->tryRd32(Reg::STANDARD).has_value());
        }
        QVERIFY(outer->tryRd32(Reg::STANDARD).has_value());
    }

    // A campaign writes one protocol log per case and results.json, reports
    // through its observer in order, and leaves no protocol log installed
    // on a session that had none.
    void campaignLogsAndResults() {
        const Catalogue cat = loadCatalogue(defaultCataloguePath());
        QTemporaryDir tmp;
        auto rig = bringUp(tmp);
        QVERIFY(!rig->host()->protocolLog());
        CampaignConfig cfg;
        cfg.log_dir = makeRunLogDir(tmp.filePath("logs").toStdString());
        struct Trace : CampaignObserver {
            std::vector<std::string> ev;
            void caseStarted(size_t i, const CaseDef& c) override { ev.push_back("start " + c.id); }
            void caseDone(size_t i, const CaseDef& c, const CaseResult& r) override {
                ev.push_back(std::string("done ") + c.id + " " + verdictName(r.verdict));
            }
            void finished(const CampaignSummary&) override { ev.push_back("finished"); }
        } trace;
        Campaign campaign(cat, {cat.find("CXP-CAM-NEG-001"), cat.find("CXP-CAM-INIT-003")}, cfg);
        std::atomic<bool> cancel{false};
        const CampaignSummary sum = campaign.run(rig->host(), cancel, &trace);
        QCOMPARE(trace.ev, (std::vector<std::string>{"start CXP-CAM-NEG-001", "done CXP-CAM-NEG-001 PASS",
                                                     "start CXP-CAM-INIT-003", "done CXP-CAM-INIT-003 NOT RUN",
                                                     "finished"}));
        QCOMPARE(sum.failures(), 0);
        QVERIFY(sum.dead_after.empty());
        QVERIFY(!sum.stopped);
        QCOMPARE(QString::fromStdString(sum.results_path), QString::fromStdString(cfg.log_dir + "/results.json"));
        QVERIFY(QFileInfo::exists(QString::fromStdString(sum.results_path)));
        std::ifstream f(cfg.log_dir + "/CXP-CAM-NEG-001.log");
        const std::string log{std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>()};
        QVERIFY(log.find("CTRL_CMD WRITE") != std::string::npos);  // the frames themselves
        QVERIFY(log.find("verdict PASS") != std::string::npos);
        QVERIFY(QFileInfo::exists(QString::fromStdString(cfg.log_dir + "/CXP-CAM-INIT-003.log")));
        QVERIFY(!rig->host()->protocolLog());
    }

    // Table 15/16/17 characters and the short-packet decoder.
    void charsCodec() {
        const Chars rise = lsTrigger(true, 51);
        QCOMPARE(rise.size(), size_t(6));
        QVERIFY(rise[0] == (Char{K28_2, true}) && rise[1] == (Char{K28_4, true}) && rise[2] == (Char{K28_4, true}));
        QVERIFY(rise[3] == (Char{51, false}) && rise[5] == (Char{51, false}));
        const Chars fall = lsTrigger(false, 0);
        QVERIFY(fall[0] == (Char{K28_4, true}) && fall[1] == (Char{K28_2, true}));
        QCOMPARE(wordsToChars(charsToWords(rise)), rise);

        ShortPacket p = decodeShortPacket(hsTrigger(true, 2));
        QVERIFY(p.kind == ShortPacket::Kind::TriggerRise && p.value == 2 && p.clean);
        p = decodeShortPacket(ioAck());
        QVERIFY(p.kind == ShortPacket::Kind::IoAck && p.value == IOACK_OK && p.clean);
        Chars bent = hsTrigger(false, 0);
        bent[1].v = K28_4;  // one replica wrong: still a falling trigger by 3 of 4
        p = decodeShortPacket(bent);
        QVERIFY(p.kind == ShortPacket::Kind::TriggerFall && !p.clean);

        // A CXP1 frame's delimiters are K characters, its body data.
        const Chars cmd = frameChars(readCmd(Reg::STANDARD, 4));
        QCOMPARE(cmd.size(), size_t(24));
        for (size_t i = 0; i < 4; ++i) QVERIFY(cmd[i].k && cmd[cmd.size() - 1 - i].k);
        for (size_t i = 4; i + 4 < cmd.size(); ++i) QVERIFY(!cmd[i].k);
        const Chars ins = insertAt(cmd, rise, 12);
        QCOMPARE(ins.size(), size_t(30));
        QVERIFY(ins[12] == rise[0] && ins[18] == cmd[12]);
    }

    // What UVM drives besides the link, the virtual camera's bench carries
    // out: these UVM cases pass against it; a case that needs a bench it
    // does not have (a host off frequency) is not run.
    void benchAgainstVirtualCamera() {
        const Catalogue cat = loadCatalogue(defaultCataloguePath());
        QTemporaryDir tmp;
        auto rig = bringUp(tmp);
        QVERIFY(rig->host()->benchCaps() & bench::CAP_CHARS);
        for (const char* id : {"UVM-test_io_ack", "UVM-test_trigger_uplink", "UVM-test_trigger_in_ctrl_packet",
                               "UVM-test_tx_trigger", "UVM-test_arbitrary_image", "UVM-test_pslverr_burst",
                               "UVM-test_router_reject", "UVM-test_xifc_stream_trigger"}) {
            const CaseResult r = run(cat, *rig, id);
            QVERIFY2(r.verdict == Verdict::Pass, (std::string(id) + ": " + r.summary).c_str());
        }
        const CaseResult r = run(cat, *rig, "CXP-EMU-REC-109");
        QCOMPARE(verdictName(r.verdict), "NOT RUN");
        QCOMPARE(rig->host()->readReg(Bootstrap::STANDARD)[0], CXP_MAGIC);

        // RESET: the inputs as asked, registers at power-on; the inputs go
        // back when the case ends.
        std::atomic<bool> cancel{false};
        Context outer(rig->host(), cancel, {});
        const auto before = outer.benchPins();
        QVERIFY(before.has_value());
        {
            Context ctx(rig->host(), cancel, {});
            ctx.wr32(Reg::MASTER_HOST_CONNECTION_ID, 0xA5A5A5A5);
            QVERIFY(ctx.benchReset(1u << (bench::TPG_RUN - 1)));
            QCOMPARE(ctx.benchPins().value_or(~0u), 1u << (bench::TPG_RUN - 1));
            QCOMPARE(ctx.rd32(Reg::MASTER_HOST_CONNECTION_ID), 0u);
            ctx.runCleanup();
        }
        QCOMPARE(outer.benchPins().value_or(~0u), *before);
    }

    // GEN-001's unzip: STORE and DEFLATE members come back byte for byte; a
    // corrupted CRC, an unknown method or a zip with no XML member is an error.
    void unzipXmlMember() {
        using checks::gen::unzipXml;
        std::string body = "<?xml version=\"1.0\"?>\n<RegisterDescription MajorVersion=\"1\">";
        for (int i = 0; i < 200; ++i) body += "<Integer Name=\"R" + std::to_string(i) + "\"/>";
        body += "</RegisterDescription>\n";
        for (int method : {0, 8}) {
            const auto z = makeZip("cxp_camera.xml", body, method);
            const auto r = unzipXml(z);
            QVERIFY2(r.error.empty(), r.error.c_str());
            QCOMPARE(r.member, std::string("cxp_camera.xml"));
            QCOMPARE(std::string(r.data.begin(), r.data.end()), body);
            if (method == 8) QVERIFY(z.size() < body.size());
            auto bad = z;
            bad[14] ^= 0x01;  // local header CRC-32
            const size_t cd = bad.size() - 22 - (46 + 14);
            bad[cd + 16] ^= 0x01;  // central directory CRC-32
            QVERIFY(!unzipXml(bad).error.empty());
        }
        auto odd = makeZip("cxp_camera.xml", body, 0);
        odd[8] = 12;  // bzip2 in the local header
        odd[odd.size() - 22 - (46 + 14) + 10] = 12;  // and in the central directory
        QVERIFY(!unzipXml(odd).error.empty());
        QVERIFY(!unzipXml(makeZip("readme.txt", body, 0)).error.empty());
        QVERIFY(!unzipXml(std::vector<uint8_t>(body.begin(), body.end())).error.empty());
    }

    // Run directories named by makeRunLogDir are pruned to the newest `keep`;
    // anything else in the root is left alone.
    void runLogDirPruning() {
        QTemporaryDir tmp;
        QDir root(tmp.path());
        for (int d = 1; d <= 12; ++d) root.mkdir(QString("200001%1_120000").arg(d, 2, 10, QChar('0')));
        root.mkdir("keep_me");
        const std::string dir = makeRunLogDir("", tmp.path().toStdString(), 5);
        QVERIFY(QFileInfo(QString::fromStdString(dir)).isDir());
        const QStringList left = root.entryList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name);
        QCOMPARE(left.size(), 6);  // 5 newest run directories + keep_me
        QVERIFY(left.contains("keep_me"));
        QVERIFY(!left.contains("20000108_120000"));
        QVERIFY(left.contains("20000112_120000"));
        const std::string again = makeRunLogDir("", tmp.path().toStdString(), 5);
        QVERIFY(again != dir);  // same second: a _2 suffix, never the same directory
    }
};

QTEST_GUILESS_MAIN(TestValidation)
#include "test_validation.moc"
