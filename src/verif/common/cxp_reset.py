"""One-sided reset helper shared by the benches.

A multi-clock block has one asynchronous reset per domain.  The
interesting case is a reset that hits one domain while the others keep
running: `pulse_reset` asserts one active-low reset for a number of edges
of that domain's clock and releases it just after an edge, as a
synchroniser in front of the reset pin would.
"""

from __future__ import annotations

from cocotb.triggers import FallingEdge, RisingEdge


async def pulse_reset(rst_n, clk, cycles: int = 3) -> None:
    """Hold ``rst_n`` low for ``cycles`` rising edges of ``clk``.

    The reset is asserted at a falling edge (away from the sampling edge)
    and released at the falling edge after the last counted rising edge,
    so the release never races a flop of the same domain."""
    await FallingEdge(clk)
    rst_n.value = 0
    for _ in range(cycles):
        await RisingEdge(clk)
    await FallingEdge(clk)
    rst_n.value = 1
