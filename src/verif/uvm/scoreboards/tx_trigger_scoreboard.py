"""tx_trigger_scoreboard — §8.3.2 / §8.3.3 device->host trigger packets.

cxp_tx_trigger_hs sends the level of the device's trigger_in_app to the
host: 4xK28.4 + Delay when it is asserted, 4xK28.2 + Delay when it is
de-asserted (Table 16), Delay always 0 (§8.3.2 "when not used set to 0").
§8.3.3 lets one packet be outstanding until the host acknowledges it (or
a timeout passes), so edges that come faster than the acknowledgments
merge: the host sees the levels the pin settles at, not every edge.

Consumes:
* IoEvent from cxp_io_agent — the trigger edges the env drove.
* ShortPacket(TRIG_RISE/TRIG_FALL) from cxp_tx_wire_agent — the packets
  the device produced.

Checks: every packet changes the level the host holds (from de-asserted:
rise, fall, rise, ...); never more packets of a kind than edges of that
kind; the host's level ends equal to the pin's (the test is held open
while it does not); Delay = 0; and §8.3.3 — no trigger packet before the
host has acknowledged the previous one (its I/O acknowledgment has left
the host, from the uplink monitor) or the device's timeout
(p_TRIG_ACK_TIMEOUT tx cycles) has passed.
"""

from __future__ import annotations

from pyuvm import uvm_tlm_analysis_fifo

from uvm.scoreboards.sb_base import CxpScoreboard

from uvm.common.clocks import tx_period_ns
from uvm.common.cxp_pkg import UplinkKind, WireBeatKind

# cxp_pkg::TRIG_ACK_TIMEOUT, the device's wait for the host's I/O ack.
TRIG_ACK_TIMEOUT_TX = 4096


class TxTriggerScoreboard(CxpScoreboard):
    def build_phase(self):
        super().build_phase()
        self.io_fifo    = uvm_tlm_analysis_fifo("io_fifo",    self)
        self.short_fifo = uvm_tlm_analysis_fifo("short_fifo", self)
        self.uplink_fifo = uvm_tlm_analysis_fifo("uplink_fifo", self)
        self.io_xp      = self.io_fifo.analysis_export
        self.short_xp   = self.short_fifo.analysis_export
        self.uplink_xp  = self.uplink_fifo.analysis_export
        self._ioacks: list = []         # host I/O-ack transactions
        self.sideband_fifo = uvm_tlm_analysis_fifo("sideband_fifo", self)
        self.sideband_xp = self.sideband_fifo.analysis_export
        self._last_pkt_ns = None
        self.gated = 0
        self.resets = 0
        self.packets: list = []         # (level, t_ns) of every trigger packet
        self._excused: list = []

        self.exp_rise = self.exp_fall = 0
        self.obs_rise = self.obs_fall = 0
        self.bad_delay = 0
        self.pin_level = 0          # from the driven edges
        self.host_level = 0         # from the packets: de-asserted at start

    async def run_phase(self):
        import cocotb
        cocotb.start_soon(self._drain_io())
        cocotb.start_soon(self._drain_uplink())
        cocotb.start_soon(self._drain_sideband())
        await self._drain_short()

    async def _drain_sideband(self):
        """§8.3.2 / §10.3.28: a ConnectionReset sets the device trigger to
        de-asserted.  The device sends one K28.2 if it had left the host
        asserted, and follows the pin again only once it has seen it
        de-asserted (a pin held asserted across the reset is not a new
        edge): for this scoreboard the pin reads de-asserted from the
        reset on."""
        prev = 0
        while True:
            evt = await self.sideband_fifo.get()
            a = evt.sb_link_reset_active
            if a and not prev:
                self.pin_level = 0
                self._last_pkt_ns = None
                self.resets += 1
            prev = a

    async def _drain_uplink(self):
        while True:
            x = await self.uplink_fifo.get()
            if x.kind == UplinkKind.IOACK:
                self._ioacks.append(x)

    def _acked_since(self, t0: float, t1: float) -> bool:
        """A host I/O acknowledgment reached the device between its packet
        at t0 and t1.  An acknowledgment carries no identifier (Table 17):
        one the host sent for an earlier packet — late, after the device's
        timeout — that arrives once the next packet is out acknowledges
        that one.  Arrival is up to one uplink word after the last
        character left the host (decoded at the word seam)."""
        from uvm.agents.host_uplink_agent import bit_period_ps
        word_ns = 40 * bit_period_ps() / 1000.0
        return any(0 <= x.t_done_ns and t0 - word_ns <= x.t_done_ns <= t1
                   for x in self._ioacks)

    def _check_gate(self, t: float) -> None:
        prev = self._last_pkt_ns
        self._last_pkt_ns = t
        if prev is None:
            return
        timeout = TRIG_ACK_TIMEOUT_TX * tx_period_ns()
        if t - prev < timeout and not self._acked_since(prev, t):
            self.err("before_ack",
                     f"trigger packet at {t:.0f} ns, {t - prev:.0f} ns after "
                     "the previous one, with no I/O acknowledgment in between "
                     "and the timeout not passed (§8.3.3)")
        elif t - prev >= timeout and not self._acked_since(prev, t):
            self.gated += 1

    async def _drain_io(self):
        while True:
            evt = await self.io_fifo.get()
            if evt.kind == "trig_rise":
                self.exp_rise += 1
                self.pin_level = 1
            elif evt.kind == "trig_fall":
                self.exp_fall += 1
                self.pin_level = 0

    async def _drain_short(self):
        while True:
            pkt = await self.short_fifo.get()
            if pkt.kind not in (WireBeatKind.TRIG_RISE, WireBeatKind.TRIG_FALL):
                continue
            level = int(pkt.kind == WireBeatKind.TRIG_RISE)
            if any(a <= pkt.t_ns <= b for a, b in self._excused):
                self.host_level = level
                self.packets.append((level, pkt.t_ns))
                continue
            if level:
                self.obs_rise += 1
            else:
                self.obs_fall += 1
            self._check_gate(pkt.t_ns)
            self.packets.append((level, pkt.t_ns))
            if level == self.host_level:
                self.err(
                    "sequence",
                    f"trigger packet {'K28.4' if level else 'K28.2'} does "
                    f"not change the host's level ({self.host_level})"
                )
            self.host_level = level
            # §8.3.2: Delay word is 0 in this build (no sub-word phase).
            if pkt.payload != 0:
                self.bad_delay += 1
                self.err(
                    "delay",
                    f"trigger Delay word non-zero: 0x{pkt.payload:08x}"
                )

    def excuse(self, t0: float, t1: float = float("inf")) -> None:
        """Packets inside [t0, t1] (a pin sense change) move the host's
        level without counting against the edges; `close_excuse` ends an
        open window."""
        self._excused.append([t0, t1])

    def close_excuse(self) -> None:
        from cocotb.utils import get_sim_time
        for w in self._excused:
            if w[1] == float("inf"):
                w[1] = get_sim_time("ns")

    def pending_count(self) -> int:
        # The device still owes the host the pin's level.
        return int(self.host_level != self.pin_level)

    def _final_check(self):
        if self.obs_rise > self.exp_rise:
            self.err(
                "rise_count",
                f"more rising-trigger packets than edges: "
                f"edges={self.exp_rise} packets={self.obs_rise}"
            )
        if self.obs_fall > self.exp_fall:
            self.err(
                "fall_count",
                f"more falling-trigger packets than edges: "
                f"edges={self.exp_fall} packets={self.obs_fall}"
            )
        if self.host_level != self.pin_level:
            self.err(
                "final_level",
                f"the host was left at level {self.host_level}, the pin is "
                f"at {self.pin_level}"
            )

    def report_phase(self):
        self.logger.info(
            f"tx_trigger_scoreboard: rise edges/packets={self.exp_rise}/{self.obs_rise} "
            f"fall edges/packets={self.exp_fall}/{self.obs_fall} "
            f"bad_delay={self.bad_delay} {self.err_summary()}"
        )

