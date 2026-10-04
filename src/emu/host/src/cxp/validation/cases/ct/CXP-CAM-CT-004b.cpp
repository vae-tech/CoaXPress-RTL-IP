// CXP-CAM-CT-004b.  See cases/_common.h.
//
// The Test Receiver compares every one of the 1024 payload words (§8.7.1:
// "increment the Error Counter for each word that is different"), the last
// ones included, and a K28.5 character in the body makes its word different
// without losing the packet.

#include "cxp/validation/cases/ct/_helpers.h"

namespace cxp::validation::checks::ct {

namespace {

// A Table 23 packet with the payload words at `words` corrupted (one byte
// lane flipped each).
Words packetWithBadWords(const std::vector<int64_t>& words) {
    Words w = hostTestPacket(0);
    for (int64_t k : words) w[2 + size_t(k)] ^= 0x0000FF00u;
    return w;
}

bool step(Context& c, const Chars& chars, uint64_t want_rx, uint32_t want_err, const std::string& what) {
    const double t0 = c.nowMs();
    c.sendChars(chars);
    const auto t = readTestCounters(c, t0, 1, c.iparam("host_wait_ms"));
    if (!t.ok) return false;
    return c.expect(t.rx == want_rx && t.err == want_err, "%s: TestPacketCountRx %llu, TestErrorCount %u (%llu, %u)",
                    what.c_str(), (unsigned long long)t.rx, t.err, (unsigned long long)want_rx, want_err);
}

void ct004b(Context& c) {
    c.needBench(bench::CAP_CHARS, "put a K character into a test packet");
    resetTestCounters(c);
    uint64_t rx = 0;
    uint32_t err = 0;

    // The last payload words, and the first with the last.
    for (const auto& set : c.rows("corrupt_sets")) {
        Chars ch;
        appendFrame(ch, packetWithBadWords(set));
        std::string names;
        for (int64_t k : set) names += (names.empty() ? "" : ", ") + std::to_string(k);
        step(c, ch, ++rx, err += uint32_t(set.size()), "payload words " + names + " corrupted");
    }

    // One K28.5 in the body, in lane P0 (where the IDLE word has it) of one
    // payload word: that word is different, the packet still counts.
    const int kw = c.iparam("k_word");
    Chars ch;
    Chars pkt = frameChars(hostTestPacket(0));
    pkt[4 * size_t(2 + kw)] = {0xBC, true};
    ch.insert(ch.end(), pkt.begin(), pkt.end());
    const Chars idle = idleChars();
    ch.insert(ch.end(), idle.begin(), idle.end());
    step(c, ch, ++rx, ++err, strprintf("K28.5 in lane P0 of payload word %d", kw));

    // The receiver is not left out of step: a clean packet after it.
    Chars clean;
    appendFrame(clean, hostTestPacket(0));
    step(c, clean, ++rx, err, "a clean packet after them");
}
CXP_CHECK("CXP-CAM-CT-004b", ct004b);

}  // namespace

}  // namespace cxp::validation::checks::ct
