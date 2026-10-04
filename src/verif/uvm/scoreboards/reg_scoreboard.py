"""reg_scoreboard — the register file at its bus, against the register map.

Every access the control plane makes to `cxp_ctrl_bootstrap_regs` (the
register-bus monitor's `RegTxn`) is checked against the reference
register file `cxp_protocol.regref.RegRef`, generated from
`src/regmap/cxp_regmap.yaml` — the same source the RTL table follows:

* a read returns the model's value (reset values, RO constants, strings,
  the §10.3.19-27 slots, the XML ROM, the manufacturer words) and its
  Table 22 code (0x40 undecoded, 0x44 a write-only feature);
* a write is accepted or refused with the model's code (0x41 a value
  the register does not take, 0x43 read-only), and an accepted one
  updates the model — through the byte enables;
* ConnectionReset written 1 loads the §10.3.28 values into the model;
  the bit itself reads 1 while the device holds it
  (`sb_link_reset_active`) and 0 after;
* the connection-test counters are live: a read returns what the
  device's counter held in that cycle (`SidebandMonitor.last`).

Since power-up executes a connection reset (§10.3.28), the model starts
from the power-on values and a device reset (`reset_model`) returns it
there.
"""

from __future__ import annotations

from pyuvm import uvm_tlm_analysis_fifo

from cxp_protocol import packets as gp
from cxp_protocol import regmap
from cxp_protocol.regref import RegRef, Unknown

from uvm.scoreboards.control_scoreboard import load_xml_blob
from uvm.scoreboards.sb_base import CxpScoreboard


_CNT = {
    regmap.TEST_ERROR_COUNT: "TestErrorCount",
    regmap.TEST_PACKET_COUNT_TX: "TestPacketCountTx",
    regmap.TEST_PACKET_COUNT_TX + 4: "TestPacketCountTx",
    regmap.TEST_PACKET_COUNT_RX: "TestPacketCountRx",
    regmap.TEST_PACKET_COUNT_RX + 4: "TestPacketCountRx",
}


class RegScoreboard(CxpScoreboard):
    def build_phase(self):
        super().build_phase()
        self.reg_fifo = uvm_tlm_analysis_fifo("reg_fifo", self)
        self.reg_xp = self.reg_fifo.analysis_export
        # Kept for connect(): the sideband snapshot is read from the monitor.
        self.sideband_fifo = uvm_tlm_analysis_fifo("sideband_fifo", self)
        self.sideband_xp = self.sideband_fifo.analysis_export
        self.sb_mon = None            # set by the env
        self.ref = RegRef()
        blob = load_xml_blob()
        if blob is not None:
            self.ref.set_xml(blob)
        self.checked_reads = 0
        self.checked_writes = 0
        self.addrs_read: set = set()

    def reset_model(self) -> None:
        self.ref.reset()

    async def run_phase(self):
        import cocotb
        cocotb.start_soon(self._drain_sideband())
        while True:
            t = await self.reg_fifo.get()
            if t.write:
                self._write(t)
            else:
                self._read(t)

    async def _drain_sideband(self):
        while True:
            await self.sideband_fifo.get()

    def _live(self) -> None:
        last = self.sb_mon.last if self.sb_mon is not None else None
        if last is None:
            return
        self.ref.set_counter("TestErrorCount", last.sb_lt_err_count)
        self.ref.set_counter("TestPacketCountTx", last.sb_lt_pkt_count_tx)
        self.ref.set_counter("TestPacketCountRx", last.sb_lt_pkt_count_rx)

    def _read(self, t) -> None:
        self.checked_reads += 1
        self.addrs_read.add(t.addr)
        self._live()
        code, words = self.ref.read(t.addr, 4)
        exp_err = 0 if code == gp.ACK_OK_DATA else code
        if t.err != exp_err:
            self.err("read_code",
                     f"read 0x{t.addr:08x}: code 0x{t.err:02x}, the map says "
                     f"0x{exp_err:02x}")
            return
        if code != gp.ACK_OK_DATA:
            return
        want = words[0]
        if t.addr == regmap.CONNECTION_RESET:
            active = self.sb_mon.last.sb_link_reset_active if self.sb_mon else 0
            if t.rdata in (0, active):
                return
        if t.addr in _CNT:
            # The counter may move in the cycle it is read: one either side.
            if abs(t.rdata - want) <= 1:
                return
        if t.rdata != want:
            self.err("read_value",
                     f"read 0x{t.addr:08x}: 0x{t.rdata:08x}, the map says "
                     f"0x{want:08x}")

    def _write(self, t) -> None:
        self.checked_writes += 1
        # The byte enables: bytes not enabled keep the register's value.
        v = t.wdata
        if t.wstrb != 0xF:
            try:
                old = self.ref.word(t.addr)
            except (Unknown, KeyError, PermissionError):
                old = 0
            m = sum(0xFF << (8 * i) for i in range(4) if (t.wstrb >> i) & 1)
            v = (old & ~m) | (v & m)
        code = self.ref.write(t.addr, [v])
        exp_err = 0 if code == gp.ACK_OK_WRITE else code
        if t.err != exp_err:
            self.err("write_code",
                     f"write 0x{v:08x} to 0x{t.addr:08x}: code 0x{t.err:02x}, "
                     f"the map says 0x{exp_err:02x}")

    def report_phase(self):
        self.logger.info(
            f"reg_scoreboard: reads={self.checked_reads} "
            f"writes={self.checked_writes} addresses read={len(self.addrs_read)} "
            f"{self.err_summary()}"
        )
