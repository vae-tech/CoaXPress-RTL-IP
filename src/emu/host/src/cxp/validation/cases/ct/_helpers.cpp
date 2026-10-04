#include "cxp/validation/cases/ct/_helpers.h"

namespace cxp::validation::checks::ct {

void stopAcqIfPossible(Context& c) {
    if (Feature* f = c.feature("AcquisitionStop")) {
        try {
            f->execute();
        } catch (const std::exception&) {
        }
        c.waitQuiet();
    }
}

void resetTestCounters(Context& c) {
    c.wr32(Reg::TEST_ERROR_COUNT_SELECTOR, 0);
    c.wr32(Reg::TEST_ERROR_COUNT, 0);
    write64(c, Reg::TEST_PACKET_COUNT_RX, 0);
}

void sendTestPackets(Context& c, int n, int corrupt_words) {
    std::vector<Words> pk(size_t(n), hostTestPacket(corrupt_words));
    c.sendOnly(pk, 100);
}

Chars idleChars() { return {{0xBC, true}, {0x3C, true}, {0x3C, true}, {0xB5, false}}; }

void appendFrame(Chars& out, const Words& frame) {
    const Chars f = frameChars(frame);
    out.insert(out.end(), f.begin(), f.end());
    const Chars idle = idleChars();
    out.insert(out.end(), idle.begin(), idle.end());
}

TestCounters readTestCounters(Context& c, double since_ms, size_t eops, int wait_ms) {
    TestCounters t;
    if (c.benchCaps() & bench::CAP_TIMES) {
        const double end = c.nowMs() + c.wait(wait_ms);
        size_t seen = 0;
        while (c.nowMs() < end) {
            seen = 0;
            for (const auto& m : c.uplinkMarks(since_ms)) seen += m.chr == (0x100u | K29_7);
            if (seen >= eops) break;
            c.sleepRawMs(50);
        }
        if (seen < eops) c.info("only %zu of %zu packet ends on the uplink within %d ms", seen, eops, c.wait(wait_ms));
    }
    const auto a = c.exchange(readCmd(Reg::TEST_ERROR_COUNT, 4), wait_ms);
    if (!is(a, Ack::READ_OK) || a->values().size() != 1) {
        c.expect(false, "TestErrorCount read after the burst: %s", ackStr(a).c_str());
        return t;
    }
    t.err = a->values()[0];
    t.rx = c.rd64(Reg::TEST_PACKET_COUNT_RX);
    t.ok = true;
    return t;
}

}  // namespace cxp::validation::checks::ct
