"""cxp_cfg_agent — single-pin agent for level config signals.

Pokes the configuration the device has no register for: cfg_use_tpg /
cfg_run / cfg_arbitrary / cfg_dsizeP / cfg_trig_polarity /
from_extension_link.  Everything else (geometry, TestMode, the
connection-test counter clears, StreamPacketSizeMax) is a register the
host writes over the uplink, as it is in the product.
"""

from __future__ import annotations

from dataclasses import dataclass

from cocotb.triggers import RisingEdge
from pyuvm import (
    ConfigDB, uvm_agent, uvm_driver, uvm_sequence, uvm_sequence_item,
    uvm_sequencer,
)
from uvm.common.cxp_pkg import CFG_DSIZE_P_KEY, DEFAULT_PKT_DSIZE_P
from uvm.common.handles import get_dut


@dataclass
class CfgXact:
    use_tpg: int = 0
    run: int = 0
    arbitrary: int = 0
    dsizeP: int = DEFAULT_PKT_DSIZE_P
    trig_polarity: int = 0
    # §5.1 register-router extension-link strap (1 = reject host writes
    # with ack 0x43).  Quasi-static; held until the next CfgItem.
    from_extension_link: int = 0
    hold_cycles: int = 1  # how many rx_clk cycles to wait before next item


class CfgItem(uvm_sequence_item):
    def __init__(self, name="cfg_item"):
        super().__init__(name)
        self.xact = CfgXact()


# rx_clk cycles for a cfg_* change to reach every clock domain.
CFG_CROSS_CYCLES = 24


class CfgSequencer(uvm_sequencer):
    pass


class CfgDriver(uvm_driver):
    async def run_phase(self):
        dut = get_dut()
        while True:
            item = await self.seq_item_port.get_next_item()
            x = item.xact
            # cxp_device_top takes its cfg_*_i levels on rx_clk.
            await RisingEdge(dut.rx_clk_in)
            dut.cfg_use_tpg.value          = x.use_tpg
            dut.cfg_run.value              = x.run
            dut.cfg_arbitrary.value        = x.arbitrary
            dut.cfg_dsizeP.value           = x.dsizeP
            dut.cfg_trig_polarity.value    = x.trig_polarity
            dut.from_extension_link.value  = x.from_extension_link
            for _ in range(max(1, x.hold_cycles)):
                await RisingEdge(dut.rx_clk_in)
            # The cfg_* levels are taken on rx_clk and cross to the pixel
            # and tx clocks (cxp_cdc_bus) before they act: an image offered
            # to the pixel port earlier would meet the old cfg_run and be
            # dropped at the acquisition gate.
            for _ in range(CFG_CROSS_CYCLES):
                await RisingEdge(dut.rx_clk_in)
            self.seq_item_port.item_done()


class CfgAgent(uvm_agent):
    def build_phase(self):
        self.seqr = CfgSequencer("seqr", self)
        self.drv  = CfgDriver("drv", self)

    def connect_phase(self):
        self.drv.seq_item_port.connect(self.seqr.seq_item_export)


class CfgToggleSeq(uvm_sequence):
    """Flips TPG/external + arbitrary/rectangular knobs (§9.1).

    `dsizeP=None` (default) reads from the ConfigDB key written by
    CxpTopTest.build_phase so the wire packet size stays in lockstep
    with the value the stream scoreboard adaptively checks against.
    """

    def __init__(self, name="cfg_toggle_seq", use_tpg=1, arbitrary=0,
                 dsizeP=None, trig_polarity=0, from_extension_link=0):
        super().__init__(name)
        self.use_tpg = use_tpg
        self.arbitrary = arbitrary
        self.dsizeP = dsizeP
        self.trig_polarity = trig_polarity
        self.from_extension_link = from_extension_link

    async def body(self):
        dsize = self.dsizeP
        if dsize is None:
            # Sequences aren't components, so look up via the root path —
            # the wildcard "*" key set by the test matches.
            dsize = ConfigDB().get(
                None, "uvm_test_top", CFG_DSIZE_P_KEY,
                default=DEFAULT_PKT_DSIZE_P,
            )
        item = CfgItem("c")
        await self.start_item(item)
        item.xact.use_tpg              = self.use_tpg
        item.xact.run                  = 1
        item.xact.arbitrary            = self.arbitrary
        item.xact.dsizeP               = int(dsize)
        item.xact.trig_polarity        = self.trig_polarity
        item.xact.from_extension_link  = self.from_extension_link
        item.xact.hold_cycles          = 4
        await self.finish_item(item)
