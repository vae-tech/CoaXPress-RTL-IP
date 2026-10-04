// CXP-CAM-PERF-003.  See cases/_common.h.

#include "cxp/validation/cases/perf/_helpers.h"

namespace cxp::validation::checks::perf {

namespace {

void perf003(Context& c) {
    c.prepareStreaming();
    const int secs = c.opt().soak_seconds;
    c.info("soak for %d s (Options::soak_seconds)", secs);
    c.startRecording();
    const size_t h0 = c.headersSeen();
    c.acqStart();
    c.waitHeaders(h0 + 1, c.opt().first_image_timeout_ms);
    StreamStats tot;
    size_t ctrl_timeouts = 0, reads = 0;
    double worst = 0;
    std::vector<StreamPkt> carry;
    int last_tag = -1;
    int last_st = -1;
    auto window = [&](std::vector<Captured> cap, bool final) {
        auto fresh = streamPackets(cap);
        for (const auto& p : fresh) {
            tot.crc_bad += !p.crc_ok;
            tot.payload_bytes += p.payload.size() * 4;
            if (last_tag >= 0 && p.tag != ((last_tag + 1) & 0xFF)) ++tot.tag_breaks;
            last_tag = p.tag;
        }
        tot.packets += fresh.size();
        std::vector<StreamPkt> pk = std::move(carry);
        pk.insert(pk.end(), fresh.begin(), fresh.end());
        auto ims = walkImages(pk);
        const size_t n_done = final ? ims.size() : (ims.empty() ? 0 : ims.size() - 1);
        for (size_t i = 0; i < n_done; ++i) {
            ++tot.images;
            if (!imageComplete(ims[i]) && !(final && i + 1 == ims.size())) ++tot.incomplete;
            if (last_st >= 0 && ims[i].source_tag != uint32_t((last_st + 1) & 0xFFFF)) ++tot.st_breaks;
            last_st = int(ims[i].source_tag);
        }
        carry.clear();
        if (!final && !ims.empty()) {
            for (const auto& p : pk) {
                if (p.t_ms >= ims.back().t_first_ms) carry.push_back(p);
            }
        }
    };
    const double end = c.nowMs() + secs * 1000.0;
    const int window_ms = c.iparam("window_ms");
    double next_window = c.nowMs() + window_ms;
    while (c.nowMs() < end) {
        c.sleepRawMs(c.iparam("read_period_ms"));  // the soak's read cadence
        auto a = c.readRaw(Reg::STANDARD, 4);
        ++reads;
        if (!is(a, Ack::READ_OK)) ++ctrl_timeouts;
        else worst = std::max(worst, a->latency_ms);
        if (c.nowMs() >= next_window) {
            window(c.takeRecording(), false);
            next_window += window_ms;
        }
    }
    c.acqStop();
    c.waitQuiet();
    window(c.stopRecording(), true);
    c.info("%zu packets, %zu images, %.2f MB payload, %zu control reads (worst %.2f ms)", tot.packets, tot.images,
           tot.payload_bytes / 1e6, reads, worst);
    c.expect(tot.crc_bad == 0, "%zu CRC errors", tot.crc_bad);
    c.expect(tot.tag_breaks == 0, "%zu packet-tag discontinuities (lost packets)", tot.tag_breaks);
    c.expect(tot.incomplete == 0, "%zu incomplete images", tot.incomplete);
    c.expect(tot.st_breaks == 0, "%zu SourceTag discontinuities (lost or repeated images)", tot.st_breaks);
    c.expect(ctrl_timeouts == 0 && worst <= c.opt().ack_latency_ms,
             "%zu of %zu periodic reads failed, worst latency %.2f ms (<= %d ms)", ctrl_timeouts, reads, worst,
             c.opt().ack_latency_ms);
}
CXP_CHECK("CXP-CAM-PERF-003", perf003);

}  // namespace

}  // namespace cxp::validation::checks::perf
