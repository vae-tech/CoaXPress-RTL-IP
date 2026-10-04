// The GUI validation window runs cases exactly as `cxp validate` does: the
// same per-case protocol logs, frame for frame, and a results.json.
//
// Drives the real ValidationWindow (offscreen) against the in-process
// virtual camera, runs the cxp binary on the same selection against its own
// virtual camera, and compares the two log directories with timestamps and
// measured times masked.

#include <QApplication>
#include <QComboBox>
#include <QDir>
#include <QFileInfo>
#include <QProcess>
#include <QPushButton>
#include <QRegularExpression>
#include <QSettings>
#include <QTemporaryDir>
#include <QTreeWidget>
#include <QtTest>

#include "device_model.h"
#include "validation_window.h"

namespace {

const QStringList kCases = {"CXP-CAM-BOOT-002", "CXP-CAM-NEG-001"};

// A protocol log line without its timestamp, and with measured times
// ("0.42 ms", "(3 ms)") masked; frames and check texts stay as they are.
QStringList normalisedLog(const QString& path) {
    QFile f(path);
    if (!f.open(QIODevice::ReadOnly | QIODevice::Text)) return {"<cannot open " + path + ">"};
    static const QRegularExpression stamp("^\\d\\d:\\d\\d:\\d\\d\\.\\d{3}  ");
    static const QRegularExpression ms("\\d+(\\.\\d+)? ms");
    QStringList out;
    for (QString line : QString::fromUtf8(f.readAll()).split('\n')) {
        line.remove(stamp);
        line.replace(ms, "# ms");
        out << line;
    }
    return out;
}

}  // namespace

class TestGuiValidation : public QObject {
    Q_OBJECT

private slots:
    void initTestCase() {
        QVERIFY(config_.isValid());
        qputenv("XDG_CONFIG_HOME", config_.path().toUtf8());  // keep QSettings off the user's config
        cxp::logging::setLevel(cxp::LogLevel::Error);
    }

    void guiLogsMatchCli() {
        QTemporaryDir tmp;
        const QString gui_root = tmp.filePath("gui_logs");
        QSettings("cxp", "cxp-gui").setValue("validation/log_root", gui_root);

        auto dev = std::make_shared<Device>();
        dev->kind = Device::Kind::Sim;
        dev->label = "Virtual Camera";
        dev->fifo_dir = tmp.filePath("gui_fifo").toStdString();
        QDir().mkpath(QString::fromStdString(dev->fifo_dir));
        openSession(*dev, SESSION_ACK_MS, 3);

        {
            ValidationWindow win;
            win.setDevice(dev);
            auto* tree = win.findChild<QTreeWidget*>();
            QVERIFY(tree);
            tree->clearSelection();  // the window opens with the first case selected
            for (const QString& id : kCases) {
                const auto hits = tree->findItems(id, Qt::MatchExactly | Qt::MatchRecursive, 0);
                QCOMPARE(hits.size(), 1);
                hits.front()->setSelected(true);
            }
            QPushButton* run = nullptr;
            QPushButton* exp = nullptr;
            for (auto* b : win.findChildren<QPushButton*>()) {
                if (b->text().contains("Run selected")) run = b;
                if (b->text().startsWith("Export")) exp = b;
            }
            QVERIFY(run && exp);
            QVERIFY(run->isEnabled());
            run->click();
            QTRY_VERIFY_WITH_TIMEOUT(exp->isEnabled() && run->isEnabled(), 60000);
            win.shutdown();
        }
        closeSession(*dev);

        const QStringList runs = QDir(gui_root).entryList(QDir::Dirs | QDir::NoDotAndDotDot);
        QCOMPARE(runs.size(), 1);
        const QString gui_dir = QDir(gui_root).filePath(runs.front());

        const QString cli_dir = tmp.filePath("cli_logs");
        QProcess cli;
        cli.setProcessChannelMode(QProcess::MergedChannels);
        cli.start(CXP_CLI_BIN, QStringList{"--fifo-dir", tmp.filePath("cli_fifo"), "--log", "ERROR", "validate"} +
                                   kCases + QStringList{"--spawn-sim", "--log-dir", cli_dir});
        QVERIFY(cli.waitForFinished(60000));
        QVERIFY2(cli.exitCode() == 0, cli.readAll().constData());  // both cases PASS on the reference camera

        QStringList gui_files = QDir(gui_dir).entryList(QDir::Files, QDir::Name);
        QStringList cli_files = QDir(cli_dir).entryList(QDir::Files, QDir::Name);
        cli_files.removeAll("connect.log");  // the CLI also logs its own connect
        QCOMPARE(gui_files, (QStringList{"CXP-CAM-BOOT-002.log", "CXP-CAM-NEG-001.log", "results.json"}));
        QCOMPARE(gui_files, cli_files);
        for (const QString& id : kCases) {
            const QStringList g = normalisedLog(QDir(gui_dir).filePath(id + ".log"));
            const QStringList c = normalisedLog(QDir(cli_dir).filePath(id + ".log"));
            QVERIFY2(g.size() > 5, qPrintable(id));
            QCOMPARE(g, c);
        }
    }

    // The list shows all cases, the runnable ones, or the UVM tests' cases.
    void scopeSelector() {
        const auto cat = cxp::validation::loadCatalogue(cxp::validation::defaultCataloguePath());
        size_t runnable = 0, uvm = 0;
        for (const auto& c : cat.cases) {
            runnable += c.emulator.runnable;
            uvm += c.isUvm();
        }
        ValidationWindow win;
        auto* tree = win.findChild<QTreeWidget*>();
        auto* scope = win.findChild<QComboBox*>();
        QVERIFY(tree && scope);
        QCOMPARE(scope->count(), 3);
        auto shown = [&](bool uvm_only) {
            size_t n = 0;
            for (int s = 0; s < tree->topLevelItemCount(); ++s) {
                const QTreeWidgetItem* sec = tree->topLevelItem(s);
                for (int k = 0; k < sec->childCount(); ++k) {
                    const QTreeWidgetItem* it = sec->child(k);
                    if (it->isHidden() || sec->isHidden()) continue;
                    if (uvm_only && !it->text(0).startsWith("UVM-")) return size_t(-1);
                    ++n;
                }
            }
            return n;
        };
        scope->setCurrentIndex(scope->findText("All"));
        QCOMPARE(shown(false), cat.cases.size());
        scope->setCurrentIndex(scope->findText("Runnable"));
        QCOMPARE(shown(false), runnable);
        scope->setCurrentIndex(scope->findText("UVM based"));
        QCOMPARE(shown(true), uvm);
        QCOMPARE(uvm, size_t(31));  // one per class in src/verif/uvm/tests/all_tests.py
    }

private:
    QTemporaryDir config_;
};

QTEST_MAIN(TestGuiValidation)
#include "test_gui_validation.moc"
