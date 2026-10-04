// CXP-EMU-CTRL-101.  See cases/_common.h.
//
// Commands sent back to back, without waiting for acknowledgments.
// §8.6.1.1 has the Host wait for the final acknowledgment, so what a device
// does with commands that overlap is its decision; this device serialises
// (decision D7): the command executing, one command waiting behind it and
// answered after it, any further one dropped unanswered and not executed.
// A bootstrap access completes in microseconds while one command takes
// 38 us of the low-speed uplink, so a plain burst never overlaps inside the
// device: every command is answered, in order.  Real overlap needs a slow
// first command: a user-window access behind the bench's REG_STALL slave.

#include "cxp/validation/cases/ctrl/_helpers.h"

namespace cxp::validation::checks::ctrl {

namespace {

// Bootstrap registers with distinct values, read once for the model.
struct Probe {
    uint32_t addr;
    uint32_t value;
};

std::vector<Probe> probes(Context& c) {
    std::vector<Probe> p;
    std::set<uint32_t> seen;
    for (uint32_t a : {Reg::STANDARD, Reg::REVISION, Reg::XML_URL_ADDRESS, Reg::CONTROL_PACKET_SIZE_MAX,
                       Reg::CONNECTION_CONFIG_DEFAULT, Reg::XML_MANIFEST_SIZE, Reg::MASTER_HOST_CONNECTION_ID,
                       Reg::STREAM_PACKET_SIZE_MAX, Reg::CONNECTION_CONFIG}) {
        const uint32_t v = c.rd32(a);
        if (seen.insert(v).second) p.push_back({a, v});
    }
    return p;
}

std::string codes(const std::vector<RawAck>& acks) {
    std::string s;
    for (const auto& a : acks) s += (s.empty() ? "" : " ") + ackName(a.code).substr(0, 4);
    return s.empty() ? "none" : s;
}

// n reads of distinct registers back to back: every one answered in order.
void plainBurst(Context& c, const std::vector<Probe>& p, int n, const char* what) {
    std::vector<Words> frames;
    for (int i = 0; i < n; ++i) frames.push_back(readCmd(p[size_t(i) % p.size()].addr, 4));
    const auto acks = c.exchangeMany(frames, size_t(n), c.iparam("burst_wait_ms"));
    size_t right = 0;
    std::string first_bad;
    for (size_t i = 0; i < acks.size() && i < size_t(n); ++i) {
        const Probe& q = p[i % p.size()];
        const bool ok = acks[i].code == Ack::READ_OK && acks[i].values().size() == 1 && acks[i].values()[0] == q.value;
        right += ok;
        if (!ok && first_bad.empty()) {
            first_bad = strprintf("; #%zu (0x%04X) read %s", i, q.addr,
                                  acks[i].values().empty() ? ackName(acks[i].code).c_str()
                                                           : hex(acks[i].values()[0]).c_str());
        }
    }
    c.expect(acks.size() == size_t(n) && right == size_t(n),
             "%s: %d reads back to back, %zu acknowledgments, %zu of them the right register in order (%s%s)", what, n,
             acks.size(), right, codes(acks).c_str(), first_bad.c_str());
}

void stall(Context& c, uint32_t ms) {
    c.benchSend({bench::REG_STALL, ms, 0});
    c.benchSync();
}

// A slow first command (user window) with n - 1 commands behind it, all sent
// while it executes.  D7: the first and the second answered, in that order,
// the rest neither answered nor executed.
void overlapped(Context& c, const std::vector<Probe>& p, int n, bool writes, uint32_t stall_ms) {
    const uint32_t user = bench::USER_BASE + 0x80;
    const uint32_t uval = 0x0B0B0000u | uint32_t(n) << 8 | uint32_t(writes);
    stall(c, 0);
    c.wr32(user, writes ? 0 : uval);
    c.wr32(Reg::MASTER_HOST_CONNECTION_ID, 0x1D1D0000u);
    stall(c, stall_ms);
    std::vector<Words> frames;
    frames.push_back(writes ? writeCmd(user, {uval}) : readCmd(user, 4));
    for (int i = 1; i < n; ++i) {
        frames.push_back(writes ? writeCmd(Reg::MASTER_HOST_CONNECTION_ID, {0x1D1D0000u | uint32_t(i)})
                                : readCmd(p[size_t(i - 1) % p.size()].addr, 4));
    }
    const double t0 = c.nowMs();
    const auto acks = c.exchangeMany(frames, size_t(n) + 1, c.iparam("burst_wait_ms"));
    waitBenchMs(c, double(stall_ms) + c.iparam("settle_ms"));
    std::string what = strprintf("%d %s behind a %u ms user %s", n - 1, writes ? "writes" : "reads", stall_ms,
                                 writes ? "write" : "read");
    // Every command was on the device before the first one finished?
    const auto marks = c.uplinkMarks(t0);
    std::vector<uint64_t> eops;
    for (const auto& m : marks) {
        if (m.chr == (0x100u | K29_7)) eops.push_back(m.time_ps);
    }
    std::vector<FrameTime> fts;
    for (const auto& f : c.frameTimes(t0)) {
        if (f.type == 0x03) fts.push_back(f);
    }
    if (eops.size() == size_t(n) && !fts.empty() && eops.back() > fts.front().sop_ps) {
        c.note("%s: the last command reached the device after the first acknowledgment; the case needs a longer stall",
               what.c_str());
    }
    // Acks after the first one's final (a Wait comes first when the stall is
    // longer than the device's Wait time).
    std::vector<RawAck> fin;
    size_t waits = 0;
    for (const auto& a : acks) {
        if (a.code == Ack::WAIT) {
            ++waits;
        } else {
            fin.push_back(a);
        }
    }
    const bool first_ok = !fin.empty() && fin[0].code == (writes ? Ack::WRITE_OK : Ack::READ_OK) &&
                          (writes || (fin[0].values().size() == 1 && fin[0].values()[0] == uval));
    const Probe& q = p[0];
    const bool second_ok = fin.size() >= 2 && fin[1].code == (writes ? Ack::WRITE_OK : Ack::READ_OK) &&
                           (writes || (fin[1].values().size() == 1 && fin[1].values()[0] == q.value));
    c.expect(first_ok && second_ok && fin.size() == 2 && waits <= 1,
             "%s: %zu final acknowledgments (%s): the first command's, then the waiting one's, no other (D7)",
             what.c_str(), fin.size(), codes(acks).c_str());
    size_t all = fts.size();
    c.expect(all == acks.size(), "%s: %zu acknowledgments on the link in all, %zu of them seen by the burst "
             "(none later)", what.c_str(), all, acks.size());
    stall(c, 0);
    if (writes) {
        const uint32_t mh = c.rd32(Reg::MASTER_HOST_CONNECTION_ID);
        c.expect(mh == 0x1D1D0001u, "%s: MasterHostConnectionID 0x%08X: the waiting write executed, the dropped "
                 "ones not (0x1D1D0001)", what.c_str(), mh);
        c.expect(c.rd32(user) == uval, "%s: the first write landed", what.c_str());
    }
    auto v = c.readRaw(Reg::STANDARD, 4);
    c.expect(is(v, Ack::READ_OK) && v->values().size() == 1 && v->values()[0] == CXP_MAGIC,
             "%s: the next read answered: %s", what.c_str(), ackStr(v).c_str());
}

void ctrl101(Context& c) {
    c.preserve(Reg::MASTER_HOST_CONNECTION_ID);
    c.wr32(Reg::MASTER_HOST_CONNECTION_ID, 0x2B2B2B2Bu);
    c.prepareStreaming();  // StreamPacketSizeMax non-zero before the model reads it
    const auto p = probes(c);
    c.info("%zu registers with distinct values", p.size());
    for (int64_t n : c.ilist("burst")) plainBurst(c, p, int(n), "idle");

    // The same writes back to back: every one answered, the last value stays.
    for (int64_t n : c.ilist("burst")) {
        std::vector<Words> frames;
        for (int i = 0; i < n; ++i) frames.push_back(writeCmd(Reg::MASTER_HOST_CONNECTION_ID, {0x3C3C0000u | uint32_t(i)}));
        const auto acks = c.exchangeMany(frames, size_t(n), c.iparam("burst_wait_ms"));
        size_t ok = 0;
        for (const auto& a : acks) ok += a.code == Ack::WRITE_OK;
        const uint32_t mh = c.rd32(Reg::MASTER_HOST_CONNECTION_ID);
        c.expect(acks.size() == size_t(n) && ok == size_t(n) && mh == (0x3C3C0000u | uint32_t(n - 1)),
                 "idle: %lld writes back to back, %zu acknowledgments 0x01 of %zu, MasterHostConnectionID 0x%08X",
                 (long long)n, ok, acks.size(), mh);
    }

    c.wr32(Reg::MASTER_HOST_CONNECTION_ID, 0x2B2B2B2Bu);  // back to the model's value

    // A read burst while the device streams.
    c.startRecording();
    const size_t h0 = c.headersSeen();
    c.acqStart();
    c.waitHeaders(h0 + 1, c.opt().first_image_timeout_ms);
    for (int64_t n : c.ilist("burst")) plainBurst(c, p, int(n), "streaming");
    c.acqStop();
    c.waitQuiet();
    c.stopRecording();

    if (!(c.benchCaps() & bench::CAP_REG_STALL) || !(c.benchCaps() & bench::CAP_TIMES)) {
        c.note("no bench REG_STALL / times: the overlapped commands (behind a slow user access) are not run");
        return;
    }
    c.onExit([&c] { stall(c, 0); });
    for (int64_t ms : c.ilist("stall_ms")) {
        for (int64_t n : c.ilist("overlap")) {
            overlapped(c, p, int(n), false, uint32_t(ms));
            overlapped(c, p, int(n), true, uint32_t(ms));
        }
    }
}
CXP_CHECK("CXP-EMU-CTRL-101", ctrl101);

}  // namespace

}  // namespace cxp::validation::checks::ctrl
