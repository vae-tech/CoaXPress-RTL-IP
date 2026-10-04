// CXP-CAM-BND-003.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::bnd {

namespace {

void bnd003(Context& c) {
    preserveLink(c);
    for (uint32_t id : {0x00000001u, 0xFFFFFFFFu}) {
        auto w = c.writeRaw(Reg::MASTER_HOST_CONNECTION_ID, {id});
        const uint32_t r = c.rd32(Reg::MASTER_HOST_CONNECTION_ID);
        c.expect(is(w, Ack::WRITE_OK) && r == id, "MasterHostConnectionID = %s: %s, reads %s", hex(id).c_str(),
                 ackStr(w).c_str(), hex(r).c_str());
    }
    c.prepareStreaming();
    for (uint32_t spsm : c.spsmList("spsm_list")) {
        auto w = c.writeRaw(Reg::STREAM_PACKET_SIZE_MAX, {spsm});
        if (!c.expect(is(w, Ack::WRITE_OK), "StreamPacketSizeMax = %s: %s", hex(spsm).c_str(), ackStr(w).c_str())) continue;
        auto cap = c.acquire(c.iparam("images"), c.iparam("acquire_timeout_ms"));
        auto pk = streamPackets(cap);
        size_t over = 0, dsize_over = 0;
        for (const auto& p : pk) {
            over += uint64_t(p.total_words) * 4 > spsm;
            dsize_over += p.payload.size() > 0xFFFF;
        }
        auto ims = completeImages(walkImages(pk));
        c.expect(!pk.empty() && over == 0 && dsize_over == 0 && !ims.empty(),
                 "SPSM %s: %zu packets, %zu over SPSM, %zu with DsizeP > 0xFFFF, %zu complete images", hex(spsm).c_str(),
                 pk.size(), over, dsize_over, ims.size());
        if (spsm == 36 && !pk.empty()) {
            size_t one = 0;
            for (const auto& p : pk) one += p.dsize_p == 1;
            c.expect(one == pk.size(), "SPSM 36: %zu of %zu packets carry exactly one data word", one, pk.size());
        }
    }
}
CXP_CHECK("CXP-CAM-BND-003", bnd003);

}  // namespace

}  // namespace cxp::validation::checks::bnd
