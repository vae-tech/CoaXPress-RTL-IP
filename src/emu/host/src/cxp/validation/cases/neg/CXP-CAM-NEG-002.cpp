// CXP-CAM-NEG-002.  See cases/_common.h.

#include "cxp/validation/cases/neg/_helpers.h"

namespace cxp::validation::checks::neg {

namespace {

void neg002(Context& c) {
    const std::string url = c.readString(c.rd32(Reg::XML_URL_ADDRESS), 64);
    auto loc = parseGenicamUrl(url);
    const uint32_t url_at = c.rd32(Reg::XML_URL_ADDRESS);
    size_t ok = 0, n = 0;
    for (int64_t addr64 : c.ilist("unused_addresses")) {
        const uint32_t addr = uint32_t(addr64);
        if (coveredByXml(c, addr) || (loc && addr >= loc->first && addr < loc->first + loc->second) ||
            (addr >= url_at && addr < url_at + 64)) {
            c.note("0x%08X is mapped on this device; skipped", addr);
            continue;
        }
        auto r = c.readRaw(addr, 4);
        auto w = c.writeRaw(addr, {0});
        n += 2;
        ok += is(r, Ack::BAD_ADDRESS) + is(w, Ack::BAD_ADDRESS);
        c.expect(is(r, Ack::BAD_ADDRESS) && is(w, Ack::BAD_ADDRESS), "0x%08X: read %s, write %s", addr,
                 ackStr(r).c_str(), ackStr(w).c_str());
    }
    auto span = c.readRaw(Reg::HS_UPCONNECTION, 8);
    c.expect(is(span, Ack::BAD_ADDRESS), "8-byte read from HsUpconnection into 0x4040: %s", ackStr(span).c_str());
    auto un = c.readRaw(0x0002, 4);
    c.note("unaligned read at 0x0002: %s", ackStr(un).c_str());
    c.expect(c.rd32(Reg::STANDARD) == CXP_MAGIC, "device responsive after the invalid accesses");
    c.info("%zu of %zu invalid accesses answered 0x40", ok, n);
}
CXP_CHECK("CXP-CAM-NEG-002", neg002);

}  // namespace

}  // namespace cxp::validation::checks::neg
