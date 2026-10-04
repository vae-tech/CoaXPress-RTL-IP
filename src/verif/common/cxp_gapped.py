"""Drive a word/valid bus the way the real uplink does: with gaps.

Every RX-side bench used to present one word per clock with `valid` tied
high for the whole sequence.  The low-speed uplink delivers one word per
40 x `p_OS_RATIO` clocks, so in the device the bus is idle almost all the
time — and whatever a bench leaves on `data` / `kmask` during those
cycles is stale from the previous word, not zero.  A module that samples
its input bus without looking at `valid` passes the back-to-back bench
and fails on real traffic.

`GappedDriver` inserts a random gap before every word and drives stale
or random junk on the bus (including the K-code mask) while `valid` is
low, so a `valid`-blind sample shows up as a wrong or extra event.

Sequence form, sampling one cycle per word::

    drv = GappedDriver(dut.rx_clk, dut.d_valid, dut.d_in, dut.d_kmask)
    async for _ in drv.words(seq):
        await ReadOnly()
        capture(...)

Each iteration hands control back during the cycle in which that word is
on the bus with `valid` high, so capture index i still belongs to
`seq[i]` whatever the gaps did.  `max_gap = 0` reproduces the
back-to-back behaviour exactly, which is how a bench is converted one
test at a time.

Sampling only the valid cycles would not catch a `valid`-blind DUT — the
junk it reacted to sits in the gaps.  Pass `idle_check`, a zero-argument
callable run at ReadOnly of every gap cycle, to assert the outputs are
quiet there; that is the check the gaps exist for.
"""

from __future__ import annotations

import random
from typing import Callable, Iterable, Optional, Sequence, Tuple

from cocotb.triggers import NextTimeStep, ReadOnly, RisingEdge


Beat = Tuple[int, int]


class GappedDriver:
    """`valid` / `data` [/ `kmask`] driver with randomised idle gaps."""

    def __init__(self, clk, valid, data, kmask=None, *,
                 max_gap: int = 7, seed: int = 0xC0FFEE,
                 idle_check: Optional[Callable[[], None]] = None,
                 data_bits: int = 32, kmask_bits: int = 4):
        self.clk, self.valid, self.data, self.kmask = clk, valid, data, kmask
        self.max_gap = max_gap
        self.idle_check = idle_check
        self.rng = random.Random(seed)
        self.data_mask = (1 << data_bits) - 1
        self.kmask_mask = (1 << kmask_bits) - 1
        self._last: Beat = (0, 0)

    # -- bus ------------------------------------------------------------
    def _drive(self, data: int, kmask: int, valid: int) -> None:
        self.data.value = data & self.data_mask
        if self.kmask is not None:
            self.kmask.value = kmask & self.kmask_mask
        self.valid.value = valid

    def _junk(self) -> Beat:
        """Stale (the last word) or fresh random garbage, 50/50."""
        if self.rng.random() < 0.5:
            return self._last
        return (self.rng.getrandbits(32) & self.data_mask,
                self.rng.getrandbits(4) & self.kmask_mask)

    async def idle(self, cycles: int) -> None:
        """Hold `valid` low for `cycles`, with junk on the bus.

        `idle_check` runs at ReadOnly of each of those cycles.
        """
        for _ in range(cycles):
            d, k = self._junk()
            self._drive(d, k, 0)
            if self.idle_check is not None:
                await ReadOnly()
                self.idle_check()
                await NextTimeStep()
            await RisingEdge(self.clk)

    # -- sequences ------------------------------------------------------
    async def words(self, seq: Sequence[Beat] | Iterable[Beat]):
        """Async-iterate `seq`, yielding once per word at its valid cycle."""
        for data, kmask in seq:
            await self.idle(self.rng.randint(0, self.max_gap))
            self._drive(data, kmask, 1)
            self._last = (data, kmask)
            yield (data, kmask)
            await NextTimeStep()
            await RisingEdge(self.clk)
        # Leave the bus as the DUT will normally see it: quiet, but not
        # cleared — the last word stays on it.
        self._drive(self._last[0], self._last[1], 0)
        await RisingEdge(self.clk)

    async def send(self, seq: Sequence[Beat] | Iterable[Beat]) -> None:
        """Drive `seq` with gaps, sampling nothing."""
        async for _ in self.words(seq):
            pass
