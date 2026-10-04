// CXP-CAM-GEN-006.  See cases/_common.h.

#include "cxp/validation/cases/gen/_helpers.h"

namespace cxp::validation::checks::gen {

namespace {

void gen006(Context& c) {
    if (!c.tree()) c.skip("no XML loaded");
    const std::set<std::string> side_effects = {"TestMode", "TpgRun", "TestErrorCount", "TestPacketCountTx",
                                                "TestPacketCountRx", "StreamPacketSizeMax"};
    std::string skipped;
    for (Feature* f : c.tree()->features()) {
        c.checkpoint();
        if (!f->reg || f->kind == FeatureKind::Command || f->kind == FeatureKind::Category) continue;
        if (side_effects.count(f->name)) {
            skipped += " " + f->name;
            continue;
        }
        const uint32_t addr = uint32_t(f->reg->address);
        const std::string acc = f->reg->access;
        auto cur = c.readRaw(addr, f->reg->length);
        if (acc == "WO") continue;
        if (!is(cur, Ack::READ_OK)) {
            c.expect(false, "%s: read %s", f->name.c_str(), ackStr(cur).c_str());
            continue;
        }
        const auto before = cur->values();
        if (acc == "RO") {
            auto w = c.writeRaw(addr, before);
            c.expect(is(w, Ack::RO_WRITE), "%s (RO): write answered %s", f->name.c_str(), ackStr(w).c_str());
            continue;
        }
        c.onExit([&c, addr, before] { c.writeRaw(addr, before); });
        if (f->kind == FeatureKind::Integer && f->reg->length == 4 && f->min && f->max) {
            for (double v : {*f->min, *f->max}) {
                auto w = c.writeRaw(addr, {uint32_t(int64_t(v))});
                auto r = c.tryRd32(addr);
                c.expect(is(w, Ack::WRITE_OK) && r == uint32_t(int64_t(v)), "%s = %lld (range end): %s, reads %s",
                         f->name.c_str(), (long long)v, ackStr(w).c_str(), r ? std::to_string(*r).c_str() : "?");
            }
        } else if (f->kind == FeatureKind::Enumeration && f->reg->length == 4) {
            for (const auto& [name, val] : f->enum_entries) {
                auto w = c.writeRaw(addr, {uint32_t(val)});
                auto r = c.tryRd32(addr);
                c.expect(is(w, Ack::WRITE_OK) && r == uint32_t(val), "%s = %s: %s", f->name.c_str(), name.c_str(),
                         ackStr(w).c_str());
            }
        }
        c.writeRaw(addr, before);
    }
    if (!skipped.empty()) c.note("not swept (side effects on the link):%s", skipped.c_str());
    c.note("out-of-range and invalid-entry writes are NEG-003; pIsLocked is not modelled by this host");
}
CXP_CHECK("CXP-CAM-GEN-006", gen006);

}  // namespace

}  // namespace cxp::validation::checks::gen
