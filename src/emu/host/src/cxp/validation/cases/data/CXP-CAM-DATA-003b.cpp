// CXP-CAM-DATA-003b.  See cases/_common.h.
//
// A ConnectionConfig write while stream packets are on the wire (§8.5.3,
// §10.3.33, v1.1.1 C.2.2: the Packet Tag restarts at 0 on a ConnectionReset
// or a ConnectionConfig write, and only then).  The packet the write meets
// completes intact; the first packet after the write's acknowledgment
// carries tag 0; the image in flight completes or the stream restarts
// cleanly with the next image (header first).

#include "cxp/validation/cases/data/_helpers.h"

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::data {

namespace {

// §9.4: 4 x K28.3, then 4 x 0x01 (rectangular header) or 4 x 0x03 (arbitrary).
bool opensWithHeader(const StreamPkt& p) {
    return p.payload.size() >= 2 && p.payload[0] == replicateByte(K28_3) &&
           (p.payload[1] == replicateByte(0x01) || p.payload[1] == replicateByte(0x03));
}

void data003b(Context& c) {
    smallTpg(c, uint32_t(c.iparam("width")), uint32_t(c.iparam("height")), c.spsmList("spsm").front());
    const uint32_t cc = c.rd32(Reg::CONNECTION_CONFIG);
    std::mt19937 rng(c.seed(3));
    const auto gap = c.ilist("gap_ms_range");
    const int writes = c.iparam("writes");

    std::vector<double> t_ack;
    std::vector<Captured> cap;
    c.startRecording();
    try {
        c.acqStart();
        if (!c.waitHeaders(c.headersSeen() + 1, c.opt().first_image_timeout_ms)) c.abort("no image header");
        for (int i = 0; i < writes; ++i) {
            c.sleepRawMs(int(gap[0] + int64_t(rng() % uint32_t(gap[1] - gap[0] + 1))));
            const double t0 = c.nowMs();
            auto a = c.writeRaw(Reg::CONNECTION_CONFIG, {cc});
            if (!c.expect(is(a, Ack::WRITE_OK), "write %d: ConnectionConfig = %s answered %s", i, hex(cc).c_str(),
                          ackStr(a).c_str())) {
                continue;
            }
            t_ack.push_back(t0);
        }
        const size_t h = c.headersSeen();
        if (c.waitHeaders(h + 1, c.opt().first_image_timeout_ms)) c.waitTail(h + 1);
        cap = c.takeRecording();
        c.acqStop();
        c.waitQuiet();
        c.stopRecording();
    } catch (...) {
        c.stopRecording();
        throw;
    }
    for (auto& t : t_ack) t = ackTime(cap, t);
    const auto pk = streamPackets(cap);

    // Table 19 on every packet: the one a write met completed intact.
    size_t bad = 0;
    for (const auto& p : pk) bad += !p.defects.empty() || p.total_words != p.payload.size() + 8;
    c.expect(!pk.empty() && bad == 0, "%zu of %zu stream packets complete and match Table 19 (CRC, DsizeP)",
             pk.size() - bad, pk.size());

    // Tags: +1 everywhere except the first packet after each write, which is 0.
    size_t restarts_ok = 0, breaks = 0, busy = 0;
    for (size_t w = 0; w < t_ack.size(); ++w) {
        const auto it = std::find_if(pk.begin(), pk.end(), [&](const StreamPkt& p) { return p.t_ms > t_ack[w]; });
        if (it == pk.end()) {
            c.expect(false, "write %zu: no stream packet after its acknowledgment", w);
            continue;
        }
        busy += it != pk.begin() && (it - 1)->t_ms > (w ? t_ack[w - 1] : -1);
        const bool ok = it->tag == 0;
        restarts_ok += ok;
        if (!ok && restarts_ok + 3 > w) {
            c.expect(false, "write %zu: first packet after the acknowledgment has tag %u (0)", w, it->tag);
        }
        // A clean restart: the packet after the write opens an image, or
        // continues the image of the packet before it.
        if (!opensWithHeader(*it)) c.info("write %zu: the stream goes on inside the image (no restart)", w);
    }
    c.expect(restarts_ok == t_ack.size(), "%zu of %zu writes: the next packet carries tag 0", restarts_ok, t_ack.size());
    for (size_t i = 1; i < pk.size(); ++i) {
        const bool at_write = std::any_of(t_ack.begin(), t_ack.end(), [&](double t) {
            return pk[i - 1].t_ms < t && pk[i].t_ms > t;
        });
        if (at_write || pk[i].tag == ((pk[i - 1].tag + 1) & 0xFF)) continue;
        if (++breaks <= size_t(c.opt().max_reported)) {
            c.expect(false, "tag %u followed by %u with no ConnectionConfig write between", pk[i - 1].tag, pk[i].tag);
        }
    }
    c.expect(breaks == 0, "tags +1 mod 256 between the writes over %zu packets (%zu breaks)", pk.size(), breaks);

    // Images: complete, or cut at a write and followed by a fresh image.
    const auto ims = walkImages(pk);
    size_t cut = 0, torn = 0;
    for (size_t i = 0; i < ims.size(); ++i) {
        const auto& im = ims[i];
        if (imageComplete(im)) continue;
        if (i + 1 == ims.size()) continue;  // the last one, cut by the stop
        const bool at_write = std::any_of(t_ack.begin(), t_ack.end(), [&](double t) {
            return im.t_last_ms < t && ims[i + 1].t_first_ms > t;
        });
        ++(at_write ? cut : torn);
        if (!at_write && torn <= size_t(c.opt().max_reported)) {
            c.expect(false, "image SourceTag %u: %zu of %u lines, not at a ConnectionConfig write", im.source_tag,
                     im.lines.size(), im.ysize);
        }
    }
    c.expect(torn == 0, "%zu images: %zu complete, %zu cut at a write and restarted with the next image, %zu torn "
             "elsewhere", ims.size(), ims.size() - cut - torn, cut, torn);
    // The test pattern fills a packet at about a quarter of the link rate, so
    // a packet is on the wire about a fifth of the time: a few of the writes
    // meet one, the rest fall between packets of an image.
    c.info("%zu of %zu writes came while an image was being sent (a packet between the previous write and this one)",
           busy, t_ack.size());
}
CXP_CHECK("CXP-CAM-DATA-003b", data003b);

}  // namespace

}  // namespace cxp::validation::checks::data
