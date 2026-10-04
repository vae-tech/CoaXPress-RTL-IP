// CXP-CAM-INIT-004.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::init {

namespace {

void init004(Context& c) {
    preserveLink(c);
    std::vector<double> lat;
    auto step = [&](const OptAck& a, int want, const char* what) {
        if (a) lat.push_back(a->latency_ms);
        return c.expect(is(a, want), "%s: %s", what, ackStr(a).c_str());
    };
    connectionReset(c);
    c.info("step 1: ConnectionReset written, waited 200 ms");
    auto a = c.readRaw(Reg::DEVICE_CONNECTION_ID, 4);
    if (step(a, Ack::READ_OK, "read DeviceConnectionID")) {
        c.expect(a->values()[0] == 0, "DeviceConnectionID = %s (0 on the master connection)",
                 hex(a->values()[0]).c_str());
    }
    step(c.writeRaw(Reg::MASTER_HOST_CONNECTION_ID, {1}), Ack::WRITE_OK, "write MasterHostConnectionID = 1");
    c.expect(c.rd32(Reg::MASTER_HOST_CONNECTION_ID) == 1, "MasterHostConnectionID reads back 1");
    a = c.readRaw(Reg::HS_UPCONNECTION, 4);
    if (step(a, Ack::READ_OK, "read HsUpconnection")) c.info("HsUpconnection = %s", hex(a->values()[0]).c_str());
    a = c.readRaw(Reg::CONTROL_PACKET_SIZE_MAX, 4);
    if (step(a, Ack::READ_OK, "read ControlPacketSizeMax")) {
        const uint32_t cpsm = a->values()[0];
        c.expect(cpsm >= 128 && cpsm % 4 == 0, "ControlPacketSizeMax = %u (>= 128, multiple of 4)", cpsm);
    }
    step(c.writeRaw(Reg::STREAM_PACKET_SIZE_MAX, {c.opt().host_spsm}), Ack::WRITE_OK,
         "write StreamPacketSizeMax = host maximum");
    c.expect(c.rd32(Reg::STREAM_PACKET_SIZE_MAX) == c.opt().host_spsm, "StreamPacketSizeMax reads back %u",
             c.opt().host_spsm);
    a = c.readRaw(Reg::CONNECTION_CONFIG_DEFAULT, 4);
    uint32_t ccd = 0;
    if (step(a, Ack::READ_OK, "read ConnectionConfigDefault")) {
        ccd = a->values()[0];
        c.expect((ccd >> 16) >= 1 && isValidSpeedCode(ccd & 0xFFFF),
                 "ConnectionConfigDefault = %s (>= 1 connection, Table 46 speed code)", hex(ccd).c_str());
        step(c.writeRaw(Reg::CONNECTION_CONFIG, {ccd}), Ack::WRITE_OK, "write ConnectionConfig = default");
        c.expect(c.rd32(Reg::CONNECTION_CONFIG) == ccd, "ConnectionConfig reads back %s", hex(ccd).c_str());
    }
    const int limit = c.opt().ack_latency_ms;
    for (double l : lat) {
        if (l > limit) c.expect(false, "an acknowledgment took %.1f ms (> %d ms)", l, limit);
    }
    c.expect(!lat.empty() && *std::max_element(lat.begin(), lat.end()) <= limit,
             "every discovery acknowledgment within %d ms (max %.1f ms)", limit,
             lat.empty() ? 0.0 : *std::max_element(lat.begin(), lat.end()));
    auto cap = c.acquire(c.iparam("images"));
    auto pk = streamPackets(cap);
    size_t bad = 0;
    for (const auto& p : pk) bad += !p.crc_ok;
    auto ims = completeImages(walkImages(pk));
    c.expect(!ims.empty(), "first image received complete (%zu complete images)", ims.size());
    c.expect(bad == 0, "no CRC errors in %zu stream packets", pk.size());
}
CXP_CHECK("CXP-CAM-INIT-004", init004);

}  // namespace

}  // namespace cxp::validation::checks::init
