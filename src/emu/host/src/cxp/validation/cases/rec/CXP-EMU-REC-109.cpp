// CXP-EMU-REC-109.  See cases/_common.h.
//
// The host's low-speed bit rate off nominal (bench::UPLINK_PPM): §6.7 allows
// each end +-100 ppm, so the Device's receiver must take a Host up to 200 ppm
// off its own clock.  Beyond that the case only measures.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::rec {

namespace {

void setPpm(Context& c, int32_t ppm) {
    c.benchSend({bench::UPLINK_PPM, uint32_t(ppm)});
    c.benchSync();
}

// Reads of Standard at the current offset: how many answered right.
int reads(Context& c, int n) {
    int ok = 0;
    for (int i = 0; i < n; ++i) {
        const auto a = c.exchange(readCmd(Reg::STANDARD, 4), c.iparam("read_wait_ms"));
        ok += is(a, Ack::READ_OK) && a->values().size() == 1 && a->values()[0] == CXP_MAGIC;
    }
    return ok;
}

void rec109(Context& c) {
    c.needBench(bench::CAP_UPLINK_PPM | bench::CAP_SYNC, "offset the host's bit rate");
    c.onExit([&c] { setPpm(c, 0); });
    const int n = c.iparam("reads"), limit = c.iparam("required_ppm");
    std::map<int, int> first_bad;  // sign -> first failing |ppm|
    for (int64_t p : c.ilist("ppm")) {
        for (int sign : {+1, -1}) {
            const int32_t ppm = int32_t(sign * p);
            setPpm(c, ppm);
            // The receiver may need a moment at a new rate: one read first.
            (void)reads(c, 1);
            const int ok = reads(c, n);
            if (std::abs(ppm) <= limit) {
                c.expect(ok == n, "%+d ppm: %d of %d reads answered 0x00 with Standard", ppm, ok, n);
            } else {
                c.info("%+d ppm: %d of %d reads answered", ppm, ok, n);
                if (ok < n && !first_bad.count(sign)) first_bad[sign] = int(p);
            }
        }
    }
    for (int sign : {+1, -1}) {
        if (first_bad.count(sign)) {
            c.note("first offset with a lost read: %c%d ppm", sign > 0 ? '+' : '-', first_bad[sign]);
        } else {
            c.note("no read lost up to %c%lld ppm", sign > 0 ? '+' : '-', (long long)c.ilist("ppm").back());
        }
    }
    setPpm(c, 0);
    (void)reads(c, 1);
    const int ok = reads(c, n);
    c.expect(ok == n, "back at 0 ppm: %d of %d reads answered (no lasting damage)", ok, n);
}
CXP_CHECK("CXP-EMU-REC-109", rec109);

}  // namespace

}  // namespace cxp::validation::checks::rec
