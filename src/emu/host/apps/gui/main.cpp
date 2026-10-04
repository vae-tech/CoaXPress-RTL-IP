// cxp-gui — Qt Widgets device explorer for the CXP host stack (cxp/gui).
//
//     cxp-gui [--fifo-dir /tmp/cxp] [--log INFO]
//
// * Explorer   — scan FIFO devices (+ an in-process virtual camera), browse
//                the SFNC/GenICam register tree, edit RW/WO features.
// * Statistics — live tree of link counters, refreshed while visible.
// * Image      — the most recently received frame, either the reconstructed
//                video stream or the §8.7 link-test counter pattern.

#include <QApplication>
#include <QCommandLineParser>

#include "cxp/utils/log.h"
#include "main_window.h"

int main(int argc, char** argv) {
    QApplication app(argc, argv);
    QApplication::setApplicationName("cxp-gui");
    QApplication::setApplicationVersion(CXP_VERSION);

    QCommandLineParser p;
    p.setApplicationDescription("CoaXPress host device explorer");
    p.addHelpOption();
    p.addVersionOption();
    QCommandLineOption fifo_dir(QStringList{"fifo-dir"}, "FIFO directory (default /tmp/cxp).", "dir",
                                "/tmp/cxp");
    QCommandLineOption log_level(QStringList{"log"}, "Log level: DEBUG, INFO, WARNING, ERROR.", "level",
                                 "INFO");
    p.addOption(fifo_dir);
    p.addOption(log_level);
    p.process(app);

    cxp::logging::setLevel(cxp::parseLogLevel(p.value(log_level).toStdString()));

    MainWindow win(p.value(fifo_dir));
    win.show();
    return app.exec();
}
