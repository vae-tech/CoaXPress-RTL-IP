#include "validation_window.h"

#include <algorithm>
#include <fstream>
#include <functional>
#include <set>

#include <QCloseEvent>
#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDir>
#include <QDoubleSpinBox>
#include <QFileDialog>
#include <QFileInfo>
#include <QFormLayout>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QLabel>
#include <QLineEdit>
#include <QMessageBox>
#include <QPointer>
#include <QProgressBar>
#include <QPushButton>
#include <QScrollBar>
#include <QSettings>
#include <QSpinBox>
#include <QSplitter>
#include <QTextBrowser>
#include <QTreeWidget>
#include <QVBoxLayout>

using namespace cxp::validation;

namespace {

enum Col { C_ID, C_TITLE, C_RUNS, C_VERDICT };
constexpr int kIndexRole = Qt::UserRole + 1;

// Which cases the list shows.
enum Scope { S_ALL, S_RUNNABLE, S_UVM };
const char* kScopeKey = "validation/scope";

const char* kPass = "#2e7d32";
const char* kFail = "#c62828";
const char* kWarn = "#ef6c00";
const char* kMuted = "#757575";

QString esc(const std::string& s) { return QString::fromStdString(s).toHtmlEscaped(); }

const char* verdictColor(Verdict v) {
    switch (v) {
    case Verdict::Pass: return kPass;
    case Verdict::Fail: return kFail;
    case Verdict::Error: return kWarn;
    default: return kMuted;
    }
}

QString lineHtml(const LogLine& l) {
    const char* color = nullptr;
    const char* tag = "";
    switch (l.kind) {
    case LineKind::Pass: color = kPass; tag = "PASS "; break;
    case LineKind::Fail: color = kFail; tag = "FAIL "; break;
    case LineKind::Warn: color = kWarn; tag = "WARN "; break;
    case LineKind::Note: color = kMuted; tag = "NOTE "; break;
    case LineKind::Info: break;
    }
    QString body = QString("<b>%1</b>%2").arg(tag, esc(l.text));
    if (!color) return QString("<div>%1</div>").arg(esc(l.text));
    return QString("<div style=\"color:%1;\">%2</div>").arg(color, body);
}

QString section(const QString& title, const QString& body) {
    if (body.trimmed().isEmpty()) return {};
    return QString("<h3>%1</h3>%2").arg(title, body);
}

QString para(const std::string& s) { return s.empty() ? QString() : QString("<p>%1</p>").arg(esc(s)); }

QString list(const std::vector<std::string>& v, bool ordered) {
    if (v.empty()) return {};
    QString out = ordered ? "<ol>" : "<ul>";
    for (const auto& s : v) out += "<li>" + esc(s) + "</li>";
    return out + (ordered ? "</ol>" : "</ul>");
}

QString criteria(const char* color, const char* label, const std::string& text) {
    if (text.empty()) return {};
    return QString("<table width=\"100%\" cellpadding=\"6\" style=\"border-left:4px solid %1;\"><tr><td>"
                   "<b style=\"color:%1;\">%2</b><br>%3</td></tr></table>")
        .arg(color, label, esc(text));
}

// Pop-up help of one run option: what it does, then its flag, default and range.
QString optionHelp(const OptionSpec& s) {
    const Options dflt;
    const QString unit = *s.unit && *s.unit != 'x' ? QString(" ") + s.unit : QString();
    const QString x = *s.unit == 'x' ? "x" : "";
    return QString("<p><b>%1</b> &mdash; %2</p><p>%3</p><p><tt>%4</tt> &middot; default %5%6%7 &middot; "
                   "range %5%8&ndash;%5%9%7</p>")
        .arg(esc(s.label), esc(s.help), esc(s.details), esc(s.flag), x,
             QString::number(s.get(dflt), 'f', s.decimals), unit, QString::number(s.min, 'f', s.decimals),
             QString::number(s.max, 'f', s.decimals));
}

}  // namespace

ValidationWindow::ValidationWindow(QWidget* parent) : QWidget(parent, Qt::Window) {
    setWindowTitle("CoaXPress Validation Plan");
    resize(1320, 820);

    // -- header --------------------------------------------------------------
    device_lbl_ = new QLabel();
    cat_lbl_ = new QLabel();
    cat_lbl_->setTextInteractionFlags(Qt::TextSelectableByMouse);
    auto* open_btn = new QPushButton("Open catalogue…");
    open_btn->setToolTip("Load another validation-case JSON file");
    connect(open_btn, &QPushButton::clicked, this, &ValidationWindow::onOpenCatalogue);
    auto* header = new QHBoxLayout();
    header->addWidget(device_lbl_, 1);
    header->addWidget(cat_lbl_);
    header->addWidget(open_btn);

    // -- case list -------------------------------------------------------------
    filter_ = new QLineEdit();
    filter_->setPlaceholderText("Filter by ID or title");
    filter_->setClearButtonEnabled(true);
    scope_ = new QComboBox();
    scope_->addItem("All", S_ALL);
    scope_->setItemData(S_ALL, "Every case: the validation plan and the UVM tests", Qt::ToolTipRole);
    scope_->addItem("Runnable", S_RUNNABLE);
    scope_->setItemData(S_RUNNABLE, "Only cases the emulator runs (no hardware, trigger or multi-link cases)",
                        Qt::ToolTipRole);
    scope_->addItem("UVM based", S_UVM);
    scope_->setItemData(S_UVM, "The PyUVM tests of src/verif/, reproduced over the emulator link", Qt::ToolTipRole);
    scope_->setCurrentIndex(std::clamp(QSettings("cxp", "cxp-gui").value(kScopeKey, int(S_ALL)).toInt(), 0, 2));
    connect(filter_, &QLineEdit::textChanged, this, &ValidationWindow::applyFilter);
    connect(scope_, qOverload<int>(&QComboBox::currentIndexChanged), this, [this](int i) {
        QSettings("cxp", "cxp-gui").setValue(kScopeKey, i);
        applyFilter();
    });
    auto* filters = new QHBoxLayout();
    filters->addWidget(filter_, 1);
    filters->addWidget(scope_);

    tree_ = new QTreeWidget();
    tree_->setColumnCount(4);
    tree_->setHeaderLabels({"Test case", "Title", "Emulator", "Verdict"});
    tree_->setSelectionMode(QAbstractItemView::ExtendedSelection);
    tree_->setUniformRowHeights(true);
    tree_->setAlternatingRowColors(true);
    tree_->header()->setStretchLastSection(false);
    tree_->header()->setSectionResizeMode(C_TITLE, QHeaderView::Stretch);
    tree_->setColumnWidth(C_ID, 240);
    tree_->setColumnWidth(C_RUNS, 90);
    tree_->setColumnWidth(C_VERDICT, 90);
    connect(tree_, &QTreeWidget::itemSelectionChanged, this, &ValidationWindow::onSelectionChanged);
    connect(tree_, &QTreeWidget::itemDoubleClicked, this, [this](QTreeWidgetItem* it, int) {
        if (it && it->data(0, kIndexRole).isValid() && !running_) onRunSelected();
    });

    auto* left = new QWidget();
    auto* ll = new QVBoxLayout(left);
    ll->setContentsMargins(0, 0, 0, 0);
    ll->addLayout(filters);
    ll->addWidget(tree_, 1);

    // -- description + result ------------------------------------------------------
    desc_ = new QTextBrowser();
    desc_->setOpenLinks(false);
    result_ = new QTextBrowser();
    result_->setOpenLinks(false);
    auto* right = new QSplitter(Qt::Vertical);
    right->addWidget(desc_);
    right->addWidget(result_);
    right->setStretchFactor(0, 3);
    right->setStretchFactor(1, 2);
    right->setChildrenCollapsible(false);

    auto* split = new QSplitter(Qt::Horizontal);
    split->addWidget(left);
    split->addWidget(right);
    split->setStretchFactor(0, 5);
    split->setStretchFactor(1, 6);
    split->setChildrenCollapsible(false);

    // -- run bar -----------------------------------------------------------------------
    run_sel_btn_ = new QPushButton("▶ Run selected");
    run_sel_btn_->setToolTip("Run the selected case(s), in list order (double-click a case to run it)");
    run_all_btn_ = new QPushButton("▶▶ Run all");
    run_all_btn_->setToolTip("Play every listed case one by one; cases the emulator cannot run are marked NOT RUN");
    stop_btn_ = new QPushButton("■ Stop");
    stop_btn_->setToolTip("Stop after the current check step; registers the check changed are restored");
    export_btn_ = new QPushButton("Export results…");
    connect(run_sel_btn_, &QPushButton::clicked, this, &ValidationWindow::onRunSelected);
    connect(run_all_btn_, &QPushButton::clicked, this, &ValidationWindow::onRunAll);
    connect(stop_btn_, &QPushButton::clicked, this, &ValidationWindow::onStop);
    connect(export_btn_, &QPushButton::clicked, this, &ValidationWindow::onExport);
    const OptionSpec& soak_spec = *findOption("soak_seconds");
    soak_ = new QSpinBox();
    soak_->setRange(int(soak_spec.min), int(soak_spec.max));
    soak_->setValue(int(soak_spec.get(opts_)));
    soak_->setSuffix(" s");
    soak_->setToolTip(optionHelp(soak_spec));
    const OptionSpec& scale_spec = *findOption("timeout_scale");
    scale_ = new QDoubleSpinBox();
    scale_->setRange(scale_spec.min, scale_spec.max);
    scale_->setDecimals(scale_spec.decimals);
    scale_->setValue(scale_spec.get(opts_));
    scale_->setPrefix("x");
    scale_->setToolTip(optionHelp(scale_spec));
    opts_btn_ = new QPushButton("Options…");
    connect(opts_btn_, &QPushButton::clicked, this, &ValidationWindow::onOptions);
    opts_btn_->setToolTip("The other run options (the cxp validate flags of the same names)");
    log_root_ = QSettings("cxp", "cxp-gui").value("validation/log_root",
                                                  QDir::current().absoluteFilePath("validation_logs")).toString();
    logs_btn_ = new QPushButton("Logs…");
    connect(logs_btn_, &QPushButton::clicked, this, &ValidationWindow::onChooseLogRoot);
    logs_btn_->setToolTip("Folder for the run logs: each run writes <folder>/<date_time>/<ID>.log and "
                          "results.json\n" + log_root_);
    progress_ = new QProgressBar();
    progress_->setTextVisible(true);
    progress_->setMaximumWidth(260);
    summary_ = new QLabel();

    auto* bar = new QHBoxLayout();
    bar->addWidget(run_sel_btn_);
    bar->addWidget(run_all_btn_);
    bar->addWidget(stop_btn_);
    bar->addSpacing(12);
    auto* soak_label = new QLabel("Soak:");
    soak_label->setToolTip(soak_->toolTip());
    bar->addWidget(soak_label);
    bar->addWidget(soak_);
    bar->addSpacing(12);
    auto* scale_label = new QLabel("Waits:");
    scale_label->setToolTip(scale_->toolTip());
    bar->addWidget(scale_label);
    bar->addWidget(scale_);
    bar->addWidget(opts_btn_);
    bar->addWidget(logs_btn_);
    bar->addSpacing(12);
    bar->addWidget(progress_);
    bar->addWidget(summary_, 1);
    bar->addWidget(export_btn_);

    auto* lay = new QVBoxLayout(this);
    lay->addLayout(header);
    lay->addWidget(split, 1);
    lay->addLayout(bar);

    loadCatalogue(QString::fromStdString(defaultCataloguePath()));
    setDevice(nullptr);
}

ValidationWindow::~ValidationWindow() { shutdown(); }

void ValidationWindow::shutdown() {
    cancel_ = true;
    if (worker_.joinable()) worker_.join();
    running_ = false;
}

void ValidationWindow::closeEvent(QCloseEvent* ev) {
    // Closing only hides the window; a run in progress keeps going.
    ev->accept();
}

// -- catalogue ------------------------------------------------------------------------
void ValidationWindow::loadCatalogue(const QString& path) {
    try {
        cat_ = cxp::validation::loadCatalogue(path.toStdString());
        cat_path_ = path;
        results_.clear();
        cat_lbl_->setText(QString("<span style=\"color:%1;\">%2 — %3 cases, plan v%4</span>")
                              .arg(kMuted, QFileInfo(path).fileName().toHtmlEscaped())
                              .arg(cat_.cases.size())
                              .arg(QString::fromStdString(cat_.plan_version).toHtmlEscaped()));
        cat_lbl_->setToolTip(path);
    } catch (const std::exception& e) {
        cat_ = Catalogue();
        cat_lbl_->setText(QString("<span style=\"color:%1;\">no catalogue</span>").arg(kFail));
        desc_->setHtml(QString("<p style=\"color:%1;\">Cannot load the validation catalogue:<br>%2</p>"
                               "<p>Set <code>$CXP_VALIDATION_JSON</code> or use <i>Open catalogue…</i>.</p>")
                           .arg(kFail, esc(e.what())));
    }
    populate();
}

void ValidationWindow::onOpenCatalogue() {
    if (running_) return;
    const QString path = QFileDialog::getOpenFileName(this, "Open validation catalogue",
                                                      QFileInfo(cat_path_).absolutePath(), "JSON (*.json)");
    if (!path.isEmpty()) loadCatalogue(path);
}

void ValidationWindow::populate() {
    tree_->clear();
    items_.clear();
    std::map<std::string, QTreeWidgetItem*> sections;
    for (size_t i = 0; i < cat_.cases.size(); ++i) {
        const CaseDef& c = cat_.cases[i];
        QTreeWidgetItem*& sec = sections[c.section];
        if (!sec) {
            sec = new QTreeWidgetItem(tree_);
            sec->setText(C_ID, QString::fromStdString(c.section));
            sec->setFirstColumnSpanned(true);
            QFont f = sec->font(C_ID);
            f.setBold(true);
            sec->setFont(C_ID, f);
            sec->setExpanded(true);
        }
        auto* it = new QTreeWidgetItem(sec);
        it->setData(0, kIndexRole, int(i));
        it->setText(C_ID, QString::fromStdString(c.id));
        it->setText(C_TITLE, QString::fromStdString(c.title));
        it->setText(C_RUNS, c.emulator.runnable ? "runs" : "—");
        it->setToolTip(C_RUNS, c.emulator.runnable ? "The emulator runs this case"
                                                   : QString::fromStdString(c.emulator.reason));
        if (!c.emulator.runnable) {
            for (int col : {C_ID, C_TITLE, C_RUNS}) it->setForeground(col, QColor(kMuted));
        }
        items_[int(i)] = it;
        refreshItem(int(i));
    }
    applyFilter();
    refreshSummary();
    updateButtons();
    if (!cat_.cases.empty()) {
        if (QTreeWidgetItem* first = itemFor(0)) tree_->setCurrentItem(first);
    }
}

void ValidationWindow::applyFilter() {
    const QString text = filter_->text().trimmed();
    const int scope = scope_->currentData().toInt();
    for (int s = 0; s < tree_->topLevelItemCount(); ++s) {
        QTreeWidgetItem* sec = tree_->topLevelItem(s);
        int shown = 0;
        for (int k = 0; k < sec->childCount(); ++k) {
            QTreeWidgetItem* it = sec->child(k);
            const CaseDef& c = cat_.cases[size_t(it->data(0, kIndexRole).toInt())];
            bool vis = (scope != S_RUNNABLE || c.emulator.runnable) && (scope != S_UVM || c.isUvm()) &&
                       (text.isEmpty() || it->text(C_ID).contains(text, Qt::CaseInsensitive) ||
                        it->text(C_TITLE).contains(text, Qt::CaseInsensitive));
            it->setHidden(!vis);
            shown += vis;
        }
        sec->setHidden(shown == 0);
    }
    updateButtons();
}

// -- selection / display -----------------------------------------------------------------
QTreeWidgetItem* ValidationWindow::itemFor(int idx) const {
    auto it = items_.find(idx);
    return it == items_.end() ? nullptr : it->second;
}

int ValidationWindow::currentIndex() const {
    QTreeWidgetItem* it = tree_->currentItem();
    return it && it->data(0, kIndexRole).isValid() ? it->data(0, kIndexRole).toInt() : -1;
}

std::vector<int> ValidationWindow::selectedIndices() const {
    std::set<int> picked;
    for (QTreeWidgetItem* it : tree_->selectedItems()) {
        if (it->data(0, kIndexRole).isValid()) {
            picked.insert(it->data(0, kIndexRole).toInt());
        } else {  // a section: every visible case in it
            for (int k = 0; k < it->childCount(); ++k) {
                if (!it->child(k)->isHidden()) picked.insert(it->child(k)->data(0, kIndexRole).toInt());
            }
        }
    }
    return {picked.begin(), picked.end()};
}

std::vector<int> ValidationWindow::visibleIndices() const {
    std::vector<int> out;
    for (const auto& [idx, it] : items_) {
        if (!it->isHidden() && !it->parent()->isHidden()) out.push_back(idx);
    }
    return out;
}

void ValidationWindow::onSelectionChanged() {
    const int idx = currentIndex();
    if (idx >= 0) {
        showCase(idx);
        showResult(idx);
    }
    updateButtons();
}

void ValidationWindow::showCase(int idx) {
    const CaseDef& c = cat_.cases[size_t(idx)];
    QString h;
    h += QString("<h2>%1 — %2</h2>").arg(esc(c.id), esc(c.title));
    QString meta = esc(c.section) + " · " + esc(c.test_class) + " · " + esc(c.automation);
    if (!c.kind.empty()) meta += " · " + esc(c.kind);
    if (c.hardware_dependent) meta += " · <b>hardware-dependent</b>";
    h += QString("<p style=\"color:%1;\">%2</p>").arg(kMuted, meta);
    h += section("Objective", para(c.objective));

    // What pressing Run does.
    QString emu;
    if (c.emulator.runnable) {
        emu += para(c.emulator.scope);
        emu += list(c.emulator.procedure, true);
        emu += criteria(kPass, "PASS on the emulator", c.emulator.pass_criteria);
        emu += "<br>";
        emu += criteria(kFail, "FAIL on the emulator", c.emulator.fail_criteria);
    } else {
        emu += QString("<p style=\"color:%1;\"><b>Not runnable on the emulator.</b> %2</p>")
                   .arg(kMuted, esc(c.emulator.reason));
    }
    h += section("Run on the emulator", emu);

    if (c.isUvm()) {
        QString u = QString("<p><b>%1</b> &nbsp;<span style=\"color:%2;\">%3 · tiers: %4</span></p>")
                        .arg(esc(c.uvm.test), kMuted, esc(c.uvm.source));
        QStringList tiers;
        for (const auto& t : c.uvm.tiers) tiers << QString::fromStdString(t);
        u = u.arg(tiers.join(", ").toHtmlEscaped());
        u += c.uvm.plan.empty() ? QString("<p>Serves no validation-plan case of its own.</p>")
                                : "<p>Validation-plan cases it serves:</p>" + list(c.uvm.plan, false);
        if (!c.uvm.expect_fail.empty()) {
            u += QString("<p style=\"color:%1;\">Tolerated in UVM today (EXPECT_FAIL); the emulator check does not "
                         "tolerate them:</p><ul>").arg(kWarn);
            for (const auto& x : c.uvm.expect_fail) {
                u += QString("<li><b>%1 / %2</b>: %3</li>").arg(esc(x.scoreboard), esc(x.kind), esc(x.finding));
            }
            u += "</ul>";
        }
        h += section("UVM test", u);
    } else if (!c.uvm_tests.empty()) {
        h += section("UVM tests serving this case", list(c.uvm_tests, false));
    }

    if (!c.requirements.empty()) {
        QString t = "<table cellpadding=\"3\">";
        for (const auto& r : c.requirements) {
            t += QString("<tr><td valign=\"top\"><b>%1</b></td><td valign=\"top\">%2</td>"
                         "<td valign=\"top\" style=\"color:%3;\">%4</td><td>%5</td></tr>")
                     .arg(esc(r.id), esc(r.level), kMuted, esc(r.clause), esc(r.text));
        }
        h += section("Requirements", t + "</table>");
    } else {
        h += section("Requirements", "<p>None (camera-functional test).</p>");
    }
    if (!c.clauses.empty()) h += QString("<p style=\"color:%1;\">CXP 1.1.1 clauses: %2</p>").arg(kMuted, esc(c.clauses));
    h += section("Preconditions", para(c.preconditions));
    h += section("Test equipment", list(c.equipment, false));
    h += section("Procedure (plan)", list(c.procedure, true));
    h += section("Stimulus", para(c.stimulus));
    h += section("Expected result", para(c.expected));
    if (!c.pass_criteria.empty() || !c.fail_criteria.empty()) {
        h += section("Pass / fail (plan)", criteria(kPass, "PASS", c.pass_criteria) + "<br>" +
                                               criteria(kFail, "FAIL", c.fail_criteria));
    }
    h += section("Evidence", para(c.evidence));
    desc_->setHtml(h);
}

void ValidationWindow::showResult(int idx) {
    auto it = results_.find(idx);
    const CaseDef& c = cat_.cases[size_t(idx)];
    if (it == results_.end()) {
        result_->setHtml(QString("<p style=\"color:%1;\">%2 has not been run in this session.</p>")
                             .arg(kMuted, esc(c.id)));
        return;
    }
    const CaseResult& r = it->second;
    QString h;
    if (idx == running_idx_) {
        h += QString("<h3 style=\"color:%1;\">RUNNING…</h3>").arg(kWarn);
    } else {
        h += QString("<h3 style=\"color:%1;\">%2</h3><p>%3<br><span style=\"color:%4;\">%5 · %6 ms</span></p>")
                 .arg(verdictColor(r.verdict), verdictName(r.verdict), esc(r.summary), kMuted,
                      esc(r.started))
                 .arg(r.duration_ms, 0, 'f', 0);
    }
    for (const LogLine& l : r.log) h += lineHtml(l);
    result_->setHtml(h);
    result_->verticalScrollBar()->setValue(result_->verticalScrollBar()->maximum());
}

void ValidationWindow::refreshItem(int idx) {
    QTreeWidgetItem* it = itemFor(idx);
    if (!it) return;
    auto r = results_.find(idx);
    if (idx == running_idx_) {
        it->setText(C_VERDICT, "RUNNING");
        it->setForeground(C_VERDICT, QColor(kWarn));
    } else if (r == results_.end()) {
        it->setText(C_VERDICT, "");
    } else {
        it->setText(C_VERDICT, verdictName(r->second.verdict));
        it->setForeground(C_VERDICT, QColor(verdictColor(r->second.verdict)));
        it->setToolTip(C_VERDICT, QString::fromStdString(r->second.summary));
    }
}

void ValidationWindow::refreshSummary() {
    std::map<Verdict, int> n;
    for (const auto& [idx, r] : results_) {
        if (idx != running_idx_) ++n[r.verdict];
    }
    auto part = [&](Verdict v) {
        return QString("<span style=\"color:%1;\">%2 %3</span>").arg(verdictColor(v)).arg(n[v]).arg(verdictName(v));
    };
    size_t runnable = 0, uvm = 0, uvm_runnable = 0;
    for (const auto& c : cat_.cases) {
        runnable += c.emulator.runnable;
        uvm += c.isUvm();
        uvm_runnable += c.isUvm() && c.emulator.runnable;
    }
    QString text = QString("%1 · %2 · %3 · %4 &nbsp; <span style=\"color:%5;\">(%6 of %7 cases runnable; "
                           "%8 of %9 UVM tests)</span>")
                       .arg(part(Verdict::Pass), part(Verdict::Fail), part(Verdict::NotRun), part(Verdict::Error),
                            kMuted)
                       .arg(runnable)
                       .arg(cat_.cases.size())
                       .arg(uvm_runnable)
                       .arg(uvm);
    // Every later "no acknowledgment" comes from the case that wedged it.
    if (!dead_after_.isEmpty()) {
        text += QString("<br><span style=\"color:%1;\"><b>The device stopped answering after %2.</b></span>")
                    .arg(kFail, dead_after_.toHtmlEscaped());
    }
    summary_->setText(text);
    summary_->setToolTip(last_log_dir_.isEmpty() ? QString() : "Logs of the last run: " + last_log_dir_);
}

void ValidationWindow::updateButtons() {
    const bool dev_ok = dev_ && dev_->host && dev_->host->connected;
    const bool have = !cat_.cases.empty();
    run_sel_btn_->setEnabled(!running_ && dev_ok && have && !selectedIndices().empty());
    run_all_btn_->setEnabled(!running_ && dev_ok && have && !visibleIndices().empty());
    stop_btn_->setEnabled(running_);
    export_btn_->setEnabled(!running_ && !results_.empty());
    soak_->setEnabled(!running_);
    scale_->setEnabled(!running_);
    opts_btn_->setEnabled(!running_);
    logs_btn_->setEnabled(!running_);
}

void ValidationWindow::setDevice(DevicePtr dev) {
    if (running_ && dev != dev_) cancel_ = true;
    dev_ = std::move(dev);
    if (dev_ && dev_->host && dev_->host->connected) {
        QString what = dev_->label.toHtmlEscaped();
        if (dev_->info) {
            what += QString(" — %1 %2").arg(esc(dev_->info->vendor_name), esc(dev_->info->model_name));
        }
        device_lbl_->setText("Device: <b>" + what + "</b>");
    } else {
        device_lbl_->setText(QString("<span style=\"color:%1;\">No device — scan and select one in the Explorer "
                                     "to run cases.</span>").arg(kWarn));
    }
    updateButtons();
}

// -- running -----------------------------------------------------------------------------------
void ValidationWindow::onRunSelected() { startRun(selectedIndices()); }

void ValidationWindow::onRunAll() { startRun(visibleIndices()); }

void ValidationWindow::onStop() {
    cancel_ = true;
    stop_btn_->setEnabled(false);
}

void ValidationWindow::startRun(std::vector<int> order) {
    if (running_ || order.empty() || !dev_ || !dev_->host) return;
    if (worker_.joinable()) worker_.join();
    CampaignConfig cfg;
    try {
        cfg.log_dir = makeRunLogDir("", log_root_.toStdString(), opts_.keep_logs);
    } catch (const std::exception& e) {
        QMessageBox::warning(this, "Validation run", QString("Cannot create the log folder:\n%1").arg(e.what()));
        return;
    }
    cfg.options = opts_;
    cfg.options.soak_seconds = soak_->value();
    cfg.options.timeout_scale = scale_->value();
    cfg.device = deviceDescription().toStdString();
    last_log_dir_ = QString::fromStdString(cfg.log_dir);
    dead_after_.clear();
    running_ = true;
    cancel_ = false;
    run_total_ = order.size();
    run_done_ = 0;
    progress_->setRange(0, int(run_total_));
    progress_->setValue(0);
    progress_->setFormat(QString("%v / %m"));
    updateButtons();
    refreshSummary();

    std::vector<const CaseDef*> cases;
    for (int idx : order) cases.push_back(&cat_.cases[size_t(idx)]);
    auto host = dev_->host;  // keep the session alive for the whole run
    QPointer<ValidationWindow> self(this);
    worker_ = std::thread([self, host, order, cases, cfg, this] {
        // Campaign progress, re-posted to the GUI thread by catalogue index.
        struct Post : CampaignObserver {
            QPointer<ValidationWindow> self;
            std::vector<int> order;
            void post(std::function<void(ValidationWindow*)> f) {
                QMetaObject::invokeMethod(self.data(), [s = self, f] { if (s) f(s.data()); }, Qt::QueuedConnection);
            }
            void caseStarted(size_t i, const CaseDef&) override {
                const int idx = order[i];
                post([idx](ValidationWindow* w) { w->caseStarted(idx); });
            }
            void caseLine(size_t i, const CaseDef&, const LogLine& l) override {
                const int idx = order[i];
                post([idx, l](ValidationWindow* w) { w->caseLine(idx, l); });
            }
            void caseDone(size_t i, const CaseDef&, const CaseResult& r) override {
                const int idx = order[i];
                post([idx, r](ValidationWindow* w) { w->caseDone(idx, r); });
            }
            void deviceLost(size_t i, const CaseDef&) override {
                const int idx = order[i];
                post([idx](ValidationWindow* w) { w->deviceLost(idx); });
            }
            void finished(const CampaignSummary& sum) override {
                post([sum](ValidationWindow* w) { w->campaignFinished(sum); });
            }
        } obs;
        obs.self = self;
        obs.order = order;
        try {
            Campaign(cat_, cases, cfg).run(host, cancel_, &obs);
        } catch (const std::exception& e) {
            const QString what = e.what();
            obs.post([what](ValidationWindow* w) {
                QMessageBox::warning(w, "Validation run", "The run ended with an error:\n" + what);
            });
        }
        QMetaObject::invokeMethod(self.data(), [self] { if (self) self->finishRun(); }, Qt::QueuedConnection);
    });
}

void ValidationWindow::caseStarted(int idx) {
    running_idx_ = idx;
    CaseResult placeholder;
    placeholder.id = cat_.cases[size_t(idx)].id;
    results_[idx] = placeholder;
    refreshItem(idx);
    // Follow the run so "Run all" plays through the list visibly.
    if (QTreeWidgetItem* it = itemFor(idx)) {
        tree_->blockSignals(true);
        tree_->clearSelection();
        tree_->setCurrentItem(it);
        it->setSelected(true);
        tree_->blockSignals(false);
        tree_->scrollToItem(it);
        showCase(idx);
    }
    showResult(idx);
}

void ValidationWindow::caseLine(int idx, const LogLine& line) {
    auto it = results_.find(idx);
    if (it == results_.end()) return;
    it->second.log.push_back(line);
    if (currentIndex() == idx) {
        result_->append(lineHtml(line));
    }
}

void ValidationWindow::caseDone(int idx, const CaseResult& res) {
    results_[idx] = res;
    if (running_idx_ == idx) running_idx_ = -1;
    ++run_done_;
    progress_->setValue(int(run_done_));
    refreshItem(idx);
    refreshSummary();
    if (currentIndex() == idx) showResult(idx);
}

void ValidationWindow::deviceLost(int idx) {
    dead_after_ = QString::fromStdString(cat_.cases[size_t(idx)].id);
    refreshSummary();
}

void ValidationWindow::campaignFinished(const CampaignSummary& sum) {
    if (!sum.log_dir.empty()) last_log_dir_ = QString::fromStdString(sum.log_dir);
    refreshSummary();
}

void ValidationWindow::onChooseLogRoot() {
    const QString dir = QFileDialog::getExistingDirectory(this, "Folder for validation logs", log_root_);
    if (dir.isEmpty()) return;
    log_root_ = dir;
    QSettings("cxp", "cxp-gui").setValue("validation/log_root", log_root_);
    logs_btn_->setToolTip("Folder for the run logs: each run writes <folder>/<date_time>/<ID>.log and "
                          "results.json\n" + log_root_);
}

// Every run option but the two on the run bar, one spin box each.
void ValidationWindow::onOptions() {
    QDialog dlg(this);
    dlg.setWindowTitle("Validation run options");
    auto* form = new QFormLayout();
    std::vector<std::pair<const OptionSpec*, QDoubleSpinBox*>> boxes;
    for (const OptionSpec& s : optionSpecs()) {
        if (std::string(s.key) == "soak_seconds" || std::string(s.key) == "timeout_scale") continue;
        auto* box = new QDoubleSpinBox();
        box->setRange(s.min, s.max);
        box->setDecimals(s.decimals);
        box->setValue(s.get(opts_));
        if (*s.unit) box->setSuffix(QString(" ") + s.unit);
        box->setToolTip(optionHelp(s));
        auto* label = new QLabel(QString(s.label) + ":");
        label->setToolTip(box->toolTip());
        form->addRow(label, box);
        boxes.emplace_back(&s, box);
    }
    auto* buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel | QDialogButtonBox::RestoreDefaults);
    connect(buttons, &QDialogButtonBox::accepted, &dlg, &QDialog::accept);
    connect(buttons, &QDialogButtonBox::rejected, &dlg, &QDialog::reject);
    connect(buttons->button(QDialogButtonBox::RestoreDefaults), &QPushButton::clicked, &dlg, [&boxes] {
        const Options dflt;
        for (auto& [s, box] : boxes) box->setValue(s->get(dflt));
    });
    auto* lay = new QVBoxLayout(&dlg);
    lay->addLayout(form);
    lay->addWidget(buttons);
    while (dlg.exec() == QDialog::Accepted) {
        Options o = opts_;
        try {
            for (auto& [s, box] : boxes) setOption(o, *s, box->value());
        } catch (const std::invalid_argument& e) {
            QMessageBox::warning(&dlg, "Validation run options", e.what());
            continue;
        }
        opts_ = o;
        opts_btn_->setToolTip(QString::fromStdString(describeOptions(opts_)));
        return;
    }
}

QString ValidationWindow::deviceDescription() const {
    if (!dev_) return {};
    QString device = dev_->label;
    if (dev_->info) {
        device += QString(" (%1 %2)").arg(QString::fromStdString(dev_->info->vendor_name),
                                          QString::fromStdString(dev_->info->model_name));
    }
    return device;
}

void ValidationWindow::finishRun() {
    if (worker_.joinable()) worker_.join();
    running_ = false;
    running_idx_ = -1;
    if (cancel_ && run_done_ < run_total_) progress_->setFormat(QString("stopped at %v / %m"));
    refreshSummary();
    updateButtons();
}

void ValidationWindow::onExport() {
    std::vector<CaseResult> list;
    for (const auto& [idx, r] : results_) list.push_back(r);
    const QString path = QFileDialog::getSaveFileName(this, "Export validation results", "cxp_validation_results.json",
                                                      "JSON (*.json)");
    if (path.isEmpty()) return;
    std::ofstream f(path.toStdString(), std::ios::binary);
    f << resultsToJson(cat_, list, deviceDescription().toStdString());
    if (!f) QMessageBox::warning(this, "Export", "Cannot write " + path);
}
