// Emulator implementations of the validation-plan test cases.
//
// Each check does what the `emulator` block of its catalogue entry
// (validation/cxp_validation_cases.json) says: the same procedure steps,
// the same PASS/FAIL criteria.  Keep the two in lock-step — the GUI shows
// the catalogue text as the definition of what ran.  Stimulus counts, lists
// and durations are that block's params (Context::iparam() and friends), so
// the prose and the check share one number.
//
// Expectations follow CXP 1.1.1 (the plan's source), not the reference
// camera: a check that fails against the in-process virtual camera is
// reporting a real difference between that camera and the standard.
//
// One file per case, cases/<area>/<ID>.cpp, registering its ID with
// CXP_CHECK next to the check's definition (validation_case_files checks the
// shape).  This header holds the helpers every area shares;
// cases/<area>/_helpers.h those one area's cases share.
#pragma once

#include <algorithm>
#include <cmath>
#include <map>
#include <numeric>
#include <random>
#include <set>
#include <cstring>
#include <thread>

#include <QXmlStreamReader>

#include "cxp/compliance/checker.h"
#include "cxp/parser/stream_parser.h"
#include "cxp/protocol/crc.h"
#include "cxp/image/test_pattern.h"
#include "cxp/validation/runner.h"

namespace cxp::validation::checks {

using OptAck = std::optional<RawAck>;

std::string ackStr(const OptAck& a);

bool is(const OptAck& a, int code);

bool replicatedWord(uint32_t w);

std::string hex(uint32_t v);

bool isDiscoveryConfig(uint32_t cc);

// Keep the link usable across checks that reset or reconfigure it.
void preserveLink(Context& c);

// §10.1.2: a ConnectionReset write need not be acknowledged; the host waits
// 200 ms before talking to the device again.  The wait is scaled: on a slow
// link the write may still be queued behind earlier traffic, and an ack
// arriving after the next command would be taken for its answer.
void connectionReset(Context& c);

// A stream packet arrives within ms (scaled) of the call: the stream a check
// started is still running.  Returns as soon as one does.
bool streamContinues(Context& c, int ms);

// Put a feature back as it was on exit.  A register-backed feature is
// restored byte for byte, so a value the XML does not allow (an enum
// register that powers up 0) still comes back.
void preserveFeature(Context& c, const std::string& name);

bool trySet(Context& c, const std::string& name, const Value& v);

std::optional<int64_t> featInt(Context& c, const std::string& name);

// The name a spec feature has in this XML, or the vendor alias it goes by.
Feature* featureOrAlias(Context& c, const std::string& sfnc, const std::string& alias,
                        std::string* used = nullptr);

// Stream images and their spec line length (words) — 0 when PixelF is unknown.
std::vector<ImageRec> imagesOf(const std::vector<Captured>& cap);

size_t specLineWords(const ImageRec& im);

bool imageComplete(const ImageRec& im);

std::vector<ImageRec> completeImages(const std::vector<ImageRec>& ims);

// Pixel bytes of an 8-bit line in transmission order: P0 first (LSByte of
// the FIFO word), as §9.4.2 requires.
std::vector<uint8_t> lineBytesP0(const Words& line);

std::vector<uint8_t> lineBytesP3(const Words& line);

uint32_t imageRequired(Context& c, size_t want, size_t got, const char* what);

std::vector<uint32_t> pktTags(const std::vector<StreamPkt>& pk, uint8_t sid);

size_t tagBreaks(const std::vector<uint32_t>& tags, int* first_bad = nullptr);

// Table 23: SOP, 4x0x04, 1024 counting words (P0 = 4k), EOP.
Words hostTestPacket(int corrupt_words = 0);

int linkTestErrors(const Words& f);

std::vector<Captured> framesOfType(const std::vector<Captured>& cap, uint8_t type);

double percentile(std::vector<double> v, double p);

// ControlPacketSizeMax, or abort when it is not a usable limit (§10.3.31:
// whole packet in bytes, multiple of 4, >= 128).  The boundary cases derive
// sizes from it, so a nonsense value must not become a 4 GB read.
uint32_t usableCpsm(Context& c);

void write64(Context& c, uint32_t addr, uint64_t v);

// -- bench time (bench::CAP_TIMES) --------------------------------------------
// One command and its acknowledgments with the bench's own clock: on a slow
// simulator the host's wall-clock says nothing about the device's
// milliseconds, the bench's time stamps do.  timed is false without
// CAP_TIMES (then only acks is filled).  The device's millisecond is the
// bench's (SYNC): a simulated device may count its control time limits
// faster than real time, and then every §8.6.1.1 limit is judged in its
// milliseconds.
struct TimedExchange {
    std::vector<RawAck> acks;
    std::vector<FrameTime> times;  // times[i] belongs to acks[i] (when timed)
    uint64_t cmd_start_ps = 0;     // first bit of the command's SOP on the uplink
    uint64_t cmd_end_ps = 0;       // last bit of its EOP
    uint64_t ms_ps = 1000000000ull;  // the device's millisecond
    bool timed = false;
    // Bench picoseconds as device milliseconds.
    double ms(uint64_t ps) const { return double(ps) / double(ms_ps); }
};

// Send cmd, collect its acknowledgments up to the first final one (waiting
// at most timeout_ms, scaled) and any in extra_ms (scaled) after it, then pair
// each with its FRAME_DL time and the command with its UPLINK_MARK times.
TimedExchange timedExchange(Context& c, const Words& cmd, int timeout_ms, int extra_ms = 300);

// Wait until ms of the device's milliseconds have passed by the bench's
// clock (bench::CAP_TIMES; without it, ms of the host's).  Gives up after
// timeout_ms of the host's (scaled).
bool waitBenchMs(Context& c, double ms, int timeout_ms = 600000);

}  // namespace cxp::validation::checks
