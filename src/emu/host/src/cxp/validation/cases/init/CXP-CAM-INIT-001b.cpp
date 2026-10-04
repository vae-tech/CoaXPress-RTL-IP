// CXP-CAM-INIT-001b.  See cases/_common.h.
//
// Power-up through the bench's reset after every register a connection
// reset touches has been moved off its reset value (§10.3.28: the Device
// executes a connection reset after power-up), so a reset value read
// afterwards was set by the reset, not left from before.  Then the stream
// after power-up: no data packet while StreamPacketSizeMax is 0, none
// before AcquisitionStart, and the first one tagged 0.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::init {

namespace {

uint32_t pinBit(uint32_t pin) { return 1u << (pin - 1); }

void init001b(Context& c) {
    c.needBench(bench::CAP_RESET | bench::CAP_PIXEL, "power-cycle the device with the test pattern selected");
    preserveLink(c);
    c.preserve(Reg::TEST_MODE);
    c.preserve(Reg::ELECTRICAL_COMPLIANCE_TEST);
    const uint32_t tpg = pinBit(bench::USE_TPG);
    if (!c.benchReset(tpg)) c.abort("no power-on reset through the bench");

    // Move everything off its reset value.
    const uint32_t mhcid = 0x5A5A1234;
    c.wr32(Reg::MASTER_HOST_CONNECTION_ID, mhcid);
    c.wr32(Reg::STREAM_PACKET_SIZE_MAX, c.opt().host_spsm);
    c.sendOnly(std::vector<Words>(size_t(c.iparam("test_packets")), hostTestPacket(1)), 100);
    // The packets cross the serial uplink bit by bit: a read behind them
    // answers once they are all in.
    c.exchange(readCmd(Reg::STANDARD, 4), c.iparam("drain_ms"));
    c.wr32(Reg::TEST_MODE, 1);
    c.sleepMs(c.iparam("test_mode_ms"));
    auto ect = c.writeRaw(Reg::ELECTRICAL_COMPLIANCE_TEST, {c.rd32(Reg::CONNECTION_CONFIG_DEFAULT)});
    const uint64_t tx = c.rd64(Reg::TEST_PACKET_COUNT_TX), rx = c.rd64(Reg::TEST_PACKET_COUNT_RX);
    const uint32_t err = c.rd32(Reg::TEST_ERROR_COUNT);
    c.info("before the reset: MasterHostConnectionID %s, SPSM %u, TestMode %u, TestPacketCountTx %llu, "
           "TestPacketCountRx %llu, TestErrorCount %u, ElectricalComplianceTest write %s",
           hex(c.rd32(Reg::MASTER_HOST_CONNECTION_ID)).c_str(), c.rd32(Reg::STREAM_PACKET_SIZE_MAX),
           c.rd32(Reg::TEST_MODE), (unsigned long long)tx, (unsigned long long)rx, err, ackStr(ect).c_str());
    c.expect(tx > 0 && rx > 0 && err > 0, "the counters moved off 0 before the reset (Tx %llu, Rx %llu, errors %u)",
             (unsigned long long)tx, (unsigned long long)rx, err);

    // Power-up with the generator selected and not free-running.
    c.expect(c.benchReset(tpg), "power-on reset through the bench, device answers afterwards");
    struct Want { const char* name; uint32_t addr; uint32_t value; };
    const Want regs[] = {
        {"ConnectionReset", Reg::CONNECTION_RESET, 0},
        {"MasterHostConnectionID", Reg::MASTER_HOST_CONNECTION_ID, 0},
        {"StreamPacketSizeMax", Reg::STREAM_PACKET_SIZE_MAX, 0},
        {"TestMode", Reg::TEST_MODE, 0},
        {"TestErrorCountSelector", Reg::TEST_ERROR_COUNT_SELECTOR, 0},
        {"TestErrorCount", Reg::TEST_ERROR_COUNT, 0},
        {"XmlManifestSelector", Reg::XML_MANIFEST_SELECTOR, 0},
        {"HsUpconnection (no HS upconnection)", Reg::HS_UPCONNECTION, 0},
    };
    for (const Want& r : regs) {
        const uint32_t v = c.rd32(r.addr);
        c.expect(v == r.value, "after power-up: %s = %s (%s)", r.name, hex(v).c_str(), hex(r.value).c_str());
    }
    const uint64_t tx0 = c.rd64(Reg::TEST_PACKET_COUNT_TX), rx0 = c.rd64(Reg::TEST_PACKET_COUNT_RX);
    c.expect(tx0 == 0 && rx0 == 0, "after power-up: TestPacketCountTx %llu, TestPacketCountRx %llu (0, 0)",
             (unsigned long long)tx0, (unsigned long long)rx0);
    const uint32_t cc = c.rd32(Reg::CONNECTION_CONFIG);
    c.expect(isDiscoveryConfig(cc), "after power-up: ConnectionConfig = %s (one connection, discovery rate)",
             hex(cc).c_str());
    // §10.3.40 against §10.3.28: judged by CT-006.
    c.info("after power-up: ElectricalComplianceTest = %s", hex(c.rd32(Reg::ELECTRICAL_COMPLIANCE_TEST)).c_str());

    // No data packet while StreamPacketSizeMax is 0, even with an acquisition.
    c.prepareStreaming();  // programs StreamPacketSizeMax: undo it for this window
    c.wr32(Reg::STREAM_PACKET_SIZE_MAX, 0);
    c.startRecording();
    c.acqStart();
    c.sleepMs(c.iparam("idle_ms"));
    c.acqStop();
    auto cap = c.stopRecording();
    c.expect(framesOfType(cap, 0x01).empty() && framesOfType(cap, 0x04).empty(),
             "SPSM 0, acquisition running %d ms: %zu stream, %zu test packets (none, §10.3.28)", c.iparam("idle_ms"),
             framesOfType(cap, 0x01).size(), framesOfType(cap, 0x04).size());

    // Power-up again: SPSM programmed, no stream before AcquisitionStart, the
    // first packet after it tagged 0.
    c.expect(c.benchReset(tpg), "second power-on reset through the bench");
    c.wr32(Reg::STREAM_PACKET_SIZE_MAX, c.opt().host_spsm);
    cap = c.record(c.iparam("idle_ms"));
    c.expect(framesOfType(cap, 0x01).empty(), "SPSM %u, no AcquisitionStart, %d ms: %zu stream packets (none)",
             c.opt().host_spsm, c.iparam("idle_ms"), framesOfType(cap, 0x01).size());
    const auto pk = streamPackets(c.acquire(1));
    if (c.expect(!pk.empty(), "after AcquisitionStart: %zu stream packets", pk.size())) {
        c.expect(pk.front().tag == 0, "the first stream packet after power-up has tag %u (0, §10.3.28)",
                 pk.front().tag);
    }

    // Power-up with the generator free-running (the integrator's strap): still
    // no data packet while StreamPacketSizeMax is 0.
    c.expect(c.benchReset(tpg | pinBit(bench::TPG_RUN)), "power-on reset with TPG_RUN = 1");
    cap = c.record(c.iparam("idle_ms"));
    c.expect(framesOfType(cap, 0x01).empty(), "TPG_RUN = 1, SPSM 0 after power-up, %d ms: %zu stream packets (none)",
             c.iparam("idle_ms"), framesOfType(cap, 0x01).size());
    c.wr32(Reg::STREAM_PACKET_SIZE_MAX, c.opt().host_spsm);
    const auto run = streamPackets(c.record(c.iparam("idle_ms")));
    c.info("TPG_RUN = 1 and SPSM %u without AcquisitionStart: %zu stream packets (the free-run strap is the "
           "integrator's%s)", c.opt().host_spsm, run.size(), run.empty() ? "" : strprintf("; first tag %u", run.front().tag).c_str());
    if (!run.empty()) c.expect(run.front().tag == 0, "free-running: first stream packet after power-up has tag %u (0)",
                               run.front().tag);
    c.benchReset(tpg);
}
CXP_CHECK("CXP-CAM-INIT-001b", init001b);

}  // namespace

}  // namespace cxp::validation::checks::init
