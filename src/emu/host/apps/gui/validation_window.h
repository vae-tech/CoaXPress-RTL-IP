// Validation window: the validation plan's test cases, their descriptions
// and PASS/FAIL criteria, and a runner for one case or all of them in turn
// against the explorer's current device.
//
// Cases come from the JSON catalogue (validation/cxp_validation_cases.json):
// the plan's cases and the PyUVM tests reproduced on the emulator.  The
// list shows all of them, only the runnable ones, or only the UVM-based ones.
// A run is a validation::Campaign on a worker thread, exactly as in
// `cxp validate`: it writes a protocol log per case and results.json into a
// fresh directory under the chosen log folder, probes the device after each
// case, and reports back line by line.  Results live for the session and
// can be exported as JSON.
#pragma once

#include <atomic>
#include <map>
#include <string>
#include <thread>
#include <vector>

#include <QWidget>

#include "cxp/validation/campaign.h"
#include "cxp/validation/cases.h"
#include "cxp/validation/runner.h"
#include "device_model.h"

class QComboBox;
class QDoubleSpinBox;
class QLabel;
class QLineEdit;
class QProgressBar;
class QPushButton;
class QSpinBox;
class QTextBrowser;
class QTreeWidget;
class QTreeWidgetItem;

class ValidationWindow : public QWidget {
    Q_OBJECT

public:
    explicit ValidationWindow(QWidget* parent = nullptr);
    ~ValidationWindow() override;

    // Stop any run and wait for the worker (call before sessions close).
    void shutdown();

public slots:
    void setDevice(DevicePtr dev);

protected:
    void closeEvent(QCloseEvent* ev) override;

private slots:
    void onOpenCatalogue();
    void onSelectionChanged();
    void onRunSelected();
    void onRunAll();
    void onStop();
    void onExport();
    void onChooseLogRoot();
    void onOptions();
    void applyFilter();

private:
    void loadCatalogue(const QString& path);
    void populate();
    void startRun(std::vector<int> order);
    void finishRun();
    // Worker -> GUI (queued).
    void caseStarted(int idx);
    void caseLine(int idx, const cxp::validation::LogLine& line);
    void caseDone(int idx, const cxp::validation::CaseResult& res);
    void deviceLost(int idx);
    void campaignFinished(const cxp::validation::CampaignSummary& sum);

    void showCase(int idx);
    void showResult(int idx);
    void refreshItem(int idx);
    void refreshSummary();
    void updateButtons();
    int currentIndex() const;
    std::vector<int> selectedIndices() const;
    std::vector<int> visibleIndices() const;
    QTreeWidgetItem* itemFor(int idx) const;
    QString deviceDescription() const;

    cxp::validation::Catalogue cat_;
    QString cat_path_;
    std::map<int, cxp::validation::CaseResult> results_;
    std::map<int, QTreeWidgetItem*> items_;
    DevicePtr dev_;

    std::thread worker_;
    std::atomic<bool> cancel_{false};
    bool running_ = false;
    int running_idx_ = -1;
    size_t run_total_ = 0, run_done_ = 0;
    QString log_root_;     // each run gets <log_root_>/<date_time>
    QString last_log_dir_;
    QString dead_after_;   // case after which the device stopped answering
    // Run options; soak and timeout scale come from the run bar, the rest
    // from the Options dialog (both built from validation::optionSpecs()).
    cxp::validation::Options opts_;

    QLabel* device_lbl_ = nullptr;
    QLabel* cat_lbl_ = nullptr;
    QLineEdit* filter_ = nullptr;
    QComboBox* scope_ = nullptr;  // All / Runnable / UVM based
    QTreeWidget* tree_ = nullptr;
    QTextBrowser* desc_ = nullptr;
    QTextBrowser* result_ = nullptr;
    QPushButton* run_sel_btn_ = nullptr;
    QPushButton* run_all_btn_ = nullptr;
    QPushButton* stop_btn_ = nullptr;
    QPushButton* export_btn_ = nullptr;
    QSpinBox* soak_ = nullptr;
    QDoubleSpinBox* scale_ = nullptr;
    QPushButton* opts_btn_ = nullptr;
    QPushButton* logs_btn_ = nullptr;
    QProgressBar* progress_ = nullptr;
    QLabel* summary_ = nullptr;
};
