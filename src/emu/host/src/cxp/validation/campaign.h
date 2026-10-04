// A validation run: an ordered list of cases against one device session.
//
// Campaign owns everything around the single case that runCase() does:
//
//   * a protocol log per case, <log dir>/<ID>.log, holding every frame of
//     the case with the check's lines and the verdict in time order;
//   * the per-case timeout scale (Options x catalogue emulator.timeout_scale);
//   * a liveness probe after each case that ran: when the device no longer
//     answers a read of Standard, every later "no acknowledgment" comes from
//     that case, so the run says which one it was;
//   * the results as JSON (<log dir>/results.json unless a path is given).
//
// The CLI and the GUI run cases only through here and differ only in the
// observer that shows the progress.
#pragma once

#include <atomic>
#include <map>
#include <memory>
#include <string>
#include <vector>

#include "cxp/validation/cases.h"
#include "cxp/validation/runner.h"

namespace cxp::validation {

struct CampaignConfig {
    Options options;
    // Per-case protocol logs and results.json go here; "" writes no logs.
    std::string log_dir;
    // Results file; "" = <log_dir>/results.json, or none without a log dir.
    std::string results_path;
    // Device description written into the results.
    std::string device;
};

struct CampaignSummary {
    std::vector<CaseResult> results;     // in run order
    std::map<std::string, int> counts;   // by verdictName()
    std::string dead_after;              // case after which the device stopped answering
    std::string log_dir;
    std::string results_path;            // "" when none was written
    bool stopped = false;                // cancelled before the last case

    // FAIL and ERROR verdicts: a run with none of them succeeded.
    int failures() const;
};

// Progress callbacks, all on the thread that calls Campaign::run.
// `i` indexes the case list the campaign was built with.
class CampaignObserver {
public:
    virtual ~CampaignObserver() = default;
    virtual void caseStarted(size_t i, const CaseDef& c) {}
    virtual void caseLine(size_t i, const CaseDef& c, const LogLine& line) {}
    virtual void caseDone(size_t i, const CaseDef& c, const CaseResult& r) {}
    // After case i the device no longer answers a read of Standard.  Called
    // once per run, right after caseDone for that case.
    virtual void deviceLost(size_t i, const CaseDef& c) {}
    virtual void finished(const CampaignSummary& s) {}
};

class Campaign {
public:
    Campaign(const Catalogue& cat, std::vector<const CaseDef*> cases, CampaignConfig cfg);

    // Run every case in order until `cancel` is set.  Needs a connected
    // session.  Uses the session's protocol log when it has one, else
    // installs one for the run and removes it afterwards.
    CampaignSummary run(const std::shared_ptr<CameraControl>& cam, const std::atomic<bool>& cancel,
                        CampaignObserver* obs = nullptr);

    const std::vector<const CaseDef*>& cases() const { return cases_; }
    const CampaignConfig& config() const { return cfg_; }

private:
    const Catalogue& cat_;
    std::vector<const CaseDef*> cases_;
    CampaignConfig cfg_;
};

// The directory for one run's logs: `requested` when given, else
// <root>/<yyyyMMdd_HHmmss> (with a _2, _3 ... suffix when that exists).
// Creates it with its parents and throws when it cannot.  A directory it
// names itself under `root` counts towards `keep`: older timestamped run
// directories there are deleted so that `keep` remain.
std::string makeRunLogDir(const std::string& requested, const std::string& root = "validation_logs",
                          int keep = 10);

}  // namespace cxp::validation
