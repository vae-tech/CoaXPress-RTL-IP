"""link_state_scoreboard — the device's link state (§10.1.1, §10.2).

Watches `sb_link_detected` (the device's "connection detected") against
what the host does on the uplink:

* after a reset the host sends IDLE; Detected rises within
  `UP_WORDS` uplink words;
* while the uplink is legal, Detected stays up — a drop outside a window
  a test declared (`allow_drop`) is an error;
* when a test makes the uplink illegal on purpose (holds the line, cuts
  a packet, slips a bit) it declares `expect_drop(deadline_ns)`: the drop
  must come by then; once IDLE resumes, Detected must come back within
  `UP_WORDS` words (`expect_up`);
* at the end of the test the link is up, unless the test says it is not
  (`end_down`).

`history` keeps every change as (t_ns, level) for the tests.
"""

from __future__ import annotations

from cocotb.utils import get_sim_time
from pyuvm import uvm_tlm_analysis_fifo

from uvm.agents.host_uplink_agent import bit_period_ps
from uvm.scoreboards.sb_base import CxpScoreboard

# Words of IDLE the device may take to (re)detect the link: the sampler
# locks on K28.5 hits and the monitor wants clean IDLE words.
UP_WORDS = 48


def word_ns() -> float:
    return 40 * bit_period_ps() / 1000.0


class LinkStateScoreboard(CxpScoreboard):
    def build_phase(self):
        super().build_phase()
        self.sb_fifo = uvm_tlm_analysis_fifo("sb_fifo", self)
        self.sideband_xp = self.sb_fifo.analysis_export
        self.history: list = []
        self.level = 0
        self._allowed: list = []        # [t0, t1] a drop is allowed in
        self._drops_due: list = []      # deadlines a drop must meet
        self._up_due: list = []         # deadlines Detected must be up by
        self.end_down = False
        self.t_reset = 0.0
        self.drops = 0

    # -- what the test declares -------------------------------------------
    def allow_drop(self, t0: float, t1: float) -> None:
        self._allowed.append([t0, t1])

    def expect_drop(self, deadline_ns: float) -> None:
        now = get_sim_time("ns")
        self._drops_due.append(deadline_ns)
        self.allow_drop(now, float("inf"))

    def expect_up(self, within_ns: float | None = None) -> None:
        now = get_sim_time("ns")
        self._up_due.append(now + (within_ns if within_ns is not None
                                   else UP_WORDS * word_ns()))

    def close_allowance(self) -> None:
        """The illegal stretch is over: drops from now on are errors
        again (after the link has come back)."""
        now = get_sim_time("ns")
        for w in self._allowed:
            if w[1] == float("inf"):
                w[1] = now + UP_WORDS * word_ns()

    def device_reset(self) -> None:
        """A reset takes the link down at once; it must come back up
        within UP_WORDS words of IDLE (plus the host's start-up)."""
        now = get_sim_time("ns")
        self.t_reset = now
        self.allow_drop(now - 1, now + 1000)
        self._up_due.append(now + (UP_WORDS + 16) * word_ns())

    # -- monitor ----------------------------------------------------------
    async def run_phase(self):
        # Power-up: the host starts sending IDLE after its start-up delay.
        self.device_reset()
        while True:
            evt = await self.sb_fifo.get()
            lvl = int(evt.sb_link_detected)
            if lvl == self.level:
                continue
            t = evt.t_ns
            self.history.append((t, lvl))
            self.level = lvl
            if lvl:
                self._up_due = [d for d in self._up_due if d < t and self._late(d, t)]
                continue
            self.drops += 1
            self._drops_due = [d for d in self._drops_due if d < t and self._late_drop(d, t)]
            if not any(a <= t <= b for a, b in self._allowed):
                self.err("link_drop",
                         f"link detected fell at {t:.0f} ns with the uplink "
                         "legal (§10.2)")

    def _late(self, due: float, t: float) -> bool:
        self.err("link_up_late",
                 f"link detected at {t:.0f} ns, due by {due:.0f} ns")
        return False

    def _late_drop(self, due: float, t: float) -> bool:
        self.err("link_drop_late",
                 f"link lost at {t:.0f} ns, due by {due:.0f} ns (§10.2)")
        return False

    def pending_count(self) -> int:
        now = get_sim_time("ns")
        return sum(1 for d in self._up_due if d > now and not self.level)

    def _final_check(self):
        now = get_sim_time("ns")
        for d in self._drops_due:
            self.err("link_no_drop",
                     f"the uplink was made illegal, the link was never lost "
                     f"(due by {d:.0f} ns)")
        for d in self._up_due:
            if not self.level and d <= now:
                self.err("link_not_up",
                         f"link detected still down at the end (due by {d:.0f} ns)")
        if not self.level and not self.end_down and not self._up_due:
            self.err("link_not_up", "the test ended with the link down")

    def report_phase(self):
        self.logger.info(f"link_state: {len(self.history)} changes, "
                         f"{self.drops} drops, level {self.level} "
                         f"{self.err_summary()}")
