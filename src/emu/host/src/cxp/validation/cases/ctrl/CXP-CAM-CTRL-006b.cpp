// CXP-CAM-CTRL-006b.  See cases/_common.h.
//
// A control channel reset (0xFF) while a command is still executing: a
// user-window read behind the bench's slow REG_STALL slave.  §8.6.1.2: the
// device aborts the operation in process, resets its control channel and
// sends 0x03.  The aborted read is answered before the 0x03 or not at all,
// never after it; no Wait follows the 0x03; the commands after it are
// served by the §8.6.1.1 rules (final acknowledgment, or a Wait, within
// 200 ms).  Times are the bench's, in the device's milliseconds.

#include "cxp/validation/cases/ctrl/_helpers.h"

namespace cxp::validation::checks::ctrl {

namespace {

constexpr uint32_t kUser = bench::USER_BASE + 0xC0;

void stall(Context& c, uint32_t ms) {
    c.benchSend({bench::REG_STALL, ms, 0});
    c.benchSync();
}

std::string seq(const std::vector<RawAck>& acks) {
    std::string s;
    for (const auto& a : acks) s += (s.empty() ? "" : ", ") + ackName(a.code);
    return s.empty() ? "none" : s;
}

// The command after the reset: one final acknowledgment (right data), within
// 200 ms or after one Wait sent within 200 ms.
void nextCommand(Context& c, const std::string& what, const Words& cmd, uint32_t want) {
    const auto r = timedExchange(c, cmd, c.iparam("host_wait_ms"));
    size_t waits = 0, finals = 0;
    for (const auto& a : r.acks) (a.code == Ack::WAIT ? waits : finals)++;
    const RawAck* fin = r.acks.empty() || r.acks.back().code == Ack::WAIT ? nullptr : &r.acks.back();
    c.expect(finals == 1 && fin && fin->code == Ack::READ_OK && fin->values().size() == 1 && fin->values()[0] == want,
             "%s: one final acknowledgment with 0x%08X (%s)", what.c_str(), want, seq(r.acks).c_str());
    if (!r.timed || r.acks.empty()) return;
    const double t_first = r.ms(r.times[0].eop_ps - r.cmd_end_ps);
    c.expect(t_first <= 200.0, "%s: first acknowledgment (%s) %.1f ms after the command (<= 200 ms, §8.6.1.1)",
             what.c_str(), ackName(r.acks[0].code).c_str(), t_first);
    if (waits > 1) c.expect(false, "%s: %zu Waits", what.c_str(), waits);
}

void variant(Context& c, uint32_t stall_ms, bool after_wait) {
    const std::string what = strprintf("slave %u ms, 0xFF %s the Wait", stall_ms, after_wait ? "after" : "before");
    stall(c, 0);
    const uint32_t uval = 0x06B00000u | stall_ms;
    c.wr32(kUser, uval);
    stall(c, stall_ms);
    const double t0 = c.nowMs();
    std::vector<RawAck> acks;
    if (after_wait) {
        acks = c.exchangeMany({readCmd(kUser, 4)}, 1, c.iparam("host_wait_ms"));
        c.expect(acks.size() == 1 && acks[0].code == Ack::WAIT, "%s: the read's Wait first (%s)", what.c_str(),
                 seq(acks).c_str());
        auto r = c.exchangeMany({resetCmd()}, 1, c.iparam("host_wait_ms"));
        acks.insert(acks.end(), r.begin(), r.end());
    } else {
        acks = c.exchangeMany({readCmd(kUser, 4), resetCmd()}, 1, c.iparam("host_wait_ms"));
    }
    const auto it = std::find_if(acks.begin(), acks.end(), [](const RawAck& a) { return a.code == Ack::RESET_OK; });
    bool pre_ok = true;
    for (auto p = acks.begin(); p != it; ++p) {
        pre_ok &= p->code == Ack::WAIT || (p->code == Ack::READ_OK && p->values().size() == 1 && p->values()[0] == uval);
    }
    c.expect(it != acks.end() && !it->long_form && pre_ok,
             "%s: 0x03 in short form, before it only the read's Wait or its right data (%s)", what.c_str(),
             seq(acks).c_str());
    // Right after the 0x03: a bootstrap read.
    const double t_after = c.nowMs();
    nextCommand(c, what + ", the bootstrap read after the 0x03", readCmd(Reg::STANDARD, 4), CXP_MAGIC);
    // Let the slave's time pass: nothing may arrive (the aborted read's data or
    // Wait, a second 0x03).  The SYNC round trip first, so the FRAME_DL event
    // of the read just answered is in before t_quiet.
    c.benchTime();
    const double t_quiet = c.nowMs();
    waitBenchMs(c, double(stall_ms) + c.iparam("settle_ms"));
    size_t late = 0;
    for (const auto& f : c.frameTimes(t_quiet)) late += f.type == 0x03;
    c.expect(late == 0, "%s: no acknowledgment in the %u + %d ms after (%zu)", what.c_str(), stall_ms,
             c.iparam("settle_ms"), late);
    (void)t0;
    (void)t_after;
    // A user read with the slave still slow: served normally (Wait if needed,
    // then the data).
    stall(c, uint32_t(c.iparam("short_stall_ms")));
    nextCommand(c, what + ", a user read after it", readCmd(kUser, 4), uval);
    stall(c, 0);
}

void ctrl006b(Context& c) {
    c.needBench(bench::CAP_REG_STALL | bench::CAP_TIMES, "stall the user register window and time the link");
    c.onExit([&c] { stall(c, 0); });
    for (int64_t ms : c.ilist("stall_ms")) {
        for (bool after : {false, true}) variant(c, uint32_t(ms), after);
    }
}
CXP_CHECK("CXP-CAM-CTRL-006b", ctrl006b);

}  // namespace

}  // namespace cxp::validation::checks::ctrl
