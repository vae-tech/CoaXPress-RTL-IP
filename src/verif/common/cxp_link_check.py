"""Downlink teardown checks for the device-level testbenches.

Two properties hold over the whole downlink word bus, in every test, and
neither was asserted anywhere before:

* the golden deframer must report no framing error — a SOP inside a
  packet, an EOP outside one, or a word that belongs to no packet, i.e.
  exactly what a torn packet looks like to a host;
* no run of more than 99 consecutive non-IDLE words (§8.2.5.1), the
  cadence a host's receiver needs to stay locked.

`cxp_device_top` runs a `Host` (`cxp_host.py`), whose deframer already
counts both; `cxp_interface_top` has no host model, so `WireMonitor`
feeds the same golden deframer straight from the word bus.

Used once per test, from the bench's setup helper::

    from cxp_link_check import WireMonitor, check_downlink
    from cxp_testcase import at_teardown

    mon = WireMonitor(dut.clk, dut.cxp_if_data_o, dut.cxp_if_kmask_o).start()
    at_teardown(lambda: check_downlink(mon))

`cxp_testcase.cxp_test` runs the registered checks after the test body,
and only when the body passed, so a teardown check never hides the
failure that produced it.
"""

from __future__ import annotations

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from cxp_protocol import packets as gp


# §8.2.5.1: at most 99 non-IDLE words between IDLE words on a high-speed
# connection.
MAX_RUN = 99


class WireMonitor:
    """Feed a golden `Deframer` from a raw downlink word bus.

    Samples `data` / `kmask` at ReadOnly after every rising edge of
    `clk`, so the word driven in a cycle is the word recorded for it.
    Start it after reset release — an undriven bus before that would be
    deframed as stray words.
    """

    def __init__(self, clk, data, kmask, deframer: gp.Deframer | None = None):
        self.clk, self.data, self.kmask = clk, data, kmask
        self.deframer = deframer or gp.Deframer()
        self.words = 0
        self._task = None

    def start(self) -> "WireMonitor":
        self._task = cocotb.start_soon(self._run())
        return self

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self):
        while True:
            await RisingEdge(self.clk)
            await ReadOnly()
            self.words += 1
            self.deframer.push(int(self.data.value), int(self.kmask.value))


def _deframer(source) -> gp.Deframer:
    """Accept a `Host`, a `WireMonitor` or a `Deframer` itself."""
    return getattr(source, "deframer", source)


def check_downlink(source, max_run: int = MAX_RUN) -> None:
    """Assert the two downlink properties on `source`'s deframer."""
    d = _deframer(source)
    assert not d.errors, (
        f"downlink framing error(s) from the golden deframer: "
        + "; ".join(d.errors[:8])
        + (f" (+{len(d.errors) - 8} more)" if len(d.errors) > 8 else "")
    )
    assert d.max_run <= max_run, (
        f"{d.max_run} consecutive non-IDLE downlink words, §8.2.5.1 allows "
        f"at most {max_run}"
    )


def split_short_packets(words) -> list[str]:
    """Two-word packets (Tables 16 / 17) torn apart on a raw word list.

    `words` is a list of (data, kmask).  Every trigger leader (4 x K28.2 or
    4 x K28.4) and every I/O-acknowledgment leader (4 x K28.6) must be
    followed on the very next word by its data word (kmask 0, four equal
    bytes).  Returns one line per violation, empty when all are intact.
    """
    leaders = {gp.rep4(gp.K28_2): "trigger", gp.rep4(gp.K28_4): "trigger",
               gp.rep4(gp.K28_6): "I/O ack"}
    bad = []
    for i, (w, k) in enumerate(words[:-1]):
        if k == 0xF and w in leaders:
            nw, nk = words[i + 1]
            b = nw & 0xFF
            if nk != 0 or nw != b * 0x01010101:
                bad.append(f"{leaders[w]} leader at word {i} followed by "
                           f"{nw:08x}/{nk:x}")
    return bad
