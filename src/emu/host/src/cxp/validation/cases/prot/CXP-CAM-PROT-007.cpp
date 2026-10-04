// CXP-CAM-PROT-007.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::prot {

namespace {

void prot007(Context& c) {
    c.info("CRC model: the host stack's crc.h, the wire convention this link uses");
    size_t acks = 0, bad = 0;
    for (uint32_t b = 4; b <= 104; b += 4) {
        auto a = c.readRaw(Reg::DEVICE_VENDOR_NAME, b);
        if (!a || a->code != Ack::READ_OK) {
            c.expect(false, "read of %u bytes: %s", b, ackStr(a).c_str());
            continue;
        }
        ++acks;
        if (!a->crc_ok) {
            ++bad;
            if (bad <= size_t(c.opt().max_reported)) c.expect(false, "read ack for %u bytes: CRC mismatch", b);
        }
    }
    c.expect(bad == 0, "%zu of %zu read acknowledgments carry a valid CRC", acks - bad, acks);
    auto pk = streamPackets(c.acquire(c.iparam("images")));
    size_t sbad = 0, with_marker = 0;
    for (const auto& p : pk) {
        sbad += !p.crc_ok;
        with_marker += std::find(p.payload.begin(), p.payload.end(), replicateByte(K28_3)) != p.payload.end();
    }
    c.expect(!pk.empty() && sbad == 0, "%zu of %zu stream packets carry a valid CRC (%zu contain K28.3 markers)",
             pk.size() - sbad, pk.size(), with_marker);
}
CXP_CHECK("CXP-CAM-PROT-007", prot007);

}  // namespace

}  // namespace cxp::validation::checks::prot
