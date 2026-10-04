// Main window: Explorer on the left, tabbed Statistics / Image on the right,
// and two independently hide-able bottom strips (cxp/gui/main_window.py):
//
// * Status — history of the explorer's log() signal.
// * Log    — mirror of the cxp logger output, marshalled onto the GUI
//            thread so records from worker / drain threads are safe.
//
// Tools > Validation opens the validation-plan window, which runs the
// plan's test cases against the explorer's current device.
#pragma once

#include <QLabel>
#include <QMainWindow>

#include "io_bridge.h"

class ExplorerTab;
class ImageTab;
class QAction;
class QFrame;
class QPlainTextEdit;
class QTabWidget;
class QToolButton;
class StatsTab;
class ValidationWindow;

// QLabel that emits clicked() on a left-button release inside it.
class ClickableLabel : public QLabel {
    Q_OBJECT

public:
    using QLabel::QLabel;

signals:
    void clicked();

protected:
    void mouseReleaseEvent(QMouseEvent* ev) override;
};

class MainWindow : public QMainWindow {
    Q_OBJECT

public:
    explicit MainWindow(const QString& fifo_dir, QWidget* parent = nullptr);
    ~MainWindow() override;

protected:
    void closeEvent(QCloseEvent* ev) override;

private:
    struct Panel {
        QFrame* frame;
        QPlainTextEdit* view;
        ClickableLabel* title;
        QToolButton* hide_btn;
    };

    Panel buildPanel(const QString& title, int max_blocks, bool monospace);
    void buildViewMenu();
    void buildToolsMenu();
    void onRightTabChanged();
    void onStatusLog(const QString& msg, const QString& level);
    void onLogLine(const QString& text, int level);
    void teardown();

    IoBridge* bridge_ = nullptr;
    ExplorerTab* explorer_ = nullptr;
    StatsTab* stats_ = nullptr;
    ImageTab* image_ = nullptr;
    ValidationWindow* validation_ = nullptr;
    QTabWidget* right_tabs_ = nullptr;
    Panel status_{};
    Panel log_{};
    QAction* act_status_ = nullptr;
    QAction* act_log_ = nullptr;
    int log_sink_ = 0;
    bool torn_down_ = false;
};
