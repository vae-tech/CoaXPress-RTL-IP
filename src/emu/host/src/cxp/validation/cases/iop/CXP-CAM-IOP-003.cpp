// CXP-CAM-IOP-003.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::iop {

namespace {

void iop003(Context& c) {
    std::vector<double> lat;
    for (int i = 0; i < c.iparam("reads"); ++i) {
        auto a = c.readRaw(Reg::STANDARD, 4);
        if (a) lat.push_back(a->latency_ms);
    }
    auto cap = c.acquire(c.iparam("images"));
    std::vector<Words> frames;
    for (const auto& f : cap) frames.push_back(f.frame);
    StreamParser parser;
    for (const auto& f : frames) parser.feedFrame(f);
    parser.flush();
    DeviceInfo info;
    info.magic = c.rd32(Reg::STANDARD);
    info.revision = c.rd32(Reg::REVISION);
    info.vendor_name = c.readString(Reg::DEVICE_VENDOR_NAME, 32);
    info.model_name = c.readString(Reg::DEVICE_MODEL_NAME, 32);
    info.xml_url = c.readString(c.rd32(Reg::XML_URL_ADDRESS), 64);
    ComplianceChecker chk;
    chk.checkCapture(frames);
    chk.checkParserStats(parser.stats);
    chk.checkTiming(lat);
    chk.checkDevice(info);
    chk.checkSfnc(c.tree());
    const auto& rep = chk.finalize();
    for (const auto& r : rep.results) {
        if (r.severity == Severity::Error) c.expect(false, "%s", r.str().c_str());
        else if (r.severity == Severity::Warning) c.warn("%s", r.str().c_str());
        else c.info("%s", r.str().c_str());
    }
    c.expect(rep.passed(), "compliance rule checker over %zu captured frames: %s", frames.size(),
             rep.passed() ? "no errors" : "errors reported");
    c.note("the rule checker is this host stack's second implementation, not an independent analyzer");
}
CXP_CHECK("CXP-CAM-IOP-003", iop003);

}  // namespace

}  // namespace cxp::validation::checks::iop
