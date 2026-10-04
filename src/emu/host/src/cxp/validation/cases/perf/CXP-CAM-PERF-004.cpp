// CXP-CAM-PERF-004.  See cases/_common.h.

#include "cxp/validation/cases/perf/_helpers.h"

namespace cxp::validation::checks::perf {

namespace {

void perf004(Context& c) {
    const int n = c.iparam("images");
    auto cap = c.acquire(size_t(n), c.iparam("acquire_timeout_ms"));
    auto pk = streamPackets(cap);
    auto ims = walkImages(pk);
    imageRequired(c, size_t(n), ims.size(), "back-to-back images");
    size_t malformed = 0;
    for (const auto& p : pk) malformed += !p.defects.empty();
    size_t partial = 0;
    for (size_t i = 0; i + 1 < ims.size(); ++i) partial += !imageComplete(ims[i]);
    std::vector<double> gaps;
    for (size_t i = 1; i < ims.size(); ++i) gaps.push_back(ims[i].t_first_ms - ims[i - 1].t_last_ms);
    c.expect(malformed == 0, "%zu malformed stream packets", malformed);
    c.expect(partial == 0, "%zu partial images between complete ones", partial);
    if (!gaps.empty()) {
        c.info("inter-image gap (last line -> next header): min %.2f ms, median %.2f ms",
               *std::min_element(gaps.begin(), gaps.end()), percentile(gaps, 50));
    }
    c.note("a sensor rate above link capacity cannot be configured on the emulator; drop/throttle behaviour not run");
}
CXP_CHECK("CXP-CAM-PERF-004", perf004);

}  // namespace

}  // namespace cxp::validation::checks::perf
