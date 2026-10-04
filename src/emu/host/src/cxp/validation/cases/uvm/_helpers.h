// Helpers the uvm/ cases share (moved from checks/uvm.cpp).
// Dissolved into fixtures / scoreboards in M2.
//
// Validation checks: the PyUVM tests of src/verif/, reproduced over the link.
// See cases/_common.h.
//
// One check per UVM test class that the FIFO link can express
// (src/verif/uvm/tests/all_tests.py); the catalogue's UVM_EMULATOR entry says
// what each one keeps of its test and what it replaces.  The stimulus is the
// UVM test's: the same registers, patterns and counts.  The judging is the
// UVM scoreboards' — stream (Table 19 format, tags, framing,
// pixels against the TPG model), control (acks against a register model),
// link test (counters) — held to CXP 1.1.1 like every other check: an
// EXPECT_FAIL tag on the UVM side does not relax the mirror.
#pragma once

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::uvm {

// The full-width RW bootstrap registers UplinkCtrlRandomSeq draws from.
constexpr uint32_t kUvmRw[] = {Reg::MASTER_HOST_CONNECTION_ID, Reg::STREAM_PACKET_SIZE_MAX,
                               Reg::CONNECTION_CONFIG, Reg::TEST_ERROR_COUNT_SELECTOR};

const char* regName(uint32_t addr);

void stopAcqIfPossible(Context& c);

bool hasEntry(const Feature* f, const std::string& name);

// UVM streams the test pattern with CfgToggleSeq(use_tpg=1).  A bench with
// the pixel port selects the generator the same way, restored on exit, so
// the case does not depend on what an earlier case left.  The free-run
// input (TPG_RUN) stays low: the device's AcquisitionStart / Stop decide
// which images the generator sends.  Free-running, it would fill the link,
// and in the slow RTL sim the acks of the writes between acquisitions would
// come seconds late.
void runTestPattern(Context& c);

// Mono8 and the Bars test pattern, restored on exit: the stream every pixel
// of which the golden model predicts.  Returns the TPG pattern the pixels are
// compared with, or -1 when the XML offers no Bars.
int selectBars(Context& c);

// Start acquisition with the recorder on; once the first image header is in,
// run `during`; then wait for `more` further headers and the last one's tail,
// take the recording, stop and wait for a quiet link.  Returns the recording.
// A failed AcquisitionStop is a failed check, not the end of the case: the
// recording is judged all the same.
std::vector<Captured> streamAround(Context& c, size_t more, const std::function<void()>& during,
                                   int timeout_ms = 10000);

struct StreamSb {
    size_t packets = 0;
    size_t complete = 0;
};

// The UVM stream scoreboard over one recording's stream packets.
//
//  * format: Table 19 (DsizeP equals the payload, CRC, N + 8 words), and no
//    packet longer than `spsm` bytes when one is given;
//  * PacketTag +1 mod 256 per stream; a restart at 0 is accepted only right
//    after a ConnectionConfig write the host made at one of `tag_resets`;
//  * framing: every image but a last one cut by the stop carries Ysize line
//    markers of DsizeL = ceil(Xsize x bpp / 32) words;
//  * pixels: with `pattern` >= 0, every complete Mono8 image equals the
//    golden TPG model (static patterns only: Bars, GreyBars).
StreamSb streamScoreboard(Context& c, const std::vector<StreamPkt>& pk, int pattern, uint32_t spsm = 0,
                          const std::vector<double>& tag_resets = {}, const char* what = "");

// The register model (RAL mirror) behind UVM's random control traffic.
using RegModel = std::map<uint32_t, uint32_t>;

RegModel readModel(Context& c);

// A value CXP 1.1.1 lets the host write: UVM writes any 32-bit value, which
// only MasterHostConnectionID has to take.  StreamPacketSizeMax gets a
// multiple of 4 in the case's spsm_range.
uint32_t legalValue(Context& c, uint32_t addr, std::mt19937& rng, const RegModel& m);

// n random reads (write = 0), writes (1) or a mix (-1) of the four RW
// registers, each judged against the model; writes update it.  The times of
// ConnectionConfig writes go to `cc_writes` (they restart the packet tag).
void ctrlTraffic(Context& c, RegModel& m, int n, int write, std::mt19937& rng, std::vector<double>* cc_writes = nullptr);

void resetTestCounters(Context& c);

void expectCounters(Context& c, uint64_t want_rx, uint32_t want_err);

bool readsStandard(const OptAck& a);

// The state UVM starts every test in: out of reset with every input at 0
// (src/verif/uvm/common/defaults.py), so the test-pattern generator stopped
// (cfg_run = 0) and the pixel port selected (cfg_use_tpg = 0).  A bench that
// can reset the device does exactly that; one that cannot stops a
// free-running generator, which cuts its image wherever it is (the RTL then
// drops or reopens the packet it had open, and that is not what the UVM
// test looks at).  Without a bench the generator runs beside the test.
// Called first in a case: the reset clears every register written before.
void uvmIdleConfig(Context& c);

// ===========================================================================
// The pixel port (UVM video agent)
// ===========================================================================
// UVM drives frames into the pixel port (s_pix_*) with cfg_use_tpg = 0.  With
// a bench that has the port (CAP_PIXEL) the checks do the same, and judge
// every image against the pixels they sent; without one the device's own
// Bars test pattern stands in and the golden model judges the pixels.
bool usePixelPort(Context& c);

// VideoRandomSeq's space (the case's frame_xsizes, frame_ysizes and
// valid_percent_range), Mono8; pixels random here (UVM: a byte ramp).
PixelImage randomImage(Context& c, std::mt19937& rng, uint32_t sourcetag);

// _OneFrameSeq: 64 x 8 Mono8, a byte ramp, every cycle valid.
PixelImage rampImage(uint32_t xsize, uint32_t ysize);

// Pixels of one received line (§9.4.2, Figures 27-31): the bytes in
// transmission order P0, P1, ... carry the pixels MSB first, the MSB of
// the first pixel in P0 bit 7.
std::vector<uint16_t> unpackLine(const Words& line, uint32_t n, int bits);

// The UVM stream scoreboard against the frames sent into the pixel port:
// every sent frame comes back (no lost frame) with the header its metadata
// asked for, every line's marker right, and every pixel bit-exact.  Images
// are paired with frames by SourceTag (each frame sent carries its own), so
// one lost frame does not shift the comparison of the others.
void injectedScoreboard(Context& c, const std::vector<StreamPkt>& pk, const std::vector<PixelImage>& sent,
                        bool arbitrary, uint32_t spsm, const char* what = "");

// Send frames into the pixel port with the recorder on; `during` runs while
// the first one is still going in.  Waits for every frame's reply, then for
// the images to come out: every frame's header and a quiet link after the
// last reply (the last image is still in the device when its last pixel is
// taken).
std::vector<Captured> injectImages(Context& c, const std::vector<PixelImage>& imgs,
                                   const std::function<void()>& during = {});

// ===========================================================================
// Triggers (§8.3.2, §8.3.3)
// ===========================================================================
// Host -> device triggers go up as Table 15 characters (CXC1), judged: every
// one answered by a Table 17 I/O acknowledgment, and the trigger the device
// recreates (bench TRIG_OUT) follows them, as in the UVM tests.
struct HostTrigRun {
    size_t sent = 0;
    size_t rising = 0;                 // rising triggers sent
    std::vector<TimedShort> acks;
    std::vector<PinEdge> edges;
    std::vector<uint32_t> want_edges;  // TRIG_OUT levels the triggers should produce, in order
};

// A host trigger as a compliant host sends it: Table 15, six characters.
Chars trigChars(bool rising);

// Put the recreated trigger in a known state (low): one falling trigger.
void settleTrigOut(Context& c);

HostTrigRun hostTriggers(Context& c, const std::vector<bool>& kinds, int spacing_ms);

std::vector<bool> randomKinds(size_t n, unsigned seed);

void judgeAcks(Context& c, const HostTrigRun& r, const char* what);

// The recreated trigger (TRIG_OUT) either follows the trigger level (a rising
// trigger raises it, a falling one lowers it) or strobes once per rise of the
// host's level (polarity 0): a rising trigger with no falling one since is a
// resend (§8.3.3), not a new event.  The plan names it a "strobe output" and the RTL
// leaves the choice open, so either reading passes; which one is reported.
enum class TrigOut { Level, Strobe, Neither };

TrigOut trigOutReading(const HostTrigRun& r, std::string* seen);

void judgeEdges(Context& c, const HostTrigRun& r, const char* what);


size_t countTriggers(Context& c, double since);

// Device -> host triggers: `n` edges on the bench TRIG_IN from the low level,
// each answered like a Host does (a Table 17 acknowledgment on the uplink).
std::vector<TimedShort> deviceTriggers(Context& c, size_t n, int gap_ms);

// Each edge its own Table 16 packet: rising -> 4 x K28.4, falling -> 4 x K28.2,
// delay 0..3 (§8.3.2.2), in edge order starting with a rising edge.
void judgeDeviceTriggers(Context& c, const std::vector<TimedShort>& got, size_t edges, const char* what = "");

// `n` VideoRandomSeq frames, SourceTag 0, 1, ...
std::vector<PixelImage> randomImages(Context& c, std::mt19937& rng, size_t n);

// ===========================================================================
// Cross-interface
// ===========================================================================
// VsStressConcurrent: random video (pixel port, or the test pattern), random
// commands, a rising and a falling host trigger and link-test packets at once.
void preemptRound(Context& c, int pat, bool pixel, unsigned seed, const char* what);

// ===========================================================================
// Arbiter after a short last packet
// ===========================================================================
// One Mono8 frame (the case's frame) in 64-word packets: the image does not
// fill its last packet.  Pixel port when there is one, else the test pattern.
void shortTailImage(Context& c);

}  // namespace cxp::validation::checks::uvm
