"""cxp_io_agent — local device->host HS trigger source BFM.

Drives the ``trigger_in_app`` application input of cxp_interface_top —
the device's local HS trigger.  Each edge makes cxp_tx_trigger_hs emit a
§8.3.2 2-word trigger packet on the wire (rising -> K28.4, falling ->
K28.2).  The input is in the tx_clk domain.

The driver is edge-aware: it publishes a ("trig_rise" / "trig_fall")
IoEvent on its monitor analysis port for every trigger edge it produces,
so tx_trigger_scoreboard knows what to expect without snooping the DUT.
"""

from __future__ import annotations

from dataclasses import dataclass

from cocotb.triggers import RisingEdge
from pyuvm import (
    uvm_agent, uvm_analysis_port, uvm_driver, uvm_monitor, uvm_sequence,
    uvm_sequence_item, uvm_sequencer,
)
from uvm.common.handles import get_dut


@dataclass
class IoXact:
    trigger: int = 0          # target trigger_in_app level (0/1)
    hold_cycles: int = 16     # tx_clk cycles to hold before the next item


@dataclass
class IoEvent:
    kind: str                 # "trig_rise" / "trig_fall"
    value: int = 0


class IoItem(uvm_sequence_item):
    def __init__(self, name="io_item"):
        super().__init__(name)
        self.xact = IoXact()


class IoSequencer(uvm_sequencer):
    pass


class IoDriver(uvm_driver):
    async def run_phase(self):
        dut = get_dut()
        cur_trig = 0
        while True:
            item = await self.seq_item_port.get_next_item()
            x = item.xact
            await RisingEdge(dut.tx_clk_in)
            if x.trigger != cur_trig:
                evt = IoEvent("trig_rise" if x.trigger else "trig_fall")
                if hasattr(self, "mon"):
                    self.mon.ap.write(evt)
                cur_trig = x.trigger
            dut.trigger_in_app.value = x.trigger
            for _ in range(max(1, x.hold_cycles)):
                await RisingEdge(dut.tx_clk_in)
            self.seq_item_port.item_done()


class IoMonitor(uvm_monitor):
    """Driver-mirrored — the driver publishes IoEvents here directly."""

    def build_phase(self):
        self.ap = uvm_analysis_port("ap", self)


class IoAgent(uvm_agent):
    def build_phase(self):
        self.seqr = IoSequencer("seqr", self)
        self.drv  = IoDriver("drv", self)
        self.mon  = IoMonitor("mon", self)

    def connect_phase(self):
        self.drv.seq_item_port.connect(self.seqr.seq_item_export)
        self.drv.mon = self.mon


class IoTriggerSeq(uvm_sequence):
    """Toggle trigger_in_app `n_edges` times (alternating rise/fall from
    the de-asserted level), spacing edges by `gap` tx_clk cycles.  The
    first edge waits until the device has detected the host: it sends
    triggers only to a connected host (§8.3.2)."""

    def __init__(self, name="io_trigger_seq", n_edges=4, gap=24):
        super().__init__(name)
        self.n_edges = n_edges
        self.gap = gap

    async def body(self):
        dut = get_dut()
        while not int(dut.sb_link_detected.value):
            await RisingEdge(dut.tx_clk_in)
        level = 0
        for _ in range(self.n_edges):
            level ^= 1
            item = IoItem("io")
            await self.start_item(item)
            item.xact.trigger = level
            item.xact.hold_cycles = self.gap
            await self.finish_item(item)
        # Leave the trigger de-asserted.
        if level != 0:
            item = IoItem("io")
            await self.start_item(item)
            item.xact.trigger = 0
            item.xact.hold_cycles = self.gap
            await self.finish_item(item)
