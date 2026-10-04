"""Three-domain clock generators driven from a cocotb start_soon.

The TB shell exposes app_clk_in / tx_clk_in / rx_clk_in as independent
inputs.  By default the cxp_clk_rst_agent runs all three at the same
nominal 10 ns period (the single-PLL MVP integration).  vs_cdc_sweep
overrides the periods at runtime to walk through the ratio bins from
§11.1 cg_cdc.

Periods are expressed in nanoseconds.  The agent also drives async
resets via reset() — one or all three domains at a time.
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer


DEFAULT_APP_PERIOD_NS = 10
DEFAULT_TX_PERIOD_NS  = 10
DEFAULT_RX_PERIOD_NS  = 10


# The periods the three clocks are running at, in ns.  The host uplink
# driver takes its nominal bit period from the rx entry, and a retune
# moves it with the clock unless the test has asked for a ppm offset.
_PERIODS: dict[str, float] = {"app": DEFAULT_APP_PERIOD_NS,
                              "tx":  DEFAULT_TX_PERIOD_NS,
                              "rx":  DEFAULT_RX_PERIOD_NS}


def current_periods() -> dict:
    return dict(_PERIODS)


def rx_period_ns() -> float:
    return _PERIODS["rx"]


def ms_ns(ms: float) -> float:
    """The device's `ms` milliseconds in simulated ns: the device counts
    time in RX_CLK_KHZ rx_clk cycles per ms (build knob)."""
    from uvm.common.build import RX_CLK_KHZ
    return ms * RX_CLK_KHZ * _PERIODS["rx"]


def tx_period_ns() -> float:
    return _PERIODS["tx"]


# The running Clock task per domain.  Two Clock coroutines driving one
# signal fight over it — every retune has to kill the previous task, so
# the handles are kept here rather than dropped on the floor.
_CLOCK_TASKS: dict[str, object] = {}


def stop_clocks():
    """Kill the running Clock coroutines (the signals hold their level)."""
    for task in _CLOCK_TASKS.values():
        task.cancel()
    _CLOCK_TASKS.clear()


def start_clocks(dut,
                 app_period=DEFAULT_APP_PERIOD_NS,
                 tx_period=DEFAULT_TX_PERIOD_NS,
                 rx_period=DEFAULT_RX_PERIOD_NS):
    """Spawn the three Clock coroutines and enable all gates.

    Any clock already running is stopped first, so this is also the way
    to change a period mid-test.
    """
    stop_clocks()
    _PERIODS.update(app=app_period, tx=tx_period, rx=rx_period)
    dut.app_clk_en.value = 1
    dut.tx_clk_en.value  = 1
    dut.rx_clk_en.value  = 1
    for name, sig, period in (("app", dut.app_clk_in, app_period),
                              ("tx",  dut.tx_clk_in,  tx_period),
                              ("rx",  dut.rx_clk_in,  rx_period)):
        _CLOCK_TASKS[name] = cocotb.start_soon(
            Clock(sig, period, unit="ns").start())


async def reset(dut, cycles: int = 8, domains: str = "all"):
    """Pulse async resets low for `cycles` rx_clk cycles, then release.

    domains: "all" | "app" | "tx" | "rx" — only those domains' rst_n are
    pulsed; the others stay at 1.
    """
    if domains in ("all", "app"):
        dut.app_rst_n.value = 0
    if domains in ("all", "tx"):
        dut.tx_rst_n.value = 0
    if domains in ("all", "rx"):
        dut.rx_rst_n.value = 0
    for _ in range(cycles):
        await RisingEdge(dut.rx_clk_in)
    dut.app_rst_n.value = 1
    dut.tx_rst_n.value  = 1
    dut.rx_rst_n.value  = 1
    for _ in range(2):
        await RisingEdge(dut.rx_clk_in)
