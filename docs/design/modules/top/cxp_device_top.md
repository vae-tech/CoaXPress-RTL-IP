# cxp_device_top

Inputs chosen from the tree: RTL `src/rtl/top/cxp_device_top.sv` (+ `cxp_cdc_reset.sv`, `cxp_interface_top.sv` and its submodules, `cxp_ctrl_bootstrap_regs.sv` and the generated `cxp_regmap_pkg.sv`, all from `src/rtl/cxp_ip.f`); register map `src/regmap/cxp_regmap.yaml`; GenICam description `src/regmap/genicam/cxp_camera.xml`; bound SVA `src/sva/cxp_sva.sv`; TB `src/tb_unit/top/cxp_device_top/` with the host model `src/verif/common/cxp_host.py` and `cxp_uplink.py`; user `src/emu/bridge/tb/cxp_hw_env.sv`; spec JIIA CXP-001-2015 v1.1.1 (`docs/spec/CXP-001-2015.pdf`) §8.2.4, §8.3.2, §8.3.3, §8.5.2, §8.7.4, §10.3.28, §10.3.32, §10.3.33, §10.3.35–39; regression `make -C src/tb_unit`; output `docs/design/modules/top/cxp_device_top.md`.

The IP boundary an integrator instantiates: `cxp_cdc_reset` (one reset for the three clock domains), `cxp_interface_top` (link, datapath, control plane) and the register file on its register-bus port (`reg_*`), with every register side effect wired to the datapath. Serial uplink in, pixels in, 32-bit downlink word + K flags out; optionally an APB3 master port (`m_apb_*`) for a user register window.

| Register (address) | Drives | Port of `cxp_interface_top` |
|---|---|---|
| Width (0x3000 / 0x10000), Height (0x3004 / 0x10004) | TPG active size, low 16 bits | `cfg.xsize`, `cfg.ysize` |
| PixelFormat (0x3014 / 0x10008) | TPG format, latched at its image start; on the sensor branch it overrides `s_meta_i.pixfmt` when non-zero, and since the register accepts only 0x0101–0x0105 it always does: the sensor's own PixelF never reaches the wire (N-35, open). The packer latches the selected format at an image's first pixel and the image header carries that value | `cfg.pixfmt` |
| TestPattern (0x1001C) | TPG pattern, low 2 bits | `cfg.testpat` |
| OffsetX (0x10020), OffsetY (0x10024) | TPG image-header Xoffs / Yoffs, low 16 bits | `cfg.xoffs`, `cfg.yoffs` |
| SourceTag (0x10030) | a change presets the SourceTag of the next TPG image, low 16 bits | `cfg.srctag` |
| TapGeometry (0x3018 / 0x10028), StreamFlags (0x10034) | TPG image-header TapG (only 0 accepted) and Flags byte | `cfg.tapg`, `cfg.flags` |
| Image1StreamID (0x301C / 0x1002C) | StreamID of TPG images (header and packets), low 8 bits; powers up at `p_IMAGE1_STREAM_ID_RESET` | `cfg.streamid` |
| TestMode (0x401C) | connection-test TX | `cfg.test_mode` |
| ConnectionReset (0x4000) write 1 | applied in the register file; its bit restarts the PacketTags, flushes the stream path, clears TestPacketCountTx and holds the device trigger de-asserted in the tx domain, which echoes it back once the flush is done; the write also stops acquisition | `conn_reset_active`, `conn_reset_done`; `acq_stop` |
| AcquisitionStart / AcquisitionStop (0x300C / 0x3010 and aliases) write | arm / disarm the image gate (`cxp_app_acq_ctrl`) for either source: an image enters only if it starts while armed, and an image that entered completes | `acq_start`, `acq_stop` |
| TpgRun (0x10038), FrameCount (0x10018) | TpgRun bit 0 = 1: images until AcquisitionStop; 0: FrameCount images (low 16 bits) | `cfg.acq_mode`, `cfg.acq_frames` |
| ConnectionConfig (0x4014) write, any accepted value | restarts the stream PacketTags (§8.5.3, §10.3.33) and flushes the stream path, so the stream resumes with the next whole image | `conn_cfg_wr` |
| StreamPacketSizeMax (0x4010) | whole-packet bytes → payload words /4 − 8; while it is below 36 bytes (0 included) no image enters and no stream packet is sent (Table 44) | `cfg.dsizeP`, `cfg.stream_en` |
| TestErrorCount / TestPacketCountTx / TestPacketCountRx (0x4024–0x4034) | read the live counters; writing 0 to one clears that one (§10.3.37–39) | `sb_status.lt_*`, `clr_lt_err` / `clr_lt_pkt_tx` / `clr_lt_pkt_rx` |

Source `src/rtl/top/cxp_device_top.sv`. Instances: `cxp_cdc_reset_i` (`:319`), `cxp_interface_top_i` (`:333`) and `cxp_ctrl_bootstrap_regs_i` (`:412`), with `p_NUM_LINKS` = 1. The configuration reaches `cxp_interface_top` as one `cxp_cfg_t` (`cfg`, built from the registers and the four straps in one assignment) and its status comes back as one `cxp_status_t`. The user window of `cxp_interface_top` is set by `p_USER_BASE` / `p_USER_SIZE`: with the default size 0 every register access goes to the register file and `m_apb_*` stays idle; otherwise accesses in [base, base + size) go to `m_apb_*` (the APB3 master inside the control plane), the rest to the register file. Used by `src/emu/bridge/tb/cxp_hw_env.sv` (one clock, interactive) and `src/tb_unit/top/cxp_device_top` (three unrelated clocks, `p_ASYNC_CLOCKS` = 1). `src/tb_unit/top/cxp_interface_top` and `src/verif/uvm/sv/tb_cxp_top.sv` do not use it: their tests reach into `cxp_ctrl_bootstrap_regs_i` by hierarchy and inject APB faults, so they keep `cxp_interface_top` with the register file behind its APB port (whole address space in the user window). `make lint` lints this top twice, with `p_ASYNC_CLOCKS` 0 and 1.

## Interface

| Name | Default | Meaning |
|---|---|---|
| `p_TPG_X_SIZE`, `p_TPG_Y_SIZE` | 64, 32 | TPG maximum geometry, passed through. |
| `p_PIXEL_FORMAT_RESET`, `p_IMAGE1_STREAM_ID_RESET` | Mono8, 1 | Power-on values of PixelFormat and Image1StreamID (passed to the register file under the same names; until 2026-09-27 `p_TPG_PIXFMT` / `p_TPG_STREAMID`). |
| `p_FIFO_DEPTH` | 1024 | Stream CDC FIFO depth (words). |
| `p_PIX_W` | 16 | Sensor pixel width. |
| `p_CTRL_BUF_DEPTH` | 64 | Control read/write buffer (words). |
| `p_OS_RATIO` | 16 | `rx_clk` ÷ LS bit rate. |
| `p_SAMP_LOCK_HITS` / `p_RX_LOSS_WORDS` | 2 / 20 000 | Uplink lock thresholds: K28.5 hits to lock, words without IDLE to lose the link. |
| `p_TRIG_ACK_TIMEOUT` | `cxp_pkg::TRIG_ACK_TIMEOUT` 4096 | `tx_clk` cycles a device trigger packet waits for the host's I/O acknowledgment before the next may go (§8.3.3); passed to `cxp_interface_top`. |
| `p_LINK_RESET_CLEAR_CYCLES` | 8 | Minimum ConnectionReset bit lifetime in the register file (`cxp_ctrl_bootstrap_regs` defaults to 16); the bit also waits for the tx echo. `p_CONN_RESET_TIMEOUT` keeps its default 65535. |
| `p_RX_CLK_KHZ` | 20 833 · `p_OS_RATIO` | `rx_clk` frequency in kHz; the control plane's per-command Wait (100 ms) and timeout (900 ms) are counted in ms of it. |
| `p_USER_BASE` / `p_USER_SIZE` | 0x0002_0000 / 0 | User window [base, base + size) on `m_apb_*`; size 0 = off. The window takes precedence over the register file for its addresses; an elaboration `$error` refuses a window that overlaps the bootstrap block (0 … `BOOTSTRAP_END`), the URL string at 0x6000, the manufacturer window or the XML file, or wraps past 0xFFFF_FFFF. |
| `p_ASYNC_CLOCKS` | 0 | Passed to `cxp_interface_top`: 1 synchronises every crossing. |

| Name | Dir | Width | Domain | Description |
|---|---|---|---|---|
| `app_clk`/`app_rst_n`, `tx_clk`/`tx_rst_n`, `rx_clk`/`rx_rst_n` | in | 1 | — | Pixel, downlink and uplink clocks with asynchronous active-low resets. The three resets act as one: any of them resets the whole device (`cxp_cdc_reset`). The register file runs on `rx_clk`. |
| `cfg_use_tpg_i` | in | 1 | rx | 1 = TPG, 0 = sensor pixels. |
| `cfg_run_i` | in | 1 | rx | Holds the image gate armed without AcquisitionStart, for either source (the TPG free-runs; sensor images enter). |
| `cfg_arbitrary_i` | in | 1 | rx | Arbitrary image header and line markers. |
| `cfg_trig_polarity_i` | in | 1 | rx | Trigger sense, 0 = active high, for both the host trigger (`trig_o`) and the device pin (`trig_i`). |
| `from_extension_link_i` | in | 1 | rx | §5.1 strap: 1 = writes rejected with 0x43, except ConnectionReset and MasterHostConnectionID writes, which are ignored and acknowledged 0x01 (`cxp_ctrl_cmd_parser.md`). |
| `trig_i` | in | 1 | any | Device trigger pin, from any clock: synchronised to `tx_clk` inside `cxp_tx_trigger_hs`. Its level is sent to the host only while the uplink is detected, one packet per acknowledgment or timeout (`cxp_interface_top.md` How it works 7). |
| `trig_o`, `trig_glitch_pulse_o` | out | 1 | rx | Host→device trigger; corrupted trigger seen. |
| `s_pix_data_i`, `s_pix_valid_i`, `s_pix_sof_i`, `s_pix_eol_i`, `s_pix_eof_i` | in | 16, 1 | app | Sensor single-pixel stream. |
| `s_pix_ready_o` | out | 1 | app | Ingress ready. Pixels of an image that does not enter, and pixels outside any image, are taken and dropped, so a free-running sensor is never held off by a stopped acquisition. |
| `s_meta_i` | in | `cxp_meta_t` | app | Sensor frame metadata; `arbitrary` is ignored (the form comes from `cfg_arbitrary_i`). |
| `rx_serial_i` | in | 1 | rx | LS uplink bit. |
| `cxp_if_data_o`, `cxp_if_kmask_o` | out | 32, 4 | tx | Downlink word (P0 in [7:0]) and K flags, to the hard 8B/10B encoder. |
| `m_apb_psel_o`, `m_apb_penable_o`, `m_apb_pwrite_o`, `m_apb_paddr_o`, `m_apb_pwdata_o` | out | 1, 1, 1, 32, 32 | rx | APB3 master of the user window (no PSTRB); all 0 when `p_USER_SIZE` = 0. |
| `m_apb_prdata_i`, `m_apb_pready_i`, `m_apb_pslverr_i` | in | 32, 1, 1 | rx | APB3 response; PSLVERR answers 0x40. A slave that stalls gets one Wait acknowledgment 100 ms into the command and a 0x40 at 900 ms, when the transfer is abandoned. Ignored when `p_USER_SIZE` = 0. |
| `device_user_id_nv_i` | in | 128 | rx | DeviceUserID power-on value, loaded while `rx_rst_n` = 0. |
| `device_user_id_o` | out | 128 | rx | DeviceUserID, for the integrator's NV storage (no write strobe). |
| `rx_lock_o`, `aligned_o`, `link_detected_o` | out | 1 | rx | Uplink status. |
| `link_reset_active_o`, `rate_to_discovery_o` | out | 1 | rx | Both are the register file's ConnectionReset bit: high while a ConnectionReset is in progress; SerDes to discovery rate. |

Notes:
- **Reset:** the three inputs are ANDed into one request; `cxp_cdc_reset` asserts all three domain resets at once (asynchronously) and releases them rx, then tx, then app, each through a 2-flop synchroniser in its own clock, so the inputs need no synchronous release. A reset of one input alone is a reset of the device, including the register file: power-on values, which are the ConnectionReset values (StreamPacketSizeMax 0).
- **Clocking:** the register bus and the register file sit on `rx_clk` beside `cxp_ctrl_plane`, so every register output is an `rx_clk` level and `cxp_interface_top` crosses it (see `cxp_interface_top.md`, Interface notes). With `p_ASYNC_CLOCKS` = 0 the three clocks must be one clock or phase-locked.
- **Not exported:** the error and status pulses of `sb_status` (including `ctrl_nack_*`) and `sb_pix_restart_pulse` (a sensor image cut short), and the register outputs ConnectionConfig (the value; its write pulse is `conn_cfg_wr`), MasterHostConnectionID, DeviceConnectionID, AcquisitionMode, the AcquisitionStart/Stop values (their write pulses are used) and TapGeometry. Each is waived in `src/rtl/cxp_ip.vlt` as "no consumer yet".

## How it works

```
 *_rst_n (AND) ─► cxp_cdc_reset ─► rx / tx / app resets (released in that order)
 rx_serial_i ─► cxp_interface_top ──────────────────────────────► cxp_if_data_o / kmask_o
 s_pix_*_i   ─►   │ reg_req/we/addr/wdata (rx_clk) ▲ cfg_* / conn_reset_active / clr_lt_* (rx_clk)
                  ▼                                │
            reg_we / reg_re ──► cxp_ctrl_bootstrap_regs (rx_clk) ──► register levels
                  ▲                                ▲
                  └── reg_ack/err = ready_o/err_o  └── conn_reset_done, sb_lt_* (rx_clk)
```

1. **Register bus** (`:174`). `cxp_ctrl_bus_master` issues one `reg_req` cycle per word for every address outside the user window; `reg_we`/`reg_re` = `reg_req & reg_we`/`~reg_we` strobe the register file, with `reg_wstrb` as its byte enables (all four except on the last word of a write whose Size is not a multiple of 4), whose `ready_o` answers one cycle later as `reg_ack` with `rdata_o`, and whose `err_o` returns as `reg_err`: the Table 22 code of the access (0x40 nothing there or unaligned, 0x43 read-only, 0x44 read of a write-only register, 0x41 refused value), which the bus master puts in the acknowledgment.
2. **Payload size.** StreamPacketSizeMax is the size of the whole packet in bytes (§10.3.32), so the chopper's payload size is `StreamPacketSizeMax / 4 − 8` (Table 19's SOP, 5 header words, CRC and EOP), at least 1 and at most 65535. `cfg.stream_en` = register ≥ 36 (`SPSM_MIN`: SOP, type, 4 header words, one data word, CRC, EOP, Table 19). While it is 0 (the register reads 0 at power-up and after a ConnectionReset, or a value of 4–32 bytes is written, which the register file accepts) no image of either source enters the gate and the framer drops whatever reaches it; the chopper then cuts at its maximum (`cfg.dsizeP` = 0), and nothing is sent (the `cfg_dsizeP_i` input that fed this state was removed 2026-09-27). Decision: a value too small for one packet holds the stream rather than being refused. The chopper may send less: an image's last packet, and at most `p_FIFO_DEPTH` − 8 words (`cxp_app_stream.md`).
3. **ConnectionReset loop.** A host write of 1 to 0x4000 makes the register file apply its §10.3.28 values once, pulse the three counter clears and set the ConnectionReset bit. The bit (`ctl_connection_reset_active_o`) drives `conn_reset_active`; `cxp_interface_top` crosses it to `tx_clk`, where it restarts the PacketTags, flushes the stream path (the FIFO and the app side, `cxp_interface_top.md` How it works 6), clears TestPacketCountTx and holds the device trigger de-asserted, and echoes it back as `conn_reset_done` once the flush is done. The bit clears once it has been set `p_LINK_RESET_CLEAR_CYCLES` + 1 cycles and the echo is high (or after `p_CONN_RESET_TIMEOUT` + 1 cycles). The write pulse also stops acquisition (`acq_stop = reg_acq_stop | reg_conn_reset`). The write is acknowledged once (0x01) like any write; the end of the reset sends nothing. `conn_reset_req_i` is tied 0.
4. **Test counters.** `sb_lt_err_count`, `sb_lt_pkt_count_tx`, `sb_lt_pkt_count_rx` feed the register file's one-link counter inputs. Writing 0 to one of them pulses its own clear (`ctl_test_err_count_clr_o`, `ctl_test_pkt_tx_clr_o`, `ctl_test_pkt_rx_clr_o` → `clr_lt_err`, `clr_lt_pkt_tx`, `clr_lt_pkt_rx`); a ConnectionReset clears all three.
5. **Reset.** `rst_req_n = app_rst_n & tx_rst_n & rx_rst_n` feeds `cxp_cdc_reset` (`cxp_cdc_reset.md`); its three outputs reset `cxp_interface_top`'s domains and the register file (`rx_rst_s_n`). The crossings therefore never see one side reset without the other.

No FSM of its own. Latency from a register write to the datapath: the write lands at the edge of the `reg_req` cycle, then the `cxp_interface_top` crossing (wire, or a few cycles of each clock with `p_ASYNC_CLOCKS` = 1).

## Arbiter integration

Not applicable: the module adds no packet source. Downlink behaviour is `cxp_interface_top`'s; the register values only select the TPG image, the stream packet size and TestMode.

## Verification

Verilator 5.046 + cocotb 2.0.1, built with `--assert`, so the bound `cxp_sva` checkers run in every test: arbiter (one port taken per word), inserter (trigger and I/O-ack words contiguous, an I/O ack within 3 words of its offer, run ≤ 99), the long-packet owner offering a word every cycle (`cxp_tx_owner_sva`), framers, short packets, IDLE rule on the wire, stream FIFO, uplink long-packet stream, control executor `cxp_ctrl_exec_sva`, ConnectionReset lifetime `cxp_conn_reset_sva`, reset release order `cxp_reset_order_sva`. No FSM coverage is registered.

### TB — `src/tb_unit/top/cxp_device_top/`

Wrapper `tb_cxp_device_top.sv`: `cxp_device_top` with `p_ASYNC_CLOCKS` = 1, `p_TPG_X_SIZE` 64, `p_TPG_Y_SIZE` 32, `p_FIFO_DEPTH` 256, `p_OS_RATIO` 4, `p_TRIG_ACK_TIMEOUT` 800 (`TRIG_ACK_TIMEOUT`); `cfg_use_tpg` and the sensor port (`s_pix_*`, and `s_meta_i` built from `s_meta_xsize`/`ysize`/`pixfmt` with StreamID 1) exposed to Python, `trig_i` from Python (`trig_in`), polarity 0, `device_user_id_nv_i` = 0; a 64-word user window at 0x0002_0000 (`p_USER_SIZE` 0x100) served by an APB slave in the wrapper, whose latency (`apb_latency` `rx_clk` cycles of ACCESS, 0xFFFF = never, 1 after `setup()`) and PSLVERR (`apb_slverr`) Python sets; `p_RX_CLK_KHZ` = 10, so the device's 100 ms Wait and 900 ms timeout are 1000 and 9000 `rx_clk` cycles; `setup()` selects the TPG and idles the sensor port. `setup()` starts `rx_clk` 10 ns, `tx_clk` 8 ns, `app_clk` 12 ns, holds all resets 100 ns and releases `rx_rst_n`, `tx_rst_n` 7 ns later and `app_rst_n` 5 ns after that (inside, `cxp_cdc_reset` releases the domains only once all three are high), then starts a `Host` (`common/cxp_host.py`, `q = cxp_protocol.DEVICE`) and waits for `link_detected`; with `start_host` false the uplink stays low and the link down. The host drives the uplink through `common/cxp_uplink.py` (golden 8B/10B, IDLE between characters), answers device triggers with Table 17 acknowledgments (or drops or delays them, per test), and splits the downlink with the golden deframer: every acknowledgment is CRC-checked and every stream packet goes to the golden reassembler (CRC, tag, DsizeP). At teardown the deframer must have seen no framing error and no run over 99 words.

| Test | Stimulus | Expect |
|---|---|---|
| `test_01_idle` | TPG stopped, 3000 `tx_clk` | IDLE only |
| `test_02_discovery` | 5 register reads | Table 45 values, vendor string |
| `test_03_registers` | Width at its feature address, Height, TestPattern | read back; the Width slot reads the feature address |
| `test_04_stream` | StreamPacketSizeMax 288, Width 12, Height 6, run | image 2 is 12 × 6, no stream errors |
| `test_05_connection_reset` | read StreamPacketSizeMax; write 0x4000 = 1 | 0 at power-up; one write ack, nothing more; StreamPacketSizeMax, TestMode = 0 |
| `test_06_test_mode` | TestMode 1, 2 test packets, TestMode 0 | 1024 counting words each; TestPacketCountTx ≥ 2 |
| `test_07_host_trigger` | one rising host trigger | `trig_o` pulses; one I/O ack 0x01 |
| `test_08_invalid_commands` | opcode 0x02; read of Size 0; reads of 65 and 64 words from the XML ROM | acks 0x42, 0x46, 0x45; the 64-word read answered 0x00 |
| `test_09_access_codes` | read 0x0020; write Standard; PixelFormat alias = 0x0199; write 1 to 0x0001_4000 | acks 0x40, 0x43, 0x41, 0x40; StreamPacketSizeMax unchanged |
| `test_10_powerup_is_connection_reset` | reads after power-up; AcquisitionStart without StreamPacketSizeMax | §10.3.28 values; no stream packet in 2000 words |
| `test_11_single_domain_reset` | one read and one host trigger; `rx_rst_n` alone 20 cycles | nothing on the downlink; a later read answered once |
| `test_12_idle_cadence_per_packet_type` | 250 pin toggles during test packets, 60 during 200-word stream packets, 30 during back-to-back largest reads | every leader followed by its Delay word; one packet per toggle; ≥ 80 cadence positions; run ≤ 99 |
| `test_13_pipelined_cmds` | read right behind a read | no ack carries the other's data |
| `test_14_single_domain_reset_streaming` | streaming; `tx_rst_n` alone, then `app_rst_n` alone | nothing stray; no packet while StreamPacketSizeMax is 0; restart at tag 0 |
| `test_15_conn_reset_postconditions` | every §10.3.28 item primed; ConnectionReset | all post-conditions; one falling trigger; restart at tag 0 |
| `test_16_poll_during_reset_window` | ConnectionReset, then 5 reads of it | one ack each; values never rise again; last 0 |
| `test_17_conn_reset_everything` | ConnectionReset under stream, trigger and host test packet; 5 more | every write acked once; no torn packet; restart at tag 0 |
| `test_18_whole_image_after_conn_reset` | ConnectionReset mid-image; StreamPacketSizeMax again | first packet tag 0 and a whole image |
| `test_19_stop_tpg_mid_packet` | 16 × 8 images, 64-word packets; `cfg_run` dropped 0–70 `tx_clk` cycles (step 7) after an image header, restarted after 0, 1, 600 or 2000 cycles | every image whole, no CRC, tag or DsizeP error |
| `test_20_acq_start_stop_sensor` | sensor path, free-running 16 × 8 sensor; 4 frames; AcquisitionStart; 3 images; AcquisitionStop mid-frame; 6 frames | no packet before the start; whole images; at most one image after the stop |
| `test_21_spsm_below_minimal_packet` | TPG running; StreamPacketSizeMax 32 for 20 000 cycles, then 36 | 32 accepted, no stream packet; at 36 every packet 9 words, images whole |
| `test_22_testmode_exit_whole_image` | TPG running; TestMode 1 for 30 000 cycles, then 0 | the stream resumes with a whole image |
| `test_23_spsm_negotiation` | StreamPacketSizeMax 128, 1024, 4096, 64, 40 between images, 1024 → 128 mid-image, 130 | every packet fits the value in force; images whole; 130 refused 0x41 |
| `test_24_ctrl_wait_ack` | user-window reads with the APB slave answering after 60 ms, 150 ms, never, and 1200 ms; a read of Standard | 0x00 only; 0x04 (one word, 100–10 000 ms) within 200 ms then 0x00; 0x04 then 0x40 before the Wait's time ends; the late answer completes nothing (both reads 0x40); Standard: 0x00, no Wait |
| `test_25_ctrl_reset_during_exec` | 0xFF behind a hung read, after its Wait, behind a 4-word read waiting behind connection-test packets (TestMode), twice back to back | [0x03] or [0x04, 0x03]; [0x04, 0x03]; [0x00 with its data, 0x03]; [0x03, 0x03]; nothing more; the next read gets its own word |
| `test_26_sensor_meta_per_image` | sensor frames of 8 sizes back to back, metadata switched as each last pixel is taken; StreamPacketSizeMax 40 | every header carries its own frame's size; pixels right |
| `test_27_ioack_latency_under_stream` | 200 host triggers during back-to-back 200-word stream packets | one 0x01 ack each; each K28.6 at most 6 words after its tx-side request; ≥ 20 inside a packet |
| `test_28_testmode_vs_stream` | TestMode written inside stream packets and cleared inside test packets, until 5 rounds hit a stream packet | no framing error; each write acked once; every stream packet reassembles; a test packet every round |
| `test_29_ioack_in_testmode` | TestMode; 4 host triggers; TestMode off | each acked within 3000 cycles in TestMode; four acks in all |
| `test_30_tx_trigger_ack_rules` | pin toggled every 5 cycles against a host that acks, drops, or acks late | no trigger before the previous one is acked or timed out; host level ends at the pin; no torn packet |
| `test_31_nested_preempt` | host trigger and device pin edge 6 cycles apart either way, inside a stream packet | 14 I/O acks, 13 trigger packets, none torn; both orders seen; stream reassembles |
| `test_32_trigger_waits_for_link` | pin toggled with the link down; then the host brings the link up | no leader while down; one K28.4 after |
| `test_33_trigger_in_linktest_packet` | Table 15 triggers inside host test packets at words 1, 512, 1023, phases 1–3, TestMode 0 and 1 | TestErrorCount unchanged, TestPacketCountRx + 3, 9 `trig_out` rises and 9 I/O acks per mode (mutant without the sampler's RD flip: 9 test errors) |
| `test_34_lock_loss_mid_cmd` | line held low for 200 words inside a write, after each of its words; back 3 bits off | at most one ack for the cut write, never 0x01; the next read of Width answers 0x00 with the old value (red before the link monitor judged the framing: no answer) |
| `test_35_trigger_in_ctrl_write` | a Table 15 trigger inside a write's data word, phases 0–3 | writes acked 0x01 and effective; 4 `trig_out` rises, 4 I/O acks (mutant without the RD flip: 0x80) |
| `test_36_sensor_meta_tiny_images` | sensor images of 1–8 pixels back to back, metadata moved on as each last pixel is taken | 8 images, each header's size and SourceTag its own (red before the metadata was taken with the first pixel) |
| `test_37_all_resets_at_once` | streaming; a read sent; all three resets for 20 cycles inside the command, after it, or inside its ack on the wire (12 points); host keeps IDLE | at most the read's own 0x00 ack; nothing else until StreamPacketSizeMax is written; relink without host action; restart at tag 0 with whole images (mutant framer state kept over reset: stream wedged) |
| `test_38_tpg_header_from_registers` | Image1StreamID 0x23, StreamFlags 0x02, TapGeometry 1 then 0; TPG images; StreamFlags 0 | TapGeometry 1 answered 0x41; images carry StreamID 0x23, TapG 0, Flags 0x02, then Flags 0; no reassembly error (red before 2026-09-27: Flags 0) |

#### test_01_idle
- *Stimulus*: `setup()` with `cfg_run` = 0; 3000 `tx_clk` cycles.
- *Checks*: at least 3000 words seen; no acknowledgment, stream, trigger, I/O-ack, connection-test or unknown packet.
- *Proves*: no source starts by itself after unrelated reset releases, and the crossed configuration comes up quiet.

#### test_02_discovery
- *Stimulus*: read Standard, Revision, ControlPacketSizeMax, ConnectionConfigDefault (one word each) and the 8 words of DeviceVendorName.
- *Checks*: 0xC0A79AE5, 0x00010001, the register-map ControlPacketSizeMax (0x118 = 280 bytes: 6 framing words plus the 64-word buffer, the limit the parser enforces), 0x00010028; the string is "AcmeCXP" NULL-padded; the multi-word read acks 0x00.
- *Proves*: uplink → control plane → register bus → register file → read buffer read on `tx_clk` → acknowledgment, across the rx→tx response crossing.

#### test_03_registers
- *Stimulus*: write Width = 12 at 0x10000, Height = 6 at 0x10004, TestPattern = 2 at 0x1001C (each must ack 0x01); read the Width slot 0x3000, then Width, Height, TestPattern.
- *Checks*: the slot reads 0x10000; then 12, 6, 2.
- *Proves*: writes over the uplink, and the 0x3000 slot pointing at its feature (Table 45).

#### test_04_stream
- *Stimulus*: Width 12, Height 6 (Mono8), `cfg_run` = 1; up to 200000 `tx_clk` cycles for three image headers.
- *Checks*: image 1 (the second) has xsize × ysize = 12 × 6, 6 lines of 3 words; the reassembler reports no error and no CRC error.
- *Proves*: register → crossed configuration → TPG, and the app→tx stream FIFO with unrelated clocks. The test does not check the packet size against §8.5.2.

#### test_05_connection_reset
- *Stimulus*: read StreamPacketSizeMax; write ConnectionReset = 1 (acked 0x01); 400 `tx_clk` cycles; read StreamPacketSizeMax and TestMode.
- *Checks*: 0 before (§10.3.28: power-up is a connection reset); no further acknowledgment during the wait (§8.6.1.1: one per command); 0 and 0 afterwards.
- *Proves*: the end of the reset adds no acknowledgment. The 0 read after the reset cannot tell a reset from none, since the register was 0 already; test_15 primes every register first.

#### test_06_test_mode
- *Stimulus*: write TestMode = 1; up to 100000 `tx_clk` cycles for two connection-test packets; write TestMode = 0; read TestPacketCountTx (2 words).
- *Checks*: each of the first two packets has 1024 words, word i = bytes 4i, 4i+1, 4i+2, 4i+3 (mod 256) in P0..P3; the ack is 0x00; the 64-bit count is ≥ 2.
- *Proves*: TestMode crosses rx→tx and the 64-bit TX count crosses back through `cxp_cdc_bus`. The count is not compared with the number of packets seen, and the write-0 clear is not exercised.

#### test_07_host_trigger
- *Stimulus*: one rising-edge Table 15 trigger from the host, Delay 0; 3000 `tx_clk` cycles.
- *Checks*: `trig_o` seen high on some `rx_clk` edge; exactly one I/O acknowledgment with code 0x01.
- *Proves*: uplink trigger → `trig_o`, and `trig_pkt_rcvd` → `cxp_cdc_pulse` → `cxp_tx_io_ack`. No latency check.

#### test_08_invalid_commands
- *Stimulus*: SPEC-built commands over the serial uplink: opcode 0x02 (read form); a read of Size 0; a read of N = 65 words (N + 6 words, one over ControlPacketSizeMax = 280 bytes) and a read of N = 64 words, both at the XML ROM (0x9000_0000), the only range long enough now that nothing-there words answer 0x40.
- *Checks*: acks 0x42, 0x46, 0x45 in order; the last read answered 0x00 with 64 data words.
- *Proves*: `cxp_ctrl_cmd_parser`'s refusals reach the wire through the executor and the ack framer, and the advertised size limit is the enforced one.

#### test_09_access_codes
- *Stimulus*: read 0x0020 (nothing there); write 1 to Standard (0x0000); write 0x0199 to PixelFormat at its alias 0x10008; write 1 to 0x0001_4000; read StreamPacketSizeMax.
- *Checks*: acks 0x40, 0x43, 0x41, 0x40; StreamPacketSizeMax reads what it read before.
- *Proves*: the register file's `err_o` → `reg_err` → bus master → acknowledgment path. **Since StreamPacketSizeMax powers up at 0, the last check no longer shows that 0x0001_4000 did not act as ConnectionReset (Minor 4).**

#### test_12_idle_cadence_per_packet_type
- *Stimulus*: `setup` (the host acknowledges every trigger); TestMode = 1 (connection-test packets back to back, every phase of the IDLE cadence on the wire); 250 pin toggles, each 0..120 `tx_clk` cycles after the previous trigger was acknowledged; TestMode = 0; StreamPacketSizeMax = 832 and `cfg_run` = 1 with 60 more toggles; `cfg_run` = 0 and 20 back-to-back largest reads with 30 more.
- *Checks*: every trigger leader is followed on the next word by its Delay word; one trigger packet per toggle; the triggers inside test packets went out at 80 or more distinct positions of the IDLE run; no run of more than 99 non-IDLE words.
- *Proves*: `cxp_tx_inserter` never splits a trigger with an IDLE inside any of the three long packet types, and the trigger waits for each acknowledgment (§8.3.3) across the rx→tx crossing.

#### test_26_sensor_meta_per_image
- *Stimulus*: sensor path; StreamPacketSizeMax 40 (two payload words, so the stream path is the bottleneck); AcquisitionStart; frames 4×4, 5×4, 29×4, 49×7, 57×6, 21×1, 25×1, 13×1 back to back, `s_meta_xsize`/`ysize` switched to the next frame's size in the cycle the last pixel is taken.
- *Checks*: 8 images; each header's Xsize × Ysize is its frame's; every line holds the frame's pixels.
- *Proves*: the header takes the metadata held at the frame-start pulse, not the live sensor ports (Table 38).

#### test_27_ioack_latency_under_stream
- *Stimulus*: StreamPacketSizeMax = 832 (200-word packets, back to back); `cfg_run` = 1; 200 host triggers, rising and falling in turn, each 0..250 `tx_clk` cycles after the previous one has left the uplink.
- *Checks*: one I/O acknowledgment (code 0x01) per trigger; each K28.6 leader on the wire at most 6 words after the tx-side request (`trig_pkt_rcvd_tx`); at least 20 requests fell inside a stream packet; the stream reassembles.
- *Proves*: §8.2.4 insertion of the I/O ack into a stream packet across the rx→tx crossing; the stream packet around it stays intact.

#### test_28_testmode_vs_stream
- *Stimulus*: StreamPacketSizeMax = 832; `cfg_run` = 1; rounds of: 0..1200 cycles after a stream packet's TYPE word, write TestMode = 1; 0..1040 cycles after a test packet's TYPE word, write TestMode = 0; wait for one more image, until five rounds had TestMode take effect inside a stream packet (at most 40 rounds).
- *Checks*: five such rounds; no framing error on the downlink; each write acknowledged once with 0x01 and nothing more; every stream packet reassembles (CRC, tag, DsizeP); a test packet in every round.
- *Proves*: TestMode holds only new stream packets: the packet on the wire completes, and the test packet in flight when TestMode clears completes too (§8.7.4).

#### test_29_ioack_in_testmode
- *Stimulus*: TestMode = 1; after the first test packet, four host triggers, each once the previous one is acknowledged; TestMode = 0; 3000 `tx_clk` cycles.
- *Checks*: each trigger acknowledged (0x01) within 3000 `tx_clk` cycles while TestMode is 1; exactly four acknowledgments in all.
- *Proves*: I/O acks are not held in TestMode, and none is left over to go out when it ends (§8.3.3 has no TestMode exemption).

#### test_30_tx_trigger_ack_rules
- *Stimulus*: the pin toggled every 5 `tx_clk` cycles for 3000 cycles against a host that (a) acknowledges each trigger packet, (b) acknowledges none, (c) acknowledges each `TRIG_ACK_TIMEOUT` + 30 % later; after each phase the pin is held and the wire runs 3 × `TRIG_ACK_TIMEOUT` cycles.
- *Checks*: no trigger packet follows another before an acknowledgment has reached the device between them or `TRIG_ACK_TIMEOUT` cycles have passed; after each phase the host's trigger level equals the pin; every leader is followed by its Delay word.
- *Proves*: the §8.3.3 transmitter rule and the level merge in `cxp_tx_trigger_hs`, through `cxp_cdc_pulse_ioack_rcvd_i`. An acknowledgment counts if it left the uplink at most 200 words (`ACK_RX_WORDS`) before the first packet, since Table 17 names no packet; phase (c) therefore does not show which trigger a late acknowledgment releases (`cxp_interface_top.md` Minor 7).

#### test_31_nested_preempt
- *Stimulus*: host acknowledging device triggers; StreamPacketSizeMax = 832; `cfg_run` = 1; the `tx_clk` latency L from a host trigger leaving the uplink to its tx-side request is measured once; then for d = −6 .. +6: 20 cycles after a stream TYPE word, a host trigger, and the device pin toggled L + d cycles after it left the uplink.
- *Checks*: 14 I/O acknowledgments (one for the calibration trigger) and 13 device trigger packets; no torn short packet; the stream reassembles; both orders ("trigger first" and "acknowledgment first") seen.
- *Proves*: the inserter keeps both two-word packets whole in either order inside a stream packet, and the stream packet resumes.

#### test_32_trigger_waits_for_link
- *Stimulus*: `setup` with the host not started (uplink held low); the pin toggled 0 → 1 → 0 → 1, 400 `tx_clk` cycles apart; 3000 cycles; then the host starts and brings the link up; 3000 cycles.
- *Checks*: no trigger leader on the wire while the link is down; one K28.4 packet after it came up.
- *Proves*: the link gate (`sb_link_detected` → `cxp_cdc_sync_link_i` → `link_up_i`) and that the level, not the edges, is sent once the link is up (§8.3.2).

#### test_37_all_resets_at_once
- *Stimulus*: 16 × 8 TPG images in 64-word packets; at a stream TYPE word a read of Standard goes out without waiting; `rx_rst_n`, `tx_rst_n` and `app_rst_n` are pulsed together for 20 of their own cycles 40–1100 `rx_clk` cycles into the command, 0–400 after its last word, or 0–6 `tx_clk` cycles after the acknowledgment's SOP; the host is not restarted and sends IDLE only; 3000 `tx_clk` cycles.
- *Checks*: at most one acknowledgment and only the read's own (0x00, Standard); no I/O ack, trigger, test or stream packet; the link comes back on IDLE; Standard and StreamPacketSizeMax (0) read back; after StreamPacketSizeMax is written, tag 0 first and whole images; a stream packet was on the wire at 7 of the 12 points.
- *Proves*: R-05 of the state review: a full reset under a live host needs no host action and replays nothing. Stale FIFO contents cannot reach the wire because the framer drops everything while StreamPacketSizeMax is 0 and the tag table is held at 0 then, so mutants that keep the FIFO pointers or the tag table over the reset stay green (equivalent); a framer that keeps its state wedges the stream.

#### test_38_tpg_header_from_registers
- *Stimulus*: StreamPacketSizeMax 288, Width 12, Height 6; Image1StreamID 0x23, StreamFlags 0x02, TapGeometry 1 (refused) then 0; `cfg_run` = 1; four images; StreamFlags 0; two more.
- *Checks*: TapGeometry 1 answered 0x41; images 1–3 carry StreamID 0x23, TapG 0, Flags 0x02; the last image Flags 0; no CRC, tag or DsizeP error.
- *Proves*: every header field of a TPG image that is not latched from the geometry comes from a register (Table 38); red on the RTL before 2026-09-27, where StreamFlags had no output and the TPG sent `p_TPG_FLAGS`.

#### test_24_ctrl_wait_ack, test_25_ctrl_reset_during_exec (control time limits and 0xFF)
- *Stimulus / Checks*: as in the table. Times are measured on `tx_clk` from the command's send and converted with `RX_CLK_KHZ`; every acknowledgment is CRC-checked by the golden deframer.
- *Proves*: §8.6.1.1 end to end with unrelated clocks: one Wait within 200 ms, the final within the time the Wait announced, the APB transfer abandoned at the timeout so a late answer is lost rather than completing the next command, no Wait for a bootstrap register; §8.6.1.2: exactly one 0x03 per 0xFF, a response already presented goes out first, nothing of the abandoned command follows.

#### test_10 – test_23 (resets, ConnectionReset, acquisition and stream size)
- *Stimulus / Checks*: as in the table; each docstring lists them. test_11 and test_14 drive one reset input at a time: through `cxp_cdc_reset` it resets all three domains, so they check that no crossing turns the reset into a stray acknowledgment, trigger or packet, and that the device comes back as after power-up. test_15 primes MasterHostConnectionID, StreamPacketSizeMax, ElectricalComplianceTest, TestErrorCount, TestPacketCountRx, TestPacketCountTx and a held trigger before the write. test_17 toggles `trig_i` every 97 `tx_clk` cycles and sends a host test packet ahead of the write. test_18 sends the ConnectionReset write 37 `tx_clk` cycles after the first image is complete, so it lands inside a later image. test_20 drives a sensor that cannot be stopped (the gate must take and drop its pixels). test_22 keeps the golden deframer running through TestMode entry and exit.
- *Proves*: the §10.3.28 post-conditions this device has, end to end across the three clocks, the one-reset behaviour of `cxp_cdc_reset`, the stream flush on ConnectionReset and TestMode, the image gate on both sources (§11.2.1.4/5) and the Table 44 / §10.3.32 packet size. test_16 reads 0 every time at these clocks (the reset lasts about 20 `rx_clk` cycles, a command about 10 µs), so the window itself is not observed here.

### Other
- `src/emu/bridge/tb/cxp_hw_env.sv` instantiates the module (`p_ASYNC_CLOCKS` = 1, three 10 ns clocks) for the `src/emu/cxp` host; not self-checking.
- `src/tb_unit/top/cxp_interface_top` (16 tests) and `src/verif/` (PyUVM) test the same datapath with an external register file; `src/tb_unit/ctrl/cxp_ctrl_bootstrap_regs` (24 tests) tests the register file alone; `src/tb_unit/tx/cxp_tx_arbiter` (arbiter + inserter) and `src/tb_unit/tx/cxp_tx_trigger_hs` test the transmit scheduler and the device trigger alone.
- `cxp_cdc_reset` has no unit bench; `cxp_reset_order_sva` checks its release order in every test here.

### Running

```
make -C src/tb_unit/top/cxp_device_top WAVES=0 COCOTB_TEST_FILTER=test_05_connection_reset
make -C src/tb_unit device_top       # one target of the regression; `make -C src/tb_unit` runs all
make lint                       # includes cxp_device_top with -Gp_ASYNC_CLOCKS=1
```

2026-09-27, `review/K6` (all three resets at once, test 37): 37/37 PASS, none tagged. Full regression not re-run for this document.

### Not covered in-tree
- The sensor path beyond test_20 and test_26: a sensor that cuts an image short, `s_meta_i` other than Mono8, sensor back-pressure.
- `cfg_trig_polarity_i` = 1 (the wrapper ties it 0), `from_extension_link_i` = 1, `cfg_arbitrary_i` = 1.
- The device trigger with the link dropping and returning, or during a ConnectionReset while a trigger waits for its acknowledgment (test_15 and test_17 hold the pin; the link-drop case is unit-tested in `src/tb_unit/tx/cxp_tx_trigger_hs`).
- `p_TRIG_ACK_TIMEOUT` at its default 4096: the wrapper uses 800.
- `m_apb_pslverr_i` (the wrapper drives `apb_slverr` but no test raises it), and a user-window write.
- The write-0 clear of the test counters, and TestErrorCount.
- TpgRun = 0 with FrameCount (MultiFrame; only the unit TB of `cxp_app_acq_ctrl`), and AcquisitionStop mid-image on the TPG path (test_20 covers it on the sensor path).
- StreamPacketSizeMax lowered below 36 in the middle of an image (Minor 5).
- ConnectionConfig writes over the uplink; the PacketTag restart is covered by `src/tb_unit/top/cxp_interface_top` test_18 (injected strobe) and end to end by the emu validation cases CXP-CAM-DATA-003 and CXP-CAM-REC-006 → Medium 1.
- A reset input released while another is still held for a long time, and resets asserted inside the ~10-cycle `cxp_cdc_link` settle window.
- Clock ratios other than 10/8/12 ns, and `p_ASYNC_CLOCKS` = 0 with a single clock (only through `src/emu/bridge`, not self-checking).
- A refused write inside the manufacturer window, and an unaligned address, end to end (unit level only).

## Known issues and recommendations

### Critical
None.

### Medium
1. **The ConnectionConfig value is not acted on.** The register takes only the default 0x00010028 (other values get 0x41). A write of it restarts the PacketTags (`ctl_connection_config_wr_o` → `conn_cfg_wr`, §8.5.3, §10.3.33), but the value does not reach any SerDes rate control: `ctl_connection_config_o` is unconnected. Fix: export `ctl_connection_config_o` for the transceiver. Effort: 0.5 day.
2. **Fixed 2026-09-27: StreamFlags and TapGeometry ignored the registers.** Both now drive the TPG header (`cfg_flags`, `cfg_tapg`); `p_TPG_FLAGS` / `p_TPG_TAPG` are gone. The sensor header still takes every field from `s_meta_i`. StreamFlags accepts reserved Flags values (bits 7:2, interlace 3; Table 38) because the XML offers 0..255.

### Minor
1. Export the error pulses of `sb_status` (`ctrl_nack_*` among them) and the sensor's framing pulses, or a sticky status word, for debug; today they end at this boundary.
2. `device_user_id_o` has no write strobe, so the integrator must compare values to know when to commit NV storage (`cxp_ctrl_bootstrap_regs.md` Medium 5).
3. `s_meta_i.arbitrary` is ignored; either use it for the sensor branch or remove it from the sensor contract. AcquisitionMode is stored without effect (TpgRun / FrameCount select the gate's mode).
4. `test_09_access_codes` checks that a write of 1 to 0x0001_4000 is not a ConnectionReset by reading StreamPacketSizeMax, which powers up at 0 and so reads 0 either way. Write a non-zero StreamPacketSizeMax first, or read ConnectionReset right after the write. Effort: 10 min.
5. **StreamPacketSizeMax lowered below 36 in the middle of an image cuts that image** [by code reading]. The gate decides at an image's first pixel, so the image goes on into the FIFO, but the framer drops every packet that reaches it while `cfg.stream_en` is low; the host gets the start of the image only. The next image after the value is raised again is whole. Acceptable as host misuse; say so in the register description.
6. Stale header and port comments: the header still maps AcquisitionStart / AcquisitionStop to "which TPG images start", says no image starts "while it reads 0", and "cfg_run_i keeps the generator running"; the `cfg_run_i` port comment says "TPG free-run, no acquisition". All four now apply to both sources and to StreamPacketSizeMax below 36. The ConnectionReset line omits the stream flush. `link_reset_active_o` and `rate_to_discovery_o` are the same signal (the ConnectionReset bit); a SerDes rate change inside the window has no separate handshake.

### Open questions
1. Answered 2026-09-27: `cfg_dsizeP_i` is removed; it never reached the wire (nothing is sent while StreamPacketSizeMax is 0).
2. Designer: what does FrameCount = 0 mean ("0 = 1" in the XML)?
3. Designer: should `cxp_interface_top`'s unit TB and `src/verif/` move onto `cxp_device_top` once they no longer need hierarchical access to the register file?
