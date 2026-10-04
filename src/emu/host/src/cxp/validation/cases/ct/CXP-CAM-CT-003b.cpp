// CXP-CAM-CT-003b.  See cases/_common.h.
//
// TestMode entered while the device streams and while a device trigger
// waits for the host's acknowledgment.  §8.7.4: in TestMode no data other
// than connection-test (or control) packets; a stream packet already on the
// wire completes (a torn packet is a Table 19 violation), none starts; D2:
// triggers and I/O acknowledgments continue in TestMode.  Packet tags
// continue across TestMode (§8.5.3: only ConnectionReset and a
// ConnectionConfig write reset them).

#include "cxp/validation/cases/trig/_device_trig.h"

namespace cxp::validation::checks::ct {

namespace {

// Stream framing without the last-image allowance of the UVM scoreboard: an
// image cut by TestMode (§8.7.4 stops its data) may be short; every image
// that starts after TestMode ends must be complete.
void judgeStream(Context& c, const std::vector<StreamPkt>& pk, double t_off) {
    size_t bad = 0, breaks = 0;
    for (size_t i = 0; i < pk.size(); ++i) {
        bad += !(pk[i].defects.empty() && pk[i].total_words == pk[i].payload.size() + 8);
        if (i && pk[i].stream_id == pk[i - 1].stream_id && pk[i].tag != ((pk[i - 1].tag + 1) & 0xFF)) {
            if (++breaks <= size_t(c.opt().max_reported)) {
                c.expect(false, "stream tag %u followed by %u", pk[i - 1].tag, pk[i].tag);
            }
        }
    }
    c.expect(!pk.empty() && bad == 0, "%zu of %zu stream packets match Table 19 (none torn by TestMode)",
             pk.size() - bad, pk.size());
    c.expect(breaks == 0, "packet tags +1 mod 256 across TestMode over %zu packets", pk.size());
    const auto ims = walkImages(pk);
    size_t after = 0, after_ok = 0, cut = 0;
    for (size_t i = 0; i + 1 < ims.size(); ++i) {  // the last one is cut by the stop
        if (ims[i].t_first_ms > t_off) {
            ++after;
            after_ok += imageComplete(ims[i]);
        } else if (!imageComplete(ims[i])) {
            ++cut;
        }
    }
    c.expect(after > 0 && after_ok == after, "%zu of %zu images started after TestMode = 0 complete", after_ok, after);
    c.info("%zu image(s) before TestMode = 0 incomplete (cut by TestMode)", cut);
}

void ct003b(Context& c) {
    devtrig::cleanStart(c, 0);
    uvm::runTestPattern(c);
    c.prepareStreaming();
    c.onExit([&c] { c.writeRaw(Reg::TEST_MODE, {0}); });
    std::vector<Captured> cap;
    double t_on = 0, t_off = 0;
    uvm::HostTrigRun h;
    const double since = c.nowMs();
    c.startRecording();
    try {
        const size_t h0 = c.headersSeen();
        c.acqStart();
        if (!c.waitHeaders(h0 + 1, c.opt().first_image_timeout_ms)) c.info("no image header before the trigger");
        // A device trigger; the host holds its acknowledgment while TestMode goes on.
        c.benchSend({bench::PIN, bench::TRIG_IN, 1});
        devtrig::waitTrigs(c, 1, since, 1000);
        t_on = c.nowMs();
        c.wr32(Reg::TEST_MODE, 1);
        c.sleepRawMs(c.iparam("ack_after_ms"));
        c.sendChars(ioAck());
        // Host triggers and a second device edge in TestMode (D2).
        for (bool rising : {true, false}) {
            c.sendChars(lsTrigger(rising, 0));
            ++h.sent;
            h.rising += rising;
            c.sleepRawMs(10);
        }
        c.benchSend({bench::PIN, bench::TRIG_IN, 0});
        devtrig::waitTrigs(c, 2, since, 1000);
        c.sendChars(ioAck());
        c.sleepMs(c.iparam("hold_ms"));
        t_off = c.nowMs();
        c.wr32(Reg::TEST_MODE, 0);
        const size_t h1 = c.headersSeen();
        if (c.waitHeaders(h1 + 2, 10000)) c.waitTail(h1 + 2);
        c.acqStop();
        c.waitQuiet();
        cap = c.stopRecording();
    } catch (...) {
        c.stopRecording();
        throw;
    }
    c.benchTime();

    // No stream packet between the first and the last test packet.
    std::vector<int> type;
    for (const auto& f : cap) type.push_back(f.frame.size() >= 3 && majorityByte(f.frame[1]).ok ? majorityByte(f.frame[1]).value : -1);
    const auto first = std::find(type.begin(), type.end(), 4);
    const auto last = std::find(type.rbegin(), type.rend(), 4);
    size_t stream_in = 0, test = 0, test_bad = 0;
    if (first != type.end()) {
        for (auto it = first; it != last.base(); ++it) stream_in += *it == 1;
    }
    for (const auto& f : framesOfType(cap, 0x04)) {
        ++test;
        test_bad += linkTestErrors(f.frame) != 0;
    }
    c.expect(test > 0 && test_bad == 0, "%zu of %zu connection-test packets intact", test - test_bad, test);
    c.expect(test > 0 && stream_in == 0, "%zu stream packets between the first and the last test packet", stream_in);
    judgeStream(c, streamPackets(cap), t_off);

    // Triggers: the device's exactly as the input went (a resend of the same
    // level after the timeout would be allowed, §8.3.3), the host's all
    // acknowledged, no extra acknowledgment.
    const auto dt = devtrig::trigs(c, since);
    std::string k = devtrig::kinds(dt), dedup;
    for (char x : k) {
        if (dedup.empty() || dedup.back() != x) dedup += x;
    }
    c.expect(dedup == "RF", "device trigger across TestMode entry and exit: packets %s (R then F)", k.c_str());
    if (k.size() > 2) c.info("the device resent an unacknowledged level (%s)", k.c_str());
    const auto acks = devtrig::ioAcks(c, since);
    size_t clean = 0;
    for (const auto& a : acks) clean += a.pkt.clean && a.pkt.value == IOACK_OK;
    c.expect(acks.size() == h.sent && clean == h.sent,
             "%zu host triggers in TestMode, %zu I/O acknowledgments (%zu clean), none stale or extra", h.sent,
             acks.size(), clean);
    (void)t_on;
}
CXP_CHECK("CXP-CAM-CT-003b", ct003b);

}  // namespace

}  // namespace cxp::validation::checks::ct
