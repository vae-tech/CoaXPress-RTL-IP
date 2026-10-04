#include "cxp/compliance/checker.h"

#include <algorithm>

#include "cxp/camera/client.h"
#include "cxp/genicam/sfnc.h"
#include "cxp/parser/stream_parser.h"
#include "cxp/protocol/packets.h"
#include "cxp/utils/log.h"

namespace cxp {

namespace {

// Mandatory SFNC features (§13.x — minimal interoperable set).
const char* const kMandatoryFeatures[] = {
    "DeviceVendorName", "DeviceModelName", "Width", "Height",
    "PixelFormat", "AcquisitionStart", "AcquisitionStop",
};

}  // namespace

const char* severityName(Severity s) {
    switch (s) {
    case Severity::Info:    return "INFO";
    case Severity::Pass:    return "PASS";
    case Severity::Warning: return "WARNING";
    case Severity::Error:   return "ERROR";
    }
    return "?";
}

std::string Result::str() const {
    return strprintf("[%-7s] %-9s %s", severityName(severity), rule.c_str(),
                     message.c_str());
}

void ComplianceReport::add(const std::string& rule, Severity sev, const std::string& msg) {
    results.push_back({rule, sev, msg});
}

bool ComplianceReport::passed() const {
    return std::none_of(results.begin(), results.end(),
                        [](const Result& r) { return r.severity == Severity::Error; });
}

std::map<std::string, int> ComplianceReport::counts() const {
    std::map<std::string, int> c{{"INFO", 0}, {"PASS", 0}, {"WARNING", 0}, {"ERROR", 0}};
    for (const auto& r : results) ++c[severityName(r.severity)];
    return c;
}

std::string ComplianceReport::render() const {
    const std::string eq(72, '='), dash(72, '-');
    std::string out = eq + "\nCoaXPress 1.1.1 Compliance Report\n" + eq + "\n";
    for (const auto& r : results) out += r.str() + "\n";
    auto c = counts();
    out += dash + "\n";
    out += strprintf("PASS=%d WARNING=%d ERROR=%d INFO=%d\n", c["PASS"], c["WARNING"],
                     c["ERROR"], c["INFO"]);
    out += std::string("VERDICT: ") + (passed() ? "COMPLIANT" : "NON-COMPLIANT") + "\n";
    out += eq;
    return out;
}

ComplianceChecker::ComplianceChecker(uint32_t max_stream_payload_words)
    : max_stream_payload_words_(max_stream_payload_words) {}

void ComplianceChecker::checkCapture(const std::vector<Words>& frames) {
    if (frames.empty()) {
        report_.add("CXP-PKT", Severity::Info, "no frames captured");
        return;
    }
    int malformed = 0, crc_err = 0, bad_size = 0, streams = 0, tag_gaps = 0;
    std::optional<uint8_t> prev_tag;
    for (const auto& fr : frames) {
        Packet pkt;
        try {
            pkt = decodePacket(fr);
        } catch (const PacketDecodeError& exc) {
            (exc.reason() == "crc" ? crc_err : malformed)++;
            continue;
        }
        if (auto* sp = std::get_if<StreamPacket>(&pkt)) {
            ++streams;
            if (sp->payload.size() > max_stream_payload_words_) ++bad_size;
            if (prev_tag && sp->tag != static_cast<uint8_t>(*prev_tag + 1)) ++tag_gaps;
            prev_tag = sp->tag;
        }
    }
    verdict("CXP-PKT", malformed == 0,
            strprintf("%d malformed / %zu frames", malformed, frames.size()));
    verdict("CXP-CRC", crc_err == 0, strprintf("%d CRC errors in capture", crc_err));
    verdict("CXP-SIZE", bad_size == 0,
            strprintf("%d stream packets exceed %u-word limit", bad_size,
                      max_stream_payload_words_));
    if (streams) {
        verdict("CXP-STR", tag_gaps == 0,
                strprintf("%d stream-tag discontinuities (%d stream packets)", tag_gaps,
                          streams));
    }
}

void ComplianceChecker::checkParserStats(const ParseStats& stats) {
    verdict("CXP-CRC", stats.crc_errors == 0,
            strprintf("parser saw %llu CRC errors", (unsigned long long)stats.crc_errors));
    verdict("CXP-PKT", stats.malformed == 0,
            strprintf("parser saw %llu malformed packets",
                      (unsigned long long)stats.malformed));
    verdict("CXP-STR", stats.missing_packets == 0,
            strprintf("parser inferred %llu missing stream packets",
                      (unsigned long long)stats.missing_packets));
}

void ComplianceChecker::checkTiming(const std::vector<double>& ack_latencies_ms) {
    if (ack_latencies_ms.empty()) return;
    double worst = *std::max_element(ack_latencies_ms.begin(), ack_latencies_ms.end());
    verdict("CXP-TIM", worst <= CTRL_ACK_TIMEOUT_MS,
            strprintf("worst control-ack latency %.1f ms (limit %d ms)", worst,
                      CTRL_ACK_TIMEOUT_MS));
}

void ComplianceChecker::checkDevice(const DeviceInfo& info) {
    verdict("CXP-REG", info.magic == CXP_MAGIC,
            strprintf("STANDARD=0x%08X (expected 0x%08X)", info.magic, CXP_MAGIC));
    verdict("CXP-REG", !info.vendor_name.empty(),
            "DeviceVendorName='" + info.vendor_name + "'");
    verdict("CXP-REG", !info.model_name.empty(),
            "DeviceModelName='" + info.model_name + "'");
    verdict("CXP-SM", !info.xml_url.empty(), "XML URL advertised: '" + info.xml_url + "'");
}

void ComplianceChecker::checkRegisterAccess(std::optional<bool> ro_write_rejected,
                                            uint32_t addr) {
    if (!ro_write_rejected) return;
    verdict("CXP-REG", *ro_write_rejected,
            *ro_write_rejected
                ? strprintf("write to RO 0x%X correctly rejected", addr)
                : strprintf("RO 0x%X accepted a write (write-protect violation)", addr));
}

void ComplianceChecker::checkSfnc(const NodeTree* tree) {
    if (tree == nullptr) {
        report_.add("CXP-FEAT", Severity::Info, "no SFNC tree supplied");
        return;
    }
    verdict("CXP-FEAT", tree->root() != nullptr, "SFNC has a Root category");
    verdict("CXP-FEAT", !tree->schema_version.empty(),
            "GenICam schema version " + tree->schema_version);
    std::string missing;
    for (const char* f : kMandatoryFeatures) {
        if (tree->find(f) == nullptr) {
            missing += (missing.empty() ? "'" : ", '") + std::string(f) + "'";
        }
    }
    verdict("CXP-FEAT", missing.empty(),
            missing.empty() ? "all mandatory SFNC features present"
                            : "missing mandatory features: [" + missing + "]");
}

void ComplianceChecker::checkErrorHandling(std::optional<bool> crc_err_acked) {
    if (!crc_err_acked) return;
    verdict("CXP-ERR", *crc_err_acked,
            *crc_err_acked ? "device flagged an injected CRC error"
                           : "device did NOT report an injected CRC error");
}

void ComplianceChecker::verdict(const std::string& rule, bool ok, const std::string& msg) {
    report_.add(rule, ok ? Severity::Pass : Severity::Error, msg);
}

}  // namespace cxp
