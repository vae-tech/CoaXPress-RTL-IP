// CXP-CAM-PROT-002.  See cases/_common.h.

#include "cxp/validation/cases/_common.h"

namespace cxp::validation::checks::prot {

namespace {

void prot002(Context& c) {
    c.startRecording();
    for (uint32_t a : {Reg::STANDARD, Reg::REVISION, Reg::DEVICE_VENDOR_NAME}) c.readRaw(a, 4);
    c.writeRaw(Reg::MASTER_HOST_CONNECTION_ID, {c.rd32(Reg::MASTER_HOST_CONNECTION_ID)});
    auto rec1 = c.stopRecording();
    auto rec2 = c.acquire(c.iparam("images"));
    rec1.insert(rec1.end(), rec2.begin(), rec2.end());
    size_t bad_frame = 0, bad_type = 0, ext = 0, embedded = 0;
    std::map<int, size_t> types;
    for (const auto& cf : rec1) {
        const Words& f = cf.frame;
        if (f.size() < 3 || f.front() != SOP_WORD || f.back() != EOP_WORD || !replicatedWord(f[1])) {
            ++bad_frame;
            continue;
        }
        const int t = uint8_t(f[1]);
        ++types[t];
        if (t == 0x02) ++bad_type;
        else if (t == 0x05 || t == 0x06) ++ext;
        else if (t != 0x01 && t != 0x03 && t != 0x04) ++bad_type;
        for (size_t i = 1; i + 1 < f.size(); ++i) embedded += f[i] == SOP_WORD || f[i] == EOP_WORD;
    }
    std::string hist;
    for (auto [t, n] : types) hist += strprintf(" 0x%02X:%zu", t, n);
    c.info("%zu downlink frames, types%s", rec1.size(), hist.c_str());
    c.expect(bad_frame == 0, "%zu frames not framed as 4xK27.7 .. 4xK29.7 with a 4x replicated type", bad_frame);
    c.expect(bad_type == 0, "%zu frames with a type a Device must not send (0x02 or reserved)", bad_type);
    if (ext) c.note("%zu frames use host-stack extension types 0x05/0x06 (event, heartbeat)", ext);
    if (embedded) c.note("%zu SOP/EOP-valued words inside frame bodies (no K flag on this link)", embedded);
}
CXP_CHECK("CXP-CAM-PROT-002", prot002);

}  // namespace

}  // namespace cxp::validation::checks::prot
