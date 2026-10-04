// CXP-EMU-DATA-104.  See cases/_common.h.
//
// StreamPacketSizeMax written while the device streams (§10.3.32: the Host
// sets the largest packet it accepts; the Device may use any size up to it).
// FIFO capture timestamps a packet when its EOP arrives.  The first packet
// captured after an acknowledgment may have started before the write, so it
// may still use the preceding limit.  Later packets obey the new limit.

#include "cxp/validation/cases/data/_helpers.h"

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::data {

namespace {

void data104(Context& c) {
    const auto steps = c.spsmList("spsm_steps");
    const uint32_t first = c.spsmList("spsm_start").front();
    smallTpg(c, uint32_t(c.iparam("width")), uint32_t(c.iparam("height")), first);
    const size_t per_step = size_t(c.iparam("images_per_step"));

    struct Write { double t_ack; uint32_t value; };
    std::vector<Write> writes;
    std::vector<Captured> cap;
    c.startRecording();
    try {
        c.acqStart();
        if (!c.waitHeaders(c.headersSeen() + 1, c.opt().first_image_timeout_ms)) c.abort("no image header after AcquisitionStart");
        for (uint32_t v : steps) {
            const double t0 = c.nowMs();
            auto a = c.writeRaw(Reg::STREAM_PACKET_SIZE_MAX, {v});
            c.expect(is(a, Ack::WRITE_OK), "StreamPacketSizeMax = %u while streaming: %s", v, ackStr(a).c_str());
            writes.push_back({t0, v});
            const size_t h = c.headersSeen();
            if (!c.waitHeaders(h + per_step, c.iparam("step_timeout_ms"))) {
                c.expect(false, "SPSM %u: only %zu of %zu more image headers", v, c.headersSeen() - h, per_step);
            }
        }
        c.waitTail(c.headersSeen());
        cap = c.takeRecording();
        c.acqStop();
        c.waitQuiet();
        c.stopRecording();
    } catch (...) {
        c.stopRecording();
        throw;
    }
    for (auto& w : writes) w.t_ack = ackTime(cap, w.t_ack);
    const auto pk = streamPackets(cap);

    // The bound in force for each packet: the value of the last write
    // acknowledged before the packet arrived.
    std::vector<size_t> over(writes.size() + 1, 0), n(writes.size() + 1, 0), biggest(writes.size() + 1, 0);
    std::vector<size_t> inherited(writes.size() + 1, 0);
    for (const auto& p : pk) {
        size_t seg = 0;
        uint32_t bound = first;
        for (size_t i = 0; i < writes.size(); ++i) {
            if (writes[i].t_ack >= 0 && p.t_ms > writes[i].t_ack) {
                seg = i + 1;
                bound = writes[i].value;
            }
        }
        ++n[seg];
        biggest[seg] = std::max(biggest[seg], p.total_words * 4);
        const bool old_packet = seg && n[seg] == 1 && uint64_t(p.total_words) * 4 <=
                                (seg == 1 ? first : writes[seg - 2].value);
        if (uint64_t(p.total_words) * 4 > bound && old_packet) {
            ++inherited[seg];
            continue;
        }
        if (uint64_t(p.total_words) * 4 > bound && ++over[seg] <= size_t(c.opt().max_reported)) {
            c.expect(false, "packet tag %u of %zu bytes, packet %zu after SPSM %u was acknowledged", p.tag,
                     p.total_words * 4, n[seg], bound);
        }
    }
    for (size_t s = 0; s <= writes.size(); ++s) {
        const uint32_t bound = s ? writes[s - 1].value : first;
        c.expect(n[s] > inherited[s] && over[s] == 0,
                 "SPSM %u: %zu packets, largest %zu bytes, %zu over limit, %zu prior-limit packet in flight",
                 bound, n[s], biggest[s], over[s], inherited[s]);
    }
    const auto sb = uvm::streamScoreboard(c, pk, -1, 0, {}, "");
    c.expect(sb.complete >= writes.size() * per_step, "%zu complete images (>= %zu)", sb.complete,
             writes.size() * per_step);
}
CXP_CHECK("CXP-EMU-DATA-104", data104);

}  // namespace

}  // namespace cxp::validation::checks::data
