#include "cxp/validation/campaign.h"

#include <algorithm>
#include <fstream>
#include <optional>
#include <stdexcept>

#include <QDateTime>
#include <QDir>
#include <QRegularExpression>

#include "cxp/camera/protocol_log.h"

namespace cxp::validation {

int CampaignSummary::failures() const {
    auto n = [this](const char* v) {
        auto it = counts.find(v);
        return it == counts.end() ? 0 : it->second;
    };
    return n("FAIL") + n("ERROR");
}

Campaign::Campaign(const Catalogue& cat, std::vector<const CaseDef*> cases, CampaignConfig cfg)
    : cat_(cat), cases_(std::move(cases)), cfg_(std::move(cfg)) {}

CampaignSummary Campaign::run(const std::shared_ptr<CameraControl>& cam, const std::atomic<bool>& cancel,
                              CampaignObserver* obs) {
    CampaignSummary sum;
    sum.log_dir = cfg_.log_dir;
    CampaignObserver none;
    if (!obs) obs = &none;

    std::shared_ptr<ProtocolLog> plog;
    bool own_plog = false;
    if (!cfg_.log_dir.empty()) {
        plog = cam->protocolLog();
        if (!plog) {
            plog = std::make_shared<ProtocolLog>();
            cam->setProtocolLog(plog);
            own_plog = true;
        }
    }
    auto event = [&](const std::string& text) {
        if (plog) plog->event(text);
    };

    // A camera has no free-run input.  A bench that has one (TPG_RUN) holds
    // it low for the run, so AcquisitionStart / Stop alone start and stop
    // the stream; free-running, the stream never stops and every wait for
    // a quiet link runs to its timeout.  A case may still raise it; its
    // cleanup lowers it again.
    std::optional<uint32_t> tpg_run_was;
    if (cam->benchCaps(int(500 * cfg_.options.timeout_scale + 0.5)) & bench::CAP_PIXEL) {
        Context hold(cam, cancel, {}, cfg_.options);
        const auto pins = hold.benchPins();
        tpg_run_was = pins ? (*pins >> (bench::TPG_RUN - 1)) & 1u : 1u;
        if (*tpg_run_was) {
            cam->sendBench({bench::PIN, bench::TPG_RUN, 0});
            hold.benchSync();
            hold.sleepMs(50);             // see a running stream first
            hold.waitQuiet();             // the image in flight completes
        }
    }

    for (size_t i = 0; i < cases_.size(); ++i) {
        if (cancel) break;
        const CaseDef& c = *cases_[i];
        if (plog) plog->open(cfg_.log_dir + "/" + c.id + ".log");
        event(strprintf("case %s  %s", c.id.c_str(), c.title.c_str()));
        if (c.emulator.runnable) {
            event(strprintf("timeout scale x%g", cfg_.options.timeout_scale * c.emulator.timeout_scale));
            event("run options " + describeOptions(cfg_.options));
            if (!c.emulator.params.empty()) event("case parameters " + describeParams(c.emulator.params));
            event(cfg_.options.seed ? strprintf("seed %u (--seed, every check)", cfg_.options.seed)
                                    : std::string("seed: each check's default (logged by the check)"));
        }
        obs->caseStarted(i, c);
        CaseResult res = runCase(c, cam, cancel, [&](const LogLine& l) {
            event(std::string(lineTag(l.kind)) + l.text);
            obs->caseLine(i, c, l);
        }, cfg_.options);
        event(strprintf("verdict %s: %s (%.0f ms)", verdictName(res.verdict), res.summary.c_str(),
                        res.duration_ms));
        bool lost = false;
        if (c.emulator.runnable && res.verdict != Verdict::Stopped && sum.dead_after.empty()) {
            Options probe_opt = cfg_.options;
            probe_opt.timeout_scale *= c.emulator.timeout_scale;
            Context probe(cam, cancel, {}, probe_opt);
            if (!probe.tryRd32(Reg::STANDARD)) {
                sum.dead_after = c.id;
                lost = true;
                event("probe: the device no longer answers a read of Standard");
            }
        }
        ++sum.counts[verdictName(res.verdict)];
        sum.results.push_back(std::move(res));
        obs->caseDone(i, c, sum.results.back());
        if (lost) obs->deviceLost(i, c);
    }
    if (tpg_run_was && *tpg_run_was) cam->sendBench({bench::PIN, bench::TPG_RUN, 1});
    sum.stopped = sum.results.size() < cases_.size();
    if (plog) plog->close();
    if (own_plog) cam->setProtocolLog(nullptr);

    sum.results_path = !cfg_.results_path.empty() ? cfg_.results_path
                       : !cfg_.log_dir.empty()    ? cfg_.log_dir + "/results.json"
                                                  : std::string();
    if (!sum.results_path.empty()) {
        std::ofstream f(sum.results_path, std::ios::binary);
        f << resultsToJson(cat_, sum.results, cfg_.device);
        if (!f) throw std::runtime_error("cannot write " + sum.results_path);
    }
    obs->finished(sum);
    return sum;
}

std::string makeRunLogDir(const std::string& requested, const std::string& root, int keep) {
    if (!requested.empty()) {
        if (!QDir().mkpath(QString::fromStdString(requested))) {
            throw std::runtime_error("cannot create log directory " + requested);
        }
        return requested;
    }
    QDir base(QString::fromStdString(root));
    if (!QDir().mkpath(base.path())) throw std::runtime_error("cannot create log directory " + root);
    const QString stamp = QDateTime::currentDateTime().toString("yyyyMMdd_HHmmss");
    QString name = stamp;
    for (int n = 2; base.exists(name); ++n) name = stamp + "_" + QString::number(n);
    if (!base.mkdir(name)) throw std::runtime_error("cannot create log directory " + root + "/" + name.toStdString());

    // Keep the newest `keep` run directories this function named.
    static const QRegularExpression run_dir("^\\d{8}_\\d{6}(_\\d+)?$");
    QStringList runs;
    for (const QString& d : base.entryList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name)) {
        if (run_dir.match(d).hasMatch()) runs << d;
    }
    std::sort(runs.begin(), runs.end(), [](const QString& a, const QString& b) {
        const QString sa = a.left(15), sb = b.left(15);  // suffix _10 sorts after _9
        if (sa != sb) return sa < sb;
        return a.mid(16).toInt() < b.mid(16).toInt();
    });
    for (int i = 0; i + keep < runs.size(); ++i) QDir(base.filePath(runs[i])).removeRecursively();
    return base.filePath(name).toStdString();
}

}  // namespace cxp::validation
