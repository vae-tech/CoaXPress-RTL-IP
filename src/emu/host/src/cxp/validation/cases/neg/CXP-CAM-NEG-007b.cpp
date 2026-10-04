// CXP-CAM-NEG-007b.  See cases/_common.h.
//
// Commands whose header is damaged beyond a vote.  Table 21's Cmd/Size and
// address words are single characters, protected by the CRC only
// (§8.2.2.2): two damaged lanes there make a command that is invalid —
// an undefined opcode (0x42), a Size inconsistent with the packet (0x46) or a
// CRC failure (0x80) — the Table 22 code the device picks among those is not
// fixed by the standard.  The 4 x 0x02 TYPE word is voted (§8.2.2.1): one bad
// character is repaired and the command executes; two of four leave no
// majority, and the packet is then malformed (0x47) or, not being known as a
// command, unanswered.  Nothing is executed for any damaged command, and the
// next good command is answered.

#include "cxp/validation/cases/neg/_helpers.h"

namespace cxp::validation::checks::neg {

namespace {

struct Hit {
    const char* what;
    size_t word;          // frame word (0 SOP, 1 TYPE, 2 Cmd/Size, 3 Addr)
    uint32_t mask;        // XOR on that word
    std::vector<int> ok;  // acceptable codes; -1 = no acknowledgment
};

void neg007b(Context& c) {
    c.preserve(Reg::MASTER_HOST_CONNECTION_ID);
    const uint32_t sentinel = uint32_t(c.iparam("sentinel"));
    c.wr32(Reg::MASTER_HOST_CONNECTION_ID, sentinel);
    const int wait_ms = c.iparam("ack_wait_ms");

    const std::vector<Hit> hits = {
        {"TYPE word, P0 and P1", 1, 0x00005A5Au, {Ack::MALFORMED, -1}},
        {"TYPE word, P2 and P3", 1, 0x5A5A0000u, {Ack::MALFORMED, -1}},
        {"TYPE word, P0 and P3", 1, 0x5A00005Au, {Ack::MALFORMED, -1}},
        {"Cmd/Size word, P0 (Cmd) and P1", 2, 0x00005A5Au, {Ack::BAD_OPCODE, Ack::SIZE_MISMATCH, Ack::CRC_ERROR}},
        {"Cmd/Size word, P2 and P3 (Size)", 2, 0x5A5A0000u, {Ack::SIZE_MISMATCH, Ack::CRC_ERROR}},
        {"Addr word, P0 and P1", 3, 0x00005A5Au, {Ack::CRC_ERROR}},
        {"Addr word, P2 and P3", 3, 0x5A5A0000u, {Ack::CRC_ERROR}},
    };
    size_t n = 0;
    for (const Hit& h : hits) {
        for (bool write : {false, true}) {
            Words f = write ? writeCmd(Reg::MASTER_HOST_CONNECTION_ID, {0xDEAD0000u | uint32_t(++n)})
                            : readCmd(Reg::MASTER_HOST_CONNECTION_ID, 4);
            f[h.word] ^= h.mask;
            auto acks = c.exchangeMany({f}, 1, wait_ms);
            const int code = acks.empty() ? -1 : acks[0].code;
            const bool ok = std::find(h.ok.begin(), h.ok.end(), code) != h.ok.end();
            std::string want;
            for (int k : h.ok) want += (want.empty() ? "" : " or ") + (k < 0 ? std::string("none") : ackName(k));
            c.expect(ok && (acks.empty() || acks[0].ok()), "%s %s: %s (%s)", write ? "write," : "read,", h.what,
                     acks.empty() ? "no acknowledgment" : ackName(code).c_str(), want.c_str());
            auto v = c.readRaw(Reg::STANDARD, 4);
            c.expect(is(v, Ack::READ_OK) && v->values().size() == 1 && v->values()[0] == CXP_MAGIC,
                     "  next read of Standard: %s", ackStr(v).c_str());
        }
    }
    // One damaged TYPE character is voted away (§8.2.2.1): the command executes.
    for (int lane = 0; lane < 4; ++lane) {
        Words f = writeCmd(Reg::MASTER_HOST_CONNECTION_ID, {sentinel});
        f[1] ^= 0x5Au << (8 * lane);
        auto a = c.exchange(f);
        c.expect(is(a, Ack::WRITE_OK), "write with TYPE character P%d damaged: %s (0x01, voted)", lane, ackStr(a).c_str());
    }
    // A packet that ends before its command word (SOP, TYPE, EOP) is malformed.
    Words stub = {SOP_WORD, replicateByte(0x02), EOP_WORD};
    auto acks = c.exchangeMany({stub}, 1, wait_ms);
    c.expect(!acks.empty() && acks[0].code == Ack::MALFORMED, "SOP, TYPE, EOP only: %s (0x47)",
             acks.empty() ? "no acknowledgment" : ackName(acks[0].code).c_str());
    auto v = c.readRaw(Reg::STANDARD, 4);
    c.expect(is(v, Ack::READ_OK), "  next read of Standard: %s", ackStr(v).c_str());
    c.expect(c.rd32(Reg::MASTER_HOST_CONNECTION_ID) == sentinel,
             "MasterHostConnectionID still 0x%08X: no damaged write executed", sentinel);
}
CXP_CHECK("CXP-CAM-NEG-007b", neg007b);

}  // namespace

}  // namespace cxp::validation::checks::neg
