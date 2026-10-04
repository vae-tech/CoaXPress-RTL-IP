"""cxp_host_uplink_agent — host→device low-speed serial BFM.

The host end of the link.  Every wire format comes from `cxp_protocol`
with `quirks.DEVICE` (rule U4): this agent decides *what* to send and
*when*, never how a packet is laid out.

Two lanes feed one serializer (`cxp_uplink.Uplink`, shared with the unit
benches):

* the **packet lane** (`seqr`) carries control commands, connection-test
  packets and IDLE fill, one packet per sequence item;
* the **trigger lane** (`trig_seqr`) carries the §8.2.4 short packets —
  Table 15 triggers and Table 17 I/O acknowledgments — which pre-empt
  whatever long packet is in flight at the next character boundary.

Bits are paced by a `Timer`, not by edges of the device's own `rx_clk`,
so the host has a clock of its own.  The nominal period is `OS_RATIO`
rx_clk periods and follows a clock retune, but `ppm`, `phase_ps` and
`jitter_ui` offset it from there — which is what makes a rate error a
rate error rather than a change both ends make together.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cocotb
from cocotb.triggers import Timer
from cocotb.utils import get_sim_time
from pyuvm import (
    ConfigDB, uvm_agent, uvm_analysis_port, uvm_component, uvm_driver,
    uvm_monitor, uvm_sequence, uvm_seq_item_port, uvm_sequence_item,
    uvm_sequencer, uvm_tlm_analysis_fifo
)

from cxp_protocol import packets as gp
from cxp_protocol import regmap as rm
from cxp_protocol.kcodes import K28_2, K28_4, rep4
from cxp_protocol.quirks import DEVICE
from cxp_uplink import Uplink          # src/verif/common, on PYTHONPATH

from uvm.common.clocks import rx_period_ns
from uvm.common.cxp_pkg import (
    CFG_HOST_JITTER_UI_KEY, CFG_HOST_PHASE_PS_KEY, CFG_HOST_PPM_KEY,
    UplinkKind, UplinkTxn, WireBeatKind,
)
from uvm.common.handles import get_dut
from uvm.common.seed import rng as seed_rng


# Oversampling ratio compiled into cxp_rx_link (p_OS_RATIO, a build knob).
# The host's nominal bit period is this many rx_clk periods.
from uvm.common.build import OS_RATIO

# Character times the driver lets pass before it takes its first item, so
# the soft sampler and the word aligner are locked when the first real
# packet starts.  The serializer fills the gap with IDLE by itself.
N_STARTUP_IDLE = 10

# Every codec call in this agent uses the device's wire format.
Q = DEVICE


def bit_period_ps() -> float:
    """Nominal host bit period: OS_RATIO periods of the device's rx_clk."""
    return OS_RATIO * rx_period_ns() * 1000.0


class UplinkItem(uvm_sequence_item):
    def __init__(self, name="uplink_item"):
        super().__init__(name)
        self.xact = UplinkTxn()


class UplinkSequencer(uvm_sequencer):
    """Packet lane — control commands, connection-test packets, IDLE."""


class UplinkTrigSequencer(uvm_sequencer):
    """§8.2.4 insertion lane — triggers and I/O acknowledgments."""


# ---------------------------------------------------------------------------
# Transaction -> character stream.  Everything here is a `cxp_protocol` call.
# ---------------------------------------------------------------------------
def _ctrl_cmd_beats(x: UplinkTxn) -> List[gp.Beat]:
    if x.opcode is not None:
        beats = gp.ctrl_cmd(x.opcode, x.address, size=x.size_bytes,
                            data=x.payload if x.opcode == gp.OP_WRITE else (),
                            q=Q, corrupt_crc=x.inject_crc_err)
        if x.opcode not in (gp.OP_READ, gp.OP_WRITE, gp.OP_RESET) and x.payload:
            # An undefined opcode with data words after the address.
            beats = beats[:4] + gp.data_beats(x.payload) + beats[4:]
    elif x.kind == UplinkKind.CTRL_CMD_READ:
        beats = gp.ctrl_cmd(gp.OP_READ, x.address, size=x.size_bytes, q=Q,
                            corrupt_crc=x.inject_crc_err)
    elif x.kind == UplinkKind.CTRL_CMD_WRITE:
        beats = gp.ctrl_cmd(gp.OP_WRITE, x.address, size=x.size_bytes,
                            data=x.payload, q=Q, corrupt_crc=x.inject_crc_err)
    else:
        beats = gp.ctrl_cmd(gp.OP_RESET, x.address, size=x.size_bytes, q=Q,
                            corrupt_crc=x.inject_crc_err)
    # §8.2.2.1: one flipped bit in one replica of the TYPE word must still
    # vote 3-of-4.  TYPE is the only replicated word of a Table 21 command
    # and is not in the CRC, so the command has to be executed anyway.
    if x.inject_one_bit_in_replica >= 0:
        w, k = beats[1]
        beats[1] = (w ^ (1 << (8 * (x.inject_one_bit_in_replica & 3))), k)
    if x.beats_edit is not None:
        beats = list(x.beats_edit(list(beats)))
    return beats


def _linktest_beats(x: UplinkTxn) -> List[gp.Beat]:
    payload = gp.linktest_payload(x.lt_n_data)
    for i in x.lt_error_indices:
        if 0 <= i < len(payload):
            payload[i] ^= 0x0000_0001
    return ([gp.SOP, (rep4(gp.TYPE_LINKTEST), 0)]
            + gp.data_beats(payload) + [gp.EOP])


def txn_chars(x: UplinkTxn) -> List[gp.Char]:
    """The character stream one transaction puts on the wire."""
    if x.kind == UplinkKind.IDLE:
        return gp.beats_to_chars([gp.IDLE])
    if x.kind in (UplinkKind.CTRL_CMD_READ, UplinkKind.CTRL_CMD_WRITE,
                  UplinkKind.CTRL_CMD_RESET):
        return gp.beats_to_chars(_ctrl_cmd_beats(x))
    if x.kind == UplinkKind.LINKTEST:
        beats = _linktest_beats(x)
        if x.beats_edit is not None:
            beats = list(x.beats_edit(beats))
        return gp.beats_to_chars(beats)
    if x.kind in (UplinkKind.TRIGGER_RISE, UplinkKind.TRIGGER_FALL):
        chars = gp.trigger_uplink(x.kind == UplinkKind.TRIGGER_RISE, x.delay, q=Q)
        if 0 <= x.trig_bad_leader < 3:
            b, k = chars[x.trig_bad_leader]
            chars[x.trig_bad_leader] = (K28_2 if b == K28_4 else K28_4, True)
        if x.trig_delays is not None:
            for i, d in enumerate(x.trig_delays[:3]):
                chars[3 + i] = (int(d) & 0xFF, False)
        return chars
    if x.kind == UplinkKind.IOACK:
        return gp.beats_to_chars(gp.io_ack(x.ioack_code))
    if x.kind == UplinkKind.RAW:
        beats = list(x.raw_beats)
        if x.beats_edit is not None:
            beats = list(x.beats_edit(beats))
        return gp.beats_to_chars(beats)
    return gp.beats_to_chars([gp.IDLE])


def _word_insert(x: UplinkTxn) -> bool:
    """§8.2.4: only the low-speed trigger is inserted at a character
    boundary; an I/O acknowledgment waits for the next word boundary."""
    return x.kind == UplinkKind.IOACK


class UplinkDriver(uvm_driver):
    """Serializes both lanes onto `rx_serial`."""

    def build_phase(self):
        super().build_phase()
        self.trig_item_port = uvm_seq_item_port("trig_item_port", self)
        # Host clock knobs.  `ppm` is the host's bit-rate offset from the
        # device's nominal, `phase_ps` shifts the first bit edge (None =
        # a random phase, so no test silently relies on one), `jitter_ui`
        # is a per-bit uniform +/- fraction of the bit period.
        self.ppm = float(ConfigDB().get(self, "", CFG_HOST_PPM_KEY, default=0.0))
        self.phase_ps: Optional[float] = ConfigDB().get(
            self, "", CFG_HOST_PHASE_PS_KEY, default=None)
        self.jitter_ui = float(ConfigDB().get(
            self, "", CFG_HOST_JITTER_UI_KEY, default=0.0))
        self.rng_seed = 0
        self.wire: Optional[Uplink] = None

    def start_of_simulation_phase(self):
        self.logger.warning(
            f"UplinkDriver configured: OS_RATIO={OS_RATIO} "
            f"bit_period={bit_period_ps() / 1e6:.3f}us ppm={self.ppm} "
            f"jitter_ui={self.jitter_ui} "
            f"N_STARTUP_IDLE={N_STARTUP_IDLE} quirks={Q.active()}"
        )

    def _publish(self, xact: UplinkTxn):
        if hasattr(self, "mon"):
            self.mon.ap.write(xact)

    async def run_phase(self):
        dut = get_dut()
        rng = seed_rng("uplink_drv", self.rng_seed)
        nominal = bit_period_ps()
        phase = rng.uniform(0, nominal) if self.phase_ps is None else self.phase_ps
        self.wire = Uplink(
            dut, dut.rx_clk_in, dut.rx_serial, os_ratio=OS_RATIO,
            bit_ps=nominal, ppm=self.ppm, phase_ps=phase,
            jitter_ui=self.jitter_ui,
            rng=rng,
        ).start()
        # Let the link come up on IDLE before anything real is sent: the
        # serializer fills the gap by itself.  Both lanes wait for it, so
        # a trigger cannot go out before the device can receive one.
        await Timer(round(N_STARTUP_IDLE * 4 * 10 * nominal), unit="ps")
        cocotb.start_soon(self._trigger_lane())
        while True:
            item = await self.seq_item_port.get_next_item()
            await self.send_txn(item.xact)
            self.seq_item_port.item_done()

    async def hold(self, bits: int, level: int = 0) -> None:
        """Hold the line at `level` for `bits` bit times after whatever is
        queued — the host stops sending (a cable pulled)."""
        await self.wire.hold(bits, level)

    async def slip(self, bits: int = 1) -> None:
        """Drop (`bits` > 0) or repeat (< 0) bits of the next character:
        a bit slip between the host's serializer and the line."""
        await self.wire.slip(bits)

    async def send_txn(self, xact: UplinkTxn) -> None:
        """Put one packet on the packet lane and return once its last
        character has left the pin.  The reactive host (`HostCtrl`) calls
        this directly; the sequencer path above does too."""
        # Publish before driving: a scoreboard has to register its
        # expectation ahead of the DUT's response, which for a trigger
        # comes back faster than the analysis FIFO is scheduled.
        self._publish(xact)
        xact.times.clear()
        await self.wire.send(
            txn_chars(xact),
            corrupt_sym_at=xact.inject_code_at,
            flip_rd_at=xact.inject_disp_at,
            times=xact.times,
        )

    async def _trigger_lane(self):
        """§8.2.4 insertion: characters go out at the next boundary, so a
        long packet in flight is split rather than queued behind."""
        while True:
            item = await self.trig_item_port.get_next_item()
            # Wait until the characters are on the wire: a sequence that
            # returned before them would let a test end mid-packet.
            await self.insert_now(item.xact).wait()
            self.trig_item_port.item_done()

    def retune_to_clock(self) -> None:
        """Follow a clock retune.  The nominal period tracks rx_clk; the
        `ppm` offset the test asked for is kept on top of it."""
        if self.wire is not None:
            self.wire.retune(bit_period_ps())

    # -- direct API for the reactive host (no sequencer round-trip) --------
    def insert_now(self, xact: UplinkTxn):
        """Put a short packet on the insertion lane immediately; returns
        the Event that fires once it has left the pin.  `xact.t_start_ns`
        / `t_done_ns` get the times its first character started and its
        last one left (-1 until then)."""
        self._publish(xact)
        xact.times.clear()
        return self.wire.insert(txn_chars(xact), word=_word_insert(xact),
                                times=xact.times, corrupt_sym_at=xact.inject_code_at)

    @property
    def char_ns(self) -> float:
        """One character time of the host (10 bits), in ns."""
        return 10 * (self.wire.bit_ps if self.wire is not None
                     and self.wire.bit_ps else bit_period_ps()) / 1000.0


class DeviceTriggerResponder(uvm_component):
    """The host's side of §8.3.3 for device triggers.

    A host acknowledges every trigger packet it receives with a Table 17
    I/O acknowledgment, inserted on the uplink at the next word boundary
    (§8.2.4); the device sends no new trigger until it sees one (or its
    timeout passes).  Watches the downlink short packets and answers
    through the uplink driver's insertion lane.

    Knobs (attributes, also read from ConfigDB at build):
      `mode`        "ack" (default) or "drop" (never answer);
      `delay_chars` host character times between the trigger packet and
                    the acknowledgment (0 = at once).
    `times` records (packet cycle, ack sent ns) per trigger.
    """

    def build_phase(self):
        self.short_fifo = uvm_tlm_analysis_fifo("short_fifo", self)
        self.short_xp = self.short_fifo.analysis_export
        self.mode = ConfigDB().get(self, "", "trig_ack_mode", default="ack")
        self.delay_chars = int(ConfigDB().get(self, "", "trig_ack_delay_chars",
                                              default=0))
        self.uplink_drv = None          # set by the env
        self.seen = self.sent = 0
        self.times: list = []

    async def run_phase(self):
        while True:
            pkt = await self.short_fifo.get()
            if pkt.kind not in (WireBeatKind.TRIG_RISE, WireBeatKind.TRIG_FALL):
                continue
            self.seen += 1
            if self.mode == "ack" and self.uplink_drv is not None:
                cocotb.start_soon(self._answer(pkt))

    async def _answer(self, pkt):
        if self.delay_chars:
            await Timer(round(self.delay_chars * self.uplink_drv.char_ns), unit="ns")
        self.sent += 1
        done = self.uplink_drv.insert_now(UplinkTxn(kind=UplinkKind.IOACK))
        await done.wait()
        self.times.append((pkt.tx_cycle, get_sim_time("ns")))

    def report_phase(self):
        self.logger.info(f"device_trigger_responder: {self.seen} device triggers, "
                         f"{self.sent} acknowledged (mode {self.mode})")


class UplinkMonitor(uvm_monitor):
    """Publishes each driven UplinkTxn to subscribers.

    The uplink is serial-only and the device is the only receiver, so the
    monitor is driver-mirrored: the driver writes what it put on the wire.
    """

    def build_phase(self):
        self.ap = uvm_analysis_port("ap", self)


class UplinkAgent(uvm_agent):
    def build_phase(self):
        self.seqr      = UplinkSequencer("seqr", self)
        self.trig_seqr = UplinkTrigSequencer("trig_seqr", self)
        self.drv       = UplinkDriver("drv", self)
        self.mon       = UplinkMonitor("mon", self)

    def connect_phase(self):
        self.drv.seq_item_port.connect(self.seqr.seq_item_export)
        self.drv.trig_item_port.connect(self.trig_seqr.seq_item_export)
        self.drv.mon = self.mon


# ---------------------------------------------------------------------------
# Sequences
# ---------------------------------------------------------------------------
class UplinkIdleSeq(uvm_sequence):
    """Hold the line at IDLE for n words."""

    def __init__(self, name="uplink_idle_seq", n=4):
        super().__init__(name)
        self.n = n

    async def body(self):
        for _ in range(self.n):
            item = UplinkItem("u")
            await self.start_item(item)
            item.xact.kind = UplinkKind.IDLE
            await self.finish_item(item)


class UplinkRegSeq(uvm_sequence):
    """One directed host ctrl-cmd: write `data` to `addr`, or read `addr`."""

    def __init__(self, name="uplink_reg_seq", addr=0, data=None, write=False):
        super().__init__(name)
        self.addr = addr
        self.data = data
        self.write = write

    async def body(self):
        item = UplinkItem("u")
        await self.start_item(item)
        x = item.xact
        x.kind = (UplinkKind.CTRL_CMD_WRITE if self.write
                  else UplinkKind.CTRL_CMD_READ)
        x.address = int(self.addr)
        x.nwords = 1
        if self.write:
            x.payload = [int(self.data) & 0xFFFF_FFFF]
        await self.finish_item(item)


# Registers a random write may hit without changing what the device does:
# MasterHostConnectionID, two reserved manufacturer words and the first
# word of DeviceUserID all take any value and act on nothing.
# (StreamPacketSizeMax, ConnectionConfig or TestMode written at random
# would change the stream under a test that is not about them.)
SAFE_RW_ADDRS = (rm.MASTER_HOST_CONNECTION_ID, rm.MFR_RESERVED1, rm.MFR_RESERVED2,
                 rm.DEVICE_USER_ID)


class UplinkCtrlRandomSeq(uvm_sequence):
    """Random ctrl-cmd reads/writes (open loop: no wait for the ack)."""

    def __init__(self, name="uplink_ctrl_random_seq", n=4, write=None, seed=1,
                 addrs=SAFE_RW_ADDRS):
        super().__init__(name)
        self.n = n
        self.write = write  # None=mix, True/False=force
        self.rng = seed_rng("uplink_ctrl_random", seed)
        self.addrs = tuple(addrs)

    async def body(self):
        for _ in range(self.n):
            item = UplinkItem("u")
            await self.start_item(item)
            x = item.xact
            do_write = self.write if self.write is not None else self.rng.choice([True, False])
            x.kind = UplinkKind.CTRL_CMD_WRITE if do_write else UplinkKind.CTRL_CMD_READ
            x.address = self.rng.choice(self.addrs)
            x.nwords  = 1
            if do_write:
                x.payload = [self.rng.randrange(0, 1 << 32)]
            await self.finish_item(item)


class UplinkTriggerRandomSeq(uvm_sequence):
    """Host→device triggers on the insertion lane (start on `trig_seqr`)."""

    def __init__(self, name="uplink_trigger_random_seq", n=4, seed=3, delay=0):
        super().__init__(name)
        self.n = n
        self.delay = delay
        self.rng = seed_rng(type(self).__name__, seed)

    async def body(self):
        for _ in range(self.n):
            item = UplinkItem("u")
            await self.start_item(item)
            item.xact.kind = self.rng.choice([UplinkKind.TRIGGER_RISE,
                                              UplinkKind.TRIGGER_FALL])
            item.xact.delay = self.delay
            await self.finish_item(item)


class UplinkLinktestSeq(uvm_sequence):
    def __init__(self, name="uplink_linktest_seq", n=2, n_data=None):
        super().__init__(name)
        self.n = n
        self.n_data = n_data

    async def body(self):
        for _ in range(self.n):
            item = UplinkItem("u")
            await self.start_item(item)
            item.xact.kind = UplinkKind.LINKTEST
            if self.n_data is not None:
                item.xact.lt_n_data = int(self.n_data)
            await self.finish_item(item)


class UplinkErrorInjectSeq(uvm_sequence):
    """Adds CRC / disparity / code / 1-bit-replica errors on top of a
    chosen uplink kind.

    Modes:
      * "crc" / "disp" / "code" / "replica" — 10b-symbol or CRC bit-flips.
      * "lt_word" — word-pattern bit-flips inside a connection-test body.
    """

    def __init__(self, name="uplink_error_inject_seq", n=4, mode="crc",
                 kind=UplinkKind.CTRL_CMD_READ, n_word_errors=3, seed=4):
        super().__init__(name)
        self.n = n
        self.mode = mode
        self.kind = kind
        self.n_word_errors = n_word_errors
        self.rng = seed_rng(type(self).__name__, seed)

    async def body(self):
        for _ in range(self.n):
            item = UplinkItem("u")
            await self.start_item(item)
            x = item.xact
            x.kind = self.kind
            if self.kind == UplinkKind.LINKTEST:
                k = min(self.n_word_errors, x.lt_n_data)
                x.lt_error_indices = sorted(
                    self.rng.sample(range(x.lt_n_data), k))
            else:
                x.address = 0x0000
                x.nwords = 1
            if self.mode == "crc":
                x.inject_crc_err = True
            elif self.mode == "disp":
                # Flip the running disparity after the TYPE character, so
                # the error lands inside the packet body, not on the comma.
                x.inject_disp_at = 4
            elif self.mode == "code":
                x.inject_code_at = 5
            elif self.mode == "replica":
                x.inject_one_bit_in_replica = self.rng.randrange(0, 4)
            elif self.mode == "lt_word":
                pass  # lt_error_indices already set above
            await self.finish_item(item)
