# cxp_interface_top

Inputs chosen from the tree: RTL `src/rtl/top/cxp_interface_top.sv` and its four blocks `cxp_app_domain.sv`, `cxp_tx_domain.sv`, `cxp_rx_domain.sv`, `cxp_cdc_layer.sv` (+ every submodule in `src/rtl/cxp_ip.f`, including `cxp_ctrl_plane.sv`, `cxp_tx_arbiter.sv`, `cxp_tx_inserter.sv`, `cxp_tx_trigger_hs.sv` and the `cxp_cdc_*` primitives); bound SVA `src/sva/cxp_sva.sv`; lint waivers `src/rtl/cxp_ip.vlt`; unit TB `src/tb_unit/top/cxp_interface_top/`; multi-clock TB `src/tb_unit/top/cxp_device_top/` (through `cxp_device_top`); integration TB `src/verif/` (PyUVM, shell `src/verif/uvm/sv/tb_cxp_top.sv`); spec JIIA CXP-001-2015 v1.1.1 (`docs/spec/CXP-001-2015.pdf`) §8.2.4 Table 13, §8.2.5, §8.3.2 Table 16, §8.3.3 Table 17, §8.5.2, §8.6.3 Table 22, §8.7.4, §10.3.28, §10.3.32; regression `make -C src/tb_unit`; output `docs/design/modules/top/cxp_interface_top.md`.

Device-side link and datapath top. It selects a pixel source (internal TPG or sensor bus), packs and frames it into stream packets, receives the LS uplink, runs the control plane onto a register bus (register file on `reg_*`, an optional user window on the control plane's APB3 master), and puts one registered 32-bit word + K-mask per `tx_clk` on the downlink: three long-packet sources through `cxp_tx_arbiter`, then `cxp_tx_inserter`, which inserts the two-word trigger and I/O-acknowledgment packets and the IDLE words. 8B/10B, SerDes and the register file are outside.

| Downlink output | Words | Source instance | Where it joins |
|---|---|---|---|
| Trigger 4×K28.4 / 4×K28.2, 4×Delay (= 0), Table 16 | 2 | `cxp_tx_domain_i.cxp_tx_trigger_hs_i` | inserter `trig_i`: at the next word while the IDLE run is ≤ 97 |
| I/O ack 4×K28.6, 4×0x01, Table 17 | 2 | `cxp_tx_domain_i.cxp_tx_io_ack_i` | inserter `ioack_i`: at the next word while the run is ≤ 95 |
| Control ack type 0x03, Table 22 | 4 (bare) or N+6 (data) | `cxp_tx_domain_i.cxp_tx_ctrl_ack_i` | arbiter port 0 `TX_PORT_ACK` |
| Connection test type 0x04, no CRC | 1027 | `cxp_tx_domain_i.cxp_tx_linktest_i` | arbiter port 1 `TX_PORT_LT` |
| Stream data type 0x01, Table 19 | DsizeP + 8, DsizeP ≤ the chopper's size | `cxp_tx_domain_i.cxp_tx_stream_pkt_i` (words from `cxp_app_domain_i.cxp_app_stream_i` through `cxp_cdc_layer_i.cxp_cdc_stream_fifo_i`) | arbiter port 2 `TX_PORT_STREAM` |
| IDLE K28.5 K28.1 K28.1 D21.5, kmask 0111, Table 14 | 1 | `cxp_tx_domain_i.cxp_tx_inserter_i` | once the run reaches `IDLE_SOFT_RUN` (95) and nothing two-word is due, and whenever nothing else is offered |

Instantiated by `cxp_device_top` (the IP with its register file; used by `src/emu/bridge/tb/cxp_hw_env.sv` and `src/tb_unit/top/cxp_device_top`), by `tb_cxp_interface_top` (unit TB) and by `src/verif/uvm/sv/tb_cxp_top.sv`. The last two keep an external `cxp_ctrl_bootstrap_regs` on the APB port (the default user window covers the whole address space) because their tests reach into it by hierarchy and inject APB faults. Whoever holds the register file connects it to `reg_*` (or to `apb_*` through the user window), drives `cfg_*`, `clr_lt_*` and `conn_reset_active` from it, and returns `conn_reset_done` to it (see `cxp_device_top.md`). `cxp_if_*` feeds the transceiver's hard 8B/10B encoder. At this level the module implements §8.2.4/Table 13 (port order and insertion), §8.2.5 (IDLE), §8.3.2/§8.3.3 (device trigger gated by the link and by the host's I/O acknowledgment), §8.7.4 (TestMode holds the stream), §10.3.28 (the tx-domain part of ConnectionReset: PacketTag restart, stream flush, TestPacketCountTx clear, device trigger de-asserted; on `rx_clk`, the host's trigger level de-asserted), the acquisition gate (§11.2.1.4/5 through `cxp_app_acq_ctrl`, Table 44 stream enable) and the clock crossings between its three domains.

**Structure (since 2026-09-27).** One block per clock and one crossing layer; the top itself is wiring only (496 lines, was 1020):

| Block | Clock | Holds |
|---|---|---|
| `cxp_app_domain_i` | `app_clk` | `cxp_app_acq_ctrl`, `cxp_app_tpg`, `cxp_app_pixel_ingress`, the source mux, `cxp_app_pixel_packer`, `cxp_app_stream` (image header, line markers, skid register, merger, DsizeP chopper) |
| `cxp_tx_domain_i` | `tx_clk` | the stream flush request, `cxp_tx_stream_pkt`, `cxp_tx_ctrl_ack`, `cxp_tx_linktest`, `cxp_tx_trigger_hs`, `cxp_tx_io_ack`, `cxp_tx_arbiter`, `cxp_tx_inserter` |
| `cxp_rx_domain_i` | `rx_clk` (+ the read buffer's read clock) | `cxp_rx_link`, `cxp_ctrl_plane`; assembles `sb_status` |
| `cxp_cdc_layer_i` | all three | every `cxp_cdc_*` crossing (`g_cdc` / `g_tied`), the configuration subsets `cxp_cfg_app_t` / `cxp_cfg_tx_t`, `cxp_cdc_stream_fifo` |

The only multi-clock pieces outside `cxp_cdc_layer` are the control plane's two-bank read buffer (written on `rx_clk` inside `cxp_ctrl_bus_master`, read on `rbuf_clk` = `tx_clk` by the acknowledgment framer; the bank crosses with the response) and the trigger pin's synchroniser inside `cxp_tx_trigger_hs`.

## Interface

| Name | Default | Meaning |
|---|---|---|
| `p_TPG_X_SIZE` / `p_TPG_Y_SIZE` | 64 / 32 | TPG maximum line width (px) / lines. `cfg.xsize`/`cfg.ysize` of 0 or above this select the maximum. Unit TB 8 / 4. |
| `p_FIFO_DEPTH` | 1024 | Stream CDC FIFO depth in words; power of two ≥ 4 (checked in `cxp_cdc_stream_fifo`). The chopper's largest packet is `p_FIFO_DEPTH` − 8 words. Unit TB 256. |
| `p_PIX_W` | 16 | Sensor pixel width; 1…16, checked at elaboration (`s_pix_data[p_PIX_W-1:0]`, the result is cast to 16 bits). |
| `p_CTRL_BUF_DEPTH` | 64 | Control read/write buffer in words = largest read reply N; ≥ 2. |
| `p_APB_AW` / `p_APB_DW` | 32 / 32 | APB widths; the bridge is 32-bit only, and any other value is an elaboration `$error` (`g_chk_apb`). |
| `p_USER_BASE` / `p_USER_SIZE` | 0 / 0xFFFF_FFFF | User window: register accesses in [base, base + size) go to the APB port through the `cxp_ctrl_apb_bridge` inside the control plane's executor, all others to `reg_*`. The default sends everything to APB; `cxp_device_top` passes its own `p_USER_BASE` / `p_USER_SIZE` (0x2_0000 / 0 = no window by default). |
| `p_RX_CLK_KHZ` | 20 833 · `p_OS_RATIO` | `rx_clk` frequency in kHz; the control plane counts a command's Wait (100 ms) and timeout (900 ms) in ms of it. |
| `p_OS_RATIO` | 16 | `rx_clk` ÷ LS bit rate; even, ≥ 4. |
| `p_SAMP_LOCK_HITS` / `p_RX_LOSS_WORDS` | `RX_LOCK_HITS_DEFAULT` 2 / `RX_LOSS_WORDS_DEFAULT` 20 000 | K28.5 hits to lock / words without IDLE before the link is lost (twice the §8.2.5.1 low-speed interval; `cxp_rx_link_mon`) |
| `p_TRIG_ACK_TIMEOUT` | `cxp_pkg::TRIG_ACK_TIMEOUT` 4096 | `tx_clk` cycles a device trigger packet waits for the host's I/O acknowledgment before the next one may go (§8.3.3); ≥ 1 (checked in `cxp_tx_trigger_hs`). Unit TB 64, `cxp_device_top` TB 800. |
| `p_ASYNC_CLOCKS` | 0 | 0: the three clocks are one clock or phase-locked and every crossing is a wire (`g_tied`). 1: every crossing goes through a `cxp_cdc_*` primitive and the control read buffer is read on `tx_clk` (`g_cdc`). |

Fixed in submodules, not exposed: the arbiter's port order (`cxp_pkg::TX_PORTS` = 3, `TX_PORT_ACK/LT/STREAM` = 0/1/2); the inserter's `p_IDLE_SOFT` = `IDLE_SOFT_RUN` 95 and its trigger / I/O-ack run limits 97 / 95 (from `IDLE_MAX_INTERVAL` 100); `LT_DATA_WORDS` 1024 / `LT_GAP_WORDS` 16.

| Name | Dir | Width | Domain | Description |
|---|---|---|---|---|
| `app_clk`, `app_rst_n` | in | 1 | — | Pixel domain: TPG, ingress, acquisition gate, packer, stream FIFO write side. |
| `tx_clk`, `tx_rst_n` | in | 1 | — | Downlink domain: FIFO read side, framers, trigger source, I/O-ack source, arbiter, inserter. |
| `rx_clk`, `rx_rst_n` | in | 1 | — | Uplink oversampling domain: `cxp_rx_link`, `cxp_ctrl_plane`, register bus, APB, configuration. |
| `cfg` | in | `cxp_cfg_t` | rx | Every quasi-static configuration level, one struct (fields below, `cxp_pkg`). |
| `cfg.use_tpg` | in | 1 | rx | 1 = TPG, 0 = `s_pix_*`. Selects pixels and metadata; also gates `s_pix_valid` and the TPG run. |
| `cfg.run` | in | 1 | rx | Holds the acquisition armed without AcquisitionStart (`cxp_app_acq_ctrl.run_i`): images of either source enter while it is 1 and `cfg.stream_en` is 1. The TPG runs on `acq_active & cfg.use_tpg` and stops at the end of its image once no image would enter. |
| `acq_start`, `acq_stop` | in | 1 | rx | AcquisitionStart / AcquisitionStop pulses (crossed with `cxp_cdc_pulse`): arm / disarm `cxp_app_acq_ctrl`. `cxp_device_top` ORs the ConnectionReset write into `acq_stop`. |
| `cfg.acq_mode`, `cfg.acq_frames` | in | 2, 16 | rx | 0 continuous, 1 one image, 2 `cfg.acq_frames` images (0 counts as 1) per AcquisitionStart. |
| `cfg.stream_en` | in | 1 | rx | A stream packet fits in StreamPacketSizeMax (`cxp_device_top`: register ≥ 36 bytes). While 0 no image enters and the framer sends nothing (Table 44). Crossed to both `app_clk` and `tx_clk`. |
| `cfg.xsize`, `cfg.ysize` | in | 16 | rx | TPG active size. |
| `cfg.pixfmt` | in | 16 | rx | When non-zero: the TPG's format, latched at its own image start (0 selects Mono8), and on the sensor branch the override of `s_meta.pixfmt`. The packer latches the selected format at the image's first pixel and the image header carries that latched value. |
| `cfg.streamid` | in | 8 | rx | Image1StreamID: StreamID of TPG images (header and stream packets); the sensor branch keeps `s_meta.streamid`. |
| `cfg.xoffs`, `cfg.yoffs` | in | 16 | rx | OffsetX / OffsetY: TPG image-header offsets, latched at frame start. |
| `cfg.srctag` | in | 16 | rx | SourceTag register: a change presets the SourceTag of the next TPG image; the TPG counts images from there (`cxp_app_tpg.md`). |
| `cfg.tapg`, `cfg.flags` | in | 16, 8 | rx | TapGeometry and StreamFlags registers: TapG and Flags of TPG images. |
| `cfg.testpat` | in | 2 | rx | TPG pattern 0 gradient / 1 bars / 2 flat / 3 grey bars. |
| `cfg.arbitrary` | in | 1 | rx | 0 = rectangular image header and line markers. |
| `cfg.dsizeP` | in | 16 | rx | Stream data words per packet, crossed to `app_clk` only: the chopper in `cxp_app_stream` cuts packets of this size (0 or more than `p_FIFO_DEPTH` − 8 gives `p_FIFO_DEPTH` − 8). The framer writes each packet's own length as DsizeP. The integrator must derive it from StreamPacketSizeMax (bytes, whole packet, §8.5.2/§10.3.32), so N ≤ Max/4 − 8. |
| `cfg.trig_polarity` | in | 1 | rx | Trigger sense, 0 = active high. Used directly by `cxp_rx_link` and, crossed, by `cxp_tx_trigger_hs` as the pin's sense (the packet carries the logical level). |
| `cfg.test_mode` | in | 1 | rx | §10.3.35 TestMode → `cxp_tx_linktest`, and the TestMode stream flush. |
| `clr_lt_err`, `clr_lt_pkt_rx`, `clr_lt_pkt_tx` | in | 1 each | rx | One-cycle clears of TestErrorCount, TestPacketCountRx, and (crossed to tx) TestPacketCountTx (§10.3.37–39). The register file pulses all three on a ConnectionReset; TestPacketCountTx is also cleared by the crossed ConnectionReset level. |
| `cfg.ext_link` | in | 1 | rx | §5.1 strap: 1 = writes answered 0x43, except ConnectionReset / MasterHostConnectionID writes, which are ignored and answered 0x01; host triggers are ignored and not acknowledged (§8.3: the I/O channel is the Master connection's). |
| `trig_in` | in | 1 | any | Device trigger pin. It may come from any clock: `cxp_tx_trigger_hs` synchronises it to `tx_clk` (`cxp_cdc_sync_pin_i`, two flops) in both builds. |
| `trig_out`, `trig_out_glitch_pulse` | out | 1 | rx | Host→device trigger rebuilt from LS trigger packets (one pulse per change of the host's level of the selected edge; a resent packet gives none; the de-assertion at ConnectionReset gives the falling edge); glitch event. |
| `s_pix_data` | in | 16 | app | Sensor pixel, LSB-justified. |
| `s_pix_valid`, `s_pix_sof`, `s_pix_eol`, `s_pix_eof` | in | 1 | app | Sensor handshake and framing. |
| `s_pix_ready` | out | 1 | app | Ingress skid free. Stays 1 while `cfg.use_tpg` = 1: sensor pixels are accepted and dropped. |
| `s_meta` | in | `cxp_meta_t` | app | Sensor frame metadata (header fields; `arbitrary` is ignored, the form comes from `cfg.arbitrary`). The ingress latches it with the image's first pixel; the packer latches that bundle again at its SOF pixel, and the header and line markers take the packer's copy, so the port needs to hold only until the SOF pixel is taken. |
| `rx_serial` | in | 1 | rx | LS uplink bit, oversampled. |
| `cxp_if_data_o`, `cxp_if_kmask_o` | out | 32, 4 | tx | Downlink word (P0 in [7:0]) and per-byte K flag, registered in `cxp_tx_inserter`; IDLE after reset. |
| `reg_req`, `reg_we` | out | 1 | rx | Register-file access request (one cycle per word) and write flag, from `cxp_ctrl_bus_master`. |
| `reg_addr`, `reg_wdata` | out | 32 | rx | Byte address, write data. |
| `reg_wstrb` | out | 4 | rx | Byte enables of a write, bit 3 = `reg_wdata[31:24]` (the lowest address): 4'hF except on the last word of a write whose Size is not a multiple of 4. The APB port has no byte enables (APB3). |
| `reg_ack`, `reg_rdata`, `reg_err` | in | 1, 32, 8 | rx | Access done, read data, Table 22 code (0 = OK). The register file is expected to answer the next cycle; one that does not gets the command's Wait and 0x40 like a user-window slave. |
| `apb_psel`, `apb_penable`, `apb_pwrite` | out | 1 | rx | APB3 master control from the control plane's `cxp_ctrl_apb_bridge` (user window only; 0 when `p_USER_SIZE` = 0). |
| `apb_paddr`, `apb_pwdata` | out | `p_APB_AW`, `p_APB_DW` | rx | Byte address, write data. |
| `apb_prdata`, `apb_pready`, `apb_pslverr` | in | `p_APB_DW`, 1, 1 | rx | Read data, ready, slave error (answered 0x40). A command whose slave holds `pready` low gets a Wait (0x04) 100 ms after its first access and 0x40 after 900 ms, when the transfer is abandoned (PSEL drops; a later `pready` is ignored). |
| `sb_status` | out | `cxp_status_t` | rx | Link status, test counters and error pulses, one struct (fields below, `cxp_pkg`). |
| `sb_status.rx_lock`, `sb_status.aligned`, `sb_status.link_detected` | out | 1 | rx | Sampler lock, link up (IDLE monitor), §10.1.1 Detected. `sb_status.link_detected` is also crossed to `tx_clk` as the device trigger's link gate. |
| `sb_status.lt_err_count` / `sb_status.lt_pkt_count_tx` / `sb_status.lt_pkt_count_rx` | out | 32 / 64 / 64 | rx | §10.3.37–39 test counters. The TX count is counted on `tx_clk` and returned on `rx_clk`. |
| `sb_status.ctrl_reset_pulse` | out | 1 | rx | Host opcode 0xFF control-channel reset. Exported only, not used inside. |
| `sb_status.ctrl_nack_pulse`, `sb_status.ctrl_nack_code` | out | 1, 8 | rx | One cycle when a command the parser refused is answered without an access: its code (0x42 … 0x47, 0x80). Not for an ignored extension-link write (0x01), nor for a refused command dropped before it started. Exported only. |
| `sb_status.pkt_err_pulse`, `sb_status.code_err_pulse`, `sb_status.disp_err_pulse` | out | 1 | rx | Parser framing error; 8B/10B code error; running-disparity error. |
| `sb_pix_restart_pulse` | out | 1 | app | A sensor SOF arrived inside an open image: that image was cut off (`cxp_app_pixel_ingress.sof_restart_o`). |
| `sb_pix_stray_eof_pulse` | out | 1 | app | A sensor EOF arrived outside an image and was dropped (`cxp_app_pixel_ingress.spurious_eof_o`, combinational). |
| `conn_reset_active` | in | 1 | rx | ConnectionReset in progress: the register file's `ctl_connection_reset_active_o` (the ConnectionReset bit). Crossed to `tx_clk` as `crst_tx`. |
| `conn_reset_done` | out | 1 | rx | `crst_tx & flush_ack` synchronised back to `rx_clk`: the tx domain has seen the ConnectionReset and the stream flush it started is done. The register file clears the bit only once this is high (`cxp_ctrl_bootstrap_regs.md` How it works 3), or after its timeout. |
| `conn_cfg_wr` | in | 1 | rx | ConnectionConfig written (the register file's `ctl_connection_config_wr_o`), same value included. Crossed to `tx_clk` and ORed with `crst_tx` into `cxp_tx_stream_pkt.stream_ctrl_reset_i`, so the next packet of each stream carries PacketTag 0 (§8.5.3, §10.3.33); it also starts a stream flush (How it works 6). |

Notes:
- **Reset:** three asynchronous active-low resets and no synchronisers inside; the integrator must release each synchronously to its own clock. `cxp_device_top` does this with `cxp_cdc_reset`, which also resets the three domains together and releases them rx, tx, app (`cxp_cdc_reset.md`). Used alone with `p_ASYNC_CLOCKS` = 1, a domain reset on one side of a crossing raises no event and the configuration buses re-converge (`cxp_cdc_link`, `cxp_cdc.md`).
- **Crossings** (`cxp_cdc_layer.sv:156`–`:257`): 13 `cxp_cdc_*` instances in `g_cdc`. The `cfg` levels go out as two structs, `cxp_cfg_app_t` (to `app_clk`) and `cxp_cfg_tx_t` (to `tx_clk`); `cfg.ext_link` and `cfg.trig_polarity` are also used uncrossed on `rx_clk`. The pixel stream crosses app→tx only through the gray-code `cxp_cdc_stream_fifo`, and `trig_in` through the pin synchroniser inside `cxp_tx_trigger_hs`; both are present in either build. The table lists what `g_cdc` does; `g_tied` replaces each of the 13 rows with a wire and reads the control buffer on `rx_clk`.

| Signal | Direction | `g_cdc` primitive | Line |
|---|---|---|---|
| `cfg_rx_app` (use_tpg, run, acq_mode, acq_frames, stream_en, xsize, ysize, pixfmt, streamid, xoffs, yoffs, srctag, tapg, flags, testpat, arbitrary, dsizeP) | rx → app | `cxp_cdc_bus_cfg_app_i` | `:156` |
| `cfg_rx_tx` (test_mode, trig_polarity, stream_en) | rx → tx | `cxp_cdc_bus_cfg_tx_i` | `:163` |
| `acq_start` → `acq_start_app` | rx → app | `cxp_cdc_pulse_acq_start_i` | `:171` |
| `acq_stop` → `acq_stop_app` | rx → app | `cxp_cdc_pulse_acq_stop_i` | `:176` |
| `conn_reset_active` → `crst_tx` | rx → tx | `cxp_cdc_sync_crst_i` (1 bit, level) | `:182` |
| `crst_done_tx` (= `crst_tx & flush_ack`, `cxp_tx_domain`) → `conn_reset_done` | tx → rx | `cxp_cdc_sync_crst_done_i` (1 bit, level) | `:191` |
| `conn_cfg_wr` → `conn_cfg_wr_tx` | rx → tx | `cxp_cdc_pulse_conn_cfg_wr_i` | `:201` |
| `clr_lt_pkt_tx` → `clr_lt_tx` | rx → tx | `cxp_cdc_pulse_clr_lt_i` | `:206` |
| `trig_pkt_rcvd_w` → `trig_pkt_rcvd_tx` (host trigger received: send an I/O ack) | rx → tx | `cxp_cdc_pulse_trig_rcvd_i` | `:211` |
| `ioack_rcvd_w` → `ioack_rcvd_tx` (host acknowledged a device trigger) | rx → tx | `cxp_cdc_pulse_ioack_rcvd_i` | `:216` |
| `sb_status.link_detected` → `link_tx` | rx → tx | `cxp_cdc_sync_link_i` (1 bit, level) | `:222` |
| control response (`cxp_ctrl_rsp_t`: code, N, Size, Wait ms, timeout, read-buffer bank) | rx → tx | `cxp_cdc_req_rsp_i`; the executor holds the response until `src_ready_o`, `dst_ready_i` = `~ack_busy` | `:233` |
| `lt_pkt_count_tx` → `sb_status.lt_pkt_count_tx` | tx → rx | `cxp_cdc_bus_lt_pkt_tx_i` (64 bits) | `:251` |
| control read buffer (two banks) | rx write, tx read | `rbuf_clk` = `tx_clk` into `cxp_ctrl_plane`; the bank crosses with the response | `:248` |
| pixel stream | app → tx | `cxp_cdc_stream_fifo` (gray-code pointers) | — |
| stream flush request / writer dropping | tx → app → tx | inside `cxp_cdc_stream_fifo` (two `cxp_cdc_sync`, and its own `cxp_cdc_link`) | — |
| `trig_in` | pin → tx | `cxp_tx_trigger_hs.cxp_cdc_sync_pin_i` (both builds) | — |

- **Not crossed:** `s_pix_*` and `s_meta` (taken as `app_clk` inputs).
- `cxp_if_data_o`/`cxp_if_kmask_o` are flops in `cxp_tx_inserter`, loaded every cycle with the word chosen from the trigger / I/O-ack / arbiter offer (combinational from the sources and the stream-FIFO LUTRAM read). The wire never stalls: with nothing due it carries IDLE, and sources wait on their `ready`.
- Left unconnected inside: the unused bits of the crossed response in `g_tied`, `ing_meta_valid` (the ingress capture pulse), packer `lane_vld`/`eol`, the inserter's use of the long word's `sop`/`eop` (the arbiter's business). Each is waived in `src/rtl/cxp_ip.vlt`; `make lint_cxp_interface_top`, `lint_cxp_device_top` and `lint_async` (`cxp_device_top` with `p_ASYNC_CLOCKS` = 1) are clean.

## How it works

1. **Pixel and metadata selection.** `cfg_app.use_tpg` (in `cxp_app_domain`, `cfg_i.use_tpg`) picks between two `cxp_pix_t` streams (`tpg_pix`, `ing_pix`) and two `cxp_meta_t` bundles (`tpg_meta`, `ing_meta` — the ingress's copy latched at the image's first pixel), and routes `ready` back to the selected source only. `sel_meta.arbitrary` comes from `cfg_app.arbitrary`; in TPG mode `sel_meta.streamid` is `cfg_app.streamid`; on the sensor branch only, a non-zero `cfg_app.pixfmt` overrides `sel_meta.pixfmt` (the TPG already latched it at its own image start). `sel_meta.pixfmt` is the packer's `cfg_pixfmt_i`; the packer latches `sel_meta` with the image's first pixel (`m_meta_o`), and the image header gets `img_meta` = that copy with PixelF the packer's `m_pixfmt_o`, so the header describes the image it opens and names the format the payload is packed in, whatever the source's ports or the registers do meanwhile. The packer word is `bswap32`'d. `sel_frame_start`/`sel_line_start` = `pk_word_valid & sof/sol & ready`, i.e. the handshake cycle of the frame's or line's first word. `cxp_app_stream`'s skid register holds that word one cycle while the header or marker generator latches, so the header goes out before the pixels.
2. **Acquisition gate** (§11.2.1.4/5, Table 44). The selected pixels pass `cxp_app_acq_ctrl` (`cxp_app_acq_ctrl.md`) before the packer. At an image's first pixel it lets the image in if the acquisition is armed (or `cfg.run` holds it) and `cfg.stream_en` is 1; otherwise it takes and drops every pixel of that image, and it drops pixels outside any image. An image that entered always completes. For the sensor the gate decides at the first pixel. The TPG instead gates itself: it starts an image only while `acq_active` is high (`cfg_run_i = acq_active & cfg_app.use_tpg`) and decides a few cycles before its first pixel reaches the gate, so `src_gated_i` is tied to `cfg_app.use_tpg` and the gate takes every TPG image as it comes; otherwise an image the TPG had begun could be dropped when the acquisition stopped in between. Either way no source is left mid-image. A sensor that cannot be stopped keeps streaming into the gate, which takes its pixels.
3. **ConnectionReset glue** (§10.3.28). The register file owns the reset: it applies the register values, pulses the three counter clears and holds `conn_reset_active` high until it sees `conn_reset_done` (at least `p_LINK_RESET_CLEAR_CYCLES` + 1 cycles, at most `p_CONN_RESET_TIMEOUT` + 1). Here the level crosses to `tx_clk` once (`crst_tx`) and, while high:
   - holds every stream PacketTag at 0 (`stream_ctrl_reset_i = crst_tx | conn_cfg_wr_tx`; a ConnectionConfig write alone restarts them too);
   - requests a stream flush (item 6);
   - clears TestPacketCountTx (`clr_pkt_count_i = clr_lt_tx | crst_tx`), so a test packet in flight is not counted (`cxp_tx_linktest.md`);
   - masks the device trigger (`cxp_tx_trigger_hs.mask_i`, item 7): the level sent is de-asserted, so a host left at asserted gets one K28.2, and after the window the pin is followed only once it has been seen de-asserted.
   `crst_tx & flush_ack` is echoed back as `conn_reset_done`, so the ConnectionReset bit clears only after the stream path is empty. The rx test counters are cleared only by the register file's pulses on `clr_lt_err`/`clr_lt_pkt_rx`. Nothing is acknowledged at the end: the ConnectionReset write is answered once by the control plane like any write. In `g_tied`, `crst_tx` is `conn_reset_active` and `conn_reset_done` is `conn_reset_active & flush_ack`.
4. **Control plane.** `cxp_rx_link` ends at the long-packet stream `rx_long` (`cxp_rxlong_t`); `cxp_ctrl_plane` (command parser → `cxp_ctrl_bus_master`, the executor) turns it into single-word register-bus accesses and one held response per command (`rsp_valid_w`, `rsp_w`, two-bank read buffer), plus one Wait (0x04) ahead of it when a command runs 100 ms, and 0x03 for a control channel reset. Accesses in the user window go through the executor's `cxp_ctrl_apb_bridge` to the APB port; the plane's `nack_*` is `sb_ctrl_nack_*`.
5. **Control-ack request.** `ack_req = rsp_valid_tx`, with code, Size, Wait ms and read-buffer bank from `rsp_tx`. The executor holds the response until it is taken: in `g_tied` `rsp_ready` = `~ack_busy`; in `g_cdc` `cxp_cdc_req_rsp_i` takes it and presents it on `tx_clk` until the framer is idle. No response is lost while the framer is busy.
6. **Stream flush request** (`tx_clk`, in `cxp_tx_domain`). `flush_req = crst_tx | cfgwr_hold_q | cfg_tx.test_mode` drives the stream FIFO's `flush_i`: the ConnectionReset level, a ConnectionConfig write held in `cfgwr_hold_q` until `flush_ack` or for 65535 cycles (16-bit `cfgwr_tmo_q`, in case the pixel clock is not running; far beyond the longest packet in flight), and the TestMode level. The FIFO lets the packet being framed finish, then empties both sides, and the app side drops everything until the next image starts after the release (`cxp_app_stream.md`). So after a ConnectionReset, a ConnectionConfig write or TestMode the stream resumes with a whole image, and nothing produced before or during them goes out afterwards.
7. **Device trigger** (§8.3.2, §8.3.3). `cxp_tx_trigger_hs` synchronises `trig_in`, takes `cfg_tx.trig_polarity` as the pin's sense and sends the logical level (K28.4 asserted, K28.2 de-asserted) whenever it differs from the level last sent to the host. It sends nothing while `link_tx` (the crossed `sb_status.link_detected`) is low, and takes the host's level as de-asserted then. After each packet it waits for `ioack_rcvd_tx` (a Table 17 packet with code 0x01, taken out of the uplink word stream by `cxp_rx_packet_parser` and exported as `cxp_rx_link.ioack_rcvd_o`) or for `p_TRIG_ACK_TIMEOUT` cycles; edges during the wait merge, so the host ends at the pin's level. `crst_tx` is its `mask_i` (item 3). The ConnectionReset mask and the re-arm on a de-asserted pin live in that module.
8. **I/O acknowledgment.** `trig_pkt_rcvd_w` (a host trigger packet received cleanly) crosses to `tx_clk` and starts one `cxp_tx_io_ack` packet (4×K28.6, 4×0x01).
9. **TX path.** The three long-packet sources drive `tx_src[TX_PORT_*]` (`cxp_txw_t`: data, kmask, valid, sop, eop) into `cxp_tx_arbiter`, which returns `tx_ready` per port and offers one word (`tx_long`) to `cxp_tx_inserter`. The inserter takes the trigger (`tx_trig`) and I/O ack (`tx_ioack`) directly and registers the word it picks onto `cxp_if_data_o`/`cxp_if_kmask_o` (next section). TestMode reaches the transmit path only through `linktest_suppress_traffic` (`cfg.test_mode` or a test packet in flight) into `cxp_app_stream.suppress_stream_i`, which stops new stream packets from starting; the packet on the wire completes and the TestMode flush empties the rest.

Same-cycle rules (this module has no FSM of its own; `cxp_tx_ctrl_ack` is documented in `docs/design/modules/tx/cxp_tx_ctrl_ack.md`):
- `rsp_valid_tx` while `cxp_tx_ctrl_ack` is busy → held until it is idle, in both builds.
- A ConnectionReset request while the bit is set → the register file reloads its counters; the level stays high, one window.
- A `trig_in` edge while ConnectionReset is set is not sent. After it, a de-asserted pin re-arms the trigger without a packet (the masked level was already de-asserted); a still-asserted pin sends nothing until it de-asserts and asserts again.
- A trigger leader and an I/O-ack leader offered in the same cycle → the trigger goes first; the I/O ack waits the two trigger words (and an IDLE if the run has reached 95).

Latency and throughput: the chosen word is on `cxp_if_data_o` one cycle after it is offered. `trig_in` change → trigger leader on the wire after the fourth `tx_clk` edge that samples it (two synchroniser flops, the packet start, the output register; read from the code), while the link is up, no acknowledgment is awaited and the run allows it. An I/O ack leaves at most three words after it is offered (`a_ioack_within_3`). With `p_ASYNC_CLOCKS` = 1 each pulse and response adds about three destination-clock cycles and each `cxp_cdc_bus` update one source plus three destination cycles (read from the primitives). One word per `tx_clk`; the longest non-IDLE run is 99 words (checked by `a_run` in the inserter and by `cxp_idle_rule_sva` on the output). Stream efficiency is N/(N+8), less the IDLE every 96 words and any inserted two-word packets.

Invariants: at most one of `tpg_pix_ready`/`ing_pix_ready` is high (by construction, not asserted); at most one arbiter `ready_o` bit is high (asserted, `cxp_arbiter_sva`); the long packet that owns the arbiter offers a word in every cycle (asserted here, `cxp_tx_owner_sva`); `ack_req` is taken only while `ack_busy` is low (held upstream).

## Arbiter integration

- **Long packets** (`cxp_tx_arbiter`): between packets it offers the SOP of the highest-priority port that has one, control ack > connection test > stream (Table 13 priority 2, the control ack first because of the host's control-cycle timer). Once that SOP is taken the port owns the arbiter (`owner_q`) until its EOP is taken. Nothing pre-empts a long packet there and nothing drops it: a started packet always completes.
- **Store and forward.** Every long source offers its packet back to back once started: the stream framer starts only once the whole packet is in the FIFO (`s_pkt_avail_i`), the control ack's read buffer is complete before the response is presented, and the test generator counts. `cxp_tx_owner_sva` (bound in this module on `cxp_tx_arbiter_i.owner_q` and the three `tx_src[i].valid`) fails if the owner ever offers no word.
- **Insertion** (`cxp_tx_inserter`, §8.2.4): per word, in order: the second word of a two-word packet whose leader just went; a trigger leader while the run since the last IDLE is ≤ 97; an I/O-ack leader while it is ≤ 95; an IDLE once the run is ≥ 95; the arbiter's long word; otherwise IDLE. A long packet is stalled by `tx_long_ready` = 0 and resumes on the next word. Two-word packets are never split, by an IDLE or by each other (`a_trig_contiguous`, `a_ioack_contiguous`); the run never passes 99 (`a_run`).
- **ctrl-ack** waits for an in-flight stream or connection-test packet; §8.2.4 allows this.
- **TestMode** (§8.7.4): triggers and I/O acks are not held; they are inserted into the connection-test packets like into any long packet. The stream port stops starting packets (`suppress_stream_i`); a stream packet on the wire completes.
- **ConnectionReset** flushes the stream path (How it works 6) but not the other sources: a stream packet already on the wire completes. In `cxp_device_top` the ConnectionReset write also stops acquisition and StreamPacketSizeMax reads 0, so no image enters until the host writes StreamPacketSizeMax again; the first stream packet then opens a whole image (`cxp_device_top` test_18).

## Verification

Verilator 5.046 + cocotb 2.0.1, plus pyuvm 4.0.1 for `src/verif/`. Every bench compiles `src/sva/cxp_sva.f` and builds with `--assert`: bound into this design are `cxp_arbiter_sva` (one port taken per word), `cxp_inserter_sva` (two-word packets contiguous, I/O ack within 3 words, run ≤ 99), `cxp_tx_owner_sva` (the owner offers a word every cycle), `cxp_framer_sva` / `cxp_short_pkt_sva` in every source, `cxp_cdc_stream_fifo_sva`, `cxp_rxlong_sva`, `cxp_ctrl_exec_sva`, and `cxp_idle_rule_sva` on `cxp_if_data_o` (at least one IDLE every 100 words). The unit TB registers no FSM with `fsm_coverage`, so it collects no coverage. `src/verif/` collects Python functional coverage and, with `RTL_COV=1`, line coverage.

### Unit TB — `src/tb_unit/top/cxp_interface_top/test_cxp_interface_top.py`

Wrapper `tb_cxp_interface_top` ties all three clocks and resets to one `clk`/`rst_n` (10 ns), leaves `p_ASYNC_CLOCKS` at 0, and sets `p_TPG_X_SIZE` 8, `p_TPG_Y_SIZE` 4, `p_FIFO_DEPTH` 256, `p_TRIG_ACK_TIMEOUT` 64. It contains a real `cxp_ctrl_bootstrap_regs` behind an inline APB3 bridge on the APB port (default user window = whole space; `reg_ack`/`reg_rdata`/`reg_err` tied 0), with `p_LINK_RESET_CLEAR_CYCLES` = 8. The bridge raises `pslverr` when the register file's `err_o` is non-zero, so a refused access is answered 0x40 (APB carries no code); its `ctl_connection_reset_active_o` drives `conn_reset_active` and `conn_reset_done` returns to it, `cfg.xsize`/`ysize`/`pixfmt_reg`/`testpat`/`test_mode` come from its outputs, and its `ctl_connection_config_wr_o` drives `conn_cfg_wr`; `cfg.streamid` is tied to 1 and `cfg.xoffs`/`cfg.yoffs`/`cfg.srctag` to 0, `acq_start`/`acq_stop` to 0 and `cfg.stream_en` to 1 (so images of either source enter only while `cfg.run` = 1), `reg_wstrb` is left open and the register file's `wstrb_i` is tied to 4'hF. Python drives `link_reset_req` (the register file's local request `conn_reset_req_i`, equivalent to a host write of 1) and `trigger_in_app` (`trig_in`) directly; the wrapper's `link_reset_active` is the ConnectionReset bit and `link_reset_done` its falling edge; `link_detected` is `sb_status.link_detected`. The trigger tests call `uplink()`: a golden `Host` (`common/cxp_host.py`, `q = cxp_protocol.DEVICE`) at 16× oversampling drives `rx_serial` with IDLE and waits for `link_detected`, because the device sends triggers only to a detected host; with `trig_ack = "ack"` it answers every trigger, but an answer takes about 2000 cycles at this oversampling, so the 64-cycle timeout paces the triggers here. Elsewhere `rx_serial` stays 1. `reset()` holds `rst_n` low for 4 cycles with defaults (`cfg.dsizeP` 8, polarity 0, TPG off), then runs 2 cycles, and registers a teardown check (`check_downlink`: the golden deframer sees no framing error and no run over 99 words). Shared helpers: `sample_wire` (read at ReadOnly, then advance), `collect_packet` (SOP…EOP, EOP excluded, fails on timeout), `wait_for_trigger_packet` (HDR, then asserts the Delay word has kmask 0 and data 0).

| Test | Stimulus | Expect |
|---|---|---|
| `test_01_idle_after_reset` | Reset only | 32 IDLE words |
| `test_02_stream_from_tpg` | TPG on, `cfg.dsizeP` 8 | First packet's TYPE word = 4×0x01 |
| `test_03_link_reset_no_ack` | 1-cycle `link_reset_req` | 400 wire words all IDLE |
| `test_04_linktest_packets_under_testmode` | TPG on, TestMode set by backdoor | 5 packets of type 0x03/0x04, at least one 0x04 |
| `test_08_trigger_rising_edge` | `uplink`; `trig_in` 0→1 | One K28.4 packet, none before or after |
| `test_09_trigger_edge_pair` | `uplink`; 0→1, then 1→0 | K28.4, then K28.2 |
| `test_10_trigger_preempts_stream` | `uplink`; edge 3 words into a stream packet | HDR within 8 samples (13 stream words remain), stream resumes |
| `test_11_trigger_in_testmode` | `uplink`; TestMode; edge 100 words into a test packet | K28.4 packet within 20 words |
| `test_12_link_reset_active_during_window` | 1-cycle `link_reset_req` | `active` rises, `done` fires, `active` falls |
| `test_13_link_reset_clears_bootstrap_regs` | Preload 4 regs, `link_reset_req` | All 4 read 0 |
| `test_15_link_reset_clears_trigger_output` | `uplink`; `trig_in` held 1, `link_reset_req` | K28.2 packet |
| `test_16_link_reset_back_to_back` | 2 × `link_reset_req` | `done` twice |
| `test_17_link_reset_done_no_ack` | 1-cycle `link_reset_req` | `done` within 64 cycles; wire IDLE through 200 words after it |
| `test_18_connection_config_write_resets_tag` | TPG on, `cfg.dsizeP` 8; injected ConnectionConfig write strobe after 3 packets | One of the next 2 packets has PacketTag 0, the one after it 1 |
| `test_19_trig_phase_sweep_100` | `uplink` without acknowledgments; sensor frames in 200-word packets, then 20 000 cycles of TestMode; pin toggled every 3–11 cycles | Every leader followed by its Delay word; leaders alternate and end at the pin; ≥ 64 words apart; ≥ 80 cadence positions |
| `test_20_trigger_held_across_reset` | `uplink`; pin held 1 through reset; a real edge; pin held through `link_reset_req`; a real edge | No packet; K28.4; one K28.2 only; K28.4 |

#### test_01_idle_after_reset
- *Stimulus*: reset with all inputs at defaults; 32 samples start 2 cycles after reset release.
- *Checks*: every sample is kmask 0111 with bytes K28.5, K28.1, K28.1, D21.5.
- *Proves*: no source is valid after reset, the inserter's registered word resets to IDLE and fills with IDLE, and the IDLE word format is correct.

#### test_02_stream_from_tpg
- *Stimulus*: `cfg.use_tpg` = `cfg.run` = 1, `cfg.arbitrary` 0, `cfg.dsizeP` 8; `collect_packet(max_idle=4000)`.
- *Checks*: `collect_packet` sees a SOP and then an EOP within 1024 words; the TYPE word has kmask 0 and all 4 lanes are 0x01; the teardown deframer check.
- *Proves*: the TPG → selection → packer → `cxp_app_stream` → stream port → arbiter → inserter path reaches the wire.
- **The docstring also promises the image-header K28.3 marker, CRC32 and K29.7 checks. Only the TYPE word is checked here (the teardown deframer checks the framing).**

#### test_03_link_reset_no_ack
- *Stimulus*: after `RisingEdge`, `link_reset_req` = 1 for one cycle.
- *Checks*: the 400 wire words that follow are all IDLE.
- *Proves*: the ConnectionReset window adds no acknowledgment. Shared stimulus with test_12, test_16 and test_17.

```wavedrom
{"signal": [
  {"name": "clk (cycle)", "wave": "p..|......", "node": ""},
  {"name": "link_reset_req", "wave": "10.|......"},
  {"name": "crst_timer_q", "wave": "===|=.....", "data": ["0", "8", "7", "0"]},
  {"name": "link_reset_active (bit)", "wave": "01.|.0....", "node": ".e....g..."},
  {"name": "link_reset_done", "wave": "0..|.10...", "node": ".....f...."},
  {"name": "cxp_if_data_o", "wave": "=..|......", "data": ["IDLE"]}
],
 "head": {"text": "c0 c1 c2 | c9 c10 c11 .. (L = 8, echo = the level itself in g_tied; read from the code) - wire IDLE throughout; e,f,g: test_12 checks"}}
```

#### test_04_linktest_packets_under_testmode
- *Stimulus*: TPG on, `cfg.dsizeP` 8. On the next edge the backdoor sets `reg_q[row_test_mode]` = 1, driving `cfg.test_mode`. Then `collect_packet(max_idle=8000, max_data=2400)` five times.
- *Checks*: packet 0 may be type 0x01; every other packet is 0x03 or 0x04; at least one 0x04 appears.
- *Proves*: `cfg.test_mode` → `cxp_tx_linktest` emission, and that the stream framer starts no packet under TestMode.
- **The tolerated in-flight 0x01 branch does not run: TestMode is set before the TPG's first packet reaches the arbiter. A stream packet that is on the wire when TestMode rises is covered by `cxp_device_top` test_28, not here. The docstring still mentions "forced IDLEs" and a Medium finding that no longer exists.**

#### test_08_trigger_rising_edge
- *Stimulus*: `uplink`; polarity 0, `trig_in` = 0 for 20 samples, then 1 and held.
- *Checks*: no K28.4 or K28.2 HDR in the first 20 samples; within 200 samples an HDR of 4×K28.4 followed by a Delay word with kmask 0 and data 0; no HDR in the next 40 samples.
- *Proves*: the host's level starts de-asserted (no packet for a de-asserted pin); asserted → K28.4; a steady level sends nothing more.

#### test_09_trigger_edge_pair
- *Stimulus*: `uplink`; `trig_in` 0→1, then 1→0 immediately after the first packet's Delay word is sampled.
- *Checks*: first HDR K28.4, second HDR K28.2 (each within 200 samples, Delay word checked by the helper).
- *Proves*: the K-code follows the level; the second packet waits for the acknowledgment timeout (64 cycles), well inside the 200-sample bound.

#### test_10_trigger_preempts_stream
- *Stimulus*: `uplink`; TPG on, `cfg.dsizeP` 8, 200 cycles. Wait for a stream SOP, sample 2 more words, then `trig_in` = 1.
- *Checks*: HDR = 4×K28.4 within 8 samples of the edge (two synchroniser flops, the start, the output register; 13 words of the stream packet remain, so waiting for its EOP fails), with the Delay word checked; another stream SOP within 2000 samples; the teardown deframer finds every stream packet framed.
- *Proves*: the inserter puts the trigger between two words of a stream packet and the packet resumes (from the code: the leader is on the wire four edges after the pin is sampled, inside the packet).
- **The bound cannot catch a trigger held until EOP. At `cfg.dsizeP` 8 the packet is 16 words; 13 remain when the edge is driven, so a scheduler that waited for EOP would also pass the 20-sample bound. The docstring still describes the arbiter pausing the stream.**

```wavedrom
{"signal": [
  {"name": "clk (word)", "wave": "p.........."},
  {"name": "trig_in", "wave": "01........."},
  {"name": "pin (synchronised)", "wave": "0..1......."},
  {"name": "trig m_valid", "wave": "0...1.0...."},
  {"name": "tx_long_ready (stream)", "wave": "1...0.1...."},
  {"name": "cxp_if_data_o", "wave": "===.===.===", "data": ["SID", "Tag", "Dsz_H", "Dsz_L", "K28.4", "Delay", "D0", "D1", "D2"], "node": ".......ab.."}
],
 "head": {"text": "read from the code: the word chosen in cycle k is on the wire in k+1; a: HDR check, b: Delay check"}}
```

#### test_11_trigger_in_testmode
- *Stimulus*: `uplink`; the backdoor sets `reg_q[row_test_mode]` = 1; wait for a test packet's SOP (≤ 4000 samples) and 100 more words; `trig_in` = 1.
- *Checks*: an HDR of 4×K28.4 with a valid Delay word within 20 words; the teardown deframer finds the test packet around it framed.
- *Proves*: TestMode does not hold the trigger: it is inserted into the 1027-word test packet, which completes (§8.7.4 restricts data packets only).

#### test_12_link_reset_active_during_window
- *Stimulus*: same pulse as test_03.
- *Checks*: `link_reset_active` = 1 at the first ReadOnly after the pulse (c1, node e); `link_reset_done` within 64 cycles (c18, node f); `link_reset_active` = 0 one cycle later (c19, node g).
- *Proves*: the ConnectionReset bit rises on the request and falls once the timer has run out and the (tied) echo is high; `link_reset_done` is the wrapper's falling-edge detector. Any window width ≤ 64 passes; the width is not asserted.

#### test_13_link_reset_clears_bootstrap_regs
- *Stimulus*: backdoor preload of the `reg_q` rows MasterHostConnectionID = 0xA5A5_A5A5, StreamPacketSizeMax = 0x80, TestMode = 1, TestErrorCountSelector = 1; one cycle; 1-cycle `link_reset_req`; wait for `done` (≤ 64 cycles), then one more cycle.
- *Checks*: the three snoops show the preloaded values before the reset; afterwards `bs_master_host_link_id`, `bs_stream_pkt_dsize`, `bs_test_mode` and `test_err_cnt_sel_q` all read 0.
- *Proves*: the register file's local request applies the §10.3.28 values. It does not involve this module: the values are the register file's own (`cxp_ctrl_bootstrap_regs.md`).

#### test_15_link_reset_clears_trigger_output
- *Stimulus*: `uplink`; `trig_in` 0→1 and held; K28.4 packet awaited (≤ 200); then a 1-cycle `link_reset_req`.
- *Checks*: first packet K28.4; a trigger packet within 400 samples with HDR K28.2; both Delay words checked.
- *Proves*: `mask_i` = `crst_tx` forces the level sent to de-asserted while the host was left at asserted → K28.2 (polarity 0 only). That no K28.4 follows at the end of the reset is checked by test_20.

```wavedrom
{"signal": [
  {"name": "trig_in", "wave": "1...|....."},
  {"name": "link_reset_req", "wave": "10..|....."},
  {"name": "crst_tx (mask_i)", "wave": "01..|.0..."},
  {"name": "armed_q", "wave": "10..|....."},
  {"name": "host_lvl_q", "wave": "1.0.|....."},
  {"name": "cxp_if_data_o", "wave": "=.==|=...", "data": ["IDLE", "4xK28.2", "Delay", "IDLE"], "node": "..a......."}
],
 "head": {"text": "a: K28.2 check (after the acknowledgment wait of the K28.4 packet has ended); trig_in stays 1, so armed_q stays 0 after the reset and no K28.4 follows"}}
```

#### test_16_link_reset_back_to_back
- *Stimulus*: two iterations of a 1-cycle `link_reset_req`, wait for `done` (≤ 64), then 8 cycles. The second request arrives about 9 cycles after the first `done`.
- *Checks*: `link_reset_done` fires within 64 cycles in each iteration.
- *Proves*: the ConnectionReset bit clears and can be set again.
- **The docstring says "50 ms apart … no stuck state". The requests do not overlap, so the reload while the bit is set is not exercised, and `active` is not checked.**

#### test_17_link_reset_done_no_ack
- *Stimulus*: as test_03; `link_reset_done` and the wire sampled every cycle for 400 cycles.
- *Checks*: `done` within 64 cycles; every word IDLE, including the ones after `done`.
- *Proves*: the end of the ConnectionReset sends nothing.

#### test_18_connection_config_write_resets_tag
- *Stimulus*: `cfg.use_tpg` = `cfg.run` = 1, `cfg.arbitrary` 0, `cfg.dsizeP` 8; three stream packets collected; then the wrapper input `conn_cfg_wr_inject`, ORed into the register file's ConnectionConfig write strobe (the strobe an accepted 0x4014 write raises), is held high for 3 edges; three more packets.
- *Checks*: the PacketTag (packet word 3 counting the SOP as word 0, byte P0, kmask 0) of the first three packets runs on by one and stays clear of the wrap; one of the two packets after the strobe has tag 0, and the packet after that tag 1.
- *Proves*: `ctl_connection_config_wr_o` → `conn_cfg_wr` → `stream_ctrl_reset_i` restarts the PacketTag (§8.5.3, §10.3.33). It accepts either outcome for a packet whose header was already latched (`cxp_tx_stream_pkt.md`), bypasses the bus write path, and runs only the tied `g_tied` build, not `cxp_cdc_pulse_conn_cfg_wr_i`. Fails with the strobe disconnected from `stream_ctrl_reset_i` (tags run on 4, 5, 6).

#### test_19_trig_phase_sweep_100
- *Stimulus*: `uplink(trig_ack="drop")` (the host never acknowledges, so the 64-cycle timeout paces the triggers); sensor path (`cfg.use_tpg` = 0, `cfg.run` = 1), `cfg.dsizeP` 200, six 128 × 32 Mono8 frames at one pixel per cycle, sent back to back from the store-and-forward FIFO; 3000 cycles; `cfg.run` = 0 and TestMode for 20 000 cycles (test packets back to back); TestMode off, 2000 cycles. `trig_in` toggles every 3..11 cycles (seed 19) for the whole run, then rests. The host records every wire word.
- *Checks*: `split_short_packets` finds no leader without its data word next; the leaders alternate K28.4 / K28.2, the last one is the pin's final level; consecutive leaders are at least 64 words apart; the leaders sat at 80 or more distinct positions of the IDLE run; the host's reassembler reports no stream error.
- *Proves*: the inserter never splits a trigger at any phase of the IDLE cadence, inside stream and test packets; the acknowledgment timeout paces the triggers and fast edges merge to the pin's level (§8.3.3).

#### test_20_trigger_held_across_reset
- *Stimulus*: `uplink`; (a) `trigger_in_app` = 1 held through a second `rst_n` pulse, the link brought back up, 300 words; (b) pin 0 for 20 cycles, then 1, 200 words; (c) pin held 1, `link_reset_req` for 3 edges, 600 words; (d) pin 0, 50 words, pin 1, 200 words.
- *Checks*: (a) no trigger packet; (b) one K28.4; (c) one K28.2 and nothing else; (d) one K28.4 only.
- *Proves*: `armed_q` in `cxp_tx_trigger_hs` (How it works 3, 7): no edge from a pin held across a reset or a ConnectionReset, the forced de-assertion during the ConnectionReset, and re-arming on a de-asserted pin without a packet. Polarity 0 only.

### Multi-clock TB — `src/tb_unit/top/cxp_device_top/`

The only bench that runs this module with `p_ASYNC_CLOCKS` = 1: `cxp_device_top` (this module + the register file) at `rx_clk` 10 ns, `tx_clk` 8 ns, `app_clk` 12 ns, `p_TRIG_ACK_TIMEOUT` 800, resets through `cxp_cdc_reset`, driven by the golden host model `src/verif/common/cxp_host.py` over the serial uplink; the wrapper exposes `cfg.use_tpg` and the sensor port. Thirty-two tests carry traffic across every crossing listed under Interface; see `cxp_device_top.md`. For this module:
- `test_05_connection_reset`, `test_16` and `test_17` assert exactly one acknowledgment per command around ConnectionReset writes, including six under traffic.
- `test_15_conn_reset_postconditions` checks the tx-domain part: a held trigger gives one falling packet and none after, TestPacketCountTx reads 0, and the first stream packet after the host writes StreamPacketSizeMax again has tag 0.
- `test_11` and `test_14` reset one domain input at a time (rx; tx and app while streaming); through `cxp_cdc_reset` that is a reset of the whole device, and nothing stray reaches the wire.
- `test_18_whole_image_after_conn_reset`: after a ConnectionReset mid-image, the first stream packet once StreamPacketSizeMax is written again opens a whole image (the flush).
- `test_19_stop_tpg_mid_packet`: `cfg.run` dropped at 11 points of a packet and restarted; every image whole.
- `test_20_acq_start_stop_sensor`: sensor path, free-running sensor; no packet before AcquisitionStart, whole images after it, at most the image in progress after AcquisitionStop (the acquisition gate).
- `test_21_spsm_below_minimal_packet` and `test_23_spsm_negotiation`: `cfg.stream_en` below 36 bytes holds the stream; every packet fits the StreamPacketSizeMax in force.
- `test_22_testmode_exit_whole_image` and `test_28_testmode_vs_stream`: the stream resumes with a whole image after TestMode, and TestMode written inside a stream packet or cleared inside a test packet cuts no packet.
- `test_07_host_trigger`, `test_27_ioack_latency_under_stream`, `test_29_ioack_in_testmode`: `trig_pkt_rcvd` → `cxp_cdc_pulse_trig_rcvd_i` → K28.6, inserted into 200-word stream packets at most 6 words after the tx-side request, and not held in TestMode.
- `test_12_idle_cadence_per_packet_type`, `test_30_tx_trigger_ack_rules`, `test_31_nested_preempt`, `test_32_trigger_waits_for_link`: the device trigger across the IDLE cadence, gated by the host's I/O acknowledgment (`cxp_cdc_pulse_ioack_rcvd_i`) or the timeout, next to a host trigger's I/O ack, and held while the link is down (`cxp_cdc_sync_link_i`).
- `test_04_stream` checks CRC, tag and DsizeP of every stream packet with the golden reassembler.
- `test_13_pipelined_cmds`, `test_24_ctrl_wait_ack` and `test_25_ctrl_reset_during_exec`: the control response and its read-buffer bank across `cxp_cdc_req_rsp_i` (a read behind a read, Wait and 0x40 from a slow APB slave on the user window, 0xFF during execution).

### Integration TB — `src/verif/uvm/tests/all_tests.py`

34 test classes, one simulation each. The DUT is the real `cxp_interface_top` (`p_ASYNC_CLOCKS` = 0) plus `cxp_ctrl_bootstrap_regs` (on `rx_clk`) behind an APB bridge with wait-state and pslverr injection; `pslverr` is also raised when the register file's `err_o` is non-zero, and the control scoreboard then expects 0x40. `ctl_connection_reset_active_o` → `conn_reset_active` and `conn_reset_done` back, so ConnectionReset uses the real host-write path; `sb_link_reset_active` is the bit and `sb_link_reset_done` its falling edge. Three clock inputs, all 10 ns by default. The host uplink is driven as oversampled serial, and every scoreboard runs in every test (control, stream, link_protocol with the 100-word IDLE cadence, io_ack, tx_trigger, linktest, reg, link_reset). The host uplink agent answers device triggers with I/O acknowledgments, and the tx_trigger scoreboard checks that the host's level ends at the pin's. The observations below predate the transmit-path rework of 2026-09-26 and were not re-run for this document.

| Test | Checks |
|---|---|
| `test_link_reset` | §10.3.28 values, one window and one `done`; 3 commands → 3 acks |
| `test_xifc_stream_linkreset` | ConnectionReset under TPG load; 1 command → 1 ack; stream CRC-clean |
| `test_link_reset_storm` | 3 × opcode 0xFF → 3 acks of 0x03; no window opened |
| `test_arbiter_preempt` | Video + control + host triggers + LS linktest together |
| `test_arbiter_stream_underflow` | I/O acks delivered after a frame whose last stream packet is short |
| `test_arbiter_underflow_ctrl` | Control acks delivered after the same frame |
| `test_tx_trigger` | Trigger packets per `trig_in` level, correct K-code, Delay 0; host ends at the pin |
| `test_xifc_stream_trigger` | Trigger packets during TPG stream; stream CRC-clean; host ends at the pin |
| `test_tx_linktest_mode` | Host TestMode 1→0; type-0x04 count = TestPacketCountTx |
| `test_io_ack` | One K28.6/0x01 per host trigger (`trig_pkt_rcvd` → I/O ack) |
| `test_xifc_stream_ctrl` | 8 control commands during TPG stream |
| `test_cdc_sweep` | Video at app/tx/rx = 8/12/10 ns |

#### test_link_reset
- *Stimulus*: write MasterHostConnectionID = 0xA5A5_A5A5, 8 IDLE words, write 0x4000 = 1, 8 IDLE words, read MasterHostConnectionID.
- *Checks*: the reg scoreboard sees the read return 0 (`link_reset_clears=1`); the link-reset scoreboard sees requests = windows = done = 1 and `rate_to_discovery` tracking `active`; the control scoreboard pairs 3 acks with 3 commands.
- *Proves*: the host path register file → ConnectionReset bit → tx echo → bit cleared.

#### test_xifc_stream_linkreset
- *Stimulus*: TPG on, 24 IDLE words, write 0x4000 = 1, 24 IDLE words.
- *Checks*: one window and one `done`; stream packets CRC-clean (stream resumes); control acks = 1.
- *Proves*: the ConnectionReset does not corrupt a busy wire, and the PacketTag reset does not break framing.
- **`strict_ack_match = False` is a leftover; the test gets one ack per command like `test_link_reset`.**

#### test_link_reset_storm
- *Stimulus*: 3 back-to-back `CTRL_CMD_RESET` (opcode 0xFF) commands.
- *Checks*: 3 acks with code 0x03; no ConnectionReset.
- *Proves*: opcode 0xFF does not start a ConnectionReset (`sb_status.ctrl_reset_pulse` is export-only). Despite the name, it does not exercise the §10.3.28 path.

#### test_arbiter_preempt
- *Stimulus*: 2 random external-sensor frames, 4 control commands, 2 host triggers and 1 LS linktest packet, all concurrent.
- *Checks*: IDLE cadence and K-code lexicon clean; 2 I/O acks for 2 triggers; 4 acks.
- *Proves*: ingress path and the I/O-ack and control-ack sources under load.
- **`strict_ack_match = False`, and `stream_scoreboard` reports an unreassembled frame as a warning ("known RTL limit") instead of an error.**

#### test_arbiter_stream_underflow
- *Stimulus*: one 64×8 Mono8 external frame at `cfg.dsizeP` 64 (169 merged words: two full packets and a 41-word last packet), then 256 uplink IDLE words and 4 host triggers.
- *Checks*: every trigger gets its K28.6/0x01 ack; the stream scoreboard warns only if the frame is not reassembled.
- *Proves*: the short last packet of an image does not block the I/O-ack source. The chopper closes it at the image's last word and the framer starts it only once it is stored whole, so the owner never stalls (`cxp_tx_owner_sva`).

#### test_arbiter_underflow_ctrl
- *Stimulus*: the same frame, then 4 control reads each followed by 16 IDLE words.
- *Checks*: every read gets its type-0x03 ack.
- *Proves*: the same for the control-ack source.

#### test_tx_trigger
- *Stimulus*: 6 `trig_in` edges, TPG off; the host acknowledges.
- *Checks*: trigger packets in the matching direction, Delay 0; the host's level ends at the pin's (the scoreboard allows edges to merge, §8.3.3).
- *Proves*: `trig_in` → `cxp_tx_trigger_hs` → inserter on an idle wire.

#### test_xifc_stream_trigger
- *Stimulus*: TPG free-running; 8 `trig_in` edges spaced 200 cycles.
- *Checks*: trigger packets in the matching direction; the host ends at the pin's level; stream packets CRC-clean.
- *Proves*: insertion leaves the stream packets intact. Word-boundary insertion is not measured.

#### test_tx_linktest_mode
- *Stimulus*: host writes TestMode = 1, 8 IDLE words, TestMode = 0, 20 µs. TPG not enabled.
- *Checks*: the number of type-0x04 packets equals `sb_status.lt_pkt_count_tx`; 2 write acks.
- *Proves*: register file → `cfg.test_mode` → linktest, and the counter export. With no stream running, the stream hold is not exercised.

#### test_io_ack
- *Stimulus*: 6 random host trigger packets on the uplink.
- *Checks*: 6 K28.6 acks with code 0x01.
- *Proves*: the `trig_pkt_rcvd_w` → `trig_pkt_rcvd_tx` wire of `g_tied`.

#### test_xifc_stream_ctrl
- *Stimulus*: TPG on; 8 random control reads and writes.
- *Checks*: acks pair with commands (8/8); stream CRC-clean.
- *Proves*: the control ack goes out between stream packets.

#### test_cdc_sweep
- *Stimulus*: `set_cdc_ratio(8, 12, 10)`, then 2 random external frames.
- *Checks*: stream scoreboard: packets CRC-clean.
- *Proves*: the app→tx FIFO crossing only. The shell keeps `p_ASYNC_CLOCKS` = 0, so the other crossings are wires between unrelated clocks; no control command or trigger crosses in this test. `set_cdc_ratio` starts new `Clock` coroutines without stopping the 10 ns ones, so the effective waveforms are [unverified].

### Other
- `src/tb_unit/tx/cxp_tx_arbiter`: 23 tests of `cxp_tx_arbiter` followed by `cxp_tx_inserter` (the wrapper instantiates both), including `test_17_ioack_inserted_mid_ack`, `test_18_trig_waits_for_ioack_code`, `test_25_insert_at_every_run_position` and `test_26_registered_wire_word`. `cxp_tx_inserter` has no bench of its own.
- `src/tb_unit/tx/cxp_tx_trigger_hs`: the trigger source alone (synchroniser, link gate, acknowledgment wait and timeout, ConnectionReset mask).
- `src/tb_unit/cdc/cxp_cdc`: the four CDC primitives at two clock ratios, including one-sided resets (`cxp_cdc.md`).
- `src/tb_unit/ctrl/cxp_ctrl_bootstrap_regs`: tests 22–24, the ConnectionReset bit's echo wait, timeout and local request.
- `src/tb_unit/tx/cxp_tx_stream_pkt`: `test_03_packet_tag_wrap`, `test_10_stream_ctrl_reset_restarts_tag` (the ConnectionReset → PacketTag effect, as a one-cycle pulse).
- `src/tb_unit/top/cxp_stream_top`: its wrapper copies this module's pixel glue (byte swap, handshake-gated frame/line start). It tests the copy, not this file.
- `src/tb_unit/rx/cxp_rx_packet_parser` and `src/tb_unit/rx/cxp_rx_link`: the Table 17 decode behind `ioack_rcvd_o`.
- `src/tb_unit/tx/cxp_tx_ctrl_ack`, `cxp_tx_io_ack`, `cxp_tx_linktest`, `cxp_ctrl_bootstrap_regs`: per-source unit TBs. The IDLE rule is asserted by `cxp_idle_rule_sva` in every bench that has this top.
- `src/emu/bridge/tb/cxp_hw_env.sv`: interactive DPI/FIFO environment for the `src/emu/cxp` host through `cxp_device_top` on one clock; not self-checking, but the bound SVA run in it.

### Running
- One unit test: `make -C src/tb_unit/top/cxp_interface_top WAVES=0 COCOTB_TEST_FILTER=test_10_trigger_preempts_stream`. All unit tests: `make -C src/tb_unit interface_top`. Multi-clock bench: `make -C src/tb_unit device_top`. Regression entry point: `make -C src/tb_unit`; lint: `make lint`.
- One integration test: `make -C src/verif UVM_TESTNAME=test_link_reset`. Tiers: `make -C src/verif smoke|nightly|weekly`.
- 2026-09-26, working tree (transmit scheduler, trigger gated by the host's acknowledgment and the link): `make -C src/tb_unit/top/cxp_interface_top WAVES=0` 16/16 PASS, none tagged; `cxp_device_top` 32/32 PASS; `make lint_cxp_interface_top lint_cxp_device_top lint_async` clean. `src/verif/` not re-run.
- Tip: `sim_build` is not rebuilt when `WAVES` changes; after a `WAVES=0` build, plain `make` fails. Use `make clean` first.

### Not covered in-tree
- A ConnectionConfig write over the uplink while a long stream packet is being framed, and the 65535-cycle give-up of the ConnectionConfig flush.
- A sensor that cuts an image short, end to end (`sb_pix_restart_pulse` is not observed by any bench above the ingress unit TB) → Minor 5.
- The host's I/O acknowledgment releasing a device trigger in this bench: at 16× oversampling an acknowledgment takes about 2000 cycles and the timeout is 64, so the timeout always paces here; the acknowledgment path is covered by `cxp_device_top` test_30 and `src/tb_unit/tx/cxp_tx_trigger_hs`.
- The link dropping and returning, at this level (unit-tested in `src/tb_unit/tx/cxp_tx_trigger_hs` test_11; `cxp_device_top` test_32 covers the link coming up only).
- ConnectionReset through the host path in the unit TB (covered in `cxp_device_top` test_05, test_15–17 and verif `test_link_reset`).
- Wait (0x04) and the command timeout through this top: not in this bench; covered on the wire by `cxp_device_top` test_24 / test_25 (an APB slave on the user window), and in `cxp_rx_link` test_11/test_12, `cxp_ctrl_plane` and the `cxp_ctrl_bus_master` unit TB.
- A domain reset during activity at this level: covered only through `cxp_device_top` (test_11, test_14), where `cxp_cdc_reset` makes it a reset of all three domains; one-sided resets of the crossings only in `src/tb_unit/cdc/cxp_cdc` → Medium 2.
- `cfg.trig_polarity` = 1 anywhere in this bench or `cxp_device_top` (both tie or drive it 0) → Medium 2.
- `cfg.use_tpg` switched mid-frame → Medium 2. The sensor path is covered by unit test_19 and end to end by `cxp_device_top` test_20 and test_26.
- Multi-clock operation: covered by `src/tb_unit/top/cxp_device_top` at one fixed ratio (10/8/12 ns). Not covered: other ratios, a new control response while one is still crossing, an event offered within the ~10-cycle `cxp_cdc_link` settle window after reset.
- Counter saturation and wrap (PacketTag, test counters): covered in submodule TBs only; the glue adds no logic to them.
- End-to-end uplink beyond the trigger tests' IDLE host: the unit TB never sends a command; `cxp_device_top` and verif do.
- X-propagation: Verilator is 2-state; not covered.

## Known issues and recommendations

### Critical

None.

### Medium
1. **`p_ASYNC_CLOCKS` defaults to 0.** `tb_cxp_interface_top`, `src/verif/uvm/sv/tb_cxp_top.sv` and `src/emu/bridge` build with wires, and verif `test_cdc_sweep` runs unrelated periods on that build, so its crossings are unsynchronised wires. Fix: default `p_ASYNC_CLOCKS` to 1 in the benches that run separate clocks (or in the module, Open question 3). Effort: 2 h.
2. **Unit-TB gaps**: stream content from the sensor path in this bench (test_19 checks trigger timing and reassembly only), `cfg.trig_polarity` = 1 (also untested in `cxp_device_top`; only `src/tb_unit/tx/cxp_tx_trigger_hs` test_05 drives it), `cfg.use_tpg` switched mid-frame. Add directed tests. Effort: 1 day.
3. **test_10 bound.** Require the HDR within about 5 samples of the edge (the latency from the code is four edges) and check the resumed packet's length and CRC. Effort: 1 h.

### Minor
1. Done 2026-09-27: the header takes the packer's copy of the ingress-latched metadata (`cxp_device_top` test 36). The line markers no longer read `ext_meta_*` live: `cxp_app_stream` holds the metadata from the frame-start pulse.
2. Document (or gate with `~cfg_app.use_tpg`) that `s_pix_ready` = 1 discards sensor pixels while the TPG is selected.
3. Test hygiene: fix the test_02, test_04, test_10 and test_16 docstrings (test_04 cites forced IDLEs and a removed finding, test_10 an arbiter pause); stage `cxp_camera_xml.mem` for the unit TB (the run still logs `$readmem file not found`).
4. SVA still missing: `$onehot0({tpg_pix_ready, ing_pix_ready})`; `rsp_valid_tx` stable until taken.
5. **The sensor status pulses end at the boundary.** `sb_pix_restart_pulse` (a sensor image cut short) and `sb_pix_stray_eof_pulse` are one-cycle `app_clk` pulses, the second combinational; `cxp_device_top` leaves both unconnected and no register counts them, so the host cannot see a cut image. Register them and make them readable (a sticky bit or a counter). Effort: 1 h.
6. **Stale comments in the RTL.** The `cfg.run` port comment says "TPG free-run enable" (it arms the gate for both sources); the `cxp_app_acq_ctrl` instance banner says "which test-pattern images start"; the header's Versions list has 0.9 above 0.8; the `linktest_suppress_traffic` wire now only holds the stream, while `cxp_tx_linktest`'s port comment still says "-> arbiter stream-mask". Effort: 15 min.
7. **A late I/O acknowledgment can release the next trigger early.** `cxp_tx_trigger_hs` counts any `ack_i` pulse while it waits. If the host answers a trigger after `p_TRIG_ACK_TIMEOUT`, that answer arrives during the next packet's wait and ends it at once [by code reading]. Table 17 carries no reference to the trigger it answers, so the device cannot tell them apart; a timeout long enough for the slowest host keeps the case rare. Document the choice of `p_TRIG_ACK_TIMEOUT` (default 4096 `tx_clk` cycles, 26 µs at 156.25 MHz) against the host's worst answer time.

### Open questions
1. Answered: §8.3.2 has both ends de-assert the trigger at link discovery. `conn_reset_active` now clears the host's level in `cxp_rx_trigger_lspd` (a host left asserted gives the falling edge on `trig_out` at polarity 1).
2. Designer: should `p_ASYNC_CLOCKS` default to 1, leaving 0 as an explicit option for single-PLL builds?
