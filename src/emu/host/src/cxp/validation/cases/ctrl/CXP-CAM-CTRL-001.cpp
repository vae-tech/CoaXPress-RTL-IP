// CXP-CAM-CTRL-001.  See cases/_common.h.

#include "cxp/validation/cases/ctrl/_helpers.h"

namespace cxp::validation::checks::ctrl {

namespace {

void ctrl001(Context& c) {
    auto ref = c.readRaw(Reg::DEVICE_VENDOR_NAME, 104);
    if (!is(ref, Ack::READ_OK)) c.abort("104-byte reference read at 0x2000 answered " + ackStr(ref));
    const auto rb = ref->bytes();
    size_t good = 0;
    for (uint32_t b = 1; b <= 104; ++b) {
        c.checkpoint();
        auto a = c.readRaw(Reg::DEVICE_VENDOR_NAME, b);
        std::string why;
        if (!is(a, Ack::READ_OK)) {
            why = ackStr(a);
        } else {
            const size_t n = (b + 3) / 4;
            auto by = a->bytes();
            if (!a->ok()) why = a->defects.front();
            else if (a->size_field != b) why = strprintf("Size %u", a->size_field);
            else if (a->data.size() != n) why = strprintf("%zu data words (want %zu)", a->data.size(), n);
            else if (std::any_of(by.begin() + b, by.end(), [](uint8_t x) { return x != 0; })) why = "non-zero padding";
            else if (!std::equal(by.begin(), by.begin() + b, rb.begin())) why = "data differs from the 104-byte read";
        }
        if (why.empty()) {
            ++good;
        } else if (b - good <= size_t(c.opt().max_reported)) {
            c.expect(false, "read of %u bytes: %s", b, why.c_str());
        }
    }
    c.expect(good == 104, "%zu of 104 read sizes answered with a correct acknowledgment", good);
}
CXP_CHECK("CXP-CAM-CTRL-001", ctrl001);

}  // namespace

}  // namespace cxp::validation::checks::ctrl
