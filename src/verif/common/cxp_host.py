"""A CoaXPress host for device-level testbenches.

Drives the uplink (`common/cxp_uplink.py`) and watches the downlink word
bus, splitting it into packets with the golden `cxp_protocol` deframer:

    host = Host(dut, q=cxp_protocol.DEVICE)
    host.start()
    await host.link_up()
    code, data = await host.read(0x0000)          # Table 22 code, words
    code = await host.write(0x4010, 1056)
    host.images                                    # reassembled images

Every acknowledgment is parsed with the golden codec and its CRC checked;
stream packets go to the golden reassembler (tag / CRC / DsizeP checks).
`host.max_run` is the longest run of non-IDLE words seen (§8.2.5.1: at
most 99 on a high-speed connection).

Device triggers (§8.3.3): with `trig_ack = "ack"` (the default) the host
answers every trigger packet it receives with a Table 17 I/O
acknowledgment, inserted on the uplink at the next word boundary (§8.2.4)
after `trig_ack_delay` rx_clk cycles; `"drop"` never answers.
`trig_times` holds the downlink word count at every trigger packet,
`trig_ack_words` the count when each acknowledgment has left the uplink
pin.
"""

from __future__ import annotations

from collections import deque
from typing import List, Optional, Sequence, Tuple

import cocotb
from cocotb.triggers import Event, NextTimeStep, ReadOnly, RisingEdge

from cxp_protocol import packets as gp, stream as gs
from cxp_protocol.kcodes import vote
from cxp_protocol.quirks import SPEC, Quirks
from cxp_uplink import Uplink


class Host:
    def __init__(self, dut, q: Quirks = SPEC, os_ratio: int = 4,
                 rx_clk=None, tx_clk=None, rx_pin=None, data=None, kmask=None,
                 link=None):
        self.dut, self.q = dut, q
        self.link = link
        pick = lambda h, name: h if h is not None else getattr(dut, name)
        self.rx_clk = pick(rx_clk, "rx_clk")
        self.tx_clk = pick(tx_clk, "tx_clk")
        self.up = Uplink(dut, self.rx_clk, pick(rx_pin, "rx_serial"), os_ratio)
        self.data = pick(data, "cxp_if_data")
        self.kmask = pick(kmask, "cxp_if_kmask")
        self.deframer = gp.Deframer()
        self.reasm = gs.StreamReassembler(q)
        self.acks: deque = deque()
        self._ack_evt = Event()
        self.triggers: List[Tuple[bool, int]] = []
        self.trig_times: List[int] = []
        self.trig_ack: Optional[str] = "ack"
        self.trig_ack_delay = 0
        self.trig_acks_sent = 0
        self.trig_ack_words: List[int] = []
        self.ioacks: List[int] = []
        self.linktests: List[List[int]] = []
        self.stream_packets = 0
        self.stream_tags: List[int] = []
        self.stream_lens: List[int] = []     # words per stream packet, SOP..EOP
        self.other: List[gp.Frame] = []
        self.words = 0
        self.raw: Optional[List[Tuple[int, int]]] = None   # set to [] to record
        self._mon = None

    # -- lifecycle ------------------------------------------------------
    def start(self):
        self.up.start()
        self._mon = cocotb.start_soon(self._monitor())
        return self

    def stop(self):
        self.up.stop()
        if self._mon is not None:
            self._mon.cancel()

    async def link_up(self, timeout: int = 20000):
        """Wait until the device has word alignment on the uplink."""
        for _ in range(timeout):
            await RisingEdge(self.rx_clk)
            link = self.link if self.link is not None else self.dut.sb_link_detected
            if int(link.value):
                return
        raise AssertionError("uplink did not come up")

    @property
    def max_run(self) -> int:
        return self.deframer.max_run

    @property
    def images(self):
        return self.reasm.images

    # -- downlink ---------------------------------------------------------
    async def _monitor(self):
        while True:
            await RisingEdge(self.tx_clk)
            await ReadOnly()
            w, k = int(self.data.value), int(self.kmask.value)
            self.words += 1
            if self.raw is not None:
                self.raw.append((w, k))
            for f in self.deframer.push(w, k):
                self._dispatch(f)

    def _dispatch(self, f: gp.Frame):
        if f.kind == "trigger":
            rising = (f.beats[0][0] & 0xFF) == 0x9C
            self.triggers.append((rising, vote(f.beats[1][0])[0]))
            self.trig_times.append(self.words)
            if self.trig_ack == "ack":
                cocotb.start_soon(self._answer_trigger(self.trig_ack_delay))
        elif f.kind == "ioack":
            self.ioacks.append(vote(f.beats[1][0])[0])
        elif f.type == gp.TYPE_CTRL_ACK:
            self.acks.append(gp.parse_ctrl_ack([w for w, _ in f.beats[1:-1]], self.q))
            self._ack_evt.set()
        elif f.type == gp.TYPE_STREAM:
            self.stream_packets += 1
            self.stream_lens.append(len(f.beats))
            self.stream_tags.append(self.reasm.push_packet(f.beats[1:-1]).tag)
        elif f.type == gp.TYPE_LINKTEST:
            self.linktests.append([w for w, _ in f.beats[2:-1]])
        else:
            self.other.append(f)

    async def _answer_trigger(self, delay: int):
        for _ in range(delay):
            await RisingEdge(self.rx_clk)
        self.trig_acks_sent += 1
        await self.up.insert(gp.io_ack(), word=True).wait()
        self.trig_ack_words.append(self.words)

    @property
    def trig_level(self) -> int:
        """The device trigger level the host holds: the last packet's edge."""
        return int(self.triggers[-1][0]) if self.triggers else 0

    # -- control ----------------------------------------------------------
    async def send(self, beats_or_chars):
        await self.up.send(beats_or_chars)

    async def wait_ack(self, timeout_words: int = 20000) -> gp.CtrlAck:
        start = self.words
        while not self.acks:
            self._ack_evt.clear()
            await RisingEdge(self.tx_clk)
            if self.words - start > timeout_words:
                raise AssertionError("no acknowledgment")
        await NextTimeStep()
        return self.acks.popleft()

    async def command(self, beats) -> gp.CtrlAck:
        await self.send(beats)
        return await self.wait_ack()

    async def read(self, addr: int, n: int = 1) -> Tuple[int, List[int]]:
        ack = await self.command(gp.ctrl_cmd(gp.OP_READ, addr, 4 * n, q=self.q))
        if ack.long_form:
            assert ack.crc_ok, f"read 0x{addr:x}: ack CRC error"
        return ack.code, ack.data

    async def read1(self, addr: int) -> int:
        code, data = await self.read(addr)
        assert code == gp.ACK_OK_DATA, f"read 0x{addr:x}: ack 0x{code:02x}"
        return data[0]

    async def write(self, addr: int, *values: int) -> int:
        ack = await self.command(gp.ctrl_cmd(gp.OP_WRITE, addr, data=list(values), q=self.q))
        return ack.code

    async def write_ok(self, addr: int, *values: int):
        code = await self.write(addr, *values)
        assert code == gp.ACK_OK_WRITE, f"write 0x{addr:x}: ack 0x{code:02x}"

    async def reset_channel(self) -> int:
        return (await self.command(gp.ctrl_cmd(gp.OP_RESET, q=self.q))).code

    async def trigger(self, rising: bool, delay: int = 0):
        await self.send(gp.trigger_uplink(rising, delay, q=self.q))
