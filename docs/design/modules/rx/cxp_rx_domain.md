# cxp_rx_domain

Inputs chosen from the tree: RTL `src/rtl/rx/cxp_rx_domain.sv` (children `cxp_rx_link`, `cxp_ctrl_plane`); integration TBs `src/tb_unit/top/cxp_interface_top/`, `src/tb_unit/top/cxp_device_top/`, `src/verif/`; spec JIIA CXP-001-2015 v1.1.1 §8.2, §8.3.2 Table 15, §8.6, §10.3.28; output `docs/design/modules/rx/cxp_rx_domain.md`.

Everything of `cxp_interface_top` that runs on `rx_clk` (the uplink's oversampling clock): the low-speed uplink receiver and the control plane. It assembles the top's `sb_status`. Created 2026-09-27 by moving the two instances out of `cxp_interface_top` unchanged.

```
rx_serial_i -> cxp_rx_link --> trig_o (host trigger), trig_rcvd_o / ioack_rcvd_o (to tx),
                  |           TestErrorCount, TestPacketCountRx, error pulses
                  v
            long packets -> cxp_ctrl_plane --> reg_*_o (register file), apb_*_o (user window),
                                               rsp_valid_o / rsp_o (one response per command)
```

Source: `src/rtl/rx/cxp_rx_domain.sv`, one instance `cxp_interface_top.cxp_rx_domain_i`.

## Interface

| Name | Default | Meaning |
|---|---|---|
| `p_OS_RATIO`, `p_SAMP_LOCK_HITS`, `p_RX_LOSS_WORDS` | 16, 2, 20 000 | Passed to `cxp_rx_link`. |
| `p_CTRL_BUF_DEPTH`, `p_USER_BASE`, `p_USER_SIZE`, `p_RX_CLK_KHZ` | 64, 0, 0xFFFF_FFFF, 20 833·`p_OS_RATIO` | Passed to `cxp_ctrl_plane` (buffer depth, user window, the clock the command time limits count). |

| Name | Dir | Width | Description |
|---|---|---|---|
| `rx_clk`, `rx_rst_n` | in | 1 | Uplink clock, reset released on `rx_clk`. |
| `rbuf_clk` | in | 1 | Read clock of the control plane's read buffer: `tx_clk` when the clocks are unrelated, `rx_clk` otherwise (`cxp_cdc_layer.rbuf_clk_o`). |
| `cfg_trig_polarity_i`, `ext_link_i`, `crst_i` | in | 1 | Trigger sense; §5.1 extension-link strap (no host trigger; ConnectionReset / MasterHostConnectionID writes ignored); ConnectionReset level (the host's trigger is de-asserted). All uncrossed, `rx_clk`. |
| `clr_lt_err_i`, `clr_lt_pkt_i` | in | 1 | Clears of TestErrorCount / TestPacketCountRx. |
| `rx_serial_i` | in | 1 | LS uplink bit. |
| `trig_o`, `trig_glitch_o` | out | 1 | Host trigger rebuilt (Table 15, Figure 20), rejected packet. |
| `trig_rcvd_o`, `ioack_rcvd_o` | out | 1 | Table 15 packet taken; Table 17 acknowledgment of a device trigger (pulses, crossed to `tx_clk`). |
| `lt_pkt_count_tx_i` | in | 64 | TestPacketCountTx, crossed from `tx_clk`, put into `status_o`. |
| `status_o` | out | `cxp_status_t` | Lock, link, Detected, the three test counters, control-reset / refusal pulses, framing and 8B/10B error pulses. |
| `reg_*`, `apb_*` | out / in | — | Register-file port and APB3 user window of the control plane. |
| `rbuf_addr_i`, `rbuf_data_o` | in / out | `$clog2(p_CTRL_BUF_DEPTH)+1`, 32 | Read-buffer port (on `rbuf_clk`). |
| `rsp_valid_o`, `rsp_o`, `rsp_ready_i` | out / out / in | 1, `cxp_ctrl_rsp_t`, 1 | One held response per command, taken by the crossing layer. |

## How it works

`cxp_rx_link` samples and decodes the uplink, detects the link, takes the Table 15 triggers and Table 17 acknowledgments out, checks host test packets and hands every long packet to `cxp_ctrl_plane`, which parses commands and executes them on the register bus or the user window (`cxp_rx_link.md`, `cxp_ctrl_plane.md`).

**Two clocks.** The block is single-clock except for the read port of the control plane's two-bank read buffer inside `cxp_ctrl_bus_master`: written on `rx_clk`, read by the acknowledgment framer on `rbuf_clk`. The bank that holds a response travels with it through `cxp_cdc_req`, and the executor starts the next read into the other bank, so a bank is never written while it is read. Moving the buffer into `cxp_cdc_layer` would change the unit-tested ports of `cxp_ctrl_bus_master` / `cxp_ctrl_plane`; it stays (Left open in `docs/reviews/2609/review_K7_260927.md`).

## Verification

No bench of its own; covered unchanged by `src/tb_unit/top/cxp_interface_top`, `src/tb_unit/top/cxp_device_top`, `src/tb_unit/rx/cxp_rx_link` (which wraps `cxp_rx_link` and `cxp_ctrl_plane` the same way), the PyUVM tiers and the emulator campaign. Split proven by identical top-level traces of all 30 benches (fixed seed).

## Known issues and recommendations

None new; see `cxp_rx_link.md` and `cxp_ctrl_plane.md`.
