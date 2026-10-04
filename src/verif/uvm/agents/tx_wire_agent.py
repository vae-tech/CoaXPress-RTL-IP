"""cxp_tx_wire_agent — passive monitor of cxp_if_data_o / cxp_if_kmask_o.

Implements proposal §6.4.  It samples every non-IDLE 32-bit beat and
classifies it (SOP / EOP / KMARK / PAYLOAD / TRIG_* / IOACK / UNKNOWN);
IDLE runs are slept through (it wakes on the shell's `wire_busy`) and
counted from the shell's `tx_words` edge counter.  It reassembles three
traffic classes:

* ``ap_pkt``   — long packets bracketed by a K27.7 SOP and a K29.7 EOP
                 (type-0x01 stream, type-0x03 ctrl-ack, type-0x04
                 link-test).
* ``ap_short`` — 2-word §8.3.2 trigger packets (K28.4/K28.2 header +
                 delay word) and §8.3.3 I/O-ack packets (K28.6 header +
                 code word).  These have NO K27.7/K29.7 framing.
* ``ap_beat``  — every non-IDLE beat, with the length of the IDLE run
                 before it, for the link-protocol scoreboard and
                 coverage.

With ``DUMP=1`` in the environment (set by the ``nightly`` / ``weekly``
tiers) every non-IDLE beat and the first word of every IDLE run is also
written, as ``(tx_cycle, word, kmask)``, to ``downlink.bin.gz`` in the
run directory, in the :mod:`cxp_protocol.dump` format with
``FLAG_IDLE_RUNS`` set; ``src/verif/Makefile`` archives it next to
results.xml.  ``DUMP_MAX_MB`` (default 16) caps the file on disk.

Short packets are stripped out of the long-packet reassembly even when
they preempt mid-stream (the arbiter inserts a trigger at any word
boundary, §8.3.2) so the long packet's word list — and therefore the
stream/ctrl CRC check — stays clean.

The agent is passive — there is no driver, the DUT drives the bus.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Tuple

from cocotb.triggers import Edge, ReadOnly, RisingEdge, NextTimeStep
from cocotb.utils import get_sim_time
from pyuvm import uvm_agent, uvm_analysis_port, uvm_monitor

from cxp_protocol.dump import FLAG_IDLE_RUNS, DumpWriter

from uvm.common.clocks import current_periods
from uvm.common.handles import get_dut

from uvm.common.cxp_pkg import (
    WireBeatKind, classify_wire_beat, IDLE_WORD,
)


@dataclass
class WirePacket:
    """One reassembled SOP..EOP downlink packet (stream / ctrl-ack / linktest)."""
    words: List[Tuple[int, int]]   # (data, kmask) pairs, SOP..EOP inclusive
    type_byte: int = 0             # majority-vote on word 1 byte 0
    has_kmark: bool = False
    sop_ns: float = 0.0            # sim time of the SOP word
    sop_cycle: int = 0             # tx_words of the SOP word
    eop_cycle: int = 0             # tx_words of the EOP word
    eop_ns: float = 0.0
    cycles: List[int] = None       # tx_words of every word in `words`


@dataclass
class ShortPacket:
    """One reassembled 2-word §8.3.2 trigger or §8.3.3 I/O-ack packet."""
    kind: WireBeatKind             # TRIG_RISE / TRIG_FALL / IOACK
    hdr: int                       # header word (4x K-char replication)
    payload: int                   # 2nd word: trigger Delay or I/O-ack Code
    code: int = 0                  # majority byte of the payload word
    tx_cycle: int = 0              # tx_words of the payload word
    idle_inside: int = 0           # IDLE words between the two words
    t_ns: float = 0.0              # sim time of the payload word


class WireBeat:
    """Single-beat event for the link-protocol scoreboard / coverage.

    IDLE words are not published one by one: `idle_before` on the next
    non-IDLE beat says how many IDLE words preceded it, and `tx_cycle` is
    the tx_clk edge the word was on."""
    def __init__(self, data: int, kmask: int, kind: WireBeatKind,
                 idle_before: int = 0, tx_cycle: int = 0):
        self.data, self.kmask, self.kind = data, kmask, kind
        self.idle_before = idle_before
        self.tx_cycle = tx_cycle


_SHORT_HDR_KINDS = (WireBeatKind.TRIG_RISE, WireBeatKind.TRIG_FALL,
                    WireBeatKind.IOACK)


class TxWireMonitor(uvm_monitor):
    def build_phase(self):
        self.ap_beat  = uvm_analysis_port("ap_beat",  self)
        self.ap_pkt   = uvm_analysis_port("ap_pkt",   self)
        self.ap_short = uvm_analysis_port("ap_short", self)
        self.beats = 0              # non-IDLE words seen
        self.last_busy = 0          # tx_words of the last non-IDLE word
        self.busy = False
        self.dump = None

    DUMP_PATH = "downlink.bin.gz"

    @property
    def idle_run(self) -> int:
        """Consecutive IDLE words on the downlink, right now.
        CxpEnv.quiesce waits on this: while it is short the device still
        has something to say."""
        if self.busy:
            return 0
        now = int(get_dut().tx_words.value)
        return max(0, now - self.last_busy - 1)

    def _open_dump(self):
        if os.environ.get("DUMP", "0") in ("", "0"):
            return None
        cap_mb = float(os.environ.get("DUMP_MAX_MB", "16"))
        return DumpWriter(self.DUMP_PATH,
                          tx_period_ps=round(current_periods()["tx"] * 1000),
                          max_bytes=int(cap_mb * 1024 * 1024),
                          flags=FLAG_IDLE_RUNS)

    async def run_phase(self):
        dut = get_dut()
        # The file header carries the period the test started the clock
        # with.
        self.dump = self._open_dump()
        in_pkt = False
        pkt_words: List[Tuple[int, int]] = []
        # Pending short packet: (kind, hdr_data) awaiting its 2nd word.
        short_pending = None
        pkt_cycles: List[int] = []
        sop_ns = 0.0
        self.last_busy = -1
        while True:
            # Sleep through an IDLE run: wake on the first non-IDLE word.
            if not int(dut.wire_busy.value):
                self.busy = False
                await RisingEdge(dut.wire_busy)
            else:
                await Edge(dut.tx_words)
            await ReadOnly()
            d  = int(dut.cxp_if_data_o.value)
            km = int(dut.cxp_if_kmask_o.value)
            cyc = int(dut.tx_words.value)
            busy = bool(int(dut.wire_busy.value))
            await NextTimeStep()
            if not busy:
                # First IDLE after a non-IDLE word: it opens the run.
                if self.dump is not None and self.busy:
                    self.dump.write(cyc, d, km)
                self.busy = False
                continue
            idle_before = max(0, cyc - self.last_busy - 1) if self.last_busy >= 0 else cyc
            self.busy = True
            self.last_busy = cyc
            kind = classify_wire_beat(d, km)
            if self.dump is not None:
                if idle_before and self.beats == 0:
                    self.dump.write(0, IDLE_WORD[0], IDLE_WORD[1])
                self.dump.write(cyc, d, km)
            self.beats += 1
            self.ap_beat.write(WireBeat(d, km, kind, idle_before, cyc))

            # ---- short-packet (trigger / I/O-ack) reassembly -------------
            if short_pending is not None:
                sk_kind, sk_hdr = short_pending
                code, _ = _majority(d)
                self.ap_short.write(ShortPacket(
                    kind=sk_kind, hdr=sk_hdr, payload=d, code=code,
                    tx_cycle=cyc, idle_inside=idle_before,
                    t_ns=get_sim_time("ns")))
                short_pending = None
                # The payload word is NOT appended to any long packet.
                continue
            if kind in _SHORT_HDR_KINDS:
                short_pending = (kind, d)
                continue

            # ---- long-packet (SOP..EOP) reassembly ----------------------
            if kind == WireBeatKind.SOP:
                in_pkt = True
                pkt_words = [(d, km)]
                pkt_cycles = [cyc]
                sop_ns = get_sim_time("ns")
                sop_cycle = cyc
            elif in_pkt:
                pkt_words.append((d, km))
                pkt_cycles.append(cyc)
                if kind == WireBeatKind.EOP:
                    pkt = WirePacket(words=list(pkt_words), sop_ns=sop_ns,
                                     sop_cycle=sop_cycle, eop_cycle=cyc,
                                     eop_ns=get_sim_time("ns"),
                                     cycles=list(pkt_cycles))
                    if len(pkt_words) >= 2:
                        pkt.type_byte = pkt_words[1][0] & 0xFF
                    pkt.has_kmark = any(
                        classify_wire_beat(w, k) == WireBeatKind.KMARK
                        for (w, k) in pkt_words
                    )
                    self.ap_pkt.write(pkt)
                    in_pkt = False

    def report_phase(self):
        # report_phase, not final_phase: the test raises its verdict in
        # final_phase, and the dump must be closed whatever it says.
        if self.dump is not None:
            self.dump.close()
            self.logger.info(
                f"downlink dump: {self.dump.records} words -> {self.DUMP_PATH}"
                + (" (TRUNCATED at DUMP_MAX_MB)" if self.dump.truncated else ""))


def _majority(word: int) -> Tuple[int, bool]:
    """3-of-4 replica majority vote on a 32-bit word."""
    lanes = [(word >> (8 * i)) & 0xFF for i in range(4)]
    best, best_count = 0, 0
    for b in set(lanes):
        c = lanes.count(b)
        if c > best_count:
            best, best_count = b, c
    return best, best_count >= 3


class TxWireAgent(uvm_agent):
    def build_phase(self):
        self.mon = TxWireMonitor("mon", self)
