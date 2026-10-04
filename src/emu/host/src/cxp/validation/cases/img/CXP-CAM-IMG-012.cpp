// CXP-CAM-IMG-012.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::img {

namespace {

void img012(Context& c) {
    c.prepareStreaming();
    std::minstd_rand rng(c.seed(12));
    size_t clean = 0;
    const int trials = c.iparam("trials");
    const auto delay = c.ilist("stop_delay_range");
    const int late_ms = c.iparam("late_ms");
    for (int t = 0; t < trials; ++t) {
        c.startRecording();
        const size_t h0 = c.headersSeen();
        c.acqStart();
        c.waitHeaders(h0 + 1, c.opt().first_image_timeout_ms);
        c.sleepRawMs(std::uniform_int_distribution<int>(int(delay[0]), int(delay[1]))(rng));
        c.acqStop();
        const double t_stop = c.nowMs();
        c.waitQuiet();
        c.sleepMs(c.iparam("after_ms"));
        auto cap = c.stopRecording();
        auto ims = imagesOf(cap);
        size_t late = 0;
        for (const auto& f : framesOfType(cap, 0x01)) late += f.t_ms > t_stop + late_ms;
        const bool last_ok = !ims.empty() && imageComplete(ims.back());
        if (last_ok && late == 0) {
            ++clean;
        } else if (int(clean) >= t - 2) {
            c.expect(false, "stop %d: last image %s, %zu packets later than %d ms after the stop", t + 1,
                     ims.empty() ? "missing" : strprintf("%zu of %u lines", ims.back().lines.size(), ims.back().ysize).c_str(),
                     late, late_ms);
        }
    }
    c.expect(clean == size_t(trials), "%zu of %d AcquisitionStop trials end on a complete image and a quiet link", clean,
             trials);
}
CXP_CHECK("CXP-CAM-IMG-012", img012);

}  // namespace

}  // namespace cxp::validation::checks::img
