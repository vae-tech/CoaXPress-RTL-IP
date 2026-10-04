// Runs validation-plan cases against a live device session.
//
// A case's verdict comes from its check (cases/<area>/<ID>.cpp):
//
//   PASS          every expectation held
//   FAIL          an expectation failed, or the device broke a precondition
//                 a spec device must meet (no ack, error ack to a plain read)
//   NOT RUN       the case needs something this host cannot do (hardware,
//                 character-level access, a feature the device lacks)
//   ERROR         the check itself failed (host-side exception)
//   STOPPED       cancelled by the user
#pragma once

#include <atomic>
#include <functional>
#include <map>
#include <memory>
#include <string>
#include <vector>

#include "cxp/validation/cases.h"
#include "cxp/validation/context.h"

namespace cxp::validation {

enum class Verdict { NotRun, Pass, Fail, Error, Stopped };

const char* verdictName(Verdict v);  // "PASS", "FAIL", "NOT RUN", "ERROR", "STOPPED"

// Fixed-width prefix of a check line in logs and on the console:
// "PASS  ", "FAIL  ", "WARN  ", "NOTE  ", or six blanks for Info.
const char* lineTag(LineKind k);

struct CaseResult {
    std::string id;
    Verdict verdict = Verdict::NotRun;
    std::string summary;
    std::vector<LogLine> log;
    int passes = 0;
    int failures = 0;
    int warnings = 0;
    double duration_ms = 0.0;
    std::string started;  // ISO-8601 local time
};

using CheckFn = std::function<void(Context&)>;

// One implemented check and the file it is defined in (__FILE__ of its
// CXP_CHECK; the file's base name is the case ID).
struct RegisteredCheck {
    CheckFn fn;
    std::string source;
};

// Every implemented check, keyed by plan test ID.  Checks add themselves
// with CXP_CHECK at namespace scope, next to their definition.
const std::map<std::string, RegisteredCheck>& checkRegistry();

// Registers one check from a static initialiser; a second check for the
// same ID throws, which ends the program at start-up.
struct CheckRegistrar {
    CheckRegistrar(const char* id, CheckFn fn, const char* source);
};

#define CXP_CHECK_CAT2(a, b) a##b
#define CXP_CHECK_CAT(a, b) CXP_CHECK_CAT2(a, b)
#define CXP_CHECK(id, fn) \
    static const ::cxp::validation::CheckRegistrar CXP_CHECK_CAT(cxp_check_registrar_, __LINE__)(id, fn, __FILE__)

// Run one case.  `line` receives each log line as it is produced (from the
// calling thread).  Non-runnable cases return NOT RUN without touching
// the device.
CaseResult runCase(const CaseDef& def, const std::shared_ptr<CameraControl>& cam,
                   const std::atomic<bool>& cancel,
                   const std::function<void(const LogLine&)>& line = {},
                   const Options& opt = Options());

// Results as JSON (schema cxp-validation-results/1).
std::string resultsToJson(const Catalogue& cat, const std::vector<CaseResult>& results,
                          const std::string& device);

}  // namespace cxp::validation
