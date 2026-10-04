"""link_protocol_scoreboard — downlink framing and lexicon (§8.2, §8.4).

Every non-IDLE downlink beat (with the IDLE run before it, from the wire
monitor) goes through the golden deframer
(`cxp_protocol.packets.Deframer`):

* framing — a SOP inside a packet, an EOP outside one, a stray word
  between packets: each deframer error is an error here;
* packet types — a long packet's type is 0x01, 0x03 or 0x04 on the
  downlink (Table 18; 0x02 is the host's);
* short packets — a trigger or I/O acknowledgment is never split by an
  IDLE (the inserter's rule, §8.2.4 inserts a whole packet);
* K-code lexicon — only K27.7, K29.7, K28.3, K28.5 / K28.1 (IDLE),
  K28.4 / K28.2 (trigger) and K28.6 (I/O acknowledgment) appear, and
  K28.3 only as a full marker word; K28.0 was deleted in v1.1.

The IDLE cadence (§8.2.5.1, at most 99 words between IDLEs) is an SVA
on the RTL; the longest run is measured here and reported.
"""

from __future__ import annotations

from pyuvm import uvm_tlm_analysis_fifo

from cxp_protocol import packets as gp
from cxp_protocol.kcodes import IDLE_KMASK, IDLE_WORD

from uvm.scoreboards.sb_base import CxpScoreboard

from uvm.common.cxp_pkg import (
    WireBeatKind, K27_7, K28_0, K28_1, K28_2, K28_3, K28_4, K28_5, K28_6,
    K29_7,
)

DOWNLINK_LEGAL_KCODES = {K27_7, K29_7, K28_3, K28_5, K28_1,
                         K28_4, K28_2, K28_6}
DOWNLINK_TYPES = {gp.TYPE_STREAM, gp.TYPE_CTRL_ACK, gp.TYPE_LINKTEST}

_SHORT = (WireBeatKind.TRIG_RISE, WireBeatKind.TRIG_FALL, WireBeatKind.IOACK)


class LinkProtocolScoreboard(CxpScoreboard):
    def build_phase(self):
        super().build_phase()
        self.beat_fifo = uvm_tlm_analysis_fifo("beat_fifo", self)
        self.beat_xp   = self.beat_fifo.analysis_export
        self.deframer = gp.Deframer()
        self.frames = 0
        self.kcode_violations = 0
        self.max_run = 0
        self.run_hist: dict = {}

    async def run_phase(self):
        prev_kind = None
        while True:
            ev = await self.beat_fifo.get()
            if ev.idle_before:
                # The run the IDLE ended (coverage: cg_idle_stretch).
                r = self.deframer.run
                if r:
                    self.run_hist[r] = self.run_hist.get(r, 0) + 1
                if prev_kind in _SHORT:
                    self.err("short_split",
                             f"{ev.idle_before} IDLE word(s) inside a "
                             f"{prev_kind.name} packet at tx cycle {ev.tx_cycle}")
                self.deframer.push(IDLE_WORD, IDLE_KMASK)
            n_err = len(self.deframer.errors)
            for f in self.deframer.push(ev.data, ev.kmask):
                self.frames += 1
                if f.kind == "long" and f.type not in DOWNLINK_TYPES:
                    self.err("packet_type",
                             f"long packet of type {f.type!r} on the downlink "
                             f"(Table 18) at tx cycle {ev.tx_cycle}")
            for e in self.deframer.errors[n_err:]:
                self.err("framing", f"{e} (tx cycle {ev.tx_cycle})")
            self.max_run = max(self.max_run, self.deframer.max_run)
            # A short packet's first word; the next beat is its second.
            prev_kind = ev.kind if prev_kind not in _SHORT else None
            self._lexicon(ev)

    def _lexicon(self, ev) -> None:
        if not ev.kmask:
            return
        lanes = [(ev.data >> (8 * i)) & 0xFF for i in range(4)]
        for i, b in enumerate(lanes):
            if (ev.kmask >> i) & 1 and b not in DOWNLINK_LEGAL_KCODES:
                self.kcode_violations += 1
                self.err("kcode",
                         f"K-character 0x{b:02x} in lane {i} is not legal on "
                         "the v1.1.1 downlink"
                         + (" (K28.0 was deleted in v1.1)" if b == K28_0 else ""))
        if any(b == K28_3 and (ev.kmask >> i) & 1 for i, b in enumerate(lanes)) \
                and not (ev.kmask == 0xF and all(b == K28_3 for b in lanes)):
            self.err("kcode", f"K28.3 not a whole marker word: 0x{ev.data:08x} "
                              f"kmask {ev.kmask:x}")

    def _final_check(self):
        if self.deframer._long is not None:
            self.err("framing", "the test ended inside a long packet")

    def report_phase(self):
        self.logger.info(
            f"link_protocol: {self.frames} packets, longest non-IDLE run "
            f"{self.max_run}, kcode_violations={self.kcode_violations} "
            f"{self.err_summary()}"
        )
