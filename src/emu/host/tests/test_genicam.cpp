// SFNC tree parsing, search, and cached register access (tests/test_genicam.py).

#include <QtTest>

#include <map>

#include "cxp/genicam/sfnc.h"
#include "cxp_regmap.hpp"

using namespace cxp;

namespace {

std::vector<uint8_t> be32(uint32_t v) {
    return {uint8_t(v >> 24), uint8_t(v >> 16), uint8_t(v >> 8), uint8_t(v)};
}

class FakeAccessor : public RegisterAccessor {
public:
    FakeAccessor() {
        mem[reg::WIDTH_ALIAS] = be32(1024);
        mem[reg::PIXEL_FORMAT_ALIAS] = be32(0x01080001);  // PFNC Mono8
        std::vector<uint8_t> vendor(reg::DEVICE_VENDOR_NAME_LEN, 0);
        const std::string s = reg::DEVICE_VENDOR_NAME_STR;
        std::copy(s.begin(), s.end(), vendor.begin());
        mem[reg::DEVICE_VENDOR_NAME] = vendor;
    }

    std::vector<uint8_t> read(uint64_t address, size_t length) override {
        ++reads;
        auto it = mem.find(address);
        return it != mem.end() ? it->second : std::vector<uint8_t>(length, 0);
    }

    void write(uint64_t address, const std::vector<uint8_t>& data) override {
        writes.emplace_back(address, data);
        mem[address] = data;
    }

    std::map<uint64_t, std::vector<uint8_t>> mem;
    std::vector<std::pair<uint64_t, std::vector<uint8_t>>> writes;
    int reads = 0;
};

const std::string kXml = CXP_DEFAULT_XML;

}  // namespace

class TestGenicam : public QObject {
    Q_OBJECT

private slots:
    void parseStructure() {
        auto t = NodeTree::fromFile(kXml);
        QCOMPARE(QString::fromStdString(t->model_name), QString(reg::DEVICE_MODEL_NAME_STR));
        QCOMPARE(QString::fromStdString(t->vendor_name), QString(reg::DEVICE_VENDOR_NAME_STR));
        QCOMPARE(QString::fromStdString(t->schema_version), QString("1.1"));
        QVERIFY(t->find("Width")->kind == FeatureKind::Integer);
        QVERIFY(t->find("PixelFormat")->kind == FeatureKind::Enumeration);
        QVERIFY(t->find("AcquisitionStart")->kind == FeatureKind::Command);
        bool found = false;
        for (Feature* c : t->root()->children) found |= c->name == "ImageFormatControl";
        QVERIFY(found);
    }

    void searchAndTreeText() {
        auto t = NodeTree::fromFile(kXml);
        bool hit = false;
        for (Feature* f : t->search("idth")) hit |= f->name == "Width";
        QVERIFY(hit);
        std::string txt = t->treeText();
        QVERIFY(txt.find("Root <Category>") != std::string::npos);
        QVERIFY(txt.find("PixelFormat") != std::string::npos);
    }

    void valueAccessAndCache() {
        auto acc = std::make_shared<FakeAccessor>();
        auto t = NodeTree::fromFile(kXml, acc);
        QCOMPARE(t->find("Width")->getValue().i, int64_t(1024));
        QCOMPARE(t->find("Width")->getValue().i, int64_t(1024));  // cached
        QCOMPARE(acc->reads, 1);
        QCOMPARE(QString::fromStdString(t->find("PixelFormat")->getValue().s), QString("Mono8"));
        QCOMPARE(QString::fromStdString(t->find("DeviceVendorName")->getValue().s),
                 QString(reg::DEVICE_VENDOR_NAME_STR));
    }

    void enumWriteAndInvalidate() {
        auto acc = std::make_shared<FakeAccessor>();
        auto t = NodeTree::fromFile(kXml, acc);
        t->find("PixelFormat")->setValue(Value::ofString("Mono16"));
        QVERIFY(acc->mem[reg::PIXEL_FORMAT_ALIAS] == be32(0x01100007));  // PFNC Mono16
        QCOMPARE(QString::fromStdString(t->find("PixelFormat")->getValue().s), QString("Mono16"));
        QVERIFY_EXCEPTION_THROWN(t->find("PixelFormat")->setValue(Value::ofString("Nope")),
                                 GenIcamError);
    }

    void commandExecutesAndFlushesCache() {
        auto acc = std::make_shared<FakeAccessor>();
        auto t = NodeTree::fromFile(kXml, acc);
        t->find("Width")->getValue();
        t->find("AcquisitionStart")->execute();
        bool wrote = false;
        for (const auto& w : acc->writes) wrote |= (w.first == reg::ACQUISITION_START_ALIAS && w.second == be32(1));
        QVERIFY(wrote);
        t->find("Width")->getValue();
        QCOMPARE(acc->reads, 2);  // cache invalidated by command
    }

    void valueFormatting() {
        QCOMPARE(QString::fromStdString(Value::ofBool(true).str()), QString("True"));
        QCOMPARE(QString::fromStdString(Value::ofFloat(2).str()), QString("2.0"));
        QCOMPARE(QString::fromStdString(Value::ofString("x").repr()), QString("'x'"));
    }
};

QTEST_GUILESS_MAIN(TestGenicam)
#include "test_genicam.moc"
