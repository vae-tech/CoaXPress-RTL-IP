// CXP-CAM-IMG-005.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::img {

namespace {

void img005(Context& c) {
    const int n = c.iparam("images");
    auto ims = imagesOf(c.acquire(size_t(n)));
    imageRequired(c, size_t(n), ims.size(), "image headers");
    size_t breaks = 0;
    for (size_t i = 1; i < ims.size(); ++i) {
        if (ims[i].source_tag != ((ims[i - 1].source_tag + 1) & 0xFFFF)) {
            if (++breaks <= size_t(c.opt().max_reported)) c.expect(false, "SourceTag %u followed by %u", ims[i - 1].source_tag, ims[i].source_tag);
        }
    }
    c.expect(breaks == 0, "SourceTag increments by one over %zu images", ims.size());
    if (Feature* st = c.feature("SourceTag"); st && st->isWritable()) {
        preserveFeature(c, "SourceTag");
        trySet(c, "SourceTag", Value::ofInt(0xFFFE));
        auto w = imagesOf(c.acquire(c.iparam("wrap_images")));
        bool wrapped = false;
        for (size_t i = 1; i < w.size(); ++i) wrapped |= w[i - 1].source_tag == 0xFFFF && w[i].source_tag == 0;
        c.expect(wrapped, "SourceTag preset to 0xFFFE wraps 0xFFFF -> 0x0000");
    } else {
        c.note("no writable SourceTag preset; wrap needs 65 536 images and is not run");
    }
}
CXP_CHECK("CXP-CAM-IMG-005", img005);

}  // namespace

}  // namespace cxp::validation::checks::img
