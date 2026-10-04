"""Host uplink driver for the cocotb testbenches.

Serializes a CoaXPress character stream onto the device's `rx_serial`
pin, 8B/10B-encoded with the golden encoder (running disparity carried
across calls).  Between queued characters it keeps the line alive with
IDLE words, as a host does (§8.2.5.1), so the soft sampler never loses
lock.

    up = Uplink(dut, dut.rx_clk, dut.rx_serial, os_ratio=8)
    up.start()
    await up.send(packets.ctrl_cmd(...))       # beats or characters
    await up.idle(10)

All packet content comes from `cxp_protocol`; pass `q=cxp_protocol.DEVICE`
to the codecs for the RTL's current wire format.

Two things the wire does that a queue of whole packets cannot express:

* **Insertion** (§8.2.4).  `insert()` queues characters — a Table 15
  trigger, a Table 17 I/O acknowledgment — that go out at the *next*
  character boundary, splitting whatever long packet is in flight.
  `insert(..., word=True)` waits for the next word boundary instead, as
  §8.2.4 asks of everything but the low-speed trigger (the I/O
  acknowledgment).

* **A clock of its own.**  With `bit_ps` the bit period comes from a
  `Timer` instead of `os_ratio` edges of `clk`, so `ppm`, `phase_ps` and
  `jitter_ui` are expressible and the host is no longer locked to the
  device's sampling clock.  Without it the driver paces on `clk` exactly
  as before.
"""

from __future__ import annotations

import random
from collections import deque
from typing import Iterable, List, Optional, Sequence, Tuple, Union

import cocotb
from cocotb.triggers import Event, RisingEdge, Timer
from cocotb.utils import get_sim_time

from cxp_protocol import enc8b10b, packets

Beat = Tuple[int, int]
Char = Tuple[int, bool]
_IDLE_CHARS = packets.beats_to_chars([packets.IDLE])
INVALID_SYM = 0x06B


class Uplink:
    def __init__(self, dut, clk, pin, os_ratio: int = 16, rd: int = 0,
                 bit_ps: Optional[float] = None, ppm: float = 0.0,
                 phase_ps: float = 0.0, jitter_ui: float = 0.0,
                 rng: Optional[random.Random] = None):
        self.dut, self.clk, self.pin, self.os = dut, clk, pin, os_ratio
        self.rd = rd
        self.ppm = ppm
        self.phase_ps = phase_ps
        self.jitter_ui = jitter_ui
        self._nominal_ps = bit_ps
        self._rng = rng or random.Random(0)
        self._q: deque = deque()          # (chars, done Event, corrupt, flip)
        self._ins: deque = deque()        # §8.2.4 insertion lane (characters)
        self.chars_sent = 0
        self.chars_inserted = 0
        self._slip = 0
        self._t_ideal = 0.0             # ideal bit grid (ps from the start)
        self._t_edge = 0.0              # where the last edge actually went
        self._task = None

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self.pin.value = 0
        self._task = cocotb.start_soon(self._run())
        return self

    def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @property
    def bit_ps(self) -> Optional[float]:
        """Current bit period in ps, `ppm` applied; None = clock-paced."""
        if self._nominal_ps is None:
            return None
        return self._nominal_ps / (1.0 + self.ppm * 1e-6)

    def retune(self, bit_ps: Optional[float]) -> None:
        """Change the nominal bit period (a clock retune under the host)."""
        self._nominal_ps = bit_ps

    @staticmethod
    def to_chars(items: Union[Sequence[Beat], Sequence[Char]]) -> List[Char]:
        items = list(items)
        if items and isinstance(items[0][1], bool):
            return items                   # already characters
        return packets.beats_to_chars(items)

    # -- queueing ----------------------------------------------------------
    async def send(self, items, corrupt_sym_at: int = -1, flip_rd_at: int = -1,
                   times: Optional[dict] = None):
        """Queue `items` (beats or characters) and wait until sent.

        corrupt_sym_at: index of a character sent as a code group no
        running disparity makes (a code error); flip_rd_at: index after which the running
        disparity is flipped (disparity errors on the next symbols).
        `times`, if given, gets "start" / "done": the sim time (ns) the
        first character started and the last one left the pin."""
        done = Event()
        self._q.append((self.to_chars(items), done, corrupt_sym_at, flip_rd_at, times))
        await done.wait()

    def post(self, items, corrupt_sym_at: int = -1, flip_rd_at: int = -1,
             times: Optional[dict] = None) -> Event:
        """`send` without waiting — returns the Event that fires when the
        last character of `items` has left the pin."""
        done = Event()
        self._q.append((self.to_chars(items), done, corrupt_sym_at, flip_rd_at, times))
        return done

    def insert(self, items, word: bool = False, times: Optional[dict] = None,
               corrupt_sym_at: int = -1) -> Event:
        """Queue characters for the §8.2.4 insertion lane.

        They are emitted at the next character boundary — inside a long
        packet if one is in flight — or at the next word boundary when
        `word` is set.  The returned Event fires once the
        last of them has left the pin; `times` and `corrupt_sym_at` as
        for `send`.
        """
        chars = self.to_chars(items)
        done = Event()
        if not chars:
            done.set()
            return done
        self._ins.append((chars, done, word, times, corrupt_sym_at))
        return done

    async def idle(self, n: int = 1):
        await self.send(_IDLE_CHARS * n)

    async def hold(self, bits: int, level: int = 0):
        """After what is queued, hold the pin at `level` for `bits` bit
        times (the host stops sending), then carry on."""
        done = Event()
        self._q.append(("hold", done, int(bits), int(level), None))
        await done.wait()

    async def slip(self, bits: int = 1):
        """After what is queued, drop `bits` bits of the next character
        (or, negative, send its first bit that many extra times): the line
        slips against the character boundary."""
        done = Event()
        self._q.append(("slip", done, int(bits), 0, None))
        await done.wait()

    # -- the wire ----------------------------------------------------------
    async def _bit(self, b: int):
        self.pin.value = b
        period = self.bit_ps
        if period is None:
            for _ in range(self.os):
                await RisingEdge(self.clk)
            return
        # Jitter moves each edge around its place on the ideal bit grid
        # (uniform +/- jitter_ui); it does not accumulate.  Varying the
        # period instead would make the edges wander without bound.
        self._t_ideal += period
        edge = self._t_ideal
        if self.jitter_ui:
            edge += self._rng.uniform(-self.jitter_ui, self.jitter_ui) * period
        wait = max(1, round(edge - self._t_edge))
        self._t_edge += wait
        await Timer(wait, unit="ps")

    async def _char(self, byte: int, k: bool, corrupt=False, flip=False):
        sym, self.rd = enc8b10b.encode_byte(byte, k, self.rd)
        if corrupt:
            # A code group no RD makes (0x06B, balanced, no comma-like run):
            # a code error, the running disparity unchanged.  Inverting the
            # symbol would give a legal group of the other disparity — a
            # disparity error, not a code error.
            sym = INVALID_SYM
        if flip:
            self.rd ^= 1
        first = 0
        if self._slip > 0:
            first, self._slip = min(self._slip, 9), 0
        elif self._slip < 0:
            for _ in range(-self._slip):
                await self._bit(sym & 1)
            self._slip = 0
        for i in range(first, 10):
            await self._bit((sym >> i) & 1)
        self.chars_sent += 1

    async def _run(self):
        if self.phase_ps:
            await Timer(max(1, round(self.phase_ps)), unit="ps")
        cur = None                  # [chars, done, corrupt_at, flip_at, index]
        ins = None                  # [chars, done, index] being inserted
        idle: List[Char] = []
        word_pos = 0
        while True:
            # 1. §8.2.4 insertion lane — pre-empts whatever is in flight.
            #    A group starts on a legal boundary and then runs to its
            #    end; re-testing the boundary per character would spread
            #    the group over the words it is supposed to interrupt.
            if ins is None and self._ins and (
                    not self._ins[0][2] or word_pos % 4 == 0):
                chars, done, wgrp, times, cor = self._ins.popleft()
                ins = [chars, done, 0, times, cor, wgrp]
                if times is not None:
                    times["start"] = get_sim_time("ns")
            if ins is not None:
                chars, done, i, times, cor, wgrp = ins
                b, k = chars[i]
                await self._char(b, k, corrupt=(i == cor))
                self.chars_inserted += 1
                # A Table 15 trigger is taken out of the stream by the
                # receiver: its characters do not move the word grid of the
                # packet it sits in.  A word-aligned group (the I/O
                # acknowledgment) is whole words and keeps it too.
                if wgrp:
                    word_pos += 1
                ins[2] = i + 1
                if ins[2] == len(chars):
                    if times is not None:
                        times["done"] = get_sim_time("ns")
                    done.set()
                    ins = None
                continue
            # 2. the packet in flight, one character at a time.  A *new*
            #    packet starts only on a word boundary: characters are
            #    grouped into words (§8.2.1) and only a Table 15 trigger
            #    is inserted between two of them, so a packet that began
            #    mid-word would be framed one lane out by the receiver.
            if cur is None and self._q and self._q[0][0] in ("hold", "slip"):
                what, done, n, level, _ = self._q.popleft()
                if what == "hold":
                    for _ in range(n):
                        await self._bit(level)
                    word_pos = 0
                else:
                    self._slip = n
                done.set()
                continue
            if cur is None and self._q and word_pos % 4 == 0:
                chars, done, cor, flip, times = self._q.popleft()
                cur = [chars, done, cor, flip, 0, times]
                if times is not None:
                    times["start"] = get_sim_time("ns")
            if cur is not None:
                chars, done, cor, flip, i, times = cur
                b, k = chars[i]
                await self._char(b, k, corrupt=(i == cor), flip=(i == flip))
                cur[4] = i + 1
                word_pos += 1
                if cur[4] == len(chars):
                    if times is not None:
                        times["done"] = get_sim_time("ns")
                    done.set()
                    cur = None
                continue
            # 3. IDLE keep-alive (§8.2.5.1), also one character at a time so
            #    an insertion can split it.
            if not idle:
                idle = list(_IDLE_CHARS)
                word_pos = 0        # the comma of an IDLE starts a word
            b, k = idle.pop(0)
            await self._char(b, k)
            word_pos += 1
