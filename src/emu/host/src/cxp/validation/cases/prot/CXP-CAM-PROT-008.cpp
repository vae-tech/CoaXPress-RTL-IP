// CXP-CAM-PROT-008.  See cases/_common.h.
//
// §8.2.2.2 / §9.2: the stream CRC counts a K28.3 marker as the data byte
// 0x7C and leaves out the IDLE words a transmitter stretches a packet with
// (§8.2.5.2).  The host's decoder computes the CRC over the bytes the link
// delivered (K28.3 -> 0x7C) with the stretching IDLE words already removed
// by the bench, so every stream packet that carries markers or was
// stretched and still passes proves the Device computed it the same way.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::prot {

namespace {

constexpr uint32_t kMarker = replicateByte(K28_3);

void prot008(Context& c) {
    uvm::runTestPattern(c);
    const bool stats = (c.benchCaps() & bench::CAP_TIMES) != 0;
    if (stats) c.benchRequest({bench::DL_STATS});  // start the count here
    const auto cap = c.acquire(size_t(c.iparam("images")));
    std::optional<Words> st;
    if (stats) st = c.benchRequest({bench::DL_STATS});
    const auto pk = streamPackets(cap);
    size_t marked = 0, marked_bad = 0, bad = 0;
    for (const auto& p : pk) {
        const bool has = std::find(p.payload.begin(), p.payload.end(), kMarker) != p.payload.end();
        marked += has;
        marked_bad += has && !p.crc_ok;
        bad += !p.crc_ok;
    }
    c.expect(marked > 0, "%zu of %zu stream packets carry K28.3 markers (image header, line markers)", marked,
             pk.size());
    c.expect(marked > 0 && marked_bad == 0, "every packet with markers passes its CRC with K28.3 as 0x7C (%zu bad)",
             marked_bad);
    c.expect(!pk.empty() && bad == 0, "%zu of %zu stream packets pass their CRC", pk.size() - bad, pk.size());
    if (!st || st->size() < 4) {
        c.note("no bench downlink statistics: whether the Device stretched packets with IDLE is not known");
        return;
    }
    const uint32_t packets = (*st)[0], idle_in = (*st)[1];
    if (idle_in == 0) {
        c.note("the Device stretched none of %u packets with IDLE words: the IDLE exclusion was not exercised",
               packets);
        return;
    }
    c.expect(bad == 0, "%u IDLE words inside %u packets were left out of the CRC: every stream packet passes",
             idle_in, packets);
}
CXP_CHECK("CXP-CAM-PROT-008", prot008);

}  // namespace

}  // namespace cxp::validation::checks::prot
