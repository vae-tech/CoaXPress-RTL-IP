# cxp_tx_domain

Inputs chosen from the tree: RTL `src/rtl/tx/cxp_tx_domain.sv` (children `cxp_tx_stream_pkt`, `cxp_tx_ctrl_ack`, `cxp_tx_linktest`, `cxp_tx_trigger_hs`, `cxp_tx_io_ack`, `cxp_tx_arbiter`, `cxp_tx_inserter`); bound SVA `src/sva/cxp_sva.sv` (`cxp_tx_owner_sva`, `cxp_idle_rule_sva` bound here); lint waivers `src/rtl/cxp_ip.vlt`; integration TBs `src/tb_unit/top/cxp_interface_top/`, `src/tb_unit/top/cxp_device_top/`, `src/tb_unit/tx/cxp_tx_arbiter/` (arbiter + inserter only), `src/verif/`; spec JIIA CXP-001-2015 v1.1.1 §8.2.4 Table 13, §8.2.5, §8.3.2 Table 16, §8.3.3 Table 17, §8.5.3, §8.7.4, §10.3.28; output `docs/design/modules/tx/cxp_tx_domain.md`.

Everything of `cxp_interface_top` that runs on `tx_clk`: the five packet sources, the long-packet arbiter, the inserter that puts the two-word packets and the IDLE words on the wire, and the stream flush request. Every input from another clock arrives already crossed by `cxp_cdc_layer`. Created 2026-09-27 by moving the `tx_clk` logic of `cxp_interface_top` unchanged.

```
stream FIFO read side -> cxp_tx_stream_pkt  --,
control response      -> cxp_tx_ctrl_ack    --+-> cxp_tx_arbiter (control ack > test > stream)
TestMode              -> cxp_tx_linktest    --'          |
trigger pin           -> cxp_tx_trigger_hs  ------> cxp_tx_inserter (+ IDLE) -> m_data_o / m_kmask_o
host trigger received -> cxp_tx_io_ack      ------>      (registered)
```

Source: `src/rtl/tx/cxp_tx_domain.sv`, one instance `cxp_interface_top.cxp_tx_domain_i`.

## Interface

| Name | Default | Meaning |
|---|---|---|
| `p_CTRL_BUF_DEPTH` | 64 | Control read-buffer depth (words); sets `rbuf_addr_o`'s width. |
| `p_TRIG_ACK_TIMEOUT` | `TRIG_ACK_TIMEOUT` 4096 | `tx_clk` cycles a device trigger waits for the host's I/O acknowledgment (§8.3.3). |

Elaboration check: `LT_GAP_WORDS` ≥ 16 (§8.7.4: at least 16 word intervals between connection-test packets), `g_chk_lt_gap`.

| Name | Dir | Width | Description |
|---|---|---|---|
| `tx_clk`, `tx_rst_n` | in | 1 | Downlink word clock, reset released on `tx_clk`. |
| `cfg_i` | in | `cxp_cfg_tx_t` | TestMode, trigger sense, stream enable, crossed. |
| `crst_i` / `crst_done_o` | in / out | 1 | ConnectionReset level; `crst_i & flush_ack_i`, echoed to `rx_clk` as `conn_reset_done`. |
| `conn_cfg_wr_i`, `clr_lt_pkt_i` | in | 1 | ConnectionConfig written; host clear of TestPacketCountTx (pulses). |
| `lt_pkt_count_o` | out | 64 | TestPacketCountTx. |
| `link_i` | in | 1 | Uplink detected: device triggers are sent only while it is 1. |
| `trig_pin_i` | in | 1 | Device trigger pin, any clock (synchronised in `cxp_tx_trigger_hs`). |
| `trig_rcvd_i`, `ioack_rcvd_i` | in | 1 | Host trigger received (send an I/O ack); host acknowledged a device trigger. |
| `rsp_valid_i`, `rsp_i`, `ack_busy_o` | in / in / out | 1, `cxp_ctrl_rsp_t`, 1 | Control response and the framer's busy (the response is held until the framer is idle). |
| `rbuf_addr_o`, `rbuf_data_i` | out / in | `$clog2(p_CTRL_BUF_DEPTH)+1`, 32 | Read-buffer port into the control plane (read on `tx_clk` when the clocks are unrelated). |
| `fifo_i`, `fifo_pkt_avail_i`, `fifo_len_i`, `fifo_streamid_i` | in | `cxp_txw_t`, 1, 16, 8 | Stream FIFO head word and the head packet's descriptor. |
| `fifo_ready_o`, `fifo_busy_o` | out | 1 | Head word taken; a packet is being read (the FIFO does not flush under it). |
| `flush_o` / `flush_ack_i` | out / in | 1 | Stream flush request and its acknowledgment. |
| `m_data_o`, `m_kmask_o` | out | 32, 4 | Wire word to the 8B/10B encoder, registered in `cxp_tx_inserter`. |

## How it works

1. **Flush request.** `flush_o = crst_i | cfgwr_hold_q | cfg_i.test_mode`: the ConnectionReset level, a ConnectionConfig write held until `flush_ack_i` (or 65535 cycles, in case the pixel clock is not running) and the TestMode level.
2. **ConnectionReset** (§10.3.28): `crst_i` restarts the PacketTags (`stream_ctrl_reset_i = crst_i | conn_cfg_wr_i`, a ConnectionConfig write alone too, §8.5.3), requests the flush, clears TestPacketCountTx and masks the device trigger at its de-asserted level; `crst_done_o` rises once the flush is done.
3. **Sources, arbiter, inserter** as described in `cxp_interface_top.md` (Arbiter integration): long packets through `cxp_tx_arbiter` (control ack > connection test > stream), the trigger and I/O-ack packets and the IDLE words inserted by `cxp_tx_inserter`.

Waived in `cxp_ip.vlt`: the response fields the framer does not read (`rsp_i`: N, timeout).

## Verification

No bench of its own; covered unchanged by `src/tb_unit/top/cxp_interface_top`, `src/tb_unit/top/cxp_device_top`, the PyUVM tiers and the emulator campaign. The two contracts bound here (`cxp_tx_owner_sva`: the long packet that owns the arbiter offers a word every cycle; `cxp_idle_rule_sva`: never more than 99 words without an IDLE) were bound on `cxp_interface_top` before 2026-09-27 and run in every bench that builds it. The split was proven by identical top-level traces of all 30 benches (fixed seed).

## Known issues and recommendations

None new; see the children's pages.
