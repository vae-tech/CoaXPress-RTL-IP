// Validation-plan test cases, loaded from the JSON catalogue
// (validation/cxp_validation_cases.json, generated from
// docs/verification/validation/cxp_camera_validation_plan.md).
//
// Each case carries the plan's own text (objective, procedure, PASS/FAIL
// criteria, ...) plus an `emulator` block that says whether this host can
// run it over the FIFO link and, if so, what exactly the check does and
// how it decides PASS or FAIL.
//
// The PyUVM tests of src/verif/ are catalogued too, one "UVM-<test>" case each
// in their own section: `uvm` says which test it reproduces and what that
// test covers of the plan.
#pragma once

#include <map>
#include <string>
#include <utility>
#include <vector>

namespace cxp::validation {

// One per-case parameter of the catalogue (emulator.params): a number, a
// string, a list of them, or a group of named ones ("test_packets": {"clean":
// 100, ...}, read as "test_packets.clean").  The generator renders the
// catalogue prose from the same values, so each number exists once.
struct ParamValue {
    enum class Kind { Number, String, List, Group };
    Kind kind = Kind::Number;
    double num = 0;
    std::string str;
    std::vector<ParamValue> list;
    std::vector<std::pair<std::string, ParamValue>> group;
};

using Params = std::map<std::string, ParamValue>;

// "trials=5 spsm_list=[36, 40, host_spsm] test_packets={clean=100, ...}"
std::string describeParams(const Params& p);

struct Requirement {
    std::string id;      // REQ-RST-001
    std::string level;   // MUST / SHOULD / MAY / INFO
    std::string clause;  // §10.3.28
    std::string text;
};

struct EmulatorPlan {
    bool runnable = false;
    std::string reason;  // why not runnable (empty when runnable)
    std::string scope;   // which part of the plan procedure the check covers
    std::vector<std::string> procedure;
    std::string pass_criteria;
    std::string fail_criteria;
    // Multiplies the check's host-side waits for a case that needs longer
    // than the standard budget on a slow device (the RTL simulation).
    double timeout_scale = 1.0;
    // The check's stimulus counts and lists (Context::iparam() and friends).
    Params params;
};

// A known finding a UVM test's EXPECT_FAIL tag tolerates today.
struct UvmExpectFail {
    std::string scoreboard;  // sb_stream
    std::string kind;        // lost_frame
    std::string finding;
};

// The PyUVM test a "UVM-<test>" case reproduces.
struct UvmRef {
    std::string test;    // test_ctrl_cmd_read; empty for a plan case
    std::string source;  // src/verif/uvm/tests/all_tests.py:<line>
    std::vector<std::string> plan;  // plan cases served, "(partly: ...)" when PLAN_PARTIAL
    std::vector<UvmExpectFail> expect_fail;
    std::vector<std::string> tiers;  // smoke, feature, xifc, nightly, weekly
};

struct CaseDef {
    std::string id;       // CXP-CAM-INIT-001
    std::string title;
    std::string area;     // INIT, BOOT, PROT, ...
    std::string section;  // "9. Protocol Validation"
    std::string test_class;  // RTL/SIM, Protocol, Software, Hardware (HW-dependent)
    std::string automation;  // AUTOMATED / PARTIALLY AUTOMATED / MANUAL
    std::string kind;        // executable / inspection / analysis
    bool hardware_dependent = false;
    std::vector<Requirement> requirements;
    std::string clauses;
    std::string objective;
    std::string preconditions;
    std::vector<std::string> equipment;
    std::vector<std::string> procedure;
    std::string stimulus;
    std::string expected;
    std::string pass_criteria;
    std::string fail_criteria;
    std::string evidence;
    EmulatorPlan emulator;
    UvmRef uvm;                          // set on UVM cases
    std::vector<std::string> uvm_tests;  // plan case: UVM tests that serve it

    bool isUvm() const { return !uvm.test.empty(); }
};

struct Catalogue {
    std::string schema;
    std::string plan;          // source plan path
    std::string plan_version;
    std::string generated;
    std::vector<CaseDef> cases;

    const CaseDef* find(const std::string& id) const;
};

// Parse a catalogue; throws std::runtime_error on malformed JSON or a
// missing mandatory field.
Catalogue parseCatalogue(const std::string& json_text);
Catalogue loadCatalogue(const std::string& path);

// $CXP_VALIDATION_JSON, else the catalogue in the source tree.
std::string defaultCataloguePath();

}  // namespace cxp::validation
