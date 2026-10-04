"""cxp_host_ctrl — the host's control channel, closed-loop (§8.6.1).

A host sends one control command and waits for its acknowledgment before
the next (§8.6.1.1).  `HostCtrl` does exactly that on top of the uplink
driver's packet lane and the downlink monitor:

    code, words = await env.host.read(0x0000)          # (0x00, [0xC0A79AE5])
    code        = await env.host.write(0x4010, [1056])  # 0x01
    code        = await env.host.reset()                # 0xFF -> 0x03

* Every acknowledgment is parsed with the golden codec
  (`cxp_protocol.packets.parse_ctrl_ack`); `read` returns the Table 22
  code and the register values of a 0x00 acknowledgment.
* A Wait acknowledgment (0x04) extends the wait by the time it announces;
  the final acknowledgment is the one returned (§8.6.1, Figure 24).
* With no acknowledgment 200 ms after the command left the host, the host
  sends it again (§8.6.1.1), at most `resends` times, then gives up and
  returns `None` for the code.  Times are the device's: one ms is
  `RX_CLK_KHZ` rx_clk periods of the build (`uvm.common.clocks.ms_ns`).
* Commands from concurrent callers are serialised by a lock, so any
  number of actors can share one host.

The open-loop sequences on the packet lane (`UplinkRegSeq`, ...) still
exist for traffic that must break the host rule (pipelined commands);
they must not run while `HostCtrl` has a command outstanding, because
acknowledgments carry no command identifier (Table 22) and are matched
by order.

`self.log` keeps (command, acknowledgments, final code, latency_ns) for
every command, for the tests' own checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import cocotb
from cocotb.queue import Queue, QueueEmpty
from cocotb.triggers import Lock, SimTimeoutError, Timer, with_timeout
from cocotb.utils import get_sim_time
from pyuvm import uvm_component, uvm_tlm_analysis_fifo

from cxp_protocol import packets as gp

from uvm.common.clocks import ms_ns
from uvm.common.cxp_pkg import PacketType, UplinkKind, UplinkTxn
from uvm.common.handles import get_dut


# §8.6.1.1: the host's acknowledgment timeout.
HOST_ACK_TIMEOUT_MS = 200


@dataclass
class CtrlExchange:
    """One command as the host saw it."""
    xact: UplinkTxn
    acks: List[gp.CtrlAck] = field(default_factory=list)
    code: Optional[int] = None
    data: List[int] = field(default_factory=list)
    sends: int = 0
    t_sent_ns: float = 0.0          # the command's last character left
    t_ack_ns: float = 0.0           # the final acknowledgment's EOP

    @property
    def latency_ns(self) -> float:
        return self.t_ack_ns - self.t_sent_ns


class HostCtrl(uvm_component):
    def build_phase(self):
        self.pkt_fifo = uvm_tlm_analysis_fifo("pkt_fifo", self)
        self.pkt_xp = self.pkt_fifo.analysis_export
        self.uplink_drv = None          # set by the env
        self._acks: Queue = Queue()
        self._lock = Lock()
        self.log: List[CtrlExchange] = []
        self.resends = 2

    async def run_phase(self):
        while True:
            pkt = await self.pkt_fifo.get()
            if pkt.type_byte != PacketType.CTRL_ACK:
                continue
            body = [w for w, _ in pkt.words[1:-1]]
            try:
                ack = gp.parse_ctrl_ack(body)
            except (ValueError, IndexError):
                continue                  # the control scoreboard reports it
            self._acks.put_nowait((ack, pkt.eop_ns))

    def _drain(self) -> None:
        """Forget acknowledgments nobody waited for (e.g. of an open-loop
        sequence that ran before)."""
        while True:
            try:
                self._acks.get_nowait()
            except QueueEmpty:
                return

    # ------------------------------------------------------------------
    async def command(self, xact: UplinkTxn,
                      timeout_ms: float = HOST_ACK_TIMEOUT_MS) -> CtrlExchange:
        """Send `xact` and wait for its final acknowledgment."""
        async with self._lock:
            self._drain()
            ex = CtrlExchange(xact)
            self.log.append(ex)
            for _ in range(1 + self.resends):
                ex.sends += 1
                await self.uplink_drv.send_txn(xact)
                ex.t_sent_ns = get_sim_time("ns")
                limit = ms_ns(timeout_ms)
                while True:
                    try:
                        ack, t = await with_timeout(self._acks.get(), round(limit), "ns")
                    except SimTimeoutError:
                        break
                    ex.acks.append(ack)
                    if ack.code == gp.ACK_WAIT:
                        # Figure 24: the final acknowledgment follows within
                        # the time the Wait announces.
                        wait_ms = ack.data[0] if ack.data else 0
                        limit = ms_ns(max(wait_ms, 1)) + ms_ns(timeout_ms)
                        continue
                    ex.code, ex.data, ex.t_ack_ns = ack.code, list(ack.data), t
                    return ex
                if xact.kind == UplinkKind.CTRL_CMD_RESET:
                    break
            return ex

    async def read(self, addr: int, nbytes: int = 4) -> Tuple[Optional[int], List[int]]:
        x = UplinkTxn(kind=UplinkKind.CTRL_CMD_READ, address=int(addr),
                      nwords=max(1, gp.nwords_of(nbytes)), size=int(nbytes))
        ex = await self.command(x)
        return ex.code, ex.data

    async def read1(self, addr: int) -> int:
        code, data = await self.read(addr)
        if code != gp.ACK_OK_DATA or not data:
            raise AssertionError(f"read 0x{addr:x}: acknowledgment {code!r}")
        return data[0]

    async def write(self, addr: int, data: Sequence[int],
                    size: Optional[int] = None) -> Optional[int]:
        if isinstance(data, int):
            data = [data]
        x = UplinkTxn(kind=UplinkKind.CTRL_CMD_WRITE, address=int(addr),
                      nwords=max(1, len(data)),
                      payload=[int(v) & 0xFFFF_FFFF for v in data], size=size)
        return (await self.command(x)).code

    async def write_ok(self, addr: int, data: Sequence[int]) -> None:
        code = await self.write(addr, data)
        if code != gp.ACK_OK_WRITE:
            raise AssertionError(f"write 0x{addr:x}: acknowledgment {code!r}")

    async def reset(self) -> Optional[int]:
        """Control channel reset (opcode 0xFF, §8.6.1.2)."""
        return (await self.command(UplinkTxn(kind=UplinkKind.CTRL_CMD_RESET))).code

    async def link_up(self, timeout_ns: int = 2_000_000) -> None:
        """Wait until the device reports the connection detected."""
        dut = get_dut()
        waited = 0
        while not int(dut.sb_link_detected.value):
            await Timer(1000, "ns")
            waited += 1000
            if waited > timeout_ns:
                raise AssertionError("the device never detected the link")
