#include "main_window.h"

#include <QAction>
#include <QCloseEvent>
#include <QFontDatabase>
#include <QFrame>
#include <QHBoxLayout>
#include <QMenuBar>
#include <QMouseEvent>
#include <QPlainTextEdit>
#include <QPointer>
#include <QSplitter>
#include <QTabWidget>
#include <QToolButton>
#include <QVBoxLayout>

#include "cxp/utils/log.h"
#include "explorer_tab.h"
#include "image_tab.h"
#include "stats_tab.h"
#include "validation_window.h"

void ClickableLabel::mouseReleaseEvent(QMouseEvent* ev) {
    if (ev->button() == Qt::LeftButton && rect().contains(ev->pos())) emit clicked();
    QLabel::mouseReleaseEvent(ev);
}

MainWindow::MainWindow(const QString& fifo_dir, QWidget* parent) : QMainWindow(parent) {
    setWindowTitle("CoaXPress Host — Device Explorer");
    resize(1480, 800);

    bridge_ = new IoBridge(this);
    explorer_ = new ExplorerTab(fifo_dir, bridge_);
    stats_ = new StatsTab();
    image_ = new ImageTab();
    right_tabs_ = new QTabWidget();
    right_tabs_->addTab(stats_, "Statistics");
    right_tabs_->addTab(image_, "Image");
    connect(right_tabs_, &QTabWidget::currentChanged, this, [this](int) { onRightTabChanged(); });

    connect(explorer_, &ExplorerTab::deviceChanged, stats_, &StatsTab::setDevice);
    connect(explorer_, &ExplorerTab::deviceChanged, image_, &ImageTab::setDevice);
    connect(explorer_, &ExplorerTab::log, this, &MainWindow::onStatusLog);

    validation_ = new ValidationWindow(this);
    connect(explorer_, &ExplorerTab::deviceChanged, validation_, &ValidationWindow::setDevice);

    auto* split = new QSplitter(Qt::Horizontal);
    split->addWidget(explorer_);
    split->addWidget(right_tabs_);
    split->setStretchFactor(0, 3);
    split->setStretchFactor(1, 2);
    split->setChildrenCollapsible(false);

    auto* central = new QWidget();
    auto* lay = new QVBoxLayout(central);
    lay->setContentsMargins(6, 6, 6, 6);
    lay->addWidget(split, 1);

    status_ = buildPanel("Status", 2000, false);
    log_ = buildPanel("Log", 5000, true);
    lay->addWidget(status_.frame);
    lay->addWidget(log_.frame);
    setCentralWidget(central);
    buildViewMenu();
    buildToolsMenu();

    // Mirror the cxp logger into the Log pane (stderr output is unchanged).
    QPointer<MainWindow> self(this);
    log_sink_ = cxp::logging::addSink([self](const std::string& line, cxp::LogLevel lvl) {
        if (!self) return;
        const QString text = QString::fromStdString(line);
        const int level = static_cast<int>(lvl);
        QMetaObject::invokeMethod(
            self.data(), [self, text, level] { if (self) self->onLogLine(text, level); },
            Qt::QueuedConnection);
    });

    onRightTabChanged();
}

MainWindow::~MainWindow() { teardown(); }

void MainWindow::onRightTabChanged() {
    QWidget* cur = right_tabs_->currentWidget();
    stats_->setActive(cur == stats_);
    image_->setActive(cur == image_);
}

MainWindow::Panel MainWindow::buildPanel(const QString& title, int max_blocks, bool monospace) {
    Panel p{};
    p.frame = new QFrame();
    p.frame->setFrameShape(QFrame::StyledPanel);
    auto* outer = new QVBoxLayout(p.frame);
    outer->setContentsMargins(4, 2, 4, 4);
    outer->setSpacing(2);

    auto* header = new QHBoxLayout();
    header->setContentsMargins(0, 0, 0, 0);
    p.title = new ClickableLabel("<b>" + title + "</b>");
    p.title->setCursor(Qt::PointingHandCursor);
    p.title->setToolTip("Click to hide " + title + " pane");
    p.hide_btn = new QToolButton();
    p.hide_btn->setText("✕");
    p.hide_btn->setToolTip("Hide " + title + " pane");
    p.hide_btn->setAutoRaise(true);
    p.hide_btn->setCursor(Qt::PointingHandCursor);
    header->addWidget(p.title);
    header->addStretch(1);
    header->addWidget(p.hide_btn);
    outer->addLayout(header);

    p.view = new QPlainTextEdit();
    p.view->setReadOnly(true);
    p.view->setMaximumBlockCount(max_blocks);
    if (monospace) p.view->setFont(QFontDatabase::systemFont(QFontDatabase::FixedFont));
    outer->addWidget(p.view);
    return p;
}

void MainWindow::buildViewMenu() {
    QMenu* view = menuBar()->addMenu("&View");
    auto wire = [this, view](QAction*& act, const QString& text, const QString& shortcut,
                             const Panel& panel) {
        act = new QAction(text, this);
        act->setCheckable(true);
        act->setChecked(true);
        act->setShortcut(QKeySequence(shortcut));
        connect(act, &QAction::toggled, panel.frame, &QWidget::setVisible);
        // Title label + ✕ button both toggle the action so the menu checkmark
        // and the header affordances stay in sync.
        connect(panel.title, &ClickableLabel::clicked, act, &QAction::toggle);
        connect(panel.hide_btn, &QToolButton::clicked, act, &QAction::toggle);
        view->addAction(act);
    };
    wire(act_status_, "&Status pane", "Ctrl+Alt+S", status_);
    wire(act_log_, "&Log pane", "Ctrl+L", log_);
}

void MainWindow::buildToolsMenu() {
    QMenu* tools = menuBar()->addMenu("&Tools");
    QAction* act = tools->addAction("&Validation plan…");
    act->setShortcut(QKeySequence("Ctrl+Shift+V"));
    act->setStatusTip("Browse the validation-plan test cases and run them against the current device");
    connect(act, &QAction::triggered, this, [this] {
        validation_->show();
        validation_->raise();
        validation_->activateWindow();
    });
}

void MainWindow::onStatusLog(const QString& msg, const QString& level) {
    const QString color = level == "ok" ? "#2e7d32" : level == "err" ? "#c62828" : QString();
    if (color.isEmpty()) {
        status_.view->appendPlainText(msg);
    } else {
        status_.view->appendHtml(QString("<span style=\"color:%1;\">%2</span>")
                                     .arg(color, msg.toHtmlEscaped()));
    }
}

void MainWindow::onLogLine(const QString& text, int level) {
    QString color;
    if (level >= static_cast<int>(cxp::LogLevel::Error)) {
        color = "#c62828";
    } else if (level >= static_cast<int>(cxp::LogLevel::Warning)) {
        color = "#ef6c00";
    }
    if (color.isEmpty()) {
        log_.view->appendPlainText(text);
    } else {
        // <pre> keeps the formatter's column alignment in the monospace font.
        log_.view->appendHtml(QString("<pre style=\"color:%1;margin:0;\">%2</pre>")
                                  .arg(color, text.toHtmlEscaped()));
    }
}

void MainWindow::closeEvent(QCloseEvent* ev) {
    teardown();
    ev->accept();
}

void MainWindow::teardown() {
    if (torn_down_) return;
    torn_down_ = true;
    // Detach the GUI log sink first so teardown-time records cannot race.
    if (log_sink_) cxp::logging::removeSink(log_sink_);
    log_sink_ = 0;
    stats_->setActive(false);
    image_->setActive(false);
    validation_->shutdown();  // before the sessions it runs on close
    validation_->close();
    explorer_->teardown();
}
