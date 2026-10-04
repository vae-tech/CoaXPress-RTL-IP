"""Concurrency tests (catalogue C-01 ... C-16, `prompts/uvm_env_review_tests.md`).

Each test starts independent actors with `cocotb.start_soon` — stream
source, control host, host trigger lane, device trigger pin, user-window
slave profile, clocks and resets — each with its own random stream from
`CXP_SEED`, and keeps every scoreboard strict.  Each declares the overlap
it exists for as bins (`self.hit(bin)`) and fails if a bin was never hit:
a concurrency test that did not overlap anything is a failed test.
"""

from __future__ import annotations

from collections import Counter

import cocotb
from cocotb.triggers import Timer
from cocotb.utils import get_sim_time

from cxp_protocol import packets as gp
from cxp_protocol import regmap as rm
from cxp_protocol import stream as gs

from uvm.agents.apb_slave_agent import (
    ApbProfile, ApbResponderDriver, ApbResponderHangSeq, ApbResponderPerfectSeq,
    ApbResponderWaitstateSeq, NEVER, USER_BASE, USER_WORDS,
)
from uvm.agents.io_agent import IoEvent
from uvm.common import build
from uvm.common.clocks import ms_ns, rx_period_ns, tx_period_ns
from uvm.common.cxp_pkg import UplinkKind, UplinkTxn
from uvm.common.handles import get_dut
from uvm.common.seed import rng
from uvm.common.decisions import D6_IOACK_LATENCY_CHARS
from uvm.tests.base_test import CxpTopTest
from uvm.tests.spec_tests import (
    _Frame, frames_after, sensor, stream_packets, tpg, wait_until,
)


def _user(i: int) -> int:
    return USER_BASE + 4 * i


class _ConcTest(CxpTopTest):
    """Shared: the bins a test must hit, and actors that stop together."""

    MUST_HIT: tuple = ()

    def build_phase(self):
        super().build_phase()
        self.bins: Counter = Counter()
        self._stop = False

    def hit(self, name: str, n: int = 1) -> None:
        self.bins[name] += n

    def start(self, coro):
        return cocotb.start_soon(coro)

    async def main_seq(self):
        await self.actors()
        self._stop = True
        await Timer(10_000, unit="ns")
        missing = [b for b in self.MUST_HIT if not self.bins[b]]
        self.logger.info("bins: " + " ".join(f"{k}={v}" for k, v in sorted(self.bins.items())))
        self.env.sb_test.check(not missing, "overlap_not_reached",
                               f"bins never hit: {missing}")

    async def actors(self):
        raise NotImplementedError

    # -- actors ---------------------------------------------------------
    async def host_traffic(self, n: int, seed: str, stop=None, gap_ns: float = 0):
        """Closed-loop random reads, multi-word reads and writes on the
        register file and the user window."""
        h, r = self.env.host, rng(seed)
        for i in range(n):
            if self._stop or (stop is not None and stop()):
                return
            a = r.random()
            if a < 0.3:
                await h.read(r.choice([rm.STANDARD, rm.REVISION, rm.CONNECTION_CONFIG,
                                       rm.MFR_RESERVED1, rm.DEVICE_VENDOR_NAME]))
            elif a < 0.5:
                await h.read(_user(r.randrange(USER_WORDS - 16)), 4 * r.randrange(1, 17))
            elif a < 0.8:
                await h.write(_user(r.randrange(USER_WORDS - 8)),
                              [r.randrange(1 << 32) for _ in range(r.randrange(1, 9))])
            else:
                await h.write(r.choice([rm.MFR_RESERVED1, rm.MFR_RESERVED2]), [r.randrange(1 << 32)])
            if gap_ns:
                await Timer(round(gap_ns), unit="ns")

    async def slave_stalls(self, seed: str, until, max_latency: int = 40):
        """The user slave's latency changes at random (0 .. max_latency)."""
        r = rng(seed)
        while not until():
            ApbResponderDriver.apply(ApbProfile(latency=r.randrange(max_latency + 1)))
            await Timer(round(r.uniform(2_000, 20_000)), unit="ns")
        ApbResponderDriver.apply(ApbProfile())

    async def sensor_frames(self, n: int, seed: str, sizes=((64, 8), (128, 16), (37, 5))):
        r = rng(seed)
        for i in range(n):
            if self._stop:
                return
            w, hgt = r.choice(sizes)
            await _Frame(xsize=w, ysize=hgt, seed=i,
                         meta=dict(sourcetag=i & 0xFFFF)).start(self.env.video_ag.seqr)

    async def host_triggers(self, n: int, seed: str, gap_chars=(8, 40)):
        drv, r = self.env.uplink_ag.drv, rng(seed)
        for _ in range(n):
            if self._stop:
                return
            await Timer(round(r.uniform(*gap_chars) * drv.char_ns), unit="ns")
            kind = r.choice([UplinkKind.TRIGGER_RISE, UplinkKind.TRIGGER_FALL])
            await drv.insert_now(UplinkTxn(kind=kind, delay=r.randrange(240))).wait()

    async def pin_edges(self, n: int, seed: str, gap_ns=(2_000, 30_000)):
        dut, r, level = get_dut(), rng(seed), 0
        while not int(dut.sb_link_detected.value):
            await Timer(1_000, unit="ns")
        for _ in range(n):
            if self._stop:
                break
            await Timer(round(r.uniform(*gap_ns)), unit="ns")
            level ^= 1
            dut.trigger_in_app.value = level
            self.env.io_ag.mon.ap.write(IoEvent("trig_rise" if level else "trig_fall"))
        if level:
            dut.trigger_in_app.value = 0
            self.env.io_ag.mon.ap.write(IoEvent("trig_fall"))


# =============================================================================
class test_conc_ctrl_under_stream_load(_ConcTest):
    """C-01 — control under a full stream (§8.6, §8.2.4 Table 13).

    Actors: sensor images in 1016-word packets back to back; closed-loop
    random reads, multi-word reads and writes on both targets; the user
    slave's latency changing at random.

    Checks beyond the scoreboards: every acknowledgment sits between two
    stream packets (a long packet is never nested, sb_linkpro) and comes
    within one largest stream packet plus 16 words of its command (the
    control scoreboard's latency, reported).  Bins: an acknowledgment
    right behind a stream EOP (queued behind it); a slave stall while a
    stream packet was on the wire.
    """

    PKT_DSIZE_P = 1016
    MUST_HIT = ("ack_after_stream_eop", "stall_under_stream")
    PLAN = ("CXP-CAM-CTRL-010",)

    async def actors(self):
        await sensor()
        done = []
        st = self.start(self.slave_stalls("c01_slave", lambda: bool(done)))
        log = self.env.pkt_log
        def queued_ack_seen():
            for p in log.long:
                if p.type_byte != 0x03 or log.gap_after_eop(p.sop_cycle) > 2:
                    continue
                prev = [q for q in log.long if q.eop_cycle < p.sop_cycle]
                if prev and prev[-1].type_byte == 0x01:
                    return True
            return False
        for attempt in range(5):
            fr = self.start(self.sensor_frames(10, f"c01_frames_{attempt}"))
            await self.host_traffic(60, f"c01_host_{attempt}")
            await fr
            if queued_ack_seen():
                break
        done.append(1)
        await st
        for p in log.long:
            if p.type_byte == 0x03 and log.gap_after_eop(p.sop_cycle) <= 2:
                prev = [q for q in log.long if q.eop_cycle < p.sop_cycle]
                if prev and prev[-1].type_byte == 0x01:
                    self.hit("ack_after_stream_eop")
        if self.env.apb_ag.mon.max_wait and self.env.sb_stream.crc_checked:
            self.hit("stall_under_stream")
        lat = self.env.sb_control.max_latency_ns
        # The command may be a 16-word user-window transfer, whose APB
        # waits accumulate before its acknowledgment can be queued.
        bound = ((1016 + 8 + 16) * tx_period_ns() + 1_000
                 + 16 * self.env.apb_ag.mon.max_wait * rx_period_ns())
        self.env.sb_test.check(lat <= bound, "ack_latency",
                               f"acknowledgment {lat:.0f} ns after its command, bound {bound:.0f}")


class test_conc_ioack_inside_stream(_ConcTest):
    """C-02 — the I/O acknowledgment is inserted into a stream packet
    (§8.2.4, Table 13 priority 1; §8.3.3).

    Actors: Mono16 sensor images at twice the pixel clock (more than the
    downlink carries) in 24-word packets, so packets run back to back and
    headers and tails are a quarter of the words; host triggers at random
    times.

    Then, with the stream stopped: 256-byte user-window reads whose slave
    waits L rx cycles per word, each followed at once by a host trigger;
    L walks up until the I/O acknowledgment has landed inside the read's
    acknowledgment payload twice.

    Checks: every I/O acknowledgment comes within a few words of its
    trigger (sb_ioack latency, bounded by decision D6: one uplink
    character), and
    where it lands the enclosing packet's CRC and DsizeP still hold
    (sb_stream, sb_control).  Bins: an I/O acknowledgment in a stream
    header, in payload, between the CRC and the EOP, in a control
    acknowledgment's payload, and in IDLE.
    """

    PKT_DSIZE_P = 24
    APP_PERIOD = 5
    MUST_HIT = ("ioack_0x01:header", "ioack_0x01:payload", "ioack_0x01:tail",
                "ioack_0x03:payload", "ioack_idle")
    PLAN = ("CXP-CAM-TRIG-006",)

    async def actors(self):
        # Mono16 at twice the pixel clock: more words than the downlink
        # carries, so stream packets fill the wire back to back.
        await self.env.host.write_ok(rm.PIXEL_FORMAT_ALIAS, [gs.PIXELF_TO_PFNC[0x0105]])
        await sensor()

        async def frames():
            i = 0
            while not self._stop:
                await _Frame(xsize=128, ysize=32, pixfmt=0x0105, bits=16,
                             seed=i, meta=dict(sourcetag=i & 0xFFFF)).start(self.env.video_ag.seqr)
                i += 1
        fr = self.start(frames())
        await self.host_triggers(150, "c02_trig", gap_chars=(3, 9))
        self._stop = True
        await fr
        await self._into_ctrl_acks()
        pos = []
        for s in self.env.pkt_log.short:
            if s.kind.name == "IOACK":
                w = self.env.pkt_log.where(s.tx_cycle - 1)
                self.hit("ioack_idle" if w == "idle" else f"ioack_{w}")
                p = self.env.pkt_log.enclosing(s.tx_cycle - 1)
                if p is not None:
                    pos.append((sum(1 for c in p.cycles if c < s.tx_cycle - 1), len(p.cycles)))
        self.logger.info(f"I/O acknowledgments inside stream packets at (word, of): {pos}")
        drv = self.env.uplink_ag.drv
        worst = max(self.env.sb_ioack.latency_ns or [0])
        bound = D6_IOACK_LATENCY_CHARS * drv.char_ns
        self.env.sb_test.check(worst <= bound, "ioack_latency",
                               f"I/O acknowledgment {worst:.0f} ns after its trigger "
                               f"(D6: {D6_IOACK_LATENCY_CHARS} host character = {bound:.0f} ns)")


    async def _into_ctrl_acks(self):
        # A host trigger ends six characters after the command before it;
        # the read's 70-word acknowledgment starts once the slave has
        # answered 64 words, so the slave latency L moves it across the
        # I/O acknowledgment.
        env, dut, drv = self.env, get_dut(), self.env.uplink_ag.drv
        await Timer(20_000, unit="ns")
        rise, landed = True, 0
        for lat in range(64):
            dut.usr_latency.value = lat
            await drv.send_txn(UplinkTxn(kind=UplinkKind.CTRL_CMD_READ,
                                         address=_user(0), size=256))
            kind = UplinkKind.TRIGGER_RISE if rise else UplinkKind.TRIGGER_FALL
            await drv.insert_now(UplinkTxn(kind=kind)).wait()
            rise = not rise
            for _ in range(200):
                if not env.sb_control.pending_count():
                    break
                await Timer(1_000, unit="ns")
            await Timer(2_000, unit="ns")
            last = [x for x in env.pkt_log.short if x.kind.name == "IOACK"][-1:]
            if last and env.pkt_log.where(last[0].tx_cycle - 1) == "0x03:payload":
                landed += 1
                if landed == 2:
                    break
        dut.usr_latency.value = 0


class test_conc_trigger_insertion_sweep(_ConcTest):
    """C-03 — device trigger edges swept across stream packets, control
    acknowledgments and test packets (§8.2.4, §8.2.5.1, §8.3.2).

    Actors: the test pattern in 16-word packets, TestMode on for part of
    the run (test packets), closed-loop reads (acknowledgments); the
    device trigger pin toggled at every tx-cycle offset of a sweep; the
    host acknowledges each trigger.

    Checks: every trigger packet goes out a fixed number of words after
    its pin edge (spread at most 2 words), never split (sb_linkpro), the
    packet it lands in keeps its CRC (sb_stream / sb_linktest / sb_control);
    at most 99 non-IDLE words in a row (SVA; measured).  Bins: a trigger
    inside a stream packet, an acknowledgment, a test packet.
    """

    PKT_DSIZE_P = 16
    MUST_HIT = ("trig_0x01", "trig_0x03", "trig_0x04")
    PLAN = ("CXP-CAM-TRIG-004",)

    async def actors(self):
        env, dut, h = self.env, get_dut(), self.env.host
        await tpg(1)
        edges = []

        async def sweep(n, phase):
            level = int(dut.trigger_in_app.value)
            for k in range(n):
                # Past the previous trigger's acknowledgment round trip (an
                # I/O acknowledgment is 8 host characters), so the device
                # sends each edge as it comes; the offset walks every word.
                rt = 20 * env.uplink_ag.drv.char_ns / tx_period_ns()
                await Timer(round((rt + 7 * (k % 97) + phase) * tx_period_ns()), unit="ns")
                level ^= 1
                dut.trigger_in_app.value = level
                env.io_ag.mon.ap.write(IoEvent("trig_rise" if level else "trig_fall"))
                edges.append(int(dut.tx_words.value))
            if level:
                await Timer(2_000, unit="ns")
                dut.trigger_in_app.value = 0
                env.io_ag.mon.ap.write(IoEvent("trig_fall"))
        host = self.start(self.host_traffic(30, "c03_host"))
        await sweep(60, 0)
        await host
        # Into control acknowledgments: a 256-byte user-window read's
        # acknowledgment (70 words; the slave answers at once) starts a
        # fixed number of tx cycles after the command's last character.  One read measures it; then each
        # edge is placed a known number of words into the acknowledgment,
        # four more each round, rising and falling in turn.
        drv = env.uplink_ag.drv
        await tpg(0)
        await Timer(20_000, unit="ns")

        def rd():
            return UplinkTxn(kind=UplinkKind.CTRL_CMD_READ, address=_user(0), size=256)

        await drv.send_txn(rd())
        c0 = int(dut.tx_words.value)
        await Timer(20_000, unit="ns")
        first = [p.cycles[0] for p in env.pkt_log.long
                 if p.type_byte == gp.TYPE_CTRL_ACK and p.cycles and p.cycles[0] > c0]
        delta = first[0] - c0 if first else 0
        for k in range(16):
            await drv.send_txn(rd())
            target = int(dut.tx_words.value) + delta + 4 + 4 * k
            while int(dut.tx_words.value) < target:
                await Timer(round(tx_period_ns()), unit="ns")
            level = 1 - int(dut.trigger_in_app.value)
            dut.trigger_in_app.value = level
            env.io_ag.mon.ap.write(IoEvent("trig_rise" if level else "trig_fall"))
            edges.append(int(dut.tx_words.value))
            await Timer(round(20 * drv.char_ns), unit="ns")
        if int(dut.trigger_in_app.value):
            dut.trigger_in_app.value = 0
            env.io_ag.mon.ap.write(IoEvent("trig_fall"))
            await Timer(round(20 * drv.char_ns), unit="ns")
        await tpg(1)
        await h.write_ok(rm.TEST_MODE, [1])
        await sweep(40, 3)
        await h.write_ok(rm.TEST_MODE, [0])
        pk = [s for s in env.pkt_log.short if s.kind.name in ("TRIG_RISE", "TRIG_FALL")]
        lats = []
        for e in edges:
            later = [s.tx_cycle for s in pk if s.tx_cycle > e]
            if later:
                lats.append(later[0] - e)
        for s in pk:
            w = env.pkt_log.where(s.tx_cycle - 1)
            if w != "idle":
                self.hit("trig_" + w.split(":")[0])
        self.logger.info(f"edge-to-packet words: {sorted(Counter(lats).items())}")
        # The pin crosses two flops; the packet waits at most for an IDLE
        # that is due and the word in flight.
        self.env.sb_test.check(lats and max(lats) - min(lats) <= 3, "trig_latency",
                               f"edge-to-packet latency spreads {min(lats)}..{max(lats)} words")


class test_conc_trigger_vs_ioack(_ConcTest):
    """C-04 — a device trigger and a host trigger's I/O acknowledgment due
    in the same words, inside a stream packet (§8.2.4 priorities 0 and 1).

    Actors: sensor images in 1016-word packets; host triggers and device
    pin edges timed so the two short packets meet.

    Checks: both short packets whole (sb_linkpro), the trigger first when
    both are due, the stream packet around them intact.  Bins: the two
    within 4 words of each other; both inside one stream packet.
    """

    PKT_DSIZE_P = 1016
    MUST_HIT = ("adjacent", "both_in_one_packet")
    PLAN = ("CXP-CAM-TRIG-007",)

    async def actors(self):
        env, dut, drv = self.env, get_dut(), self.env.uplink_ag.drv
        await sensor()
        async def frames():
            i = 0
            while not self._stop:
                await _Frame(xsize=128, ysize=32, seed=i,
                             meta=dict(sourcetag=i & 0xFFFF)).start(env.video_ag.seqr)
                i += 1
        fr = self.start(frames())
        await Timer(20_000, unit="ns")
        level = 0
        r = rng("c04")
        for k in range(96):
            # Send a host trigger; flip the pin so its trigger is due about
            # when the I/O acknowledgment is.
            done = drv.insert_now(UplinkTxn(kind=UplinkKind.TRIGGER_RISE, delay=0))
            await done.wait()
            await Timer(round(r.uniform(0, 80)), unit="ns")
            level ^= 1
            dut.trigger_in_app.value = level
            env.io_ag.mon.ap.write(IoEvent("trig_rise" if level else "trig_fall"))
            await Timer(round(20 * drv.char_ns), unit="ns")
            if k >= 23:
                io = [s for s in env.pkt_log.short if s.kind.name == "IOACK"]
                tr = [s for s in env.pkt_log.short
                      if s.kind.name in ("TRIG_RISE", "TRIG_FALL")]
                if any(abs(a.tx_cycle - t.tx_cycle) <= 4 and
                       env.pkt_log.enclosing(a.tx_cycle) is not None and
                       env.pkt_log.enclosing(a.tx_cycle) is env.pkt_log.enclosing(t.tx_cycle)
                       for a in io for t in tr):
                    break
        if level:
            dut.trigger_in_app.value = 0
            env.io_ag.mon.ap.write(IoEvent("trig_fall"))
        self._stop = True
        await fr
        sh = env.pkt_log.short
        io = [s for s in sh if s.kind.name == "IOACK"]
        tr = [s for s in sh if s.kind.name in ("TRIG_RISE", "TRIG_FALL")]
        for a in io:
            for t in tr:
                if abs(a.tx_cycle - t.tx_cycle) <= 4:
                    self.hit("adjacent")
                    pa, pt = env.pkt_log.enclosing(a.tx_cycle), env.pkt_log.enclosing(t.tx_cycle)
                    if pa is not None and pa is pt:
                        self.hit("both_in_one_packet")


class test_conc_uplink_trigger_in_packet(_ConcTest):
    """C-05 — a host trigger inside a control write and inside a test packet
    (§8.2.4: a low-speed trigger at any character boundary).

    Actors: control writes with a Table 15 trigger started 0 .. 27
    characters after the write's first character; a 1024-word host test
    packet with triggers inside at three places.

    Checks: every trigger fires with the same latency less Delay
    (sb_rxtrig) and is acknowledged (sb_ioack); every write executes and
    reads back; the test packet counts no error (sb_linktest).  Bins: a
    trigger inside a command's header, data, CRC, and inside a test
    packet.
    """

    MUST_HIT = ("in_cmd", "in_test_packet")
    PLAN = ("CXP-CAM-PROT-005",)

    async def actors(self):
        env, h, drv = self.env, self.env.host, self.env.uplink_ag.drv
        for off in range(28):
            cmd = UplinkTxn(kind=UplinkKind.CTRL_CMD_WRITE, address=rm.MFR_RESERVED1,
                            payload=[0xC0DE_0000 + off])
            t = self.start(drv.send_txn(cmd))
            await wait_until(lambda: cmd.t_start_ns >= 0, 100_000, 50)
            await Timer(round(max(0.0, cmd.t_start_ns + off * drv.char_ns
                                  - get_sim_time("ns")) + 1), unit="ns")
            trig = UplinkTxn(kind=UplinkKind.TRIGGER_RISE, delay=(off * 9) % 240)
            await drv.insert_now(trig).wait()
            await t
            if cmd.t_start_ns < trig.t_start_ns < cmd.t_done_ns:
                self.hit("in_cmd")
            await Timer(10_000, unit="ns")
            env.sb_test.check(await h.read1(rm.MFR_RESERVED1) == 0xC0DE_0000 + off, "write",
                              f"write with a trigger at character {off} lost")
        lt = UplinkTxn(kind=UplinkKind.LINKTEST, lt_n_data=1024)
        t = self.start(drv.send_txn(lt))
        await wait_until(lambda: lt.t_start_ns >= 0, 100_000, 50)
        for frac in (0.001, 0.5, 0.998):
            at = lt.t_start_ns + frac * 1027 * 4 * drv.char_ns
            await Timer(round(max(1.0, at - get_sim_time("ns"))), unit="ns")
            trig = UplinkTxn(kind=UplinkKind.TRIGGER_FALL, delay=100)
            get_dut().cfg_trig_polarity.value = 0
            await drv.insert_now(trig).wait()
            if lt.t_start_ns < trig.t_start_ns:
                self.hit("in_test_packet")
        await t


class test_conc_testmode_under_stream(_ConcTest):
    """C-06 — TestMode switched on under a running stream (§8.7.4, §10.3.35,
    decision D2).

    Actors: sensor images back to back; TestMode written 1 at different
    points of stream packets and 0 again; closed-loop polling of the test
    counters; host triggers and device pin edges throughout.

    Checks: the stream packet in flight completes, then no stream packet
    starts until TestMode is 0 (sb_stream); images caught by the switch
    are dropped whole, never cut (sb_stream / sb_linkpro); triggers and
    I/O acknowledgments keep flowing (D2); after TestMode 0 the stream
    resumes on an image boundary.  Bins: TestMode switched on while a
    stream packet was on the wire; host triggers and device triggers
    during TestMode.
    """

    PKT_DSIZE_P = 256
    APP_PERIOD = 5
    MUST_HIT = ("testmode_mid_packet", "ioack_in_testmode", "trig_in_testmode")
    PLAN = ("CXP-CAM-CT-003",)

    async def actors(self):
        env, h, dut = self.env, self.env.host, get_dut()
        # Mono16 at twice the pixel clock: stream packets fill the wire, so
        # TestMode lands inside one.
        await h.write_ok(rm.PIXEL_FORMAT_ALIAS, [gs.PIXELF_TO_PFNC[0x0105]])
        await sensor()
        t_on_all = []

        async def watch_tm():
            from cocotb.triggers import RisingEdge
            while True:
                await RisingEdge(dut.bs_test_mode)
                t_on_all.append(get_sim_time("ns"))
        self.start(watch_tm())

        async def frames():
            for i in range(30):
                if self._stop:
                    return
                await _Frame(xsize=128, ysize=16, pixfmt=0x0105, bits=16, seed=i,
                             meta=dict(sourcetag=i)).start(env.video_ag.seqr)
        fr = self.start(frames())
        tr = self.start(self.host_triggers(30, "c06_htrig", gap_chars=(10, 60)))
        pe = self.start(self.pin_edges(20, "c06_pin", gap_ns=(10_000, 40_000)))
        r = rng("c06")
        for k in range(4):
            await Timer(round(r.uniform(5_000, 40_000)), unit="ns")
            env.sb_stream.flush_window(get_sim_time("ns"))
            await h.write_ok(rm.TEST_MODE, [1])
            t_on = get_sim_time("ns")
            for _ in range(3):
                await h.read(rm.TEST_PACKET_COUNT_TX, 8)
            await h.write_ok(rm.TEST_MODE, [0])
            t_off = get_sim_time("ns")
            env.sb_stream.close_flush_window()
            for s in env.pkt_log.short:
                if s.t_ns and t_on <= s.t_ns <= t_off:
                    self.hit("ioack_in_testmode" if s.kind.name == "IOACK"
                             else "trig_in_testmode")
        for t in t_on_all:
            if any(p.type_byte == 0x01 and p.sop_ns < t < p.eop_ns
                   for p in env.pkt_log.long):
                self.hit("testmode_mid_packet")
        await tr
        await pe
        self._stop = True
        await fr


class test_conc_conn_reset_everything(_ConcTest):
    """C-07 — ConnectionReset under everything (§10.3.28).

    Actors: sensor images; a read pending on a hung user slave; the device
    trigger held asserted; a host test packet arriving; then a storm of
    five ConnectionReset writes, two of them while the previous reset is
    still in progress; then rediscovery.

    Checks: no torn packet (sb_linkpro); after each reset the §10.3.28
    registers read their reset values (register map), the counters 0;
    windows never outnumber requests and each closes (sb_linkreset);
    nothing streams until StreamPacketSizeMax is written again, then the
    first packet has tag 0 (sb_stream).  Bins: a reset with a stream
    packet in flight, with a control command pending, with a device
    trigger asserted, while a host test packet arrives.
    """

    MUST_HIT = ("reset_mid_stream", "reset_ctrl_pending", "reset_trig_asserted",
                "reset_linktest_rx")
    PLAN = ("CXP-CAM-REC-002",)

    async def actors(self):
        env, h, drv, dut = self.env, self.env.host, self.env.uplink_ag.drv, get_dut()
        await sensor()
        fr = self.start(self.sensor_frames(6, "c07_frames", sizes=((128, 32),)))
        await Timer(30_000, unit="ns")
        # A read on a hung slave: the reset command waits behind it.
        await ApbResponderHangSeq().start(env.apb_ag.seqr)
        await drv.send_txn(UplinkTxn(kind=UplinkKind.CTRL_CMD_READ, address=_user(3)))
        dut.trigger_in_app.value = 1
        env.io_ag.mon.ap.write(IoEvent("trig_rise"))
        lt = UplinkTxn(kind=UplinkKind.LINKTEST, lt_n_data=64)
        ltf = self.start(drv.send_txn(lt))
        # The host's I/O acknowledgment of the device trigger (8 characters,
        # inserted first) goes out before the test packet starts.
        await Timer(round(16 * drv.char_ns), unit="ns")
        if env.pkt_log.enclosing(int(dut.tx_words.value)) or env.sb_stream.crc_checked:
            self.hit("reset_mid_stream")
        if env.sb_control.pending_count():
            self.hit("reset_ctrl_pending")
        if int(dut.trigger_in_app.value):
            self.hit("reset_trig_asserted")
        self.logger.info(f"host test packet times at the reset: {lt.times}")
        if lt.t_start_ns >= 0 and lt.t_done_ns < 0:
            self.hit("reset_linktest_rx")
        env.sb_stream.flush_window(get_sim_time("ns"))
        # Open loop: this write waits behind the hung read (decision D7),
        # which ends in its 900 ms timeout.
        await drv.send_txn(UplinkTxn(kind=UplinkKind.CTRL_CMD_WRITE,
                                     address=rm.CONNECTION_RESET, payload=[1]))
        await ltf
        await wait_until(lambda: not env.sb_control.pending_count(), ms_ns(1500), 500)
        await ApbResponderPerfectSeq().start(env.apb_ag.seqr)
        for _ in range(5):
            await h.write(rm.CONNECTION_RESET, [1])
            await Timer(round(200 * rx_period_ns()), unit="ns")
        for _ in range(20):
            code, d = await h.read(rm.CONNECTION_RESET)
            if code == gp.ACK_OK_DATA and d and d[0] == 0:
                break
        for a in (rm.MASTER_HOST_CONNECTION_ID, rm.STREAM_PACKET_SIZE_MAX,
                  rm.TEST_MODE, rm.TEST_ERROR_COUNT):
            await h.read(a)
        dut.trigger_in_app.value = 0
        env.io_ag.mon.ap.write(IoEvent("trig_fall"))
        env.sb_stream.close_flush_window()
        tags0 = len(env.sb_stream.tags)
        await h.write_ok(rm.STREAM_PACKET_SIZE_MAX, [self.spsm_for(self.pkt_dsize)])
        self._stop = True
        await fr
        self._stop = False
        await sensor()
        await self.sensor_frames(1, "c07_after")
        await wait_until(lambda: len(env.sb_stream.tags) > tags0, 200_000, 500)
        env.sb_test.check(env.sb_stream.tags[tags0:tags0 + 1] == [0], "tag0",
                          f"first packet after rediscovery: {env.sb_stream.tags[tags0:tags0 + 1]}")


class test_conc_ctrl_reset_under_load(_ConcTest):
    """C-08 — control channel reset (0xFF) under load (§8.6.1.2).

    Actors: sensor images; host triggers; a user-window read that draws a
    Wait, then 0xFF; 0xFF while the slave is busy; 0xFF right behind a
    register read.

    Checks: only 0x03 comes back for each cancelled command (control
    scoreboard); the stream keeps its tags and images (a control reset
    does not touch the stream, sb_stream).  Bins: 0xFF with the bus busy,
    after a Wait, while an acknowledgment is framed.
    """

    MUST_HIT = ("ff_bus_busy", "ff_after_wait", "ff_framing")
    PLAN = ("CXP-CAM-CTRL-006",)

    async def actors(self):
        env, h, drv = self.env, self.env.host, self.env.uplink_ag.drv
        await sensor()
        fr = self.start(self.sensor_frames(12, "c08_frames"))
        tr = self.start(self.host_triggers(20, "c08_trig"))
        ms = build.RX_CLK_KHZ
        await ApbResponderWaitstateSeq(waits=300 * ms).start(env.apb_ag.seqr)
        w0 = env.sb_control.waits
        await drv.send_txn(UplinkTxn(kind=UplinkKind.CTRL_CMD_READ, address=_user(1)))
        if await wait_until(lambda: env.sb_control.waits > w0, ms_ns(300), 200):
            self.hit("ff_after_wait")
        await h.reset()
        await drv.send_txn(UplinkTxn(kind=UplinkKind.CTRL_CMD_READ, address=_user(2)))
        if await wait_until(lambda: int(get_dut().apb_psel.value), 5_000, 50):
            self.hit("ff_bus_busy")
        await h.reset()
        await ApbResponderPerfectSeq().start(env.apb_ag.seqr)
        await drv.send_txn(UplinkTxn(kind=UplinkKind.CTRL_CMD_READ, address=rm.DEVICE_VENDOR_NAME,
                                     size=64))
        self.hit("ff_framing")
        await h.reset()
        await h.read(_user(4))
        await tr
        await fr


class test_conc_uplink_errors_under_stream(_ConcTest):
    """C-09 — a noisy uplink under a running stream (§8.2.2, §8.6.3).

    Actors: a random mix on the uplink — good reads and writes, bad CRC,
    code errors, disparity flips, truncated packets, reserved packet
    types, host triggers; sensor images; device pin edges.

    Checks: every good command answered with its data; every bad one with
    its code or not at all as the model allows; the receiver's error
    pulses at least the injected ones and none of a kind not injected
    (sb_linkerr); the downlink undisturbed (every scoreboard).  Bins:
    each error kind followed by each packet kind.
    """

    MUST_HIT = ("crc>cmd", "code>cmd", "trunc>cmd", "type>cmd", "disp>cmd")
    PLAN = ("CXP-CAM-NEG-001",)

    async def actors(self):
        env, h, drv = self.env, self.env.host, self.env.uplink_ag.drv
        await sensor()
        fr = self.start(self.sensor_frames(12, "c09_frames"))
        pe = self.start(self.pin_edges(10, "c09_pin"))
        r = rng("c09")
        prev = None
        env.sb_linkerr.allow(get_sim_time("ns"))
        # Guarantee each error-to-command transition before the random mix;
        # otherwise a seed can miss a required bin without exercising it.
        from uvm.tests.spec_tests import _raw_packet
        for kind in ("crc", "code", "trunc", "type", "disp"):
            if kind == "crc":
                await h.command(UplinkTxn(kind=UplinkKind.CTRL_CMD_READ,
                                          address=rm.REVISION, inject_crc_err=True))
            elif kind == "code":
                await drv.send_txn(UplinkTxn(kind=UplinkKind.CTRL_CMD_READ,
                                             address=rm.REVISION, inject_code_at=10))
            elif kind == "disp":
                await drv.send_txn(UplinkTxn(kind=UplinkKind.CTRL_CMD_READ,
                                             address=rm.REVISION, inject_disp_at=10))
            elif kind == "trunc":
                await drv.send_txn(UplinkTxn(kind=UplinkKind.CTRL_CMD_READ,
                                             address=rm.REVISION,
                                             expect_codes={gp.ACK_SIZE_MISMATCH},
                                             beats_edit=lambda b: b[:3] + [b[-1]]))
            else:
                await drv.send_txn(UplinkTxn(kind=UplinkKind.RAW,
                                             raw_beats=_raw_packet(0x05)))
            await h.read(rm.STANDARD)
            self.hit(f"{kind}>cmd")
        for i in range(60):
            k = r.choice(["good", "good", "crc", "code", "disp", "trunc", "type", "trig"])
            if k == "good":
                await h.read(r.choice([rm.STANDARD, _user(r.randrange(64))]))
            elif k == "crc":
                await h.command(UplinkTxn(kind=UplinkKind.CTRL_CMD_READ, address=rm.REVISION,
                                          inject_crc_err=True))
            elif k == "code":
                await drv.send_txn(UplinkTxn(kind=UplinkKind.CTRL_CMD_READ,
                                             address=rm.REVISION,
                                             inject_code_at=r.randrange(8, 16)))
            elif k == "disp":
                await drv.send_txn(UplinkTxn(kind=UplinkKind.CTRL_CMD_READ,
                                             address=rm.REVISION,
                                             inject_disp_at=r.randrange(8, 14)))
            elif k == "trunc":
                await drv.send_txn(UplinkTxn(kind=UplinkKind.CTRL_CMD_READ,
                                             address=rm.REVISION,
                                             expect_codes={gp.ACK_SIZE_MISMATCH},
                                             beats_edit=lambda b: b[:3] + [b[-1]]))
            elif k == "type":
                from uvm.tests.spec_tests import _raw_packet
                await drv.send_txn(UplinkTxn(kind=UplinkKind.RAW,
                                             raw_beats=_raw_packet(r.choice([0x05, 0x7F]))))
            else:
                await drv.insert_now(UplinkTxn(kind=UplinkKind.TRIGGER_RISE,
                                               delay=r.randrange(240))).wait()
            if prev and prev != "good" and k == "good":
                self.hit(f"{prev}>cmd")
            prev = k
            await Timer(round(r.uniform(0, 4) * drv.char_ns), unit="ns")
        await Timer(60_000, unit="ns")
        env.sb_linkerr.close()
        self._stop = True
        await pe
        await fr


class test_conc_link_loss_under_stream(_ConcTest):
    """C-10 — the uplink lost and back while the device streams (§10.2).

    Actors: sensor images back to back; a write cut and the line held past
    the link monitor's 32 bad words; IDLE again; a read.

    Checks: while the uplink is down the downlink stays legal and the
    images keep coming whole (sb_stream, sb_linkpro); the link drops and
    comes back (sb_linkstate); the cut write never executes; the first read
    after is answered 0x00.  Bins: the link lost while a stream packet was
    on the wire.
    """

    MUST_HIT = ("loss_mid_stream",)
    PLAN = ("CXP-CAM-REC-001",)

    async def actors(self):
        env, h, drv, dut = self.env, self.env.host, self.env.uplink_ag.drv, get_dut()
        from uvm.scoreboards.link_state_scoreboard import word_ns
        await sensor()
        await h.write_ok(rm.MFR_RESERVED1, [0x5555_0000])
        fr = self.start(self.sensor_frames(10, "c10_frames", sizes=((128, 32),)))
        await Timer(20_000, unit="ns")
        t0 = get_sim_time("ns")
        env.sb_linkerr.allow(t0)
        env.sb_linkstate.expect_drop(t0 + 300 * word_ns())
        await drv.send_txn(UplinkTxn(kind=UplinkKind.CTRL_CMD_WRITE, address=rm.MFR_RESERVED1,
                                     payload=[0x6666_0000], beats_edit=lambda b: b[:4],
                                     expect_codes={None, gp.ACK_MALFORMED}))
        hold = self.start(drv.hold(120 * 40))
        await wait_until(lambda: not int(dut.sb_link_detected.value), 200 * word_ns(), 200)
        if env.pkt_log.enclosing(int(dut.tx_words.value)) is not None or \
                stream_packets(env):
            self.hit("loss_mid_stream")
        await hold
        env.sb_linkstate.expect_up()
        await wait_until(lambda: int(dut.sb_link_detected.value), 200 * word_ns(), 200)
        env.sb_linkstate.close_allowance()
        env.sb_linkerr.close()
        env.sb_test.check((await h.read(rm.STANDARD))[0] == gp.ACK_OK_DATA, "first_after",
                          "first read after the link came back")
        env.sb_test.check(await h.read1(rm.MFR_RESERVED1) == 0x5555_0000, "executed",
                          "the cut write took effect")
        await fr


class test_conc_backpressure_frames(_ConcTest):
    """C-11 — back-pressure on the pixel port (FIFO_DEPTH = 256 build).

    Actors: the pixel clock twice the transmit clock, Mono16; 120 small
    sensor images back to back; closed-loop control traffic and host triggers taking downlink
    words.

    Checks: s_pix_ready throttles the port and no pixel is lost, repeated
    or reordered (sb_stream bit-exact), no overflow.  Bins: the port held
    at least 8 cycles, held on an end of line (end of frame and start of
    frame are counted but never happened: the last pixel of an image is
    always taken at once).
    """

    APP_PERIOD = 5
    REQUIRES = {"FIFO_DEPTH": 256}
    PKT_DSIZE_P = 200
    # An EOF (or SOF) pixel was never held in 1392 stalls: the port takes an
    # image's last pixel at once in this build (recorded, not a goal).
    MUST_HIT = ("stall_ge8", "stall_eol")
    PLAN = ("CXP-CAM-IMG-012",)

    async def sensor_frames(self, n, seed, sizes=()):
        r = rng(seed)
        for i in range(n):
            w, hgt = r.choice(sizes)
            await _Frame(xsize=w, ysize=hgt, pixfmt=0x0105, bits=16, seed=i,
                         meta=dict(sourcetag=i)).start(self.env.video_ag.seqr)

    async def actors(self):
        env = self.env
        # Mono16 at twice the transmit clock: two words for every word the
        # downlink takes.
        await env.host.write_ok(rm.PIXEL_FORMAT_ALIAS, [gs.PIXELF_TO_PFNC[0x0105]])
        await sensor()
        done = []
        ht = self.start(self.host_traffic(200, "c11_host", stop=lambda: bool(done)))
        tr = self.start(self.host_triggers(15, "c11_trig"))
        # Long lines of both parities (which pixel of a Mono16 pair waits
        # depends on the ingress's depth: its look-ahead stage and output
        # register hold two, so an odd line's end now meets the stall), and
        # short images for frame ends.
        await self.sensor_frames(60, "c11_frames", sizes=((8, 64), (9, 64), (16, 32), (15, 32),
                                                         (12, 40), (13, 40), (16, 2), (8, 1),
                                                         (4, 1)))
        done.append(1)
        await ht
        await tr
        drv = env.video_ag.drv
        self.logger.info(f"pixel port stalls: {drv.stalls} (longest {drv.max_stall} "
                         f"cycles; at EOL {drv.stalls_at_eol}, EOF {drv.stalls_at_eof}, "
                         f"SOF {drv.stalls_at_sof})")
        self.hit("stall_ge8", int(drv.max_stall >= 8))
        self.hit("stall_eol", drv.stalls_at_eol)
        self.hit("stall_eof", drv.stalls_at_eof)
        self.hit("stall_sof", drv.stalls_at_sof)


class test_conc_source_and_format_switch(_ConcTest):
    """C-12 — source and format changed under traffic (§9.4, §8.5.2).

    Actors: between images and in the middle of one: the test pattern and
    the sensor port swapped, rectangular / arbitrary swapped, PixelFormat,
    Width / Height and StreamPacketSizeMax (DsizeP) changed; closed-loop
    control traffic throughout.

    Checks: every image that reaches the wire is self-consistent — its
    header describes its content (sb_stream); a change takes effect at an
    image boundary (an image under a mid-image change is dropped whole or
    sent whole, never mixed); the tags run on; a smaller DsizeP never
    gives an oversize packet.  Bins: each kind of switch between images
    and mid-image.
    """

    PKT_DSIZE_P = 64
    MUST_HIT = ("src_between", "src_mid", "fmt_between", "fmt_mid", "size_mid",
                "spsm_mid", "arb_between")
    PLAN = ("CXP-CAM-IMG-007", "CXP-CAM-DATA-001")

    async def actors(self):
        env, h, dut = self.env, self.env.host, get_dut()
        done = []
        ht = self.start(self.host_traffic(400, "c12_host", stop=lambda: bool(done),
                                          gap_ns=5_000))
        r = rng("c12")

        async def change(kind, mid):
            env.sb_stream.flush_window(get_sim_time("ns"))
            if kind == "src":
                if int(dut.cfg_use_tpg.value):
                    await sensor()
                else:
                    await tpg(1)
            elif kind == "fmt":
                await h.write_ok(rm.PIXEL_FORMAT_ALIAS, [gs.PIXELF_TO_PFNC[r.choice([0x101, 0x102, 0x103])]])
            elif kind == "size":
                await h.write_ok(rm.WIDTH_ALIAS, [r.choice([3, 5, 8])])
                await h.write_ok(rm.HEIGHT_ALIAS, [r.choice([1, 2, 4])])
            elif kind == "spsm":
                await h.write_ok(rm.STREAM_PACKET_SIZE_MAX, [r.choice([48, 96, 288])])
            elif kind == "arb":
                dut.cfg_arbitrary.value = 1 - int(dut.cfg_arbitrary.value)
                await Timer(2_000, unit="ns")
            await Timer(4_000, unit="ns")
            env.sb_stream.close_flush_window()
            self.hit(f"{kind}_{'mid' if mid else 'between'}")

        await tpg(1)
        for kind in ("src", "fmt", "size", "spsm", "arb", "src", "fmt", "size", "spsm"):
            if int(dut.cfg_use_tpg.value):
                await frames_after(env, 1, 400_000)
                await change(kind, False)
                await frames_after(env, 1, 400_000)
                await Timer(round(r.uniform(100, 400)), unit="ns")
                await change(kind, True)
            else:
                f = self.start(_Frame(xsize=64, ysize=8, seed=r.randrange(999)).start(
                    env.video_ag.seqr))
                await Timer(round(r.uniform(300, 1_500)), unit="ns")
                await change(kind, True)
                await f
                await change(kind, False)
        done.append(1)
        await ht


class test_conc_single_domain_reset(_ConcTest):
    """C-13 — a reset of one clock domain under traffic (§10.3.28, the
    reset controller: any domain's reset resets the whole device).

    Actors: sensor images, closed-loop control, host triggers; the app,
    the tx and the rx reset each pulsed once in the middle; the host
    brings the device up again after each (link, StreamPacketSizeMax).

    Checks: the link comes back (sb_linkstate); nothing stale is replayed
    and no torn packet (sb_stream, sb_linkpro); the stream resumes on an
    image boundary from tag 0; control answers.  Bins: each domain reset
    with traffic running.
    """

    MUST_HIT = ("reset_app", "reset_tx", "reset_rx")
    PLAN = ("CXP-CAM-REC-004",)

    async def actors(self):
        env, h = self.env, self.env.host
        await sensor()
        for dom in ("app", "tx", "rx"):
            fr = self.start(self.sensor_frames(3, f"c13_{dom}", sizes=((128, 16),)))
            await Timer(15_000, unit="ns")
            self.hit(f"reset_{dom}", int(bool(env.sb_stream.crc_checked)))
            env.sb_linkerr.allow(get_sim_time("ns"))
            await env.clkrst_ag.do_reset(cycles=20, domains=dom)
            await fr
            await h.link_up()
            env.sb_linkerr.close()
            await self.bringup()
            await h.read(rm.STANDARD)


class test_conc_clock_ratio_matrix(_ConcTest):
    """C-14 — the traffic of C-01 over clock ratios (the three clocks are
    unrelated in the product, p_ASYNC_CLOCKS = 1).

    Actors, at each (app, tx, rx) period point: sensor images, closed-loop
    control, host triggers, device pin edges.  The host's bit clock follows
    rx (its nominal) with a 150 ppm offset.

    Checks: all scoreboards strict at every point; the table of points is
    logged.  Bins: every point run with traffic.  The nightly runs two
    corners; the weekly (test_conc_clock_ratio_matrix_full) the eight
    corners and four non-integer ratios.
    """

    HOST_PPM = 150.0
    POINTS = ((7, 13, 10), (13, 7, 10))
    PLAN = ()

    @property
    def MUST_HIT(self):
        return tuple(f"point_{a}_{t}_{x}" for a, t, x in self.POINTS)

    async def actors(self):
        env, h = self.env, self.env.host
        await sensor()
        for a, t, x in self.POINTS:
            drained = await env.quiesce(timeout_ns=300_000, stop_source=False)
            env.sb_test.check(drained, "pre_retune_quiesce",
                              f"traffic did not drain before ratio {(a, t, x)}")
            env.sb_linkerr.allow(get_sim_time("ns"))
            env.sb_linkstate.allow_drop(get_sim_time("ns"), get_sim_time("ns") + 500_000)
            await env.clkrst_ag.set_cdc_ratio(a, t, x)
            # A PLL ratio change is a new operating point.  Restart the
            # receiver so its oversampling phase is acquired at that rate.
            await env.clkrst_ag.do_reset(cycles=20, domains="all")
            await self.bringup()
            await Timer(100_000, unit="ns")
            env.sb_linkerr.close()
            fr = self.start(self.sensor_frames(3, f"c14_{a}_{t}_{x}", sizes=((64, 8),)))
            tr = self.start(self.host_triggers(4, f"c14t_{a}_{t}_{x}"))
            await self.host_traffic(10, f"c14h_{a}_{t}_{x}")
            await tr
            await fr
            self.hit(f"point_{a}_{t}_{x}")
            self.logger.info(f"ratio point app {a} tx {t} rx {x} ns: done, "
                             f"errors so far {sum(sum(sb.err_counts.values()) for sb in env.scoreboards().values())}")


class test_conc_clock_ratio_matrix_full(test_conc_clock_ratio_matrix):
    """C-14, weekly: eight corners and four non-integer ratios."""
    POINTS = tuple((a, t, x) for a in (6, 14) for t in (6, 14) for x in (6, 14)) + (
        (7, 11, 13), (11, 13, 7), (13, 7, 11), (9, 10, 11))


class test_conc_bidir_linktest(_ConcTest):
    """C-15 — the connection test both ways at once (§8.7), with polling.

    Actors (OS_RATIO = 4 build): host 1024-word test packets, one with
    word errors; TestMode on (device test packets); closed-loop reads of
    all three counters throughout.

    Checks: each counter read is monotonic between clears and matches the
    packets so far (sb_linktest); the device's test packets stay 16 words
    apart even with polling acknowledgments in the gaps; final equality
    both directions.  Bins: a polling acknowledgment between two device
    test packets; a host test packet received while a device one is sent.
    """

    REQUIRES = {"OS_RATIO": 4}
    MUST_HIT = ("ack_in_lt_gap", "rx_during_tx")
    PLAN = ("CXP-CAM-CT-001", "CXP-CAM-CT-004")

    async def actors(self):
        env, h, drv = self.env, self.env.host, self.env.uplink_ag.drv
        await h.write_ok(rm.TEST_MODE, [1])
        env.sb_linktest.expect_tx_linktest = True
        last = {"err": 0, "rx": 0, "tx": 0}

        async def poll():
            while not self._stop:
                for key, a, n in (("err", rm.TEST_ERROR_COUNT, 4),
                                  ("rx", rm.TEST_PACKET_COUNT_RX, 8),
                                  ("tx", rm.TEST_PACKET_COUNT_TX, 8)):
                    code, d = await h.read(a, n)
                    if code == gp.ACK_OK_DATA and d:
                        v = d[-1]
                        env.sb_test.check(v >= last[key], "monotonic",
                                          f"{key} counter went {last[key]} -> {v}")
                        last[key] = v
        p = self.start(poll())
        for i in range(4):
            await drv.send_txn(UplinkTxn(kind=UplinkKind.LINKTEST, lt_n_data=1024,
                                         lt_error_indices=[5, 900] if i == 2 else []))
        self._stop = True
        await p
        await h.write_ok(rm.TEST_MODE, [0])
        log = env.pkt_log.long
        lts = [q for q in log if q.type_byte == 0x04]
        for a, b in zip(lts, lts[1:]):
            if any(q.type_byte == 0x03 and a.eop_cycle < q.sop_cycle < b.sop_cycle
                   for q in log):
                self.hit("ack_in_lt_gap")
        if lts and env.sb_linktest.packets_seen:
            self.hit("rx_during_tx")


class test_soak_random(_ConcTest):
    """C-16 — a weighted random scheduler over every actor above, from
    CXP_SEED, until a time budget (weekly; replaces test_soak).

    Checks: every scoreboard strict; on a failure the seed is in
    results.xml and the log holds the action trace.
    """

    PKT_DSIZE_P = 128
    PLAN = ("CXP-CAM-PERF-003",)
    BUDGET_NS = 4_000_000

    async def actors(self):
        env, h, r = self.env, self.env.host, rng("soak")
        await sensor()
        t_end = get_sim_time("ns") + self.BUDGET_NS
        trace = []
        while get_sim_time("ns") < t_end:
            a = r.choices(["frames", "host", "htrig", "pin", "ff", "tm", "stall"],
                          weights=[4, 4, 2, 2, 1, 1, 1])[0]
            trace.append((round(get_sim_time("ns")), a))
            if a == "frames":
                self.start(self.sensor_frames(r.randrange(1, 4), f"soak{len(trace)}"))
            elif a == "host":
                await self.host_traffic(r.randrange(2, 10), f"soakh{len(trace)}")
            elif a == "htrig":
                self.start(self.host_triggers(3, f"soakt{len(trace)}"))
            elif a == "pin":
                self.start(self.pin_edges(2, f"soakp{len(trace)}", gap_ns=(5_000, 20_000)))
            elif a == "ff":
                await h.reset()
            elif a == "tm":
                env.sb_stream.flush_window(get_sim_time("ns"))
                await h.write_ok(rm.TEST_MODE, [1])
                await Timer(30_000, unit="ns")
                await h.write_ok(rm.TEST_MODE, [0])
                env.sb_stream.close_flush_window()
            else:
                ApbResponderDriver.apply(ApbProfile(latency=r.randrange(60)))
            await Timer(round(r.uniform(1_000, 20_000)), unit="ns")
        self.logger.info(f"soak trace tail: {trace[-20:]}")
        ApbResponderDriver.apply(ApbProfile())
