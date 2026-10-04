#include "cxp/validation/cases/ctrl/_helpers.h"

namespace cxp::validation::checks::ctrl {

std::vector<RegRef> registerList(Context& c) {
    std::vector<RegRef> out;
    for (const BootReg& r : bootstrapTable()) {
        const bool w = r.access == Access::RW && r.addr != Reg::CONNECTION_RESET && r.addr != Reg::TEST_MODE;
        out.push_back({r.name, r.addr, r.bytes, w});
    }
    if (c.tree()) {
        for (Feature* f : c.tree()->features()) {
            if (!f->reg || f->reg->address < Reg::MANUFACTURER_SPACE || f->reg->access == "WO") continue;
            const bool w = f->reg->access == "RW" && f->name != "TpgRun" && f->name != "TestMode";
            out.push_back({f->name, uint32_t(f->reg->address), f->reg->length, w});
        }
    }
    return out;
}

void latencySweep(Context& c, const std::vector<RegRef>& regs, int reads, int write_backs, const char* mode,
                  std::vector<double>& all, size_t& lost) {
    double worst = 0;
    std::string worst_reg;
    for (const auto& r : regs) {
        c.checkpoint();
        for (int i = 0; i < reads; ++i) {
            auto a = c.readRaw(r.addr, r.bytes);
            if (!a) {
                ++lost;
                continue;
            }
            all.push_back(a->latency_ms);
            if (a->latency_ms > worst) worst = a->latency_ms, worst_reg = r.name;
            if (r.writable && i < write_backs && a->code == Ack::READ_OK) {
                auto w = c.writeRaw(r.addr, a->values());
                if (!w) ++lost;
                else all.push_back(w->latency_ms);
            }
        }
    }
    c.info("%s: worst %.2f ms (%s)", mode, worst, worst_reg.c_str());
}

}  // namespace cxp::validation::checks::ctrl
