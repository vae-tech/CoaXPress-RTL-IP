"""link_error_scoreboard — the receiver's error pulses against the plan (§8.2.2).

The device's uplink receiver reports an 8B/10B code error, a running
disparity error, a packet framing error (`sb_*_err_pulse`) and a
corrupted trigger (`trig_out_glitch_pulse`).  On a clean link all four
stay 0 for the whole test; each error the host injects on purpose must
show up.

The plan comes from the uplink transactions: a symbol inverted
(`inject_code_at`) must give at least one code error, a disparity flip
(`inject_disp_at`) at least one disparity error, a trigger sent with a
corrupted copy that cannot be repaired (`glitch`) one glitch pulse.  An
injected error may cascade (a flipped disparity makes the next symbols
disparity errors too, a lost comma a framing error), so injected kinds
are checked as "at least"; kinds nothing injected must stay at 0.

A test that makes the line illegal some other way (holds it, slips a
bit) declares the stretch with `allow(t0, t1)`: pulses inside it are
not counted against the plan.
"""

from __future__ import annotations

from cocotb.utils import get_sim_time
from pyuvm import uvm_tlm_analysis_fifo

from uvm.common.cxp_pkg import UplinkKind
from uvm.scoreboards.sb_base import CxpScoreboard

_KINDS = ("code", "disp", "pkt", "glitch")


class LinkErrorScoreboard(CxpScoreboard):
    def build_phase(self):
        super().build_phase()
        self.uplink_fifo = uvm_tlm_analysis_fifo("uplink_fifo", self)
        self.sb_fifo = uvm_tlm_analysis_fifo("sb_fifo", self)
        self.uplink_xp = self.uplink_fifo.analysis_export
        self.sideband_xp = self.sb_fifo.analysis_export
        self.injected = {k: 0 for k in _KINDS}
        self.seen = {k: 0 for k in _KINDS}
        self.excused = {k: 0 for k in _KINDS}
        self._allowed: list = []

    def allow(self, t0: float, t1: float = float("inf")) -> None:
        self._allowed.append([t0, t1])

    def close(self) -> None:
        now = get_sim_time("ns")
        for w in self._allowed:
            if w[1] == float("inf"):
                w[1] = now

    async def run_phase(self):
        import cocotb
        cocotb.start_soon(self._drain_uplink())
        while True:
            evt = await self.sb_fifo.get()
            if evt.domain != "rx":
                continue
            hits = {"code": evt.sb_rx_code_err_pulse,
                    "disp": evt.sb_rx_disp_err_pulse,
                    "pkt": evt.sb_pkt_err_pulse,
                    "glitch": evt.sb_trigger_glitch_pulse}
            excused = any(a <= evt.t_ns <= b for a, b in self._allowed)
            for k, v in hits.items():
                if v:
                    if excused:
                        self.excused[k] += 1
                    else:
                        self.seen[k] += 1

    async def _drain_uplink(self):
        while True:
            x = await self.uplink_fifo.get()
            trig = x.kind in (UplinkKind.TRIGGER_RISE, UplinkKind.TRIGGER_FALL)
            # A Table 15 character is taken out of the stream before the
            # decoder: damage there shows in the trigger, not as a code error.
            if x.inject_code_at >= 0 and not trig:
                self.injected["code"] += 1
            if x.inject_disp_at >= 0:
                self.injected["disp"] += 1
            if x.kind in (UplinkKind.TRIGGER_RISE, UplinkKind.TRIGGER_FALL) \
                    and not x.trig_repairable:
                self.injected["glitch"] += 1

    def _final_check(self):
        inj_any = any(self.injected[k] for k in ("code", "disp"))
        for k in _KINDS:
            if self.seen[k] + self.excused[k] < self.injected[k]:
                self.err(f"{k}_missing",
                         f"{self.injected[k]} {k} error(s) injected, the "
                         f"device reported {self.seen[k]}")
            if not self.injected[k] and self.seen[k]:
                # A symbol error may cascade into disparity and framing
                # errors; nothing else may raise a pulse.
                if k in ("disp", "pkt") and inj_any:
                    continue
                if k == "code" and self.injected["disp"]:
                    continue
                self.err(f"{k}_unexpected",
                         f"{self.seen[k]} {k} error pulse(s) on a link the "
                         "host kept clean")

    def report_phase(self):
        self.logger.info(
            "link_error: " + " ".join(
                f"{k}={self.seen[k]}/{self.injected[k]}"
                + (f"(+{self.excused[k]} excused)" if self.excused[k] else "")
                for k in _KINDS) + f" {self.err_summary()}")
