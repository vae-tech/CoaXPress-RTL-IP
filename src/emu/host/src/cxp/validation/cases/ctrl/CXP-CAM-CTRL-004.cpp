// CXP-CAM-CTRL-004.  See cases/_common.h.
//
// The long operation is the bench's user register window behind a slave that
// answers late (bench::REG_STALL): a register access that takes longer than
// the §8.6.1.1 200 ms.  Every time is the bench's (the simulated device's
// milliseconds), not the host's wall clock.

#include "cxp/validation/cases/ctrl/_helpers.h"

namespace cxp::validation::checks::ctrl {

namespace {

constexpr uint32_t kUser = bench::USER_BASE + 0x40;  // a word of the user window

void stall(Context& c, uint32_t ms, uint32_t err = 0) {
    c.benchSend({bench::REG_STALL, ms, err});
    c.benchSync();
}

std::string fmtMs(double ms) { return strprintf("%.1f ms", ms); }

// One access to the user window with the slave answering after stall_ms.
// §8.6.1.1: a final acknowledgment within 200 ms of the command, or exactly
// one Wait (0x04, Size 4, 100..10000 ms) within 200 ms and then exactly one
// final acknowledgment within the time it gives.  want_code >= 0: the final
// code the access must end with; -1: any final code (a slave that answers
// later than the Wait allows cannot complete it).  Returns the final ack.
OptAck longAccess(Context& c, const Words& cmd, int stall_ms, int want_code, const std::string& what) {
    const auto r = timedExchange(c, cmd, c.iparam("host_wait_ms"));
    std::vector<size_t> waits, finals;
    for (size_t i = 0; i < r.acks.size(); ++i) (r.acks[i].code == Ack::WAIT ? waits : finals).push_back(i);
    std::string seq;
    for (const auto& a : r.acks) seq += (seq.empty() ? "" : ", ") + ackName(a.code);
    if (!r.timed) {
        c.expect(false, "%s: acknowledgments without bench times (%s)", what.c_str(), seq.empty() ? "none" : seq.c_str());
        return std::nullopt;
    }
    const auto at = [&](size_t i) { return r.ms(r.times[i].sop_ps - r.cmd_end_ps); };
    const auto end = [&](size_t i) { return r.ms(r.times[i].eop_ps - r.cmd_end_ps); };
    std::string when;
    for (size_t i = 0; i < r.acks.size(); ++i) {
        when += strprintf("%s%s at %s", when.empty() ? "" : ", ", ackName(r.acks[i].code).c_str(), fmtMs(at(i)).c_str());
    }
    c.info("%s (slave %d ms): %s", what.c_str(), stall_ms, when.empty() ? "no acknowledgment" : when.c_str());
    if (!c.expect(finals.size() == 1 && finals.back() == r.acks.size() - 1,
                  "%s: exactly one final acknowledgment, the last (%s)", what.c_str(), seq.c_str())) {
        return std::nullopt;
    }
    const size_t f = finals.back();
    const RawAck& fin = r.acks[f];
    if (waits.empty()) {
        c.expect(end(f) <= 200.0, "%s: no Wait, so the final acknowledgment within 200 ms (%s)", what.c_str(),
                 fmtMs(end(f)).c_str());
    } else {
        const size_t w = waits.front();
        const RawAck& wa = r.acks[w];
        c.expect(waits.size() == 1, "%s: one Wait acknowledgment (%zu)", what.c_str(), waits.size());
        c.expect(end(w) <= 200.0, "%s: the Wait within 200 ms of the command (%s)", what.c_str(), fmtMs(end(w)).c_str());
        const uint32_t wms = wa.data.empty() ? 0 : wa.values()[0];
        c.expect(wa.ok() && wa.long_form && wa.size_field == 4 && wa.data.size() == 1 && wa.crc_ok,
                 "%s: the Wait is 0x04 with Size 4, one data word and a good CRC (Size %u, %zu words)", what.c_str(),
                 wa.size_field, wa.data.size());
        c.expect(wms >= 100 && wms <= 10000, "%s: the Wait gives %u ms (100..10000)", what.c_str(), wms);
        const double after = r.ms(r.times[f].eop_ps - r.times[w].eop_ps);
        c.expect(after <= double(wms), "%s: the final acknowledgment %s after the Wait (<= %u ms)", what.c_str(),
                 fmtMs(after).c_str(), wms);
    }
    if (want_code >= 0) {
        c.expect(fin.code == want_code && fin.ok(), "%s: final %s (%s)", what.c_str(), ackName(fin.code).c_str(),
                 ackName(want_code).c_str());
    } else {
        c.expect(fin.code != Ack::READ_OK && fin.code != Ack::WRITE_OK && fin.ok(),
                 "%s: final %s, a Table 22 error (the slave answers after the time the Wait allows)", what.c_str(),
                 ackName(fin.code).c_str());
    }
    return fin;
}

void ctrl004(Context& c) {
    c.needBench(bench::CAP_REG_STALL | bench::CAP_TIMES, "stall the user register window and time the link");
    c.onExit([&c] { stall(c, 0); });
    stall(c, 0);

    // A quick slave: the window answers like any register.
    auto a = c.writeRaw(kUser, {0x5A5A0000});
    c.expect(is(a, Ack::WRITE_OK), "user window write, slave at once: %s", ackStr(a).c_str());
    a = c.readRaw(kUser, 4);
    c.expect(is(a, Ack::READ_OK) && a->values().size() == 1 && a->values()[0] == 0x5A5A0000,
             "user window read back, slave at once: %s", ackStr(a).c_str());

    // Slaves slower than 200 ms but within the Wait: the access completes.
    uint32_t value = 0x5A5A0001;
    for (int64_t ms : c.ilist("stall_ms")) {
        stall(c, uint32_t(ms));
        for (int t = 0; t < c.iparam("trials"); ++t, ++value) {
            longAccess(c, writeCmd(kUser, {value}), int(ms), Ack::WRITE_OK, strprintf("write, trial %d", t));
            const auto fin = longAccess(c, readCmd(kUser, 4), int(ms), Ack::READ_OK, strprintf("read, trial %d", t));
            if (fin && fin->code == Ack::READ_OK) {
                c.expect(fin->values().size() == 1 && fin->values()[0] == value, "read, trial %d: 0x%08X (0x%08X)", t,
                         fin->values().empty() ? 0 : fin->values()[0], value);
            }
        }
    }

    // A bootstrap register is never behind the slow slave: no Wait (§10.3.3).
    stall(c, uint32_t(c.iparam("slow_ms")));
    const auto b = timedExchange(c, readCmd(Reg::STANDARD, 4), c.iparam("host_wait_ms"));
    c.expect(b.acks.size() == 1 && b.acks[0].code == Ack::READ_OK && b.acks[0].values().size() == 1 &&
                 b.acks[0].values()[0] == CXP_MAGIC,
             "bootstrap read while the user slave stalls: one 0x00 with Standard (%zu acks)", b.acks.size());
    if (b.timed && !b.times.empty()) {
        const double t = b.ms(b.times[0].eop_ps - b.cmd_end_ps);
        c.expect(t <= 200.0, "  ... within 200 ms (%s)", fmtMs(t).c_str());
    }

    // A slave slower than the Wait allows, and one that never answers: one
    // Wait, then a final error within its time; the late answer completes
    // nothing, and the write it would have made is not made.
    for (bool hung : {false, true}) {
        const std::string what = hung ? "hung slave" : strprintf("slave at %d ms", c.iparam("slow_ms"));
        stall(c, 0);
        c.wr32(kUser, 0x11110000);
        stall(c, hung ? 0xFFFFFFFFu : uint32_t(c.iparam("slow_ms")));
        longAccess(c, readCmd(kUser, 4), hung ? -1 : c.iparam("slow_ms"), -1, what + ", read");
        longAccess(c, writeCmd(kUser, {0x22220000}), hung ? -1 : c.iparam("slow_ms"), -1, what + ", write");
        // The next command: a bootstrap read answers at once.
        a = c.readRaw(Reg::STANDARD, 4);
        c.expect(is(a, Ack::READ_OK) && a->values().size() == 1 && a->values()[0] == CXP_MAGIC,
                 "%s: the next bootstrap read answers 0x00 with Standard (%s)", what.c_str(), ackStr(a).c_str());
        // Let the slow slave's own time run out; nothing may arrive meanwhile.
        // (The SYNC round trip first: the FRAME_DL event of the read just
        // answered is in before t_quiet.)
        c.benchTime();
        const double t_quiet = c.nowMs();
        waitBenchMs(c, c.iparam("late_window_ms"));
        size_t late = 0;
        for (const auto& f : c.frameTimes(t_quiet)) late += f.type == 0x03;
        c.expect(late == 0, "%s: no acknowledgment in the %d ms after (%zu)", what.c_str(), c.iparam("late_window_ms"), late);
        stall(c, 0);
        a = c.readRaw(kUser, 4);
        c.expect(is(a, Ack::READ_OK) && a->values().size() == 1 && a->values()[0] == 0x11110000,
                 "%s: the write answered with an error did not land (0x%08X, 0x11110000)", what.c_str(),
                 a && !a->values().empty() ? a->values()[0] : 0);
    }

    // A slave error (PSLVERR) is a Table 22 error, not success.
    stall(c, 0, 1);
    a = c.readRaw(kUser, 4);
    c.expect(a && a->code != Ack::READ_OK && a->code != Ack::WAIT, "slave error on a read: %s (a Table 22 error)",
             ackStr(a).c_str());
}
CXP_CHECK("CXP-CAM-CTRL-004", ctrl004);

}  // namespace

}  // namespace cxp::validation::checks::ctrl
