// Worker-thread <-> Qt glue (cxp/gui/async_bridge.py).
//
// Every call into the camera stack blocks (register I/O waits on device
// acks), so the GUI ships it to a one-slot worker thread and receives the
// outcome back on the Qt main thread.  One slot keeps device operations
// strictly serialised — a scan and a feature write never interleave.
#pragma once

#include <functional>

#include <QObject>
#include <QString>
#include <QThreadPool>

class IoBridge : public QObject {
    Q_OBJECT

public:
    using Work = std::function<void()>;
    // err is empty on success, else the exception text.
    using Done = std::function<void(const QString& err)>;

    explicit IoBridge(QObject* parent = nullptr);
    ~IoBridge() override;

    // Run `work` on the worker; deliver the result to `done` on the GUI
    // thread, unless `ctx` has been destroyed in the meantime.
    void run(QObject* ctx, Work work, Done done);

    bool waitForDone(int msecs);

private:
    QThreadPool pool_;
};
