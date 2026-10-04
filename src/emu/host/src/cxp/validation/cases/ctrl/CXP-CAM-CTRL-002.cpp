// CXP-CAM-CTRL-002.  See cases/_common.h.

#include "cxp/validation/cases/ctrl/_helpers.h"

namespace cxp::validation::checks::ctrl {

namespace {

void ctrl002(Context& c) {
    const auto old = c.readBlock(Reg::DEVICE_USER_ID, 16);
    auto words = [](const std::vector<uint8_t>& b) {
        std::vector<uint32_t> v;
        for (size_t i = 0; i < b.size(); i += 4) {
            v.push_back(uint32_t(b[i]) << 24 | uint32_t(b[i + 1]) << 16 | uint32_t(b[i + 2]) << 8 | b[i + 3]);
        }
        return v;
    };
    c.onExit([&c, old, words] { c.writeRaw(Reg::DEVICE_USER_ID, words(old)); });
    size_t good = 0, form_bad = 0;
    for (uint32_t b = 1; b <= 16; ++b) {
        c.writeRaw(Reg::DEVICE_USER_ID, words(std::vector<uint8_t>(16, 0x55)));
        std::vector<uint8_t> pat((b + 3) / 4 * 4, 0);
        for (uint32_t i = 0; i < b; ++i) pat[i] = uint8_t(0x41 + i);
        CmdSpec s;
        s.opcode = 0x01;
        s.size_bytes = b;
        s.address = Reg::DEVICE_USER_ID;
        for (uint32_t v : words(pat)) s.data.push_back(bswap32(v));
        auto a = c.exchange(buildCmd(s));
        const bool form = is(a, Ack::WRITE_OK) && !a->long_form && a->ok() && a->n_words == 4;
        form_bad += !form;
        auto back = c.readBlock(Reg::DEVICE_USER_ID, 16);
        bool data_ok = true;
        for (uint32_t i = 0; i < 16; ++i) data_ok &= back[i] == (i < b ? uint8_t(0x41 + i) : uint8_t(0x55));
        if (form && data_ok) {
            ++good;
        } else if (b - good <= size_t(c.opt().max_reported)) {
            c.expect(false, "write of %u bytes: ack %s%s, %s", b, ackStr(a).c_str(),
                     a && a->long_form ? " (long form)" : "", data_ok ? "data correct" : "bytes beyond B changed or data wrong");
        }
    }
    c.expect(good == 16, "%zu of 16 write sizes: 4-word 0x01 ack and only B bytes written", good);
    c.expect(form_bad == 0, "%zu write acknowledgments not the 4-word short form", form_bad);
    CmdSpec s;
    s.opcode = 0x01;
    s.size_bytes = 1;
    s.address = Reg::DEVICE_USER_ID;
    s.data = {bswap32(0x41FFFFFF)};
    auto a = c.exchange(buildCmd(s));
    c.note("write of 1 byte with non-zero padding: %s", ackStr(a).c_str());
}
CXP_CHECK("CXP-CAM-CTRL-002", ctrl002);

}  // namespace

}  // namespace cxp::validation::checks::ctrl
