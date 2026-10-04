// CXP-CAM-IMG-002.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::img {

namespace {

void img002(Context& c) {
    const int n_images = c.iparam("images");
    auto ims = imagesOf(c.acquire(size_t(n_images)));
    imageRequired(c, size_t(n_images), ims.size(), "image headers");
    size_t lines_bad = 0, len_bad = 0, n = 0;
    for (const auto& im : ims) {
        if (&im == &ims.back() && im.lines.size() < im.ysize) continue;  // cut by the stop
        ++n;
        if (im.lines.size() != im.ysize) {
            if (++lines_bad <= size_t(c.opt().max_reported)) c.expect(false, "image %u: %zu line markers, Ysize %u", im.source_tag, im.lines.size(), im.ysize);
        }
        size_t off = 0;
        for (size_t w : im.line_words) off += w != im.dsize_l;
        if (off && ++len_bad <= size_t(c.opt().max_reported)) {
            c.expect(false, "image %u: %zu lines not DsizeL = %u words long (first is %zu words)", im.source_tag, off,
                     im.dsize_l, im.line_words.empty() ? 0 : im.line_words.front());
        }
    }
    c.expect(lines_bad == 0, "%zu of %zu images carry one line marker per line", n - lines_bad, n);
    c.expect(len_bad == 0, "%zu of %zu images carry DsizeL words per line", n - len_bad, n);
}
CXP_CHECK("CXP-CAM-IMG-002", img002);

}  // namespace

}  // namespace cxp::validation::checks::img
