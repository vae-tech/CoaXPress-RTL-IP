// CXP-CAM-BOOT-004.  See cases/_common.h.

#include "cxp/validation/cases/boot/_helpers.h"

namespace cxp::validation::checks::boot {

namespace {

void boot004(Context& c) {
    checkString(c, "DeviceVendorName", Reg::DEVICE_VENDOR_NAME, 32, true);
    checkString(c, "DeviceModelName", Reg::DEVICE_MODEL_NAME, 32, true);
    checkString(c, "DeviceManufacturerInfo", Reg::DEVICE_MANUFACTURER_INFO, 48, true);
    checkString(c, "DeviceVersion", Reg::DEVICE_VERSION, 32, true);
    checkString(c, "DeviceSerialNumber", Reg::DEVICE_SERIAL_NUMBER, 16, true);
    checkString(c, "DeviceUserID", Reg::DEVICE_USER_ID, 16, false);
    const std::vector<uint8_t> old = c.readBlock(Reg::DEVICE_USER_ID, 16);
    auto toWords = [](const std::string& s) {
        std::vector<uint32_t> v(4, 0);
        for (size_t i = 0; i < s.size() && i < 16; ++i) v[i / 4] |= uint32_t(uint8_t(s[i])) << (24 - 8 * (i % 4));
        return v;
    };
    c.onExit([&c, old] {
        std::vector<uint32_t> v;
        for (size_t i = 0; i < 16; i += 4) {
            v.push_back(uint32_t(old[i]) << 24 | uint32_t(old[i + 1]) << 16 | uint32_t(old[i + 2]) << 8 | old[i + 3]);
        }
        c.writeRaw(Reg::DEVICE_USER_ID, v);
    });
    for (const std::string& s : {std::string("CXP-VALIDATION1"), std::string("CXP-VALIDATION16")}) {
        auto w = c.writeRaw(Reg::DEVICE_USER_ID, toWords(s));
        const std::string back = c.readString(Reg::DEVICE_USER_ID, 16);
        c.expect(is(w, Ack::WRITE_OK) && back == s, "DeviceUserID round trip of %zu chars: %s, read '%s'", s.size(),
                 ackStr(w).c_str(), back.c_str());
    }
    const uint32_t iidc2 = c.rd32(Reg::IIDC2_ADDRESS);
    if (iidc2 == 0) {
        c.info("Iidc2Address = 0: no IIDC2 register space");
    } else {
        auto a = c.readRaw(iidc2, 4);
        c.expect(is(a, Ack::READ_OK), "Iidc2Address = %s is readable: %s", hex(iidc2).c_str(), ackStr(a).c_str());
    }
}
CXP_CHECK("CXP-CAM-BOOT-004", boot004);

}  // namespace

}  // namespace cxp::validation::checks::boot
