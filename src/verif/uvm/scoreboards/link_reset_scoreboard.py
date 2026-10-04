"""link_reset_scoreboard — §10.3.28 link-reset controller checks.

A host write of 1 to bootstrap register 0x4000 (ConnectionReset) makes the
register file apply the §10.3.28 values and hold the ConnectionReset bit
(link_reset_active) until the tx domain has echoed it; link_reset_done
pulses once when it clears.

Consumes:
* UplinkTxn from host_uplink_agent — counts host writes of 1 to 0x4000
  (not on an extension connection: decision D3 ignores them there).
* SidebandEvent from cxp_sideband_monitor — link_reset_active /
  link_reset_done / rate_to_discovery.

Checks:
* every link-reset window produces exactly one link_reset_done pulse
  (active 0->1 edges == done pulses);
* rate_to_discovery mirrors link_reset_active every cycle (§10.3.28 the
  device rolls back to its discovery configuration for the window);
* link_reset_done never fires without the window having been active;
* at least one window opened, and no more windows than requests, when
  the host issued ConnectionReset writes (back-to-back requests inside a
  window legally merge into one window).
"""

from __future__ import annotations

from pyuvm import uvm_tlm_analysis_fifo

from uvm.scoreboards.sb_base import CxpScoreboard

from uvm.common.clocks import ms_ns
from cxp_protocol import regmap as rm
from uvm.common.cxp_pkg import UplinkKind
from uvm.common.decisions import D3_EXTENSION_MHCID_SILENT
from uvm.common.handles import get_dut

_CONN_RESET_ADDR = rm.CONNECTION_RESET


class LinkResetScoreboard(CxpScoreboard):
    def build_phase(self):
        super().build_phase()
        self.uplink_fifo   = uvm_tlm_analysis_fifo("uplink_fifo",   self)
        self.sideband_fifo = uvm_tlm_analysis_fifo("sideband_fifo", self)
        self.uplink_xp     = self.uplink_fifo.analysis_export
        self.sideband_xp   = self.sideband_fifo.analysis_export

        self.reset_requests = 0
        self.ignored_requests = 0   # on an extension connection (D3)
        self.active_windows = 0     # link_reset_active 0->1 edges
        self.done_pulses    = 0
        self.rate_mismatch  = 0
        self._prev_active   = 0
        self._window_open   = False
        self._t_open        = 0.0
        self.windows_ns: list = []

    async def run_phase(self):
        import cocotb
        cocotb.start_soon(self._drain_uplink())
        await self._drain_sideband()

    async def _drain_uplink(self):
        while True:
            cmd = await self.uplink_fifo.get()
            if (cmd.kind == UplinkKind.CTRL_CMD_WRITE
                    and (cmd.address & 0xFFFF) == _CONN_RESET_ADDR
                    and cmd.payload and (cmd.payload[0] & 1)):
                if D3_EXTENSION_MHCID_SILENT and int(get_dut().from_extension_link.value):
                    self.ignored_requests += 1
                    continue
                self.reset_requests += 1

    async def _drain_sideband(self):
        while True:
            evt = await self.sideband_fifo.get()
            active = evt.sb_link_reset_active
            # §10.3.28: rate_to_discovery mirrors link_reset_active.
            if evt.sb_rate_to_discovery != active:
                self.rate_mismatch += 1
                self.err(
                    "rate_mirror",
                    f"rate_to_discovery={evt.sb_rate_to_discovery} does not "
                    f"mirror link_reset_active={active} (§10.3.28)"
                )
            if active and not self._prev_active:
                self.active_windows += 1
                self._window_open = True
                self._t_open = evt.t_ns
            if self._prev_active and not active:
                dur = evt.t_ns - self._t_open
                self.windows_ns.append(dur)
                # The device's own time: the host polls ConnectionReset
                # until it reads 0 and gives up after 200 ms (§10.3.28).
                if dur > ms_ns(200):
                    self.err("window_long",
                             f"ConnectionReset held {dur:.0f} ns, 200 ms is "
                             f"{ms_ns(200):.0f} ns")
            self._prev_active = active
            if evt.sb_link_reset_done:
                self.done_pulses += 1
                if not self._window_open:
                    self.err(
                        "done_no_window",
                        "link_reset_done pulsed with no active window")
                self._window_open = False

    def _final_check(self):
        if self.active_windows != self.done_pulses:
            self.err(
                "window_done",
                f"link-reset window/done mismatch: windows="
                f"{self.active_windows} done={self.done_pulses}"
            )
        if self.reset_requests > 0:
            if self.done_pulses < 1:
                self.err(
                    "no_window",
                    f"{self.reset_requests} ConnectionReset request(s) "
                    "produced no link-reset window"
                )
            if self.done_pulses > self.reset_requests:
                self.err(
                    "excess_windows",
                    f"more link-reset windows ({self.done_pulses}) than "
                    f"ConnectionReset requests ({self.reset_requests})"
                )

    def report_phase(self):
        self.logger.info(
            f"link_reset_scoreboard: requests={self.reset_requests} "
            f"windows={self.active_windows} done={self.done_pulses} "
            f"rate_mismatch={self.rate_mismatch} {self.err_summary()}"
        )
