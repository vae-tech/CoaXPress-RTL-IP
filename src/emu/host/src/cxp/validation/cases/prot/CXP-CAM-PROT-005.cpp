// CXP-CAM-PROT-005.  See cases/_common.h.
//
// Word alignment on the low-speed upconnection after the bench disturbs the
// host's line (bench::UPLINK_BITS): every bit and character phase (drop 1..39
// bits), inserted bits, and the line held low.  After each disturbance a
// batch of writes goes up, each followed by an IDLE word (§8.2.5: the K28.5
// the receiver aligns on).  With the bench's times each acknowledgment is
// matched to the write whose end preceded it, so the case knows which writes
// the device executed: the register must hold the value of the last one
// acknowledged 0x01, and no write may be acknowledged twice.  Recovery is
// the time from the end of the disturbance to the first acknowledged write.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::prot {

namespace {

constexpr uint32_t kReg = Reg::MASTER_HOST_CONNECTION_ID;

Chars idleChars() { return {{0xBC, true}, {0x3C, true}, {0x3C, true}, {0xB5, false}}; }

struct Outcome {
    bool recovered = false;
    double recovery_bits = 0;
    bool consistent = true;
};

// Wait until the bench's clock passes t_ps (or the host's wait runs out).
bool waitUntilPs(Context& c, uint64_t t_ps) {
    const double end = c.nowMs() + c.wait(c.iparam("host_wait_ms"));
    while (c.nowMs() < end) {
        const auto t = c.benchTime();
        if (t && t->now_ps >= t_ps) return true;
        c.sleepRawMs(50);
    }
    return false;
}

Outcome disturb(Context& c, uint32_t mode, uint32_t n, uint32_t bit_ps, uint32_t& base, const std::string& what) {
    Outcome o;
    const int batch = c.iparam("batch");
    const uint32_t before = c.rd32(kReg);
    const auto t_a = c.benchTime();
    const double t0 = c.nowMs();
    c.benchSend({bench::UPLINK_BITS, mode, n});
    // A held line is let go before the writes: they are judged against the
    // moment it comes back (the characters would wait behind it anyway).
    const uint64_t t_end = t_a ? t_a->now_ps + (mode >= 2 ? uint64_t(n) * bit_ps : 0) : 0;
    if (mode >= 2 && t_a) waitUntilPs(c, t_end);
    Chars ch;
    const Chars idle = idleChars();
    for (int i = 0; i < batch; ++i) {
        const Chars f = frameChars(writeCmd(kReg, {base + uint32_t(i)}));
        ch.insert(ch.end(), f.begin(), f.end());
        ch.insert(ch.end(), idle.begin(), idle.end());
    }
    const auto acks = c.exchangeChars(ch, size_t(batch), c.iparam("ack_window_ms"));
    // Every write has left once its EOP mark is in; then a margin of bench
    // time for the last acknowledgment, so none comes after this call.
    std::vector<uint64_t> eop;
    const double end = c.nowMs() + c.wait(c.iparam("host_wait_ms"));
    while (c.nowMs() < end) {
        eop.clear();
        for (const auto& m : c.uplinkMarks(t0)) {
            if (m.chr == (0x100u | K29_7)) eop.push_back(m.time_ps + 40ull * m.bit_ps);
        }
        if (eop.size() >= size_t(batch)) break;
        c.sleepRawMs(50);
    }
    if (!eop.empty()) waitUntilPs(c, eop.back() + 2000ull * bit_ps);
    std::vector<FrameTime> at;
    for (const auto& f : c.frameTimes(t0)) {
        if (f.type == 0x03) at.push_back(f);
    }
    // The read of `before` answered before t0; the acks since are the batch's.
    if (eop.size() < size_t(batch) || !t_a) {
        c.expect(false, "%s: the batch did not leave the host within the wait (%zu of %d ends)", what.c_str(),
                 eop.size(), batch);
        return o;
    }
    const size_t judged = std::min(acks.size(), at.size());
    if (at.size() > acks.size()) {
        c.warn("%s: %zu acknowledgments came after the host stopped collecting; not judged", what.c_str(),
               at.size() - acks.size());
    }
    std::vector<int> per_cmd(size_t(batch), 0);
    int last_ok = -1;
    std::string codes;
    for (size_t j = 0; j < judged; ++j) {
        int cmd = -1;
        for (int i = 0; i < batch; ++i) {
            if (eop[size_t(i)] <= at[j].sop_ps) cmd = i;
        }
        codes += strprintf("%s#%d:%02X", codes.empty() ? "" : " ", cmd, acks[j].code & 0xFF);
        if (cmd < 0) {
            o.consistent = false;
            continue;
        }
        ++per_cmd[size_t(cmd)];
        if (acks[j].code == Ack::WRITE_OK) {
            if (last_ok < 0) o.recovery_bits = (double(at[j].sop_ps) - double(t_end)) / double(bit_ps);
            last_ok = cmd;
        }
    }
    o.recovery_bits = std::max(0.0, o.recovery_bits);
    o.recovered = last_ok >= 0;
    const bool dup = std::any_of(per_cmd.begin(), per_cmd.end(), [](int k) { return k > 1; });
    const uint32_t want = last_ok >= 0 ? base + uint32_t(last_ok) : before;
    const uint32_t now = c.rd32(kReg);
    c.info("%s: acks %s", what.c_str(), codes.empty() ? "none" : codes.c_str());
    o.consistent = o.consistent && !dup && (now == want || at.size() > acks.size());
    c.expect(o.consistent, "%s: register 0x%08X = the last write acknowledged 0x01 (0x%08X); no write "
             "acknowledged twice; every acknowledgment after a write", what.c_str(), now, want);
    base += uint32_t(batch);
    return o;
}

void prot005(Context& c) {
    c.needBench(bench::CAP_UPLINK_BITS | bench::CAP_TIMES | bench::CAP_CHARS, "slip and hold the host's line");
    c.preserve(kReg);
    // The bit period, from the marks of one command.
    const double t_bit = c.nowMs();
    c.rd32(Reg::STANDARD);
    c.benchTime();
    uint32_t bit_ps = 0;
    for (const auto& m : c.uplinkMarks(t_bit)) bit_ps = m.bit_ps;
    if (!bit_ps) c.abort("no UPLINK_MARK from the bench");
    const double limit = c.iparam("recover_bits");
    uint32_t base = 0x50000000;
    double worst = 0;
    size_t n = 0, lost = 0;
    auto run = [&](uint32_t mode, uint32_t bits, const std::string& what) {
        const Outcome o = disturb(c, mode, bits, bit_ps, base, what);
        ++n;
        if (!o.recovered) {
            ++lost;
            c.expect(false, "%s: no write of the batch acknowledged (not realigned)", what.c_str());
            // Give the receiver the rest of its chance before the next one.
            (void)c.exchange(readCmd(Reg::STANDARD, 4), c.iparam("host_wait_ms"));
            return;
        }
        worst = std::max(worst, o.recovery_bits);
        c.expect(o.recovery_bits <= limit, "%s: first write executed %.0f bits after the disturbance (<= %.0f)",
                 what.c_str(), o.recovery_bits, limit);
    };
    for (int64_t k : c.ilist("drop_bits")) run(0, uint32_t(k), strprintf("%lld bits dropped", (long long)k));
    for (int64_t k : c.ilist("insert_bits")) run(1, uint32_t(k), strprintf("%lld bits inserted", (long long)k));
    for (int64_t k : c.ilist("hold_bits")) run(2, uint32_t(k), strprintf("line low for %lld bits", (long long)k));
    c.info("%zu disturbances, %zu without recovery; slowest recovery %.0f bits", n, lost, worst);
    const auto a = c.readRaw(Reg::STANDARD, 4);
    c.expect(is(a, Ack::READ_OK), "a read after all disturbances: %s", ackStr(a).c_str());
}
CXP_CHECK("CXP-CAM-PROT-005", prot005);

}  // namespace

}  // namespace cxp::validation::checks::prot
