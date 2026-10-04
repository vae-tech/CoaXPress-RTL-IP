// UVM-test_router_reject.  See cases/_common.h.

#include "cxp/validation/cases/uvm/_helpers.h"

namespace cxp::validation::checks::uvm {

namespace {

void routerReject(Context& c) {
    c.needBench(bench::CAP_EXT_LINK, "make the command link an extension connection");
    uvmIdleConfig(c);
    preserveLink(c);
    c.preserve(Reg::TEST_ERROR_COUNT_SELECTOR);
    RegModel m = readModel(c);
    c.benchPin(bench::EXT_LINK, 1);
    c.benchSync();
    std::mt19937 rng(c.seed(1));
    // Decision D3: a MasterHostConnectionID write there is ignored and
    // acknowledged 0x01 (§10.3.30 note); every other write is refused.
    size_t refused = 0, x43 = 0, ignored = 0, n_ignore = 0;
    const int writes = c.iparam("writes"), n_reads = c.iparam("reads");
    for (int i = 0; i < writes; ++i) {
        const uint32_t addr = kUvmRw[rng() % 4];
        uint32_t v = legalValue(c, addr, rng, m);
        if (addr == Reg::MASTER_HOST_CONNECTION_ID && v == m[addr]) v ^= 1;
        auto a = c.writeRaw(addr, {v});
        if (addr == Reg::MASTER_HOST_CONNECTION_ID) {
            ++n_ignore;
            ignored += is(a, Ack::WRITE_OK);
            c.expect(is(a, Ack::WRITE_OK), "write %s = %s over the extension link: %s (ignored, 0x01)",
                     regName(addr), hex(v).c_str(), ackStr(a).c_str());
            continue;
        }
        const bool ref = a && a->code >= Ack::BAD_ADDRESS && a->code != Ack::CRC_ERROR;
        refused += ref;
        x43 += is(a, Ack::RO_WRITE);
        c.expect(ref, "write %s = %s over the extension link: %s (refused)", regName(addr), hex(v).c_str(),
                 ackStr(a).c_str());
    }
    size_t reads = 0;
    for (int i = 0; i < n_reads; ++i) {
        const uint32_t addr = kUvmRw[rng() % 4];
        auto a = c.readRaw(addr, 4);
        const bool ok = is(a, Ack::READ_OK) && a->data.size() == 1 && a->values()[0] == m[addr];
        reads += ok;
        c.expect(ok, "read %s over the extension link: %s", regName(addr), ackStr(a).c_str());
    }
    c.benchSend({bench::PIN, bench::EXT_LINK, 0});
    c.benchSync();
    size_t kept = 0;
    for (uint32_t addr : kUvmRw) kept += c.rd32(addr) == m[addr];
    c.expect(refused + n_ignore == size_t(writes) && ignored == n_ignore && kept == 4,
             "%zu of %zu writes refused and %zu of %zu MasterHostConnectionID writes ignored over the extension "
             "link; %zu of 4 registers unchanged when read over the master link (REQ-ERR-012: read-only)",
             refused, size_t(writes) - n_ignore, ignored, n_ignore, kept);
    c.expect(reads == size_t(n_reads), "%zu of %d reads over the extension link answered 0x00 with the value", reads,
             n_reads);
    if (refused && x43 != refused) {
        c.note("refused with a code other than 0x43; §10.3.30 leaves the code open (plan clarification), UVM expects 0x43");
    }
}
CXP_CHECK("UVM-test_router_reject", routerReject);

}  // namespace

}  // namespace cxp::validation::checks::uvm
