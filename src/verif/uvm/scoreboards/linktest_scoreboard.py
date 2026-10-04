"""linktest_scoreboard — the connection test, both directions (§8.7, §10.3.35-39).

RX (host -> device, §8.7.3, §10.3.37 / §10.3.39):
  every LINKTEST packet the host sends owes the device's error counter
  one count per differing word plus one per missing or surplus word of
  the 1024-word Table 23 payload (§8.7.1), and its packet counter one.
  Both are compared with the device's counters (`sb_lt_err_count`,
  `sb_lt_pkt_count_rx`) at the end, and with every register read of
  TestErrorCount / TestPacketCountRx on the register bus.

TX (device -> host, §8.7.4, §10.3.35 / §10.3.38):
  every type-0x04 packet on the downlink carries the Table 23 payload,
  1024 words, bit for bit (`cxp_protocol.packets.linktest_errors`); two
  test packets are at least 16 words apart; none starts once TestMode
  has been 0 for a margin, and the one in flight when it fell completes
  (a torn one is a framing error in sb_linkpro); TestPacketCountTx
  equals the packets on the wire, by sideband and by register read.

Clears: a write of 0 to a counter (a register-bus write) restarts its
expectation; a ConnectionReset clears them all (§10.3.28).
"""

from __future__ import annotations

from cocotb.utils import get_sim_time
from pyuvm import uvm_tlm_analysis_fifo

from cxp_protocol import packets as gp
from cxp_protocol import regmap

from uvm.scoreboards.sb_base import CxpScoreboard
from uvm.common.cxp_pkg import UplinkKind

_PKT_TYPE_LINKTEST = 0x04
LT_GAP_WORDS = 16                 # §8.7.4
_TESTMODE_MARGIN_NS = 400.0


class LinktestScoreboard(CxpScoreboard):
    def build_phase(self):
        super().build_phase()
        self.uplink_fifo   = uvm_tlm_analysis_fifo("uplink_fifo",   self)
        self.sideband_fifo = uvm_tlm_analysis_fifo("sideband_fifo", self)
        self.pkt_fifo      = uvm_tlm_analysis_fifo("pkt_fifo",      self)
        self.reg_fifo      = uvm_tlm_analysis_fifo("reg_fifo",      self)
        self.uplink_xp     = self.uplink_fifo.analysis_export
        self.sideband_xp   = self.sideband_fifo.analysis_export
        self.pkt_xp        = self.pkt_fifo.analysis_export
        self.reg_xp        = self.reg_fifo.analysis_export

        # RX direction (since the last clear).
        self.expected       = 0
        self.expected_pkts  = 0
        self.last_count     = 0
        self.last_pkt_count_rx = 0
        self.packets_seen   = 0
        self.sb_events_seen = 0
        # TX direction.
        self.expect_tx_linktest = False
        self.tx_lt_packets      = 0       # since the last clear
        self.tx_total           = 0
        self.last_pkt_count_tx  = 0
        self._last_eop_cycle    = None
        self._tmode: list = [(0.0, 0)]
        self._prev_crst = 0
        self.reg_reads = 0
        self._last_err = 0

    async def run_phase(self):
        import cocotb
        cocotb.start_soon(self._drain_uplink())
        cocotb.start_soon(self._drain_pkt())
        cocotb.start_soon(self._drain_reg())
        cocotb.start_soon(self._watch_testmode())
        await self._drain_sideband()

    async def _watch_testmode(self):
        from cocotb.triggers import Edge
        from uvm.common.handles import get_dut
        dut = get_dut()
        while True:
            await Edge(dut.bs_test_mode)
            self._tmode.append((get_sim_time("ns"), int(dut.bs_test_mode.value)))

    def _tmode_at(self, t: float) -> int:
        v = 0
        for ht, hv in self._tmode:
            if ht > t:
                break
            v = hv
        return v

    async def _drain_uplink(self):
        while True:
            cmd = await self.uplink_fifo.get()
            if cmd.kind != UplinkKind.LINKTEST:
                continue
            self.packets_seen += 1
            self.expected_pkts += 1
            # §8.7.1: one count per differing word — flipped words, plus
            # every Table 23 word a short packet leaves out or adds.
            self._last_err = len(cmd.lt_error_indices) + abs(gp.LT_DATA_WORDS - cmd.lt_n_data)
            self.expected += self._last_err

    async def _drain_pkt(self):
        while True:
            pkt = await self.pkt_fifo.get()
            if pkt.type_byte != _PKT_TYPE_LINKTEST:
                continue
            self.tx_lt_packets += 1
            self.tx_total += 1
            payload = [w for w, k in pkt.words[2:-1]]
            n_err = gp.linktest_errors(payload)
            if n_err or len(payload) != gp.LT_DATA_WORDS:
                self.err("tx_payload",
                         f"test packet #{self.tx_total}: {len(payload)} words, "
                         f"{n_err} differ from Table 23")
            if self._last_eop_cycle is not None:
                gap = pkt.sop_cycle - self._last_eop_cycle - 1
                if gap < LT_GAP_WORDS:
                    self.err("tx_gap",
                             f"test packet #{self.tx_total} starts {gap} words "
                             f"after the previous one (§8.7.4: at least "
                             f"{LT_GAP_WORDS})")
            self._last_eop_cycle = pkt.eop_cycle
            t0 = pkt.sop_ns - _TESTMODE_MARGIN_NS
            if not self._tmode_at(t0) and not self._tmode_at(pkt.sop_ns):
                self.err("tx_after_testmode",
                         f"test packet #{self.tx_total} started at "
                         f"{pkt.sop_ns:.0f} ns with TestMode 0 (§10.3.35)")

    async def _drain_reg(self):
        """Register-bus traffic: counter clears and counter reads."""
        while True:
            t = await self.reg_fifo.get()
            if t.err:
                continue
            a = t.addr
            if t.write:
                if t.wdata == 0 and a == regmap.TEST_ERROR_COUNT:
                    self.expected = 0
                elif t.wdata == 0 and a == regmap.TEST_PACKET_COUNT_RX:
                    self.expected_pkts = 0
                elif t.wdata == 0 and a == regmap.TEST_PACKET_COUNT_TX:
                    self.tx_lt_packets = 0
                continue
            self.reg_reads += 1
            # A read races the packet being counted: one either side.
            if a == regmap.TEST_ERROR_COUNT:
                w = min(self.expected, 0xFFFF_FFFF)
                self._cmp("reg_err_count", t.rdata, w - self._last_err, w)
            elif a == regmap.TEST_PACKET_COUNT_RX + 4:
                self._cmp("reg_pkt_count_rx", t.rdata, self.expected_pkts - 1,
                          self.expected_pkts)
            elif a == regmap.TEST_PACKET_COUNT_TX + 4:
                self._cmp("reg_pkt_count_tx", t.rdata, self.tx_lt_packets,
                          self.tx_lt_packets + 1)

    def _cmp(self, kind: str, got: int, lo: int, hi: int) -> None:
        """A read races the packet being counted: the count before it or
        after it."""
        if not max(0, lo) <= got <= hi:
            self.err(kind, f"register read {got}, expected {max(0, lo)}..{hi}")

    async def _drain_sideband(self):
        while True:
            evt = await self.sideband_fifo.get()
            self.sb_events_seen += 1
            self.last_count = evt.sb_lt_err_count
            self.last_pkt_count_tx = evt.sb_lt_pkt_count_tx
            self.last_pkt_count_rx = evt.sb_lt_pkt_count_rx
            crst = evt.sb_link_reset_active
            if crst and not self._prev_crst:
                # §10.3.28: the counters are cleared.
                self.expected = self.expected_pkts = 0
                self.tx_lt_packets = 0
            self._prev_crst = crst

    def pending_count(self) -> int:
        """1 while the device has counted fewer errors or packets than the
        host's packets owe: it counts a packet at its end."""
        return int(self.last_count < min(self.expected, 0xFFFF_FFFF)
                   or self.last_pkt_count_rx < self.expected_pkts)

    def _final_check(self):
        if self.packets_seen > 0 and self.sb_events_seen == 0:
            self.err("sideband_dead",
                     "linktest sent packets but no sideband events observed")
        clamped = min(self.expected, 0xFFFF_FFFF)
        if self.last_count != clamped:
            self.err("err_count",
                     f"TestErrorCount {self.last_count}, expected {clamped} "
                     f"({self.packets_seen} host test packets)")
        if self.last_pkt_count_rx != self.expected_pkts:
            self.err("pkt_count_rx",
                     f"TestPacketCountRx {self.last_pkt_count_rx}, expected "
                     f"{self.expected_pkts}")
        if self.expect_tx_linktest and self.tx_total < 1:
            self.err("tx_no_packet",
                     "TestMode was enabled but no test packet was sent")
        if self.tx_lt_packets != self.last_pkt_count_tx:
            self.err("tx_count",
                     f"TestPacketCountTx {self.last_pkt_count_tx}, test "
                     f"packets on the wire {self.tx_lt_packets}")

    def report_phase(self):
        self.logger.info(
            f"linktest_scoreboard: rx_packets={self.packets_seen} "
            f"rx_expected={self.expected} rx_observed={self.last_count} "
            f"rx_pkt_count={self.last_pkt_count_rx}/{self.expected_pkts} "
            f"tx_packets={self.tx_total} tx_pkt_count_reg={self.last_pkt_count_tx} "
            f"register_reads={self.reg_reads} {self.err_summary()}"
        )
