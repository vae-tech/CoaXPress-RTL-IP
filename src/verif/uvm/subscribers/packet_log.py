"""packet_log — every downlink packet with where it sat on the wire.

The concurrency tests have to prove that the things they overlap did
overlap: that an I/O acknowledgment landed inside a stream packet, that a
trigger was inserted on an IDLE-due word, that an acknowledgment went out
right behind a stream EOP.  This keeps every long packet (type, first and
last tx cycle, the cycle of each word) and every short packet (kind,
cycle), and answers where a short packet sat.
"""

from __future__ import annotations

from pyuvm import uvm_component, uvm_tlm_analysis_fifo


class PacketLog(uvm_component):
    def build_phase(self):
        self.pkt_fifo = uvm_tlm_analysis_fifo("pkt_fifo", self)
        self.short_fifo = uvm_tlm_analysis_fifo("short_fifo", self)
        self.pkt_xp = self.pkt_fifo.analysis_export
        self.short_xp = self.short_fifo.analysis_export
        self.long: list = []            # WirePacket
        self.short: list = []           # ShortPacket

    async def run_phase(self):
        import cocotb
        cocotb.start_soon(self._shorts())
        while True:
            self.long.append(await self.pkt_fifo.get())

    async def _shorts(self):
        while True:
            self.short.append(await self.short_fifo.get())

    def enclosing(self, cycle: int):
        """The long packet a word at `cycle` sat inside, or None."""
        for p in reversed(self.long):
            if p.sop_cycle < cycle < p.eop_cycle:
                return p
            if p.eop_cycle < cycle:
                break
        return None

    def where(self, cycle: int) -> str:
        """Where a short packet's word sat: 'idle', or '<type>:<part>' with
        part header (SOP to the last header word), payload, tail (CRC to
        EOP).  Type 0x01 has 6 header words, 0x03 3, 0x04 2."""
        p = self.enclosing(cycle)
        if p is None:
            return "idle"
        t = p.type_byte
        i = sum(1 for c in p.cycles if c < cycle)
        n = len(p.cycles)
        hdr = {0x01: 6, 0x03: 3, 0x04: 2}.get(t, 2)
        tail = 1 if t == 0x04 else 2
        part = "header" if i <= hdr else ("tail" if i >= n - tail else "payload")
        return f"0x{t:02x}:{part}"

    def gap_after_eop(self, cycle: int) -> int:
        """Words between the last long packet's EOP before `cycle` and it."""
        for p in reversed(self.long):
            if p.eop_cycle < cycle:
                return cycle - p.eop_cycle - 1
        return 1 << 30
