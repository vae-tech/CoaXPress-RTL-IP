// Helpers the ct/ cases share (moved from checks/ct.cpp).
// Dissolved into fixtures / scoreboards in M2.
#pragma once

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::ct {

// ===========================================================================
// Connection test (§8.7)
// ===========================================================================
void stopAcqIfPossible(Context& c);

void resetTestCounters(Context& c);

void sendTestPackets(Context& c, int n, int corrupt_words);

// The Table 14 IDLE word as characters.
Chars idleChars();

// A frame's characters followed by one IDLE word (§8.7.3: at least one IDLE
// between test packets), appended to out.
void appendFrame(Chars& out, const Words& frame);

// Read the connection-test counters after uplink traffic the host queued
// since since_ms: with bench times, first wait (up to wait_ms, scaled) until
// `eops` packet ends of it have left on the uplink; without, the first read
// waits up to wait_ms behind the traffic.
struct TestCounters {
    uint32_t err = 0;
    uint64_t rx = 0;
    bool ok = false;
};
TestCounters readTestCounters(Context& c, double since_ms, size_t eops, int wait_ms);

}  // namespace cxp::validation::checks::ct
