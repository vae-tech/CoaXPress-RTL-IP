// CXP-EMU-PROT-107.  See cases/_common.h.
//
// A Table 15 trigger and a command with no IDLE between them, in one
// character frame: the trigger right before the command's SOP and right
// after its EOP (§8.2.4 lets the low-speed trigger sit at any character
// boundary).  Both the trigger and the command must be taken.

#include "cxp/validation/cases/trig/_host_trig.h"

namespace cxp::validation::checks::prot {

namespace {

using namespace htrig;

void prot107(Context& c) {
    c.needBench(bench::CAP_CHARS | bench::CAP_TRIG_OUT, "send a trigger and a command in one character frame");
    uvm::uvmIdleConfig(c);
    preserveLink(c);
    uint32_t value = 0x51A70000;
    for (int64_t d : c.ilist("delays")) {
        for (bool before : {true, false}) {
            const char* where = before ? "trigger then command" : "command then trigger";
            // A write of a fresh value, then its read, each with a rising and a
            // falling trigger glued to it.
            ++value;
            for (bool write : {true, false}) {
                settleLow(c);
                const Chars cmd = frameChars(write ? writeCmd(Reg::MASTER_HOST_CONNECTION_ID, {value})
                                                   : readCmd(Reg::MASTER_HOST_CONNECTION_ID, 4));
                const Chars trig = lsTrigger(true, uint8_t(d));
                Chars ch = before ? trig : cmd;
                const Chars& tail = before ? cmd : trig;
                ch.insert(ch.end(), tail.begin(), tail.end());
                const Shot s = shoot(c, ch, 1, true);
                const bool cmd_ok = !s.cmd.empty() &&
                                    (write ? s.cmd[0].code == Ack::WRITE_OK
                                           : s.cmd[0].code == Ack::READ_OK && s.cmd[0].values().size() == 1 &&
                                                 s.cmd[0].values()[0] == value);
                c.expect(cmd_ok, "%s (Delay %lld), %s: %s", where, (long long)d, write ? "write" : "read back",
                         s.cmd.empty() ? "no acknowledgment" : ackName(s.cmd[0].code).c_str());
                c.expect(s.ioacks.size() == 1 && s.rises() == 1,
                         "%s (Delay %lld), %s: %zu I/O acknowledgment (1), %zu recreated rising edge (1)", where,
                         (long long)d, write ? "write" : "read", s.ioacks.size(), s.rises());
            }
        }
    }
    settleLow(c);
}
CXP_CHECK("CXP-EMU-PROT-107", prot107);

}  // namespace

}  // namespace cxp::validation::checks::prot
