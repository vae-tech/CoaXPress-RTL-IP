// Rule-based CoaXPress 1.1.1 compliance validation
// (cxp/compliance/checker.py).
//
// The checker is fed whatever evidence is available — a decoded packet
// capture, the parser statistics, the SFNC tree, discovered device info,
// and measured control-ack latencies — and emits a ComplianceReport.  Each
// rule has a stable id so reports diff cleanly across runs:
//
//     CXP-PKT  §9.2   packet format / framing
//     CXP-SIZE §9.2.6 packet-size limits
//     CXP-CRC  §9.2.7 CRC correctness
//     CXP-TIM  §9.6.3 control timing (ack within 200 ms)
//     CXP-STR  §10    stream format
//     CXP-REG  §12.3  register-access behaviour
//     CXP-FEAT §13    mandatory SFNC features
//     CXP-SM   §9.3   link-init / state-machine ordering
//     CXP-ERR  §9.6   error handling
#pragma once

#include <map>
#include <optional>
#include <string>
#include <vector>

#include "cxp/protocol/constants.h"

namespace cxp {

struct DeviceInfo;
struct ParseStats;
class NodeTree;

enum class Severity { Info = 0, Pass = 1, Warning = 2, Error = 3 };

const char* severityName(Severity s);  // "INFO", "PASS", "WARNING", "ERROR"

struct Result {
    std::string rule;
    Severity severity;
    std::string message;

    std::string str() const;
};

struct ComplianceReport {
    std::vector<Result> results;

    void add(const std::string& rule, Severity sev, const std::string& msg);
    bool passed() const;
    std::map<std::string, int> counts() const;
    std::string render() const;
};

class ComplianceChecker {
public:
    explicit ComplianceChecker(uint32_t max_stream_payload_words = 0xFFFF);

    // CXP-PKT / CXP-SIZE / CXP-CRC / CXP-STR over a packet capture.
    void checkCapture(const std::vector<Words>& frames);
    void checkParserStats(const ParseStats& stats);
    void checkTiming(const std::vector<double>& ack_latencies_ms);
    void checkDevice(const DeviceInfo& info);
    void checkRegisterAccess(std::optional<bool> ro_write_rejected,
                             uint32_t addr = Bootstrap::STANDARD);
    void checkSfnc(const NodeTree* tree);
    void checkErrorHandling(std::optional<bool> crc_err_acked);

    const ComplianceReport& finalize() const { return report_; }

private:
    void verdict(const std::string& rule, bool ok, const std::string& msg);

    uint32_t max_stream_payload_words_;
    ComplianceReport report_;
};

}  // namespace cxp
