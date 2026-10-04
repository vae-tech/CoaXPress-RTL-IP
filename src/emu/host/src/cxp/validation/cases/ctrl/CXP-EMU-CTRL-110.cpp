// CXP-EMU-CTRL-110.  See cases/_common.h.
//
// Writes of B bytes into the user window (Table 21: Size is the number of
// bytes; §10.3 the space is byte-addressed): only those B bytes change.  The
// register file honours its byte enables; the user window is an APB slave,
// so the device must drive PSTRB (APB4) and the bench's slave honours it.
// A device without byte enables writes the last word whole, the pad bytes
// as zero.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::ctrl {

namespace {

void stall(Context& c, uint32_t ms) {
    c.benchSend({bench::REG_STALL, ms, 0});
    c.benchSync();
}

void ctrl110(Context& c) {
    c.needBench(bench::CAP_REG_STALL, "a user register window");
    c.onExit([&c] { stall(c, 0); });
    stall(c, 0);
    const uint32_t base = bench::USER_BASE + 0x100;
    for (int64_t size : c.ilist("sizes")) {
        const uint32_t nw = uint32_t(size + 3) / 4;
        // Sentinels in every word the write touches; the bytes it carries.
        std::vector<uint32_t> before, value(nw, 0);
        for (uint32_t i = 0; i < nw; ++i) {
            before.push_back(0xA1B2C3D4u ^ (i << 4));
            c.wr32(base + 4 * i, before.back());
        }
        for (uint32_t j = 0; j < uint32_t(size); ++j) value[j / 4] |= uint32_t(0x11 * (j % 15 + 1)) << (24 - 8 * (j % 4));
        CmdSpec s;
        s.opcode = 0x01;
        s.size_bytes = uint32_t(size);
        s.address = base;
        for (uint32_t v : value) s.data.push_back(bswap32(v));
        auto a = c.exchange(buildCmd(s));
        if (!c.expect(is(a, Ack::WRITE_OK), "Size %lld write at 0x%08X: %s", (long long)size, base, ackStr(a).c_str()))
            continue;
        std::string got, want;
        bool ok = true;
        for (uint32_t i = 0; i < nw; ++i) {
            uint32_t exp = 0;
            for (uint32_t b = 0; b < 4; ++b) {
                const uint32_t sh = 24 - 8 * b;
                const uint32_t src = (4 * i + b < uint32_t(size)) ? value[i] : before[i];
                exp |= src & (0xFFu << sh);
            }
            const uint32_t r = c.rd32(base + 4 * i);
            ok &= r == exp;
            got += strprintf(" %08X", r);
            want += strprintf(" %08X", exp);
        }
        c.expect(ok, "Size %lld: words read back%s (only the %lld written bytes changed:%s)", (long long)size,
                 got.c_str(), (long long)size, want.c_str());
    }
}
CXP_CHECK("CXP-EMU-CTRL-110", ctrl110);

}  // namespace

}  // namespace cxp::validation::checks::ctrl
