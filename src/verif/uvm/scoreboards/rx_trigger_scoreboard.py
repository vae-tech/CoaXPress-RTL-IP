"""rx_trigger_scoreboard — host triggers against the device's trigger output (§8.3.2).

Every Table 15 trigger the host sends is paired, in order, with what the
device does with it:

* a trigger the device can take (two of three Delay copies alike, Delay
  at most 239, at most one leader character damaged — §8.2.2) that
  changes the host's trigger level and whose edge matches
  `cfg_trig_polarity` (0 rising, 1 falling) gives one pulse of
  `trig_out`; one of the other edge gives none, and one that repeats the
  level (a resend, §8.3.3) gives none;
* a ConnectionReset de-asserts the level (§8.3.2): a host left asserted
  gives the falling edge — a pulse at polarity 1 — and a countdown still
  running is dropped;
* on an extension connection (`from_extension_link`) triggers are not the
  device's: nothing;
* a trigger it cannot take gives a glitch pulse (`trig_out_glitch_pulse`)
  and no `trig_out`;
* the pulse comes a fixed time after the trigger's first character plus
  Delay units of 1/24 bit (§8.3.2.1, Figure 20): `latency - Delay x unit`
  is the same for every trigger, within one rx_clk cycle and the host's
  jitter.  Every such normalised latency is recorded; the spread is
  checked at the end and the min / max / histogram reported.

A `trig_out` pulse with no trigger waiting for it is an error; so is a
glitch pulse with no damaged trigger.
"""

from __future__ import annotations

from collections import Counter, deque

from pyuvm import uvm_tlm_analysis_fifo

from uvm.agents.host_uplink_agent import bit_period_ps
from uvm.common.build import OS_RATIO
from uvm.common.clocks import current_periods
from uvm.common.cxp_pkg import UplinkKind
from uvm.common.handles import get_dut
from uvm.scoreboards.sb_base import CxpScoreboard


class _Deassert:
    """The falling edge of a ConnectionReset: no Delay, no start time."""
    t_start_ns = -1.0
    trig_delay_taken = 0


class RxTriggerScoreboard(CxpScoreboard):
    def build_phase(self):
        super().build_phase()
        self.uplink_fifo   = uvm_tlm_analysis_fifo("uplink_fifo",   self)
        self.sideband_fifo = uvm_tlm_analysis_fifo("sideband_fifo", self)
        self.uplink_xp     = self.uplink_fifo.analysis_export
        self.sideband_xp   = self.sideband_fifo.analysis_export

        self.trig_seen = 0
        self.trig_matched = 0
        self.trig_filtered = 0
        self.trig_resent = 0
        self.level = 0                    # the host's trigger level
        self.glitch_seen = 0
        self._q: deque = deque()          # triggers owed a trig_out pulse
        self._glitch_q: deque = deque()   # triggers owed a glitch pulse
        self.norm_latency: list = []      # (delay, ns) per trigger
        self._latency_by_ratio: dict = {}
        self.jitter_ui = 0.0              # set by the env from the host

    def device_reset(self) -> None:
        """The receiver and its trigger-level latch returned to power-on."""
        self._q.clear()
        self._glitch_q.clear()
        self.level = 0

    async def run_phase(self):
        import cocotb
        cocotb.start_soon(self._drain_uplink())
        await self._drain_sideband()

    async def _drain_uplink(self):
        dut = get_dut()
        while True:
            x = await self.uplink_fifo.get()
            if x.kind not in (UplinkKind.TRIGGER_RISE, UplinkKind.TRIGGER_FALL):
                continue
            self.trig_seen += 1
            if int(dut.from_extension_link.value):
                self.trig_filtered += 1
                continue
            if not x.trig_repairable:
                self._glitch_q.append(x)
                continue
            pol = int(dut.cfg_trig_polarity.value)
            rise = x.kind == UplinkKind.TRIGGER_RISE
            if int(rise) == self.level:
                self.trig_resent += 1           # a resend: no event
                continue
            self.level = int(rise)
            if rise == (pol == 0):
                self._q.append(x)
            else:
                self.trig_filtered += 1

    async def _drain_sideband(self):
        prev_crst = 0
        while True:
            evt = await self.sideband_fifo.get()
            if evt.domain != "rx":
                continue
            crst = evt.sb_link_reset_active
            if crst and not prev_crst:
                # §8.3.2: discovery de-asserts the trigger; what was counting
                # is dropped, a host left asserted gives the falling edge.
                self._q.clear()
                if self.level and int(get_dut().cfg_trig_polarity.value):
                    self._q.append(_Deassert())
                self.level = 0
            prev_crst = crst
            if evt.sb_trigger_glitch_pulse:
                self.glitch_seen += 1
                if self._glitch_q:
                    self._glitch_q.popleft()
                else:
                    self.err("glitch_unexpected",
                             f"trigger glitch pulse at {evt.t_ns:.0f} ns with no "
                             "damaged trigger sent")
            if evt.sb_trigger_out_app_edge:
                if not self._q:
                    self.err("trig_unexpected",
                             f"trig_out pulse at {evt.t_ns:.0f} ns with no trigger "
                             "waiting for one")
                    continue
                x = self._q.popleft()
                self.trig_matched += 1
                if x.t_start_ns >= 0:
                    unit_ns = bit_period_ps() / 1000.0 / 24.0
                    d = x.trig_delay_taken
                    latency = evt.t_ns - x.t_start_ns - d * unit_ns
                    self.norm_latency.append((d, latency))
                    ratio = tuple(current_periods().values())
                    self._latency_by_ratio.setdefault(ratio, []).append(latency)

    def pending_count(self) -> int:
        """Triggers whose pulse has not come yet: the device waits out the
        Delay after the packet's last character."""
        return len(self._q) + len(self._glitch_q)

    def spread_ns(self) -> float:
        v = [n for _, n in self.norm_latency]
        return (max(v) - min(v)) if v else 0.0

    def _final_check(self):
        if self._q:
            self.err("trig_missing",
                     f"{len(self._q)} trigger(s) never produced a trig_out pulse")
        if self._glitch_q:
            self.err("glitch_missing",
                     f"{len(self._glitch_q)} damaged trigger(s) gave no glitch pulse")
        # Fixed latency (§8.3.2.1): one rx_clk cycle of quantisation, one
        # oversample of phase, and the host's own jitter.
        for ratio, values in self._latency_by_ratio.items():
            rx_ns = ratio[2]
            limit = 2 * rx_ns + 3 * self.jitter_ui * OS_RATIO * rx_ns + 1
            spread = max(values) - min(values)
            if len(values) > 1 and spread > limit:
                self.err("latency_spread",
                         f"trigger latency less Delay spreads {spread:.1f} ns "
                         f"at app/tx/rx ratio {ratio} over {len(values)} triggers "
                         f"(limit {limit:.1f} ns): the Delay law does not hold")

    def report_phase(self):
        v = [n for _, n in self.norm_latency]
        hist = Counter(round(n) for n in v)
        lat = (f" latency-Delay min {min(v):.0f} max {max(v):.0f} ns "
               f"hist {dict(sorted(hist.items()))}" if v else "")
        self.logger.info(
            f"rx_trigger: trig={self.trig_seen} matched={self.trig_matched} "
            f"filtered={self.trig_filtered} resent={self.trig_resent} "
            f"glitch={self.glitch_seen}{lat} "
            f"{self.err_summary()}"
        )
