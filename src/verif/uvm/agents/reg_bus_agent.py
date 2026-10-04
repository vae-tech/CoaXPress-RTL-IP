"""cxp_reg_bus_agent — passive monitor of the device's register bus.

Inside `cxp_device_top` the control plane reaches the register file
(`cxp_ctrl_bootstrap_regs`) over a one-cycle request / acknowledge bus; the TB
shell mirrors it to the `rb_*` ports.  Every access is published as one
`RegTxn`: the address, direction, write data and byte enables of the
request, and the read data and Table 22 code (`err`, 0 = accepted) of the
acknowledgment one cycle later.

The monitor wakes on a request and samples every rx_clk only while
accesses are in flight.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from cocotb.triggers import NextTimeStep, ReadOnly, RisingEdge
from pyuvm import uvm_agent, uvm_analysis_port, uvm_monitor

from uvm.common.handles import get_dut


@dataclass
class RegTxn:
    addr: int
    write: bool
    wdata: int
    wstrb: int
    rdata: int = 0
    err: int = 0               # Table 22 code of the access; 0 = accepted


class RegBusMonitor(uvm_monitor):
    def build_phase(self):
        self.ap = uvm_analysis_port("ap", self)
        self.accesses = 0

    async def run_phase(self):
        dut = get_dut()
        pend: deque = deque()
        quiet = 0
        while True:
            if not pend and quiet >= 2 and not int(dut.rb_req.value):
                # The request is a flop output: it rises in the delta
                # cycles of an rx_clk edge, so sample that same edge.
                await RisingEdge(dut.rb_req)
                quiet = 0
            else:
                await RisingEdge(dut.rx_clk_in)
            await ReadOnly()
            req, ack = int(dut.rb_req.value), int(dut.rb_ack.value)
            done = None
            if ack and pend:
                t = pend.popleft()
                t.rdata = int(dut.rb_rdata.value)
                t.err = int(dut.rb_err.value)
                done = t
            if req:
                pend.append(RegTxn(addr=int(dut.rb_addr.value),
                                   write=bool(int(dut.rb_we.value)),
                                   wdata=int(dut.rb_wdata.value),
                                   wstrb=int(dut.rb_wstrb.value)))
            quiet = 0 if (req or ack) else quiet + 1
            await NextTimeStep()
            if done is not None:
                self.accesses += 1
                self.ap.write(done)

    def report_phase(self):
        self.logger.info(f"reg bus: {self.accesses} accesses")


class RegBusAgent(uvm_agent):
    def build_phase(self):
        self.mon = RegBusMonitor("mon", self)
