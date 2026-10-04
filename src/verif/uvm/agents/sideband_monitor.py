"""cxp_sideband_monitor — device status levels and pulses.

Publishes a `SidebandEvent` snapshot whenever a watched signal changes,
and one per clock cycle of its domain while a pulse is high, so every
cycle a pulse is high is counted exactly once and an idle device costs
no Python work.

Two domains, sampled on their own clocks (the shell's ports are what
`cxp_device_top` drives, or taps into it):

* rx_clk — link state (`sb_rx_lock`, `sb_aligned`, `sb_link_detected`),
  the ConnectionReset bit and its end, the receiver's error pulses, the
  control plane's status pulses, the recreated host trigger and its
  glitch pulse, and the connection-test counters;
* app_clk — the pixel port's framing pulses.

A pulse field of an event is set only by the loop of its own domain, so
a consumer that counts pulses per event counts each cycle once; levels
appear in every event.
"""

from __future__ import annotations

from dataclasses import dataclass

import cocotb
from cocotb.triggers import Edge, First, NextTimeStep, ReadOnly, RisingEdge
from cocotb.utils import get_sim_time
from pyuvm import uvm_agent, uvm_analysis_port, uvm_monitor
from uvm.common.handles import get_dut


@dataclass
class SidebandEvent:
    sb_rx_lock: int = 0
    sb_aligned: int = 0
    sb_link_detected: int = 0
    sb_trigger_out_app: int = 0
    sb_trigger_out_app_edge: int = 0
    sb_trigger_glitch_pulse: int = 0
    sb_lt_err_count: int = 0
    sb_ctrl_reset_pulse: int = 0
    sb_pkt_err_pulse: int = 0
    sb_rx_code_err_pulse: int = 0
    sb_rx_disp_err_pulse: int = 0
    # A command the parser answered itself, without a register access:
    # 0x80 CRC, 0x42/0x45/0x46/0x47 malformed, 0x43 write on an extension
    # link (§5.1).
    sb_ctrl_nack_pulse: int = 0
    sb_ctrl_nack_code: int = 0
    # §10.3.28 ConnectionReset.
    sb_link_reset_active: int = 0
    sb_link_reset_done: int = 0
    sb_rate_to_discovery: int = 0
    # §10.3.38/39 connection-test 64-bit packet counters.
    sb_lt_pkt_count_tx: int = 0
    sb_lt_pkt_count_rx: int = 0
    # Pixel port framing (app_clk).
    sb_pix_restart_pulse: int = 0
    sb_pix_stray_eof_pulse: int = 0
    domain: str = "rx"
    t_ns: float = 0.0

    # The three views the scoreboards and coverage count, by code.
    @property
    def sb_cmd_crc_err_pulse(self) -> int:
        return int(bool(self.sb_ctrl_nack_pulse) and self.sb_ctrl_nack_code == 0x80)

    @property
    def sb_cmd_logical_err_pulse(self) -> int:
        c = self.sb_ctrl_nack_code
        return int(bool(self.sb_ctrl_nack_pulse) and c >> 4 == 0x4 and c != 0x43)

    @property
    def sb_cmd_logical_err_code(self) -> int:
        return self.sb_ctrl_nack_code

    @property
    def sb_router_reject_pulse(self) -> int:
        return int(bool(self.sb_ctrl_nack_pulse) and self.sb_ctrl_nack_code == 0x43)

    @property
    def sb_router_reject_code(self) -> int:
        return self.sb_ctrl_nack_code


_RX_LEVELS = ("sb_rx_lock", "sb_aligned", "sb_link_detected",
              "sb_trigger_out_app", "sb_lt_err_count", "sb_ctrl_nack_code",
              "sb_link_reset_active", "sb_rate_to_discovery",
              "sb_lt_pkt_count_tx", "sb_lt_pkt_count_rx")
_RX_PULSES = ("sb_trigger_glitch_pulse", "sb_ctrl_reset_pulse",
              "sb_pkt_err_pulse", "sb_rx_code_err_pulse",
              "sb_rx_disp_err_pulse", "sb_ctrl_nack_pulse",
              "sb_link_reset_done")
_APP_PULSES = ("sb_pix_restart_pulse", "sb_pix_stray_eof_pulse")


class SidebandMonitor(uvm_monitor):
    def build_phase(self):
        self.ap = uvm_analysis_port("ap", self)
        self._prev_trig = 0
        self.events = 0
        # The latest snapshot, for code that wants a level now.
        self.last = SidebandEvent()

    async def run_phase(self):
        cocotb.start_soon(self._domain("app", "app_clk_in", (), _APP_PULSES))
        await self._domain("rx", "rx_clk_in", _RX_LEVELS, _RX_PULSES)

    async def _domain(self, name, clk_name, levels, pulses):
        dut = get_dut()
        clk = getattr(dut, clk_name)
        watched = [getattr(dut, s) for s in levels + pulses]
        prev = None
        per_clock = False
        while True:
            if per_clock:
                await RisingEdge(clk)
            else:
                await First(*[Edge(s) for s in watched])
            await ReadOnly()
            vals = {s: int(getattr(dut, s).value) for s in levels + pulses}
            await NextTimeStep()
            per_clock = any(vals[p] for p in pulses)
            snap = tuple(vals[s] for s in levels)
            if not per_clock and snap == prev:
                continue
            prev = snap
            self._publish(name, vals)

    def _publish(self, domain: str, vals: dict) -> None:
        evt = SidebandEvent(domain=domain, t_ns=get_sim_time("ns"))
        for k, v in vals.items():
            setattr(evt, k, v)
        # Levels of the other domain: from the last snapshot.
        for k in _RX_LEVELS:
            if k not in vals:
                setattr(evt, k, getattr(self.last, k))
        if domain == "rx":
            trig = vals["sb_trigger_out_app"]
            evt.sb_trigger_out_app_edge = int(bool(trig) and not self._prev_trig)
            self._prev_trig = trig
        self.last = evt
        self.events += 1
        self.ap.write(evt)


class SidebandAgent(uvm_agent):
    def build_phase(self):
        self.mon = SidebandMonitor("mon", self)
