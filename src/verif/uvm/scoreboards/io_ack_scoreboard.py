"""io_ack_scoreboard — §8.3.3 I/O acknowledgments of host triggers.

"Trigger packets shall be acknowledged" (§8.3.2): every Table 15 trigger
the device takes is answered with one Table 17 packet, K28.6 and code
0x01, on the downlink.  A trigger it cannot take (unrepairable Delay or
leader, §8.2.2) is not acknowledged — the acknowledgment says the
trigger was received.

Pairing is in order.  Each acknowledgment's latency, from the trigger's
last character to the acknowledgment's code word, is recorded and
reported (min / max / histogram in ns); decision D6 bounds it once
taken.  An acknowledgment nothing asked for is an error.
"""

from __future__ import annotations

from collections import Counter, deque

from cocotb.utils import get_sim_time
from pyuvm import uvm_tlm_analysis_fifo

from uvm.common.cxp_pkg import UplinkKind, WireBeatKind
from uvm.common.handles import get_dut
from uvm.common.decisions import D6_IOACK_LATENCY_CHARS
from uvm.scoreboards.sb_base import CxpScoreboard

_IOACK_CODE_OK = 0x01


class IoAckScoreboard(CxpScoreboard):
    def build_phase(self):
        super().build_phase()
        self.uplink_fifo = uvm_tlm_analysis_fifo("uplink_fifo", self)
        self.short_fifo  = uvm_tlm_analysis_fifo("short_fifo",  self)
        self.uplink_xp   = self.uplink_fifo.analysis_export
        self.short_xp    = self.short_fifo.analysis_export

        self.trig_expected = 0
        self.ioack_seen    = 0
        self.bad_code      = 0
        self._q: deque = deque()
        self.latency_ns: list = []
        self.char_ns = 0.0              # set by the env (host character time)

    async def run_phase(self):
        import cocotb
        cocotb.start_soon(self._drain_uplink())
        await self._drain_short()

    async def _drain_uplink(self):
        while True:
            x = await self.uplink_fifo.get()
            # §8.3: the I/O channel is the Master connection's; a trigger on
            # an extension connection is not acknowledged.
            if x.kind in (UplinkKind.TRIGGER_RISE, UplinkKind.TRIGGER_FALL) \
                    and x.trig_repairable \
                    and not int(get_dut().from_extension_link.value):
                self.trig_expected += 1
                self._q.append(x)

    async def _drain_short(self):
        while True:
            pkt = await self.short_fifo.get()
            if pkt.kind != WireBeatKind.IOACK:
                continue
            self.ioack_seen += 1
            if pkt.code != _IOACK_CODE_OK or len({(pkt.payload >> (8 * i)) & 0xFF
                                                  for i in range(4)}) != 1:
                self.bad_code += 1
                self.err("code",
                         f"I/O-ack code word 0x{pkt.payload:08x}, Table 17 "
                         f"wants 0x{_IOACK_CODE_OK:02x} four times")
            if not self._q:
                self.err("unexpected",
                         "I/O acknowledgment with no host trigger to answer")
                continue
            x = self._q.popleft()
            if x.t_done_ns >= 0:
                lat = pkt.t_ns - x.t_done_ns
                self.latency_ns.append(lat)
                if D6_IOACK_LATENCY_CHARS is not None and self.char_ns and \
                        lat > D6_IOACK_LATENCY_CHARS * self.char_ns:
                    self.err("latency",
                             f"I/O acknowledgment {lat:.0f} ns after the trigger, "
                             f"decision D6 allows {D6_IOACK_LATENCY_CHARS} "
                             "host characters")

    def pending_count(self) -> int:
        """Host triggers not acknowledged yet: the device receives a trigger
        only after its last character is on the wire."""
        return len(self._q)

    def _final_check(self):
        if self._q:
            self.err("count",
                     f"{len(self._q)} host trigger(s) never acknowledged "
                     f"({self.trig_expected} sent, {self.ioack_seen} acks)")

    def report_phase(self):
        v = self.latency_ns
        lat = (f" latency min {min(v):.0f} max {max(v):.0f} ns hist "
               f"{dict(sorted(Counter(round(n, -1) for n in v).items()))}" if v else "")
        self.logger.info(
            f"io_ack_scoreboard: host_triggers={self.trig_expected} "
            f"io_acks={self.ioack_seen} bad_code={self.bad_code}{lat} "
            f"{self.err_summary()}"
        )
