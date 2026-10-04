// CXP-CAM-INIT-001.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::init {

namespace {

void init001(Context& c) {
    // §10.3.28: the device executes a connection reset at power-up, so the
    // registers read their connection-reset values straight after a
    // power-on reset, with no ConnectionReset written by the host.
    c.needBench(bench::CAP_RESET, "power-cycle the device");
    preserveLink(c);
    c.expect(c.benchReset(0), "power-on reset through the bench, device answers afterwards");
    c.waitQuiet();
    struct Want { const char* name; uint32_t addr; uint32_t value; };
    const Want regs[] = {
        {"ConnectionReset", Reg::CONNECTION_RESET, 0},
        {"MasterHostConnectionID", Reg::MASTER_HOST_CONNECTION_ID, 0},
        {"StreamPacketSizeMax", Reg::STREAM_PACKET_SIZE_MAX, 0},
        {"TestMode", Reg::TEST_MODE, 0},
        {"TestErrorCountSelector", Reg::TEST_ERROR_COUNT_SELECTOR, 0},
        {"TestErrorCount", Reg::TEST_ERROR_COUNT, 0},
        {"XmlManifestSelector", Reg::XML_MANIFEST_SELECTOR, 0},
    };
    for (const Want& r : regs) {
        const uint32_t v = c.rd32(r.addr);
        c.expect(v == r.value, "%s = %s (reset value %s)", r.name, hex(v).c_str(), hex(r.value).c_str());
    }
    const uint64_t tx = c.rd64(Reg::TEST_PACKET_COUNT_TX);
    const uint64_t rx = c.rd64(Reg::TEST_PACKET_COUNT_RX);
    c.expect(tx == 0, "TestPacketCountTx = %llu (reset value 0)", (unsigned long long)tx);
    c.expect(rx == 0, "TestPacketCountRx = %llu (reset value 0)", (unsigned long long)rx);
    const uint32_t cc = c.rd32(Reg::CONNECTION_CONFIG);
    c.expect(isDiscoveryConfig(cc), "ConnectionConfig = %s (1 connection at a discovery rate, 0x28 or 0x38)",
             hex(cc).c_str());
    const uint32_t hs = c.rd32(Reg::HS_UPCONNECTION);
    c.expect((hs & ~1u) == 0, "HsUpconnection = %s (bits 31:1 zero, bit 0 = support)", hex(hs).c_str());
    const int idle_ms = c.iparam("idle_ms");
    auto cap = c.record(idle_ms);
    const size_t stream = framesOfType(cap, 0x01).size();
    const size_t test = framesOfType(cap, 0x04).size();
    c.expect(stream == 0 && test == 0,
             "downlink idle for %d ms after reset: %zu stream, %zu test packets", c.wait(idle_ms), stream, test);
}
CXP_CHECK("CXP-CAM-INIT-001", init001);

}  // namespace

}  // namespace cxp::validation::checks::init
