"""cxp_apb_slave_agent — the user window's APB3 slave (§10.3 manufacturer range).

`cxp_device_top` sends control accesses to [p_USER_BASE, +p_USER_SIZE) to
its APB3 master port; everything else goes to the register file on the
register bus (`reg_bus_agent`).  The slave is a word memory in the TB
shell (`tb_cxp_top.sv`, reset value 0xA500_0000 | index); this agent

1. **drives its profile** — the answer latency in rx cycles, "never
   answers" (a hung bus) and PSLVERR — through `usr_latency` /
   `usr_slverr`, so a sequence can make the slave slow enough for the
   device's Wait (100 ms) and timeout (900 ms) acknowledgments;

2. **monitors the bus** — one `ApbTxn` per completed transfer, with the
   latency the transfer actually had (`wait_cycles`, counted from the
   access phase to PREADY), and keeps `mem`, a mirror of the slave memory
   built from the completed writes.  A transfer that never completes (a
   hung slave abandoned by the device) is published with `completed`
   False when the bus drops PSEL.

The monitor wakes on PSEL and samples every rx_clk only while a transfer
is open, so an idle bus costs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from cocotb.triggers import ReadOnly, RisingEdge, NextTimeStep
from pyuvm import (
    uvm_agent, uvm_analysis_port, uvm_driver, uvm_monitor,
    uvm_sequence, uvm_sequence_item, uvm_sequencer
)
from uvm.common.handles import get_dut


# TB shell parameters (tb_cxp_top.sv).
USER_BASE = 0x0002_0000
USER_WORDS = 256
USER_SIZE = 4 * USER_WORDS
NEVER = 0xFFFF


def user_reset_value(i: int) -> int:
    """The slave memory's reset content (tb_cxp_top.sv)."""
    return 0xA500_0000 | i


def in_user_window(addr: int) -> bool:
    return USER_BASE <= addr < USER_BASE + USER_SIZE


@dataclass
class ApbTxn:
    addr: int
    wdata: int
    rdata: int
    write: bool
    pslverr: bool
    wait_cycles: int           # rx cycles from the access phase to PREADY
    completed: bool = True     # False: PSEL dropped without PREADY


@dataclass
class ApbProfile:
    latency: int = 0           # rx cycles before PREADY; NEVER = hung
    slverr: int = 0


class ApbProfileItem(uvm_sequence_item):
    def __init__(self, name="apb_profile_item"):
        super().__init__(name)
        self.profile = ApbProfile()


class ApbSequencer(uvm_sequencer):
    pass


class ApbResponderDriver(uvm_driver):
    """Pushes the slave profile into the TB shell."""

    async def run_phase(self):
        dut = get_dut()
        while True:
            item = await self.seq_item_port.get_next_item()
            self.apply(item.profile)
            self.seq_item_port.item_done()

    @staticmethod
    def apply(p: ApbProfile) -> None:
        dut = get_dut()
        dut.usr_latency.value = min(int(p.latency), NEVER)
        dut.usr_slverr.value = 1 if p.slverr else 0


class ApbMonitor(uvm_monitor):
    def build_phase(self):
        self.ap = uvm_analysis_port("ap", self)
        # The start of every transfer (its SETUP phase): (address, write).
        self.ap_start = uvm_analysis_port("ap_start", self)
        self.mem = {i: user_reset_value(i) for i in range(USER_WORDS)}
        self.transfers = 0
        self.abandoned = 0
        self.max_wait = 0

    def _store(self, addr: int, wdata: int, strb: int) -> None:
        """A write lands on the bytes PSTRB enables (PSTRB[n]: PWDATA[8n+7:8n])."""
        i = (addr - USER_BASE) >> 2
        mask = sum(0xFF << (8 * n) for n in range(4) if strb >> n & 1)
        self.mem[i] = (self.mem.get(i, 0) & ~mask) | (wdata & mask)

    def reset_mem(self) -> None:
        """The slave memory after an rx reset."""
        self.mem = {i: user_reset_value(i) for i in range(USER_WORDS)}

    async def run_phase(self):
        dut = get_dut()
        while True:
            if not int(dut.apb_psel.value):
                await RisingEdge(dut.apb_psel)
            # One transfer: SETUP, then ACCESS until PREADY or PSEL drops.
            addr = write = wdata = None
            strb = 0xF
            waited = 0
            while True:
                await RisingEdge(dut.rx_clk_in)
                await ReadOnly()
                psel = int(dut.apb_psel.value)
                if not psel:
                    if addr is not None:
                        if int(dut.apb_done.value):
                            self.transfers += 1
                            self.max_wait = max(self.max_wait, waited)
                            rdata = int(dut.apb_done_rdata.value)
                            slverr = bool(int(dut.apb_done_slverr.value))
                            txn = ApbTxn(addr, wdata, rdata, write,
                                         slverr, waited)
                            if write and not slverr and in_user_window(addr):
                                self._store(addr, wdata, strb)
                        else:
                            self.abandoned += 1
                            txn = ApbTxn(addr, wdata, 0, write, False, waited, False)
                        await NextTimeStep()
                        self.ap.write(txn)
                    else:
                        await NextTimeStep()
                    break
                if addr is None:
                    addr = int(dut.apb_paddr.value)
                    write = bool(int(dut.apb_pwrite.value))
                    wdata = int(dut.apb_pwdata.value)
                    strb = int(dut.apb_pstrb.value)
                    started = (addr, write)
                else:
                    started = None
                if started is not None:
                    await NextTimeStep()
                    self.ap_start.write(started)
                    await ReadOnly()
                if int(dut.apb_penable.value):
                    if int(dut.apb_pready.value):
                        txn = ApbTxn(
                            addr=addr, wdata=wdata,
                            rdata=int(dut.apb_prdata.value), write=write,
                            pslverr=bool(int(dut.apb_pslverr.value)),
                            wait_cycles=waited)
                        await NextTimeStep()
                        self.transfers += 1
                        self.max_wait = max(self.max_wait, waited)
                        if write and not txn.pslverr and in_user_window(addr):
                            self._store(addr, wdata, strb)
                        self.ap.write(txn)
                        break
                    waited += 1
                await NextTimeStep()

    def report_phase(self):
        self.logger.info(f"apb (user window): {self.transfers} transfers, "
                         f"{self.abandoned} abandoned, max wait {self.max_wait}")


class ApbSlaveAgent(uvm_agent):
    def build_phase(self):
        self.seqr = ApbSequencer("seqr", self)
        self.drv  = ApbResponderDriver("drv", self)
        self.mon  = ApbMonitor("mon", self)

    def connect_phase(self):
        self.drv.seq_item_port.connect(self.seqr.seq_item_export)


class _ProfileSeq(uvm_sequence):
    LATENCY = 0
    SLVERR = 0

    def __init__(self, name=None, latency=None, slverr=None):
        super().__init__(name or type(self).__name__)
        self.latency = self.LATENCY if latency is None else latency
        self.slverr = self.SLVERR if slverr is None else slverr

    async def body(self):
        item = ApbProfileItem("p")
        await self.start_item(item)
        item.profile.latency = self.latency
        item.profile.slverr = self.slverr
        await self.finish_item(item)


class ApbResponderPerfectSeq(_ProfileSeq):
    """Answer every transfer at once, no error."""


class ApbResponderPslverrSeq(_ProfileSeq):
    """Answer every transfer with PSLVERR (the device answers 0x40)."""
    SLVERR = 1


class ApbResponderWaitstateSeq(_ProfileSeq):
    """Answer after `waits` rx cycles."""

    def __init__(self, name="apb_responder_waitstate_seq", waits=3):
        super().__init__(name, latency=waits)


class ApbResponderHangSeq(_ProfileSeq):
    """Never answer (a hung bus)."""
    LATENCY = NEVER
