// CXP-CAM-DATA-003.  See cases/_common.h.

#include "cxp/validation/cases/data/_helpers.h"

namespace cxp::validation::checks::data {

namespace {

void data003(Context& c) {
    preserveLink(c);
    preserveFeature(c, "Width");
    c.prepareStreaming();
    const size_t n = size_t(c.iparam("images"));
    auto t1 = tagsOfRun(c, n);
    auto w = featInt(c, "Width");
    const int64_t step = c.iparam("width_step");
    if (w) trySet(c, "Width", Value::ofInt(*w > 2 * step ? *w - step : *w + step));
    auto t2 = tagsOfRun(c, n);
    c.expect(t2.front() == ((t1.back() + 1) & 0xFF), "after stop / Width change / start: first tag %u follows %u",
             t2.front(), t1.back());
    c.wr32(Reg::CONNECTION_CONFIG, c.rd32(Reg::CONNECTION_CONFIG));
    auto t3 = tagsOfRun(c, n);
    c.expect(t3.front() == 0, "after a same-value ConnectionConfig write: first tag %u (0)", t3.front());
    connectionReset(c);
    if (c.rd32(Reg::STREAM_PACKET_SIZE_MAX) == 0) c.wr32(Reg::STREAM_PACKET_SIZE_MAX, c.opt().host_spsm);
    auto t4 = tagsOfRun(c, n);
    c.expect(t4.front() == 0, "after ConnectionReset: first tag %u (0)", t4.front());
}
CXP_CHECK("CXP-CAM-DATA-003", data003);

}  // namespace

}  // namespace cxp::validation::checks::data
