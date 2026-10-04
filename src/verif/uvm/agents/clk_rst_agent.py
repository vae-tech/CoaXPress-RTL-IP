"""cxp_clk_rst_agent — utility agent for clocks & resets.

Implements §6.7.  Starts the three free-running clocks (different
periods → different app:tx:rx ratios), randomises ppm offsets, and
fires async resets one or all domains at a time.

For vs_cdc_sweep the agent re-spawns clocks with new periods between
sub-runs.  Coverage on the resulting ratio bins is collected by
cg_cdc on the cxp_coverage_subscriber.
"""

from __future__ import annotations

from cocotb.triggers import Timer
from pyuvm import uvm_agent, uvm_analysis_port
from uvm.common.handles import get_dut

from uvm.common.clocks import start_clocks, reset


class ClkRstAgent(uvm_agent):
    def build_phase(self):
        self.ap = uvm_analysis_port("ap", self)
        # Set by the env: the host whose bit clock follows a retune.
        self.uplink_drv = None
        # Called with the domains before and after every reset this agent
        # drives.
        self.on_reset_start: list = []
        self.on_reset: list = []
        self.resets_done: list = []     # domains, per reset (coverage)
        self.ratios: list = []          # (app, tx, rx) ns, per retune
        self.app_period = 10
        self.tx_period  = 10
        self.rx_period  = 10

    def start_of_simulation_phase(self):
        self.logger.warning(
            f"ClkRstAgent configured: "
            f"app_period={self.app_period}ns tx_period={self.tx_period}ns "
            f"rx_period={self.rx_period}ns"
        )

    def start_default(self):
        dut = get_dut()
        start_clocks(dut, self.app_period, self.tx_period, self.rx_period)

    async def do_reset(self, cycles=8, domains="all"):
        dut = get_dut()
        for cb in self.on_reset_start:
            cb(domains)
        await reset(dut, cycles=cycles, domains=domains)
        for cb in self.on_reset:
            cb(domains)
        self.resets_done.append(domains)
        self.ap.write(("reset_done", domains))

    async def set_cdc_ratio(self, app_ns: int, tx_ns: int, rx_ns: int):
        """Replace running clocks with new periods.  Used by vs_cdc_sweep."""
        dut = get_dut()
        # Freeze the DUT's clocks at the gates, kill the running Clock
        # coroutines (two of them driving one signal fight over it), then
        # start the new ones and release the gates.
        dut.app_clk_en.value = 0
        dut.tx_clk_en.value  = 0
        dut.rx_clk_en.value  = 0
        await Timer(max(app_ns, tx_ns, rx_ns) * 4, unit="ns")
        start_clocks(dut, app_ns, tx_ns, rx_ns)
        # The host is an independent transmitter, but a bench that retunes
        # the device's clock is modelling a different link, not a host that
        # drifted: its nominal bit period follows, and only `ppm` offsets it.
        if self.uplink_drv is not None:
            self.uplink_drv.retune_to_clock()
        self.app_period, self.tx_period, self.rx_period = app_ns, tx_ns, rx_ns
        self.ratios.append((app_ns, tx_ns, rx_ns))
        self.logger.warning(
            f"ClkRstAgent retuned: app_period={app_ns}ns "
            f"tx_period={tx_ns}ns rx_period={rx_ns}ns"
        )
        self.ap.write(("cdc_ratio", app_ns, tx_ns, rx_ns))
