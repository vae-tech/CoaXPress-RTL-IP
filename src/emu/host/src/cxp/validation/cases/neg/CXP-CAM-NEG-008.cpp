// CXP-CAM-NEG-008.  See cases/_common.h.

#include "cxp/validation/cases/neg/_helpers.h"

namespace cxp::validation::checks::neg {

namespace {

void neg008(Context& c) {
    size_t good = 0, n = 0;
    for (int t = 0x00; t <= 0xFF; ++t) {
        if (t == 0x02 || t == 0x04) continue;
        Words f = {SOP_WORD, replicateByte(uint8_t(t))};
        for (int i = 0; i < c.iparam("payload_words"); ++i) {
            const uint32_t b = uint32_t(4 * i + 1);
            f.push_back(b << 24 | (b + 1) << 16 | (b + 2) << 8 | (b + 3));  // 0x01020304, 0x05060708, ...
        }
        f.push_back(EOP_WORD);
        c.sendOnly({f}, 2);
        auto v = c.readRaw(Reg::STANDARD, 4);
        ++n;
        const bool ok = is(v, Ack::READ_OK) && v->values()[0] == CXP_MAGIC;
        good += ok;
        if (!ok && n - good <= size_t(c.opt().max_reported)) c.expect(false, "after a type 0x%02X packet: read %s", t, ackStr(v).c_str());
    }
    c.expect(good == n, "control channel responsive after %zu of %zu unexpected packet types", good, n);
    c.note("type 0x06 is this host stack's discovery/heartbeat extension; a reference camera answers it");
}
CXP_CHECK("CXP-CAM-NEG-008", neg008);

}  // namespace

}  // namespace cxp::validation::checks::neg
