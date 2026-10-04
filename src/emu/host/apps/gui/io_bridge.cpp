#include "io_bridge.h"

#include <exception>

#include <QMetaObject>
#include <QPointer>

IoBridge::IoBridge(QObject* parent) : QObject(parent) {
    pool_.setMaxThreadCount(1);
    pool_.setExpiryTimeout(-1);
}

IoBridge::~IoBridge() { pool_.waitForDone(); }

void IoBridge::run(QObject* ctx, Work work, Done done) {
    QPointer<QObject> guard(ctx);
    pool_.start([this, guard, work = std::move(work), done = std::move(done)]() {
        QString err;
        try {
            work();
        } catch (const std::exception& e) {
            err = QString::fromStdString(e.what());
            if (err.isEmpty()) err = QStringLiteral("error");
        } catch (...) {
            err = QStringLiteral("unknown error");
        }
        QMetaObject::invokeMethod(
            this,
            [guard, done, err]() {
                if (guard && done) done(err);
            },
            Qt::QueuedConnection);
    });
}

bool IoBridge::waitForDone(int msecs) { return pool_.waitForDone(msecs); }
