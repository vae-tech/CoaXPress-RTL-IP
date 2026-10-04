# cxp_cdc_layer

Inputs chosen from the tree: RTL `src/rtl/cdc/cxp_cdc_layer.sv` (children `cxp_cdc_sync`, `cxp_cdc_pulse`, `cxp_cdc_bus`, `cxp_cdc_req`, `cxp_cdc_stream_fifo`); lint waivers `src/rtl/cxp_ip.vlt`; multi-clock TB `src/tb_unit/top/cxp_device_top/` (`p_ASYNC_CLOCKS` = 1, three unrelated clocks); integration TB `src/tb_unit/top/cxp_interface_top/` (`p_ASYNC_CLOCKS` = 0); `src/verif/` (both builds); output `docs/design/modules/cdc/cxp_cdc_layer.md`.

The only block of `cxp_interface_top` on more than one clock: every crossing between `rx_clk` (register file, control plane, uplink), `app_clk` (pixel path) and `tx_clk` (downlink), and the stream FIFO. Created 2026-09-27 by moving the `g_cdc` / `g_tied` block of `cxp_interface_top` and the stream FIFO (from `cxp_stream_top`) unchanged; the instance names are the ones they had.

Source: `src/rtl/cdc/cxp_cdc_layer.sv`, one instance `cxp_interface_top.cxp_cdc_layer_i`.

## Interface

| Name | Default | Meaning |
|---|---|---|
| `p_FIFO_DEPTH` | 1024 | Stream FIFO depth (words), power of two ≥ 4 (checked in `cxp_cdc_stream_fifo`), ≥ 16 (checked in `cxp_app_stream`). |
| `p_ASYNC_CLOCKS` | 0 | 0: the clocks are one (or phase-locked) and every crossing but the FIFO is a wire (`g_tied`). 1: every crossing is a `cxp_cdc_*` primitive (`g_cdc`) and the read buffer is read on `tx_clk`. |

Ports are grouped by the clock they belong to: `rx_clk` (`cfg_i`, the events, `crst_i` / `crst_done_o`, the response, `lt_pkt_count_o`, `rbuf_clk_o`), `app_clk` (`app_*`: configuration, acquisition events, FIFO write side, flush view) and `tx_clk` (`tx_*`: configuration, ConnectionReset and its echo, events, link, response, TestPacketCountTx, FIFO read side, flush request / acknowledgment).

## Crossings

| From → to | What | `g_cdc` primitive |
|---|---|---|
| rx → app | `cxp_cfg_app_t` (built here from `cfg_i`) | `cxp_cdc_bus_cfg_app_i` |
| rx → tx | `cxp_cfg_tx_t` (TestMode, trigger sense, stream enable) | `cxp_cdc_bus_cfg_tx_i` |
| rx → app | AcquisitionStart / AcquisitionStop | `cxp_cdc_pulse_acq_start_i`, `_acq_stop_i` |
| rx → tx | ConnectionReset level | `cxp_cdc_sync_crst_i` |
| tx → rx | ConnectionReset applied and flushed | `cxp_cdc_sync_crst_done_i` |
| rx → tx | ConnectionConfig written, TestPacketCountTx clear, host trigger received, device trigger acknowledged | four `cxp_cdc_pulse` |
| rx → tx | uplink detected | `cxp_cdc_sync_link_i` |
| rx → tx | control response (`cxp_ctrl_rsp_t`), held until the framer is idle | `cxp_cdc_req_rsp_i` |
| tx → rx | TestPacketCountTx (64 bits) | `cxp_cdc_bus_lt_pkt_tx_i` |
| app → tx | stream words with packet boundaries, length and StreamID; flush request / acknowledgment | `cxp_cdc_stream_fifo` (gray pointers, both builds) |

`cfg_i.ext_link` does not cross (used on `rx_clk` only); with `p_ASYNC_CLOCKS` = 0, `rx_rst_n` has no user. Both waived in `cxp_ip.vlt`.

## Verification

No bench of its own; the `g_cdc` build carries real traffic in `cxp_device_top` (rx 10 ns, tx 8 ns, app 12 ns; 38 tests) and the PyUVM tiers, the `g_tied` build in `cxp_interface_top` (one clock). The primitives have `src/tb_unit/cdc/cxp_cdc`, the FIFO `src/tb_unit/cdc/cxp_cdc_stream_fifo`. Split proven by identical top-level traces of all 30 benches (fixed seed).

## Known issues and recommendations

- 13 primitives plus the FIFO (and the trigger pin's synchroniser in `cxp_tx_trigger_hs`, the read buffer inside the control plane). The state review's "double `stream_en` crossing" is not dead: the app side stops new images (`cxp_app_acq_ctrl`), the tx side drops the rest of an image in flight when the host writes StreamPacketSizeMax = 0 directly (Table 44) and holds the PacketTags at 0.
