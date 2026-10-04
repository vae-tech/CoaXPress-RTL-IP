// CXP-CAM-PERF-001.  See cases/_common.h.

#include "cxp/validation/cases/perf/_helpers.h"

namespace cxp::validation::checks::perf {

namespace {

void perf001(Context& c) {
    c.preserve(Reg::STREAM_PACKET_SIZE_MAX);
    c.prepareStreaming();
    c.wr32(Reg::STREAM_PACKET_SIZE_MAX, c.opt().host_spsm);
    auto cap = streamFor(c, c.opt().perf_seconds);
    auto s = analyse(cap);
    const double secs = cap.empty() ? 1 : std::max(1e-3, (cap.back().t_ms - cap.front().t_ms) / 1000.0);
    c.info("%.1f s: %zu packets, %zu images, %.3f MB/s payload, %.1f packets/s", secs, s.packets, s.images,
           s.payload_bytes / secs / 1e6, s.packets / secs);
    c.expect(s.packets > 0, "stream sustained for %d s", c.opt().perf_seconds);
    c.expect(s.crc_bad == 0, "%zu CRC errors", s.crc_bad);
    c.expect(s.tag_breaks == 0, "%zu lost packets (tag discontinuities)", s.tag_breaks);
    c.note("no declared throughput for this device: the rate is recorded, not judged; IDLE ratio is not "
           "observable on the FIFO link");
}
CXP_CHECK("CXP-CAM-PERF-001", perf001);

}  // namespace

}  // namespace cxp::validation::checks::perf
