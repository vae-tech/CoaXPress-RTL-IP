// CXP-CAM-PERF-002.  See cases/_common.h.

#include "cxp/validation/cases/perf/_helpers.h"

namespace cxp::validation::checks::perf {

namespace {

void perf002(Context& c) {
    preserveFeature(c, "Width");
    preserveFeature(c, "Height");
    Feature& w = c.need("Width");
    Feature& h = c.need("Height");
    const int64_t min_width = c.iparam("min_width");
    trySet(c, "Width", Value::ofInt(w.min ? std::max<int64_t>(int64_t(*w.min), min_width) : min_width));
    trySet(c, "Height", Value::ofInt(h.min ? int64_t(*h.min) : 1));
    auto cap = streamFor(c, c.opt().perf_seconds);
    auto s = analyse(cap);
    const double secs = cap.empty() ? 1 : std::max(1e-3, (cap.back().t_ms - cap.front().t_ms) / 1000.0);
    c.info("minimum ROI: %zu images in %.1f s = %.1f fps", s.images, secs, s.images / secs);
    c.expect(s.images > 0, "images stream at the minimum ROI");
    c.expect(s.tag_breaks == 0 && s.incomplete == 0, "no loss: %zu tag discontinuities, %zu incomplete images",
             s.tag_breaks, s.incomplete);
    c.note("no declared frame rate for this device: the rate is recorded, not judged");
}
CXP_CHECK("CXP-CAM-PERF-002", perf002);

}  // namespace

}  // namespace cxp::validation::checks::perf
