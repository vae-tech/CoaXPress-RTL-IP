#include "cxp/validation/runner.h"

#include <chrono>
#include <stdexcept>

#include <QDateTime>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>

namespace cxp::validation {

namespace {

const char* lineKindName(LineKind k) {
    switch (k) {
    case LineKind::Info: return "info";
    case LineKind::Pass: return "pass";
    case LineKind::Fail: return "fail";
    case LineKind::Warn: return "warn";
    case LineKind::Note: return "note";
    }
    return "info";
}

std::map<std::string, RegisteredCheck>& registry() {
    static std::map<std::string, RegisteredCheck> r;  // built before first use, whatever the TU order
    return r;
}

}  // namespace

const std::map<std::string, RegisteredCheck>& checkRegistry() { return registry(); }

CheckRegistrar::CheckRegistrar(const char* id, CheckFn fn, const char* source) {
    auto [it, fresh] = registry().emplace(id, RegisteredCheck{std::move(fn), source});
    if (!fresh) {
        throw std::logic_error(std::string("two validation checks registered for ") + id + ": " + it->second.source +
                               " and " + source);
    }
}

const char* lineTag(LineKind k) {
    switch (k) {
    case LineKind::Info: return "      ";
    case LineKind::Pass: return "PASS  ";
    case LineKind::Fail: return "FAIL  ";
    case LineKind::Warn: return "WARN  ";
    case LineKind::Note: return "NOTE  ";
    }
    return "      ";
}

const char* verdictName(Verdict v) {
    switch (v) {
    case Verdict::NotRun: return "NOT RUN";
    case Verdict::Pass: return "PASS";
    case Verdict::Fail: return "FAIL";
    case Verdict::Error: return "ERROR";
    case Verdict::Stopped: return "STOPPED";
    }
    return "?";
}

CaseResult runCase(const CaseDef& def, const std::shared_ptr<CameraControl>& cam,
                   const std::atomic<bool>& cancel, const std::function<void(const LogLine&)>& line,
                   const Options& opt) {
    CaseResult r;
    r.id = def.id;
    r.started = QDateTime::currentDateTime().toString(Qt::ISODate).toStdString();
    auto record = [&](const LogLine& l) {
        r.log.push_back(l);
        if (line) line(l);
    };
    if (!def.emulator.runnable) {
        r.verdict = Verdict::NotRun;
        r.summary = def.emulator.reason.empty() ? "not runnable on the emulator" : def.emulator.reason;
        return r;
    }
    auto it = checkRegistry().find(def.id);
    if (it == checkRegistry().end()) {
        r.verdict = Verdict::Error;
        // The catalogue is read at run time (defaultCataloguePath()), the checks
        // are compiled in: a binary older than the catalogue lands here.
        r.summary = "the catalogue marks this case runnable but this build has no check for it"
                    " (binary older than " + defaultCataloguePath() + "? rebuild src/emu/host)";
        record({LineKind::Fail, r.summary});
        return r;
    }
    if (!cam || !cam->connected) {
        r.verdict = Verdict::Error;
        r.summary = "no connected device";
        record({LineKind::Fail, r.summary});
        return r;
    }

    const auto t0 = std::chrono::steady_clock::now();
    {
        Options o = opt;
        o.timeout_scale *= def.emulator.timeout_scale;
        Context ctx(cam, cancel, record, o);
        ctx.setParams(def.emulator.params);
        try {
            it->second.fn(ctx);
            ctx.runCleanup();
            r.verdict = ctx.failures() ? Verdict::Fail : Verdict::Pass;
            r.summary = ctx.failures()
                            ? strprintf("%d of %d checks failed", ctx.failures(),
                                        ctx.failures() + ctx.passes())
                            : strprintf("%d checks passed", ctx.passes());
            if (ctx.warnings()) r.summary += strprintf(", %d warning(s)", ctx.warnings());
        } catch (const Skip& e) {
            ctx.runCleanup();
            r.verdict = Verdict::NotRun;
            r.summary = e.what();
            record({LineKind::Note, std::string("not run: ") + e.what()});
        } catch (const Cancelled&) {
            ctx.runCleanup();
            r.verdict = Verdict::Stopped;
            r.summary = "stopped by the user";
        } catch (const Abort& e) {
            ctx.runCleanup();
            r.verdict = Verdict::Fail;
            r.summary = e.what();
            record({LineKind::Fail, e.what()});
        } catch (const CameraError& e) {
            ctx.runCleanup();
            r.verdict = Verdict::Fail;
            r.summary = std::string("device: ") + e.what();
            record({LineKind::Fail, r.summary});
        } catch (const std::exception& e) {
            ctx.runCleanup();
            r.verdict = Verdict::Error;
            r.summary = std::string("check error: ") + e.what();
            record({LineKind::Fail, r.summary});
        }
        r.passes = ctx.passes();
        r.failures = ctx.failures();
        r.warnings = ctx.warnings();
    }
    r.duration_ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    return r;
}

std::string resultsToJson(const Catalogue& cat, const std::vector<CaseResult>& results,
                          const std::string& device) {
    QJsonObject root;
    root["schema"] = "cxp-validation-results/1";
    root["plan"] = QString::fromStdString(cat.plan);
    root["plan_version"] = QString::fromStdString(cat.plan_version);
    root["device"] = QString::fromStdString(device);
    root["generated"] = QDateTime::currentDateTime().toString(Qt::ISODate);
    QJsonObject counts;
    QJsonArray arr;
    for (const CaseResult& r : results) {
        const QString v = verdictName(r.verdict);
        counts[v] = counts.value(v).toInt() + 1;
        QJsonObject o;
        o["id"] = QString::fromStdString(r.id);
        if (const CaseDef* d = cat.find(r.id)) o["title"] = QString::fromStdString(d->title);
        o["verdict"] = v;
        o["summary"] = QString::fromStdString(r.summary);
        o["passes"] = r.passes;
        o["failures"] = r.failures;
        o["warnings"] = r.warnings;
        o["duration_ms"] = r.duration_ms;
        o["started"] = QString::fromStdString(r.started);
        QJsonArray log;
        for (const LogLine& l : r.log) {
            QJsonObject lo;
            lo["kind"] = lineKindName(l.kind);
            lo["text"] = QString::fromStdString(l.text);
            log.append(lo);
        }
        o["log"] = log;
        arr.append(o);
    }
    root["counts"] = counts;
    root["results"] = arr;
    return QJsonDocument(root).toJson(QJsonDocument::Indented).toStdString();
}

}  // namespace cxp::validation
