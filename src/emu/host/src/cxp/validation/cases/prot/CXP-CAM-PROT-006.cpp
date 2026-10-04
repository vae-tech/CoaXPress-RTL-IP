// CXP-CAM-PROT-006.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::prot {

namespace {

void prot006(Context& c) {
    const char* where[] = {"SOP", "TYPE", "EOP"};
    const int word_idx[] = {0, 1, -1};
    const int repeats = c.iparam("repeats");
    size_t ok = 0, total = 0;
    for (int w = 0; w < 3; ++w) {
        for (int lane = 0; lane < 4; ++lane) {
            bool pos_ok = true;
            for (int rep = 0; rep < repeats && pos_ok; ++rep) {  // a failed position is not repeated
                CmdSpec s;
                s.opcode = 0x00;
                s.size_bytes = 4;
                s.address = Reg::STANDARD;
                s.header_lane_word = word_idx[w];
                s.header_lane_hit = lane;
                auto a = c.exchange(buildCmd(s), c.iparam("ack_wait_ms"));
                const bool good = is(a, Ack::READ_OK) && !a->data.empty() && a->values()[0] == CXP_MAGIC;
                ++total;
                ok += good;
                pos_ok = good;
                if (!good) {
                    c.expect(false, "%s character P%d corrupted: %s", where[w], lane, ackStr(a).c_str());
                }
                auto v = c.readRaw(Reg::STANDARD, 4);
                if (!is(v, Ack::READ_OK)) c.expect(false, "valid read after the corrupted one: %s", ackStr(v).c_str());
            }
        }
    }
    const size_t want = size_t(12 * repeats);  // SOP, TYPE, EOP x P0..P3
    c.expect(ok == total && total == want, "%zu of %zu reads with one corrupted framing character decoded correctly "
             "(%zu sent; a position that fails is not repeated)", ok, want, total);
    c.note("command header words are CRC-covered on this link; only the spec-replicated K27.7 / type / K29.7 "
           "characters are corrupted");
}
CXP_CHECK("CXP-CAM-PROT-006", prot006);

}  // namespace

}  // namespace cxp::validation::checks::prot
