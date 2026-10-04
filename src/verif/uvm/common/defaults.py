"""Default values poked into the DUT before any test starts.

The PyUVM env owns several level signals (cfg_*, ext_meta_*, ...) that
must be at sane values from t=0 so the DUT doesn't enter a weird
state during reset.  reset_defaults() runs once in build_phase before
clocks are started.
"""

from __future__ import annotations

from uvm.common.cxp_pkg import DEFAULT_PKT_DSIZE_P


def apply_defaults(dut, pkt_dsize: int = DEFAULT_PKT_DSIZE_P):
    """Drive every DUT input to a clean default value.

    Held by the env until an agent overrides.

    `pkt_dsize` is the initial cfg_dsizeP (32-bit words per stream
    packet).  The device uses it only while StreamPacketSizeMax reads 0,
    when it sends no stream packet at all; the size on the wire comes
    from the StreamPacketSizeMax the host writes (`CxpTopTest.bringup`).
    """
    dut.cfg_use_tpg.value        = 0
    dut.cfg_run.value            = 0
    dut.cfg_arbitrary.value      = 0
    dut.cfg_dsizeP.value         = int(pkt_dsize)
    dut.cfg_trig_polarity.value  = 0

    # §5.1 register-router strap — master-link path (writes permitted) by
    # default; test_router_reject flips it to exercise the 0x43 rejection.
    dut.from_extension_link.value = 0

    # Local device->host I/O sources (trigger_in_app drives cxp_tx_trigger_hs).
    dut.trigger_in_app.value     = 0

    dut.s_pix_data.value         = 0
    dut.s_pix_valid.value        = 0
    dut.s_pix_sof.value          = 0
    dut.s_pix_eol.value          = 0
    dut.s_pix_eof.value          = 0

    dut.ext_meta_xsize.value     = 0
    dut.ext_meta_ysize.value     = 0
    dut.ext_meta_xoffs.value     = 0
    dut.ext_meta_yoffs.value     = 0
    dut.ext_meta_pixfmt.value    = 0
    dut.ext_meta_tapg.value      = 0
    dut.ext_meta_streamid.value  = 0
    dut.ext_meta_sourcetag.value = 0
    dut.ext_meta_flags.value     = 0

    dut.rx_serial.value          = 1

    # User-window slave: answers at once, no error.
    dut.usr_latency.value        = 0
    dut.usr_slverr.value         = 0

    # Resets default LOW so reset() can release them; clock-enables default
    # HIGH so clocks run from t=0 (gating is only used by vs_cdc_sweep).
    dut.app_rst_n.value          = 0
    dut.tx_rst_n.value           = 0
    dut.rx_rst_n.value           = 0
    dut.app_clk_en.value         = 1
    dut.tx_clk_en.value          = 1
    dut.rx_clk_en.value          = 1
