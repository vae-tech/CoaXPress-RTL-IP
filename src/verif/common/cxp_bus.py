"""Tiny synchronous register-bus driver used by the cxp_ctrl_bootstrap_regs TB.

The bus mirrors the simple slave the RTL exposes:

    addr / wdata / we / re   (drive synchronous to ``sys_clk``)
    rdata / ready / err      (registered, valid one cycle after request)

Read protocol (single outstanding request):

    cycle N    : addr = A, re = 1
    cycle N+1  : ready = 1, rdata holds the registered value, re = 0

The same protocol is used for writes; the slave registers the new value on
cycle N+1 and asserts ``ready``.
"""

from __future__ import annotations

import cocotb
from cocotb.triggers import NextTimeStep, ReadOnly, RisingEdge


class CxpRegBus:
    """Single-cycle register-file master."""

    def __init__(self, dut, clk):
        self.dut = dut
        self.clk = clk
        dut.addr.value  = 0
        dut.wdata.value = 0
        dut.we.value    = 0
        dut.re.value    = 0
        self.err = 0          # Table 22 code of the last access (`err` port)

    def _sample_err(self) -> None:
        if hasattr(self.dut, "err"):
            self.err = int(self.dut.err.value)

    async def write(self, addr: int, data: int) -> None:
        await RisingEdge(self.clk)
        self.dut.addr.value  = addr & 0xFFFF_FFFF
        self.dut.wdata.value = data & 0xFFFF_FFFF
        self.dut.we.value    = 1
        self.dut.re.value    = 0
        await RisingEdge(self.clk)
        await ReadOnly()
        self._sample_err()
        await NextTimeStep()
        self.dut.we.value    = 0
        self.dut.addr.value  = 0
        self.dut.wdata.value = 0

    async def read(self, addr: int) -> int:
        await RisingEdge(self.clk)
        self.dut.addr.value  = addr & 0xFFFF_FFFF
        self.dut.we.value    = 0
        self.dut.re.value    = 1
        await RisingEdge(self.clk)
        # `rdata` was registered on this edge.  Sample it in the read-only
        # phase so the post-NBA value is observed deterministically across
        # both Verilator and Questa.
        await ReadOnly()
        val = int(self.dut.rdata.value)
        self._sample_err()
        # Step back to the next active phase so the caller may drive signals
        # immediately after this returns.
        await NextTimeStep()
        self.dut.re.value    = 0
        self.dut.addr.value  = 0
        return val
