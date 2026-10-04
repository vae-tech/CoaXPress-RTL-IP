// CXP-CAM-BND-003b.  See cases/_common.h.
//
// StreamPacketSizeMax below the smallest useful packet (36 bytes: the 8
// words of Table 19 around one data word) and not a multiple of 4
// (§10.3.32).  Decision D4: not a multiple of 4 is refused 0x41 and the
// register keeps its value; a multiple of 4 below 36 is accepted and holds
// the stream (IDLE only) until a usable value comes back.

#include "cxp/validation/cases/data/_helpers.h"

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::bnd {

namespace {

size_t longer(const std::vector<StreamPkt>& pk, uint32_t bound, double after_ms) {
    size_t n = 0;
    for (const auto& p : pk) n += p.t_ms > after_ms && uint64_t(p.total_words) * 4 > bound;
    return n;
}

void bnd003b(Context& c) {
    const uint32_t host = c.opt().host_spsm;
    data::smallTpg(c, uint32_t(c.iparam("width")), uint32_t(c.iparam("height")), host);
    const auto bad = c.spsmList("not_multiple"), below = c.spsmList("below_36");

    // Idle.
    for (uint32_t v : bad) {
        const uint32_t old = c.rd32(Reg::STREAM_PACKET_SIZE_MAX);
        auto a = c.writeRaw(Reg::STREAM_PACKET_SIZE_MAX, {v});
        const uint32_t now = c.rd32(Reg::STREAM_PACKET_SIZE_MAX);
        c.expect(is(a, Ack::BAD_DATA) && now == old, "idle: SPSM = %u answered %s, reads %u (0x41, keeps %u)", v,
                 ackStr(a).c_str(), now, old);
    }
    for (uint32_t v : below) {
        auto a = c.writeRaw(Reg::STREAM_PACKET_SIZE_MAX, {v});
        const uint32_t now = c.rd32(Reg::STREAM_PACKET_SIZE_MAX);
        c.expect(is(a, Ack::WRITE_OK) && now == v, "idle: SPSM = %u answered %s, reads %u (0x01, holds %u)", v,
                 ackStr(a).c_str(), now, v);
        c.startRecording();
        c.acqStart();
        c.sleepMs(c.iparam("hold_ms"));
        c.acqStop();
        const auto pk = streamPackets(c.stopRecording());
        c.expect(pk.empty(), "SPSM %u: %zu stream packets during a %d ms acquisition (none: no packet fits)", v,
                 pk.size(), c.iparam("hold_ms"));
        c.expect(c.tryRd32(Reg::STANDARD) == CXP_MAGIC, "SPSM %u: the device answers a read", v);
        c.wr32(Reg::STREAM_PACKET_SIZE_MAX, host);
        const auto back = streamPackets(c.acquire(size_t(c.iparam("images"))));
        const auto sb = uvm::streamScoreboard(c, back, -1, host, {}, strprintf("SPSM %u -> %u: ", v, host).c_str());
        c.expect(sb.complete >= size_t(c.iparam("images")), "SPSM %u -> %u: %zu complete images (>= %d)", v, host,
                 sb.complete, c.iparam("images"));
    }

    // Streaming: every value written while an image is on its way.
    for (uint32_t v : below) {
        c.startRecording();
        std::vector<Captured> cap;
        double t_bad = -1, t_hold = -1, t_back = -1;
        try {
            c.acqStart();
            if (!c.waitHeaders(c.headersSeen() + 1, c.opt().first_image_timeout_ms)) c.abort("no image header");
            for (uint32_t b : bad) {
                const double t0 = c.nowMs();
                auto a = c.writeRaw(Reg::STREAM_PACKET_SIZE_MAX, {b});
                c.expect(is(a, Ack::BAD_DATA), "streaming: SPSM = %u answered %s (0x41)", b, ackStr(a).c_str());
                if (t_bad < 0) t_bad = t0;
            }
            c.expect(c.rd32(Reg::STREAM_PACKET_SIZE_MAX) == host, "streaming: SPSM still %u after the refused writes",
                     host);
            t_hold = c.nowMs();
            auto a = c.writeRaw(Reg::STREAM_PACKET_SIZE_MAX, {v});
            c.expect(is(a, Ack::WRITE_OK), "streaming: SPSM = %u answered %s (0x01)", v, ackStr(a).c_str());
            c.sleepMs(c.iparam("hold_ms"));
            c.expect(c.tryRd32(Reg::STANDARD) == CXP_MAGIC, "streaming, SPSM %u: the device answers a read", v);
            t_back = c.nowMs();
            c.wr32(Reg::STREAM_PACKET_SIZE_MAX, host);
            const size_t h = c.headersSeen();
            if (c.waitHeaders(h + size_t(c.iparam("images")), c.iparam("resume_timeout_ms"))) c.waitTail(c.headersSeen());
            cap = c.takeRecording();
            c.acqStop();
            c.waitQuiet();
            c.stopRecording();
        } catch (...) {
            c.stopRecording();
            throw;
        }
        const double ack_hold = data::ackTime(cap, t_hold), ack_back = data::ackTime(cap, t_back);
        const auto pk = streamPackets(cap);
        size_t held = 0, held_over = 0;
        // Held: after the small value's 0x01 and before the host sent the
        // write that restores its value.  Packets the device held go out
        // as soon as it executes that write, possibly before the write's
        // own 0x01 is on the link, so the window ends at the send.
        for (const auto& p : pk) {
            if (p.t_ms > ack_hold && p.t_ms < t_back) {
                ++held;
                held_over += uint64_t(p.total_words) * 4 > v;
            }
        }
        c.expect(held_over == 0, "streaming: %zu packets between the 0x01 for SPSM %u and the next write, %zu longer "
                 "than %u bytes (none may be)", held, v, held_over, v);
        c.expect(longer(pk, host, -1) == 0, "streaming: no packet longer than %u bytes", host);
        const auto sb = uvm::streamScoreboard(c, pk, -1, 0, {}, strprintf("streaming, SPSM %u held: ", v).c_str());
        size_t after = 0;
        for (const auto& p : pk) after += p.t_ms > ack_back;
        c.expect(after > 0 && sb.complete >= size_t(c.iparam("images")),
                 "streaming: %zu packets after SPSM %u came back, %zu complete images", after, host, sb.complete);
        (void)t_bad;
    }
}
CXP_CHECK("CXP-CAM-BND-003b", bnd003b);

}  // namespace

}  // namespace cxp::validation::checks::bnd
