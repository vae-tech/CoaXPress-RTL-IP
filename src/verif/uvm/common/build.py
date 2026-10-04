"""Build parameters of the TB shell, as the Python side sees them.

`src/verif/Makefile` passes each knob to Verilator as a `-G` parameter of
`tb_cxp_top` *and* exports it as `CXP_<NAME>`, so the model and the
environment always agree; a knob selects its own `sim_build_<cfg>`
directory (rule: one build per parameter set).

    OS_RATIO       uplink oversampling ratio (cxp_rx_link p_OS_RATIO)
    RX_CLK_KHZ     the device's millisecond: RX_CLK_KHZ rx_clk cycles
                   (Wait after 100 ms, timeout at 900 ms)
    RX_LOSS_WORDS  words without IDLE before the link is lost
    FIFO_DEPTH     stream FIFO depth in words
"""

from __future__ import annotations

import os


def _knob(name: str, default: int) -> int:
    v = os.environ.get(f"CXP_{name}", "")
    return int(v) if v.strip() else default


OS_RATIO = _knob("OS_RATIO", 16)
RX_CLK_KHZ = _knob("RX_CLK_KHZ", 20)
RX_LOSS_WORDS = _knob("RX_LOSS_WORDS", 20_000)
FIFO_DEPTH = _knob("FIFO_DEPTH", 1024)
