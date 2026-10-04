// CXP-CAM-CTRL-008.  See cases/_common.h.

#include "cxp/validation/cases/ctrl/_helpers.h"

namespace cxp::validation::checks::ctrl {

namespace {

void ctrl008(Context& c) {
    const uint32_t cpsm = usableCpsm(c);
    const std::string url = c.readString(c.rd32(Reg::XML_URL_ADDRESS), 64);
    auto loc = parseGenicamUrl(url);
    const uint32_t base = loc ? uint32_t(loc->first) : Reg::DEVICE_VENDOR_NAME;
    const uint32_t maxb = cpsm - 24;
    auto a = c.readRaw(base, maxb);
    c.expect(is(a, Ack::READ_OK) && a->n_words * 4 <= cpsm, "read of CPSM-24 = %u bytes: %s, ack packet %zu bytes (<= %u)",
             maxb, ackStr(a).c_str(), a ? a->n_words * 4 : 0, cpsm);
    for (uint32_t extra : {1u, 4u}) {
        a = c.readRaw(base, maxb + extra);
        c.expect(is(a, Ack::SIZE_TOO_LARGE), "read of CPSM-24+%u bytes: %s (0x45)", extra, ackStr(a).c_str());
    }
    CmdSpec s;
    s.opcode = 0x01;
    s.address = Reg::DEVICE_USER_ID;
    s.size_bytes = cpsm - 20;
    s.data.assign((cpsm - 20) / 4, 0);
    a = c.exchange(buildCmd(s));
    c.expect(is(a, Ack::SIZE_TOO_LARGE), "write of CPSM-20 = %u bytes: %s (0x45)", cpsm - 20, ackStr(a).c_str());
    a = c.readRaw(Reg::DEVICE_VENDOR_NAME, 104);
    c.expect(is(a, Ack::READ_OK), "104-byte read (128-byte packet) accepted: %s", ackStr(a).c_str());
    c.note("a write of CPSM-24 bytes needs a documented writable area that large; not run");
}
CXP_CHECK("CXP-CAM-CTRL-008", ctrl008);

}  // namespace

}  // namespace cxp::validation::checks::ctrl
