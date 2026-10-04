# CoaXPress device IP — PyUVM verification plan

_Generated from the test sources in `src/verif/uvm/tests/` on branch `refactor/rtl-rename` @ `0f53a91`, 2026-09-30. Companion pages: `docs/verification/cxp_verif_atlas.html` (environment structure, interactive) and `docs/design/cxp_rtl_atlas.html` (RTL hierarchy and connectivity)._

## Contents

1. [Scope](#1-scope)
2. [Verification approach](#2-verification-approach)
3. [Environment](#3-environment)
4. [How to run](#4-how-to-run)
5. [Pass / fail criteria common to every test](#5-pass--fail-criteria-common-to-every-test)
6. [Coverage closure](#6-coverage-closure)
7. [Test case summary](#7-test-case-summary)
8. [Test cases](#8-test-cases)
9. [Traceability: specification clause → tests](#9-traceability-specification-clause--tests)
10. [Known gaps and open items](#10-known-gaps-and-open-items)

## 1. Scope

**Design under test.** `cxp_device_top` (`src/rtl/top/cxp_device_top.sv`): the complete CoaXPress 1.1.1 (JIIA CXP-001-2015) device link layer — low-speed uplink receiver, control plane, bootstrap register file, pixel path, stream framer and the downlink scheduler — wrapped by the harness `src/verif/uvm/sv/tb_cxp_top.sv`. The transceiver (8B/10B encoder, SerDes) is outside the DUT: the environment observes the 32-bit word and 4-bit K-mask on `cxp_if_data_o` / `cxp_if_kmask_o` and drives the serial uplink bit `rx_serial`.

**In scope.** Every externally visible behaviour of the device at its pins: uplink reception and link state, control commands and acknowledgments, the register map, stream data, host and device triggers, I/O acknowledgments, the connection test, ConnectionReset and resets, downlink scheduling and IDLE rules, and their interaction.

**Out of scope here.** Block-level corner cases covered by the unit benches in `src/tb_unit/` (30 cocotb benches), the bound assertions in `src/sva/cxp_sva.sv` (compiled into every PyUVM run), and system validation against the virtual camera in `src/emu/`. High-speed uplink (CXP-12 / §8.2.1 HS upconnection) and multi-link configurations are not implemented by the RTL.

## 2. Verification approach

- **Closed loop, device-accurate host.** `host_ctrl` behaves like a CoaXPress host: one command at a time, a 200 ms (device time) acknowledgment timeout with up to two resends, and Wait (0x04) acknowledgments honoured. The host serialiser (`host_uplink_agent`) drives characters with configurable ppm offset and jitter.
- **Scoreboards are the judges.** Every test runs with all twelve scoreboards armed. Each one predicts its channel from a golden reference (`RegRef` register model generated from `src/regmap`, golden stream packer, golden deframer, Delay-law model) and records an error *kind* on any mismatch. A test passes only if no scoreboard recorded any error kind.
- **Test-specific checks** that a scoreboard cannot know go through `env.sb_test.check(cond, kind, msg)`, so they are reported and gated exactly like scoreboard errors.
- **Decisions.** Where the standard leaves a value open, the expected value is a named decision (D1-D10) in `uvm/common/decisions.py`; an undecided (`None`) decision logs and does not gate.
- **Seeds.** One seed, `CXP_SEED`, drives every random stream; it is recorded in each `results.xml`. Nightly uses seed 1; weekly draws a fresh seed per test.
- **Coverage.** `uvm/coverage/model.py` samples functional cells at the end of each test; `make cov_gate` fails a tier when a goal cell was never hit.

## 3. Environment

### 3.1 Harness `tb_cxp_top.sv`

cxp_device_top (instance cxp_device_top_i) — the product boundary: cxp_interface_top + generated register file cxp_ctrl_bootstrap_regs with every register side effect. The shell adds only verification hooks: clock gates, a user-window APB3 slave, flat sensor-metadata packing, IDLE-detect/word counter for the downlink monitor, and hierarchical taps mirrored to ports.

| Parameter | Value / meaning |
|---|---|
| `p_ASYNC_CLOCKS` | 1'b1 default; Makefile ASYNC_CLOCKS (default 1) -> -Gp_ASYNC_CLOCKS; passed to cxp_device_top p_ASYNC_CLOCKS (real CDC crossings) |
| `OS_RATIO` | 16; uplink oversampling -> p_OS_RATIO |
| `RX_CLK_KHZ` | 20; rx cycles per device millisecond -> p_RX_CLK_KHZ (100 ms Wait = 20 us, 900 ms timeout = 180 us at rx 10 ns) |
| `RX_LOSS_WORDS` | cxp_pkg::RX_LOSS_WORDS_DEFAULT (Makefile passes 20000); words without IDLE before link lost -> p_RX_LOSS_WORDS |
| `FIFO_DEPTH` | 1024; stream FIFO depth -> p_FIFO_DEPTH |
| `TPG_X_SIZE` | 8 -> p_TPG_X_SIZE |
| `TPG_Y_SIZE` | 4 -> p_TPG_Y_SIZE |
| `USER_BASE` | 32'h0002_0000 -> p_USER_BASE |
| `USER_WORDS` | 256; USER_SIZE = 4*USER_WORDS -> p_USER_SIZE |
| `fixed_dut_params` | p_PIX_W=16, p_CTRL_BUF_DEPTH=64, p_SAMP_LOCK_HITS=2; device_user_id_nv_i tied 128'h0, device_user_id_o unconnected |

**Clocks and resets.** Python drives app_clk_in/tx_clk_in/rx_clk_in continuously (cocotb Clock via uvm/common/clocks.start_clocks); internal clock = *_clk_in & *_clk_en (gate used to freeze a domain during a retune). app: 10 ns, tx: 10 ns, rx: 10 ns CxpTopTest.APP_PERIOD/TX_PERIOD/RX_PERIOD (all 10 ns); ClkRstAgent.set_cdc_ratio(app_ns, tx_ns, rx_ns) retunes at runtime (conc clock-ratio tests). host bit = OS_RATIO x rx period (160 ns at default) paced by a Timer, not rx_clk edges, with host ppm/phase/jitter offsets app_rst_n/tx_rst_n/rx_rst_n async, active-low; clocks.reset() pulses selected domains ('all'\|'app'\|'tx'\|'rx') for N rx_clk cycles (default 8)

**Models and helpers inside the harness.**

- `user_window_apb3_slave` — rx_clk domain word memory umem[USER_WORDS], reset value 0xA500_0000 \| index (on rx_rst_n). PREADY = psel & penable & usr_latency != 0xFFFF & wait_q >= usr_latency; PSLVERR = usr_slverr & pready; writes land only if !usr_slverr; prdata = umem[paddr[UW+1:2]]. usr_wait records the latency of the last completed access; apb_done/apb_done_rdata/apb_done_slverr capture the handshake one edge later for the passive monitor (the master drops PSEL on the same edge).
- `meta_packing` — ext_meta_* flat inputs + cfg_arbitrary packed into cxp_pkg::cxp_meta_t s_meta (streamid truncated to 8 bits).
- `downlink_helpers` — wire_busy = word is not IDLE (kmask 4'b0111 && data {B5,3C,3C,BC}); tx_words = free-running tx_clk edge counter from t=0 (the monitor sleeps through IDLE runs and reads run lengths from it).
- `link_reset_done_pulse` — sb_link_reset_done = 1-cycle rx_clk pulse on the falling edge of sb_link_reset_active (built in the shell, not a DUT port).
- `TESTCASE` — int unsigned public_flat_rw, written by run_test via cxp_testcase._publish for wave labelling.
- `taps` — hierarchical refs (no driving): cxp_device_top_i.lt_err_count[0], lt_pkt_count_tx[0], lt_pkt_count_rx[0]; cxp_interface_top_i.sb_status.{ctrl_reset_pulse, ctrl_nack_pulse, ctrl_nack_code, pkt_err_pulse, code_err_pulse, disp_err_pulse}; cxp_interface_top_i.sb_pix_restart_pulse / sb_pix_stray_eof_pulse; reg bus reg_req/reg_wr/reg_addr/reg_wdata/reg_wstrb/reg_ready/reg_rdata/reg_err; register values reg_stream_pkt_size, reg_test_mode, reg_conn_cfg_wr, reg_width, reg_height, reg_pixfmt, reg_stream_id. TpgMonitor additionally reads cxp_device_top_i.cxp_interface_top_i.cxp_app_domain_i.cxp_app_tpg_i internals directly from Python (m_pix_*, xsize_q, ysize_q, pixfmt_q, xoffs_q, yoffs_q, srctag_q, cfg_tapg_i, cfg_flags_i).

**Harness signals by interface.**

<details><summary><code>clock_reset</code> (3)</summary>

| Signal | Meaning |
|---|---|
| `app_clk_in / tx_clk_in / rx_clk_in (in)` | free-running clocks driven by Python |
| `app_rst_n / tx_rst_n / rx_rst_n (in)` | per-domain async active-low resets |
| `app_clk_en / tx_clk_en / rx_clk_en (in)` | clock gate enables (0 freezes the domain; used by set_cdc_ratio) |

</details>

<details><summary><code>config_no_register</code> (6)</summary>

| Signal | Meaning |
|---|---|
| `cfg_use_tpg (in)` | 1 = internal TPG is the pixel source, 0 = external s_pix_* port |
| `cfg_run (in)` | acquisition run level (image gate) |
| `cfg_arbitrary (in)` | arbitrary (Table 39/40) vs rectangular image form, packed into s_meta.arbitrary |
| `cfg_dsizeP (in, 16b)` | legacy stream payload size in words; NOT connected to cxp_device_top in this shell (only driven by apply_defaults/CfgDriver) — packet size comes from StreamPacketSizeMax |
| `cfg_trig_polarity (in)` | host-trigger edge that produces trig_o (0 rising, 1 falling) |
| `from_extension_link (in)` | strap: this connection is an extension link (writes refused 0x43, triggers ignored) |

</details>

<details><summary><code>device_trigger</code> (1)</summary>

| Signal | Meaning |
|---|---|
| `trigger_in_app (in)` | device's local trigger pin (§8.3.2 device->host K28.4/K28.2 packets) |

</details>

<details><summary><code>pixel_port</code> (3)</summary>

| Signal | Meaning |
|---|---|
| `s_pix_data[15:0] (in)` | one raw pixel per app_clk |
| `s_pix_valid / s_pix_sof / s_pix_eol / s_pix_eof (in)` | pixel valid and frame/line framing |
| `s_pix_ready (out)` | ingress back-pressure |

</details>

<details><summary><code>frame_metadata</code> (6)</summary>

| Signal | Meaning |
|---|---|
| `ext_meta_xsize/ysize/xoffs/yoffs[23:0] (in)` | image geometry |
| `ext_meta_pixfmt[15:0] (in)` | PFNC pixel format (used when the PixelFormat register reads 0) |
| `ext_meta_tapg[15:0] (in)` | tap geometry |
| `ext_meta_streamid[15:0] (in)` | StreamID (low 8 bits used) |
| `ext_meta_sourcetag[15:0] (in)` | SourceTag (passthrough, decision D9) |
| `ext_meta_flags[7:0] (in)` | image flags |

</details>

<details><summary><code>uplink</code> (1)</summary>

| Signal | Meaning |
|---|---|
| `rx_serial (in)` | host->device low-speed serial line (8B/10B) |

</details>

<details><summary><code>downlink</code> (3)</summary>

| Signal | Meaning |
|---|---|
| `cxp_if_data_o[31:0] / cxp_if_kmask_o[3:0] (out)` | downlink 32-bit word + K mask per tx_clk |
| `wire_busy (out)` | current word is not IDLE |
| `tx_words[31:0] (out)` | tx_clk edges since t=0 |

</details>

<details><summary><code>status_product_ports</code> (8)</summary>

| Signal | Meaning |
|---|---|
| `sb_rx_lock (out)` | rx_lock_o — low-speed sampler lock |
| `sb_aligned (out)` | aligned_o — word alignment |
| `sb_link_detected (out)` | link_detected_o — connection detected |
| `sb_trigger_out_app (out)` | trig_o — recreated host trigger |
| `sb_trigger_glitch_pulse (out)` | trig_glitch_pulse_o — unrepairable trigger received |
| `sb_link_reset_active (out)` | link_reset_active_o — ConnectionReset bit held |
| `sb_link_reset_done (out)` | shell-made 1-cycle pulse when the bit clears |
| `sb_rate_to_discovery (out)` | rate_to_discovery_o — rolled back to discovery rate during reset window |

</details>

<details><summary><code>status_taps</code> (8)</summary>

| Signal | Meaning |
|---|---|
| `sb_lt_err_count[31:0]` | TestErrorCount (link 0) |
| `sb_lt_pkt_count_tx[63:0] / sb_lt_pkt_count_rx[63:0]` | TestPacketCountTx/Rx |
| `sb_ctrl_reset_pulse` | control channel reset (0xFF) executed |
| `sb_ctrl_nack_pulse / sb_ctrl_nack_code[7:0]` | command answered without an access (0x42..0x47, 0x80) |
| `sb_pkt_err_pulse` | uplink packet framing error |
| `sb_rx_code_err_pulse / sb_rx_disp_err_pulse` | 8B/10B code / disparity error |
| `sb_pix_restart_pulse (app_clk)` | SOF inside an image |
| `sb_pix_stray_eof_pulse (app_clk)` | EOF outside an image |

</details>

<details><summary><code>register_bus_taps</code> (2)</summary>

| Signal | Meaning |
|---|---|
| `rb_req / rb_we / rb_addr / rb_wdata / rb_wstrb` | control plane -> register file request |
| `rb_ack / rb_rdata / rb_err[7:0]` | register file answer one cycle later; err = Table 22 code (0 accepted) |

</details>

<details><summary><code>register_value_taps</code> (6)</summary>

| Signal | Meaning |
|---|---|
| `bs_stream_pkt_dsize` | StreamPacketSizeMax as held |
| `bs_test_mode` | TestMode |
| `bs_conn_cfg_wr` | ConnectionConfig write strobe |
| `bs_width / bs_height` | Width / Height registers (not read by any Python component) |
| `bs_pixel_format` | PixelFormat register |
| `bs_stream_id` | Image1StreamID |

</details>

<details><summary><code>user_window_apb</code> (5)</summary>

| Signal | Meaning |
|---|---|
| `apb_psel/penable/pwrite/paddr/pwdata/prdata/pready/pslverr (out)` | mirror of DUT m_apb_* <-> shell slave |
| `apb_done / apb_done_rdata / apb_done_slverr (out)` | handshake captured one rx edge later |
| `usr_latency[15:0] (in)` | slave answer latency in rx cycles, 0xFFFF = never answers |
| `usr_slverr (in)` | answer with PSLVERR |
| `usr_wait[15:0] (out)` | cycles the last access took (not read by Python) |

</details>

### 3.2 Components

| Component | Kind | File | Role |
|---|---|---|---|
| `VideoAgent` | agent | `uvm/agents/video_agent.py` | Active agent for the external single-pixel sensor port. VideoDriver drives one frame per item: sets ext_meta_* levels, then clocks pixels (raw_pixels, or golden 32-bit words unpacked Mono8 LSB-first) with random valid gaps (dval_density) and honours s_pix_ready, counting stalls. VideoMonitor rebuilds each accepted frame with the golden packer (cxp_protocol.stream.pack_line at device_bits(pixfmt, DEVICE)) and publishes a GoldenFrame; TpgMonitor does the same from the internal TPG's output handshake so TPG streams also have a golden. |
| `CfgAgent` | agent | `uvm/agents/cfg_agent.py` | Driver-only agent for the level configuration the device has no register for. Each item writes all six cfg levels on an rx_clk edge, holds hold_cycles, then waits CFG_CROSS_CYCLES=24 rx cycles so the levels cross to app/tx clocks before item_done. |
| `UplinkAgent (host_uplink_agent)` | agent | `uvm/agents/host_uplink_agent.py` | Host end of the low-speed uplink. UplinkDriver owns a cxp_uplink.Uplink serializer (golden 8B/10B, IDLE fill, own Timer-paced bit clock with ppm/phase/jitter) and two lanes: the packet lane (seqr; control commands, link-test packets, IDLE, RAW) and the §8.2.4 insertion lane (trig_seqr; Table 15 triggers at a character boundary, Table 17 I/O acks at a word boundary). All wire formats come from cxp_protocol with quirks.DEVICE. UplinkMonitor is driver-mirrored: the driver publishes each UplinkTxn before driving it (times filled in as it goes). DeviceTriggerResponder is the host's §8.3.3 side: answers every downlink K28.4/K28.2 trigger with an IOACK via insert_now. |
| `HostCtrl` | agent | `uvm/agents/host_ctrl.py` | Closed-loop host control channel (§8.6.1). One command at a time under a Lock: sends via uplink_drv.send_txn, waits for the acknowledgment parsed with gp.parse_ctrl_ack from the downlink, extends the wait on a Wait (0x04) ack, resends after 200 ms device time (ms_ns) up to resends=2 (0xFF never resent), returns final code/data. Also link_up() polls sb_link_detected. |
| `TxWireAgent` | agent | `uvm/agents/tx_wire_agent.py` | Passive downlink monitor. Sleeps through IDLE runs (wakes on wire_busy, reads run lengths from tx_words), classifies each non-IDLE beat with classify_wire_beat, and reassembles long SOP..EOP packets (type byte from word 1) and 2-word short packets (trigger K28.4/K28.2, I/O ack K28.6) — short packets are stripped out of any long packet they pre-empt. Optional DUMP=1 writes downlink.bin.gz (cxp_protocol.dump, FLAG_IDLE_RUNS, capped DUMP_MAX_MB). |
| `ApbSlaveAgent` | agent | `uvm/agents/apb_slave_agent.py` | User-window (0x20000..+1 KiB) APB3 slave control. The driver pushes a profile (latency in rx cycles, NEVER=0xFFFF hang, PSLVERR) into the shell's usr_latency/usr_slverr. The monitor wakes on PSEL, publishes the start of each transfer, then one ApbTxn per completed transfer (wait_cycles measured) or per abandoned one (completed=False when PSEL drops without PREADY), and keeps a mirror of the slave memory. |
| `RegBusAgent` | agent | `uvm/agents/reg_bus_agent.py` | Passive monitor of the internal one-cycle request/ack register bus between the control plane and cxp_ctrl_bootstrap_regs (mirrored by the shell's rb_* taps). Queues requests and pairs each ack in order, publishing one RegTxn with rdata and Table 22 err. |
| `SidebandAgent` | agent | `uvm/agents/sideband_monitor.py` | Passive status monitor. Two loops: rx_clk domain (link levels, ConnectionReset bit/done, error and control pulses, trigger out + glitch, connection-test counters) and app_clk domain (pixel framing pulses). Publishes a snapshot when a level changes and one per clock while any pulse is high; derives sb_trigger_out_app_edge and views sb_cmd_crc_err_pulse (nack 0x80), sb_cmd_logical_err_pulse (0x4x except 0x43), sb_router_reject_pulse (0x43). Keeps `last` snapshot for level lookups. |
| `ClkRstAgent` | agent | `uvm/agents/clk_rst_agent.py` | Utility agent for clocks and resets. do_reset(cycles, domains) calls env callbacks on_reset_start before and on_reset after clocks.reset(); set_cdc_ratio(app, tx, rx) gates all clocks, restarts the Clock tasks with new periods, retunes the host bit clock (uplink_drv.retune_to_clock) and records the ratio. |
| `IoAgent` | agent | `uvm/agents/io_agent.py` | Drives the device's local trigger pin trigger_in_app (tx_clk domain), each edge producing a §8.3.2 K28.4/K28.2 packet. Driver-mirrored monitor: the driver publishes an IoEvent('trig_rise'/'trig_fall') per edge it makes. IoTriggerSeq waits for sb_link_detected, toggles n_edges times with gap tx cycles and leaves the pin low. |
| `CxpScoreboard / TestChecks` | scoreboard | `uvm/scoreboards/sb_base.py` | Base of every scoreboard: err(kind, msg) counts errors per kind (the kind is what EXPECT_FAIL tags name), pending_count() (0 by default) feeds CxpEnv.quiesce, finalize() runs _final_check exactly once. TestChecks (env.sb_test) lets a test report its own checks as scoreboard errors via check(cond, kind, msg). |
| `ControlScoreboard` | scoreboard | `uvm/scoreboards/control_scoreboard.py` | Predicts the acknowledgment and bus accesses of every control command. Prediction: parser codes first (_parser_code: injected CRC/code/disp error 0x80; undefined opcode 0x42; Size 0 or extra words 0x46; > MAX_WORDS data words 0x45; extension-link write D3 code 0x43, or 0x01 for ConnectionReset/MasterHostConnectionID when D3_EXTENSION_MHCID_SILENT), else executed on RegRef (golden register file generated from src/regmap, XML blob from cxp_camera_xml.mem) or on a model of the user-window memory. Matches each reg-bus/APB access to its command, each 0x03 packet to the oldest command (0xFF cancels unanswered commands, D7 allows one waiting command; a third is optional). |
| `RegScoreboard` | scoreboard | `uvm/scoreboards/reg_scoreboard.py` | Checks every register-bus access at the register file against cxp_protocol.regref.RegRef (same YAML source as the RTL). Writes merge through byte enables and update the model; reads compare value and Table 22 code. Live counters come from the sideband monitor's last snapshot (±1 tolerance), ConnectionReset read may be 0 or the current active bit. |
| `StreamScoreboard` | scoreboard | `uvm/scoreboards/stream_scoreboard.py` | Checks every type-0x01 downlink packet (replicas, DsizeP, CRC, PacketTag continuity, gating by StreamPacketSizeMax/TestMode/ConnectionReset) and feeds the idle-stripped payload to a frame reassembler that walks K28.3 image headers (rect Table 37 / arbitrary Table 39) and line markers (rect 2-word / arbitrary 11-word), then compares each frame against the next GoldenFrame (non-blocking; goldens begun before a flush may be skipped). Reference model: GoldenFrame from VideoMonitor/TpgMonitor (golden §9.4.2 packer, cxp_protocol.stream device_bits/dsizel/line_words with quirks.DEVICE), crc32_words (zlib CRC without final XOR, register = wire word), majority_byte. |
| `LinkProtocolScoreboard` | scoreboard | `uvm/scoreboards/link_protocol_scoreboard.py` | Feeds every non-IDLE downlink beat (with an IDLE pushed for each preceding run) through the golden cxp_protocol.packets.Deframer: framing, legal long-packet types, short packets never split by IDLE, and the v1.1.1 K-code lexicon. Longest non-IDLE run is measured and reported (the 99-word IDLE rule itself is an SVA). |
| `LinkStateScoreboard` | scoreboard | `uvm/scoreboards/link_state_scoreboard.py` | Checks sb_link_detected against what the host did: up within UP_WORDS=48 uplink words after a reset (plus 16 words start-up), no drop outside declared windows, required drops when a test makes the uplink illegal (expect_drop), relock within UP_WORDS (expect_up), up at end unless end_down. |
| `LinkErrorScoreboard` | scoreboard | `uvm/scoreboards/link_error_scoreboard.py` | Explains every receiver error pulse (code, disparity, packet framing, trigger glitch) by an injected error: injected kinds counted from UplinkTxn (inject_code_at on non-trigger, inject_disp_at, unrepairable trigger) must each be seen at least once; non-injected kinds must stay 0 except allowed cascades; pulses inside allow() windows are excused. |
| `LinkResetScoreboard` | scoreboard | `uvm/scoreboards/link_reset_scoreboard.py` | Checks §10.3.28 ConnectionReset sequencing: host writes of 1 to 0x4000 (ignored on an extension link per D3) against link_reset_active windows and done pulses, rate_to_discovery mirroring the active bit, window duration. |
| `LinktestScoreboard` | scoreboard | `uvm/scoreboards/linktest_scoreboard.py` | Connection test both directions. RX: every host LINKTEST owes len(lt_error_indices) + \|1024 - lt_n_data\| error counts and one packet count; compared with sideband counters at end and with register reads (±1 packet race). TX: every type-0x04 downlink packet must carry the Table 23 payload (gp.linktest_errors, 1024 words), be >= 16 words after the previous, start only with TestMode 1 (margin 400 ns); TestPacketCountTx matches packets on the wire. Clears on counter write-0 and ConnectionReset. |
| `IoAckScoreboard` | scoreboard | `uvm/scoreboards/io_ack_scoreboard.py` | §8.3.3: every repairable host trigger on the master link (not from_extension_link) must get one K28.6 I/O acknowledgment with code 0x01 x4, paired in order; latency from the trigger's last character to the ack's code word is recorded and bounded by D6 (1 host character time). |
| `RxTriggerScoreboard` | scoreboard | `uvm/scoreboards/rx_trigger_scoreboard.py` | Pairs host Table 15 triggers with sb_trigger_out_app pulses: repairable level changes matching cfg_trig_polarity owe one pulse, resends and wrong-polarity edges none, extension-link triggers none, unrepairable ones a glitch pulse; ConnectionReset drops pending triggers and owes a falling-edge pulse at polarity 1 when the host was asserted. Checks the §8.3.2.1 Delay law: latency from first character minus Delay x (bit/24) must be constant per clock ratio. |
| `TxTriggerScoreboard` | scoreboard | `uvm/scoreboards/tx_trigger_scoreboard.py` | Device->host trigger packets against the edges IoAgent drove: every K28.4/K28.2 packet must change the host-held level, never more packets than edges of a kind, final host level equals pin level, Delay word 0, and §8.3.3 gating — no new trigger packet before the host's I/O ack (from UplinkTxn IOACK t_done_ns) or the TRIG_ACK_TIMEOUT_TX=4096 tx-cycle timeout. ConnectionReset forces pin level 0; excuse() windows let a sense change move the level without counting. |
| `CovSubscriber` | coverage | `uvm/coverage/cov_subscriber.py` | Plain-Python bin counter subscribed to every monitor port (cg_packet_type, cg_short_packet, cg_uplink_packet, cg_errors, cg_router, cg_link_reset, cg_regbus, cg_apb bins). In report_phase it adds the functional model uvm.coverage.model.collect(env) cells and writes cov_summary.json ({test, bins}); a failing model adds cg_model_error. |
| `coverage model` | coverage | `uvm/coverage/model.py` | Functional coverage cells computed at end of test from recorded state of pkt_log, sb_control, sb_stream, sb_linkpro, sb_linkerr, sb_linkstate, sb_linkreset, clkrst_ag, apb_ag.mon and video_ag.drv. GOALS is the list cov_gate requires; NOT_REACHABLE documents excluded cells. |
| `cov gate` | util | `uvm/coverage/gate.py` | `make cov_gate`: merges 00_test_results/<test>/cov_summary.json for TIER_TESTS, prints per-group hits, lists holes and NOT_REACHABLE; exit 1 on any GOALS hole or any test with cg_model_error. |
| `cov merge` | util | `uvm/coverage/merge.py` | `make cov_report`: merges coverage JSON files into coverage_report.html. Effectively dead: the Makefile passes cov_*.xml which nothing produces, so it falls back to cov_*.json / cov_summary.json in the cwd (the last test only). |
| `PacketLog` | subscriber | `uvm/subscribers/packet_log.py` | Records every long and short downlink packet with tx cycles; answers where a short packet sat (where(): 'idle' or '<type>:header\|payload\|tail'), enclosing(), gap_after_eop(). Used by concurrency tests and by coverage cg_insertion. |
| `StreamDumpSubscriber` | subscriber | `uvm/subscribers/stream_dump_subscriber.py` | Writes every downlink long packet to cxp_stream_dump.txt with a decoded summary (stream StreamID/tag/DsizeP/CRC OK\|BAD, ctrl-ack code/size) and a per-word annotated dump walking image headers and line markers. Diagnostic only; no checks. Its rect header labels (word 17 'HDR_RESERVED', DsizeL[15:0]) disagree with the scoreboard's 24-bit DsizeL decode (b[17..19]). |
| `CxpRegBlock (RAL)` | ral | `uvm/ral/cxp_reg_block.py` | Hand-rolled declarative register mirror (16 bootstrap fields, RO/RW/RW1C, reset values) instantiated as env.ral (not a component). Only test_ral_sweep uses it, calling write_mirror; read_mirror is never called and no check reads it — the authoritative model is RegRef in reg/control scoreboards. Reset values look stale vs the device (e.g. STR_PKT_DSIZE reset 0x100 while the device powers up StreamPacketSizeMax = 0). |
| `virtual sequences` | sequence | `uvm/seqs/virtual_seqs.py` | Compose per-agent sequences on the VirtualSequencer handles (apb/cfg/uplink/video/io). VsSmoke: perfect APB + TPG + one random read. VsStressConcurrent: sensor frames + random ctrl + host triggers + linktest in parallel on the packet lane. VsLinkResetStorm: 3 open-loop 0xFF. VsStreamPlusCtrl / PlusTrigger / PlusLinkReset / FullConcurrent: TPG stream with ctrl traffic, device triggers, a ConnectionReset via HostCtrl then SPSM rewrite, or all at once. All are used by all_tests.py. |
| `TpgCfgSeq` | sequence | `uvm/seqs/tpg_cfg.py` | Open-loop register writes of Width/Height/PixelFormat/TestPattern at their feature addresses (regmap.WIDTH_ALIAS, HEIGHT_ALIAS, PIXEL_FORMAT_ALIAS, TEST_PATTERN) followed by gap_words IDLE. DEAD: not imported by any test (tests use HostCtrl instead). |
| `cxp_pkg (env-local)` | util | `uvm/common/cxp_pkg.py` | Environment transaction types and constants: K-code bytes, beat classification, UplinkTxn (what to send + injection knobs + serializer times), bootstrap address enum, header word counts (RECT_HDR_WORDS=25, ARB_HDR_WORDS=16), DEFAULT_PKT_DSIZE_P=256, STREAM_PKT_OVERHEAD_WORDS=8, ConfigDB keys cfg_dsizeP/host_ppm/host_phase_ps/host_jitter_ui. Wire formats themselves live in cxp_protocol. |
| `build knobs` | util | `uvm/common/build.py` | Reads CXP_<NAME> env exported by the Makefile so Python agrees with the -G build parameters; CxpTopTest.REQUIRES is checked against these. |
| `clocks` | util | `uvm/common/clocks.py` | Starts/kills the three cocotb Clock tasks and enables the gates, keeps the current periods, pulses domain resets for N rx cycles (+2 cycles after release), converts device ms to ns (ms x RX_CLK_KHZ x rx period). |
| `defaults` | util | `uvm/common/defaults.py` | Drives every shell input to a clean value in build_phase before clocks start: cfg_* 0 (cfg_dsizeP=pkt_dsize), trigger low, pixels/meta 0, rx_serial 1, usr_latency 0, usr_slverr 0, resets asserted, clock enables 1. |
| `handles` | util | `uvm/common/handles.py` | Module-level slot for the cocotb dut, set by run_test; every component uses get_dut() instead of ConfigDB. |
| `seed` | util | `uvm/common/seed.py` | One seed CXP_SEED (default 1) for every random stream; rng(tag, salt) = random.Random(f'{SEED}:{tag}:{salt}'). Recorded in results.xml as cxp_seed. |
| `decisions` | util | `uvm/common/decisions.py` | Expected values that are choices, not spec facts; None = undecided (logs, does not gate). See 'decisions'. |
| `CxpEnv / VirtualSequencer` | test-base | `uvm/tests/env.py` | Builds all agents, 11 scoreboards + TestChecks, CovSubscriber, StreamDumpSubscriber, PacketLog, RAL block and the virtual sequencer; wires analysis ports in connect_phase. Provides quiesce() (stop source, wait for 64 IDLE words and all pending_count()==0, 400 us timeout), reset_starts/reset_models (reset callbacks), scoreboards() name map and error_kinds() (finalises every scoreboard). |
| `CxpTopTest` | test-base | `uvm/tests/base_test.py` | Base of every test_*: records plan/seed/EXPECT_FAIL properties to results.xml, enforces REQUIRES build knobs, publishes ConfigDB knobs, builds CxpEnv, applies defaults; run_phase starts clocks, resets, bring-up (link_up + StreamPacketSizeMax = 4 x (PKT_DSIZE_P + 8) via HostCtrl), main_seq(), quiesce. check_phase collects per-kind errors; final_phase fails on untagged errors or tags that never fired. |
| `run_test` | test-base | `uvm/tests/run_test.py` | Single cocotb entry (MODULE=uvm.tests.run_test): imports all_tests/spec_tests/conc_tests, publishes the test name to TESTCASE, set_dut, uvm_root().run_test(UVM_TESTNAME, default test_idle_baseline), 200 ns grace. |
| `cocotb_sim.mk` | util | `src/verif/common/cocotb_sim.mk` | Shared simulator fragment for unit benches and PyUVM: SIM=verilator default (vsim/questasim/modelsim -> questa), Verilator args -Wall -Wno-fatal -Werror-USERERROR --assert --timing --public-flat-rw, WAVES/WAVES_FMT with a build stamp that wipes sim_build on change; Questa +define+CXP_SVA_FATAL, GUI; sources RTL_SOURCES + SVA_SOURCES (src/rtl/cxp_ip.mk) + TB_SOURCES; PYTHONPATH TB_DIR:COMMON_DIR:src/model/cxp_protocol. |
| `cxp_uplink` | model | `src/verif/common/cxp_uplink.py` | Serial uplink driver shared by unit benches and PyUVM: golden 8B/10B with RD kept across calls, IDLE keep-alive, send/post/insert (character- or word-boundary insertion, §8.2.4), idle/hold/slip, symbol corruption and RD flip at a character index, Timer-paced bit clock with ppm/phase/jitter or os_ratio clk-edge pacing, retune. |
| `cxp_host` | model | `src/verif/common/cxp_host.py` | Golden CoaXPress host for device-level cocotb benches (tb_unit top/cxp_device_top etc.): commands with CRC-checked acks, stream reassembly via golden deframer, trigger send, auto I/O-ack of device triggers, max_run. Not used by the PyUVM env (HostCtrl replaces it there). |
| `cxp_link_check` | util | `src/verif/common/cxp_link_check.py` | Unit-bench teardown checks: golden deframer framing errors and max 99-word non-IDLE run on the downlink. Not used by PyUVM. |
| `cxp_testcase` | util | `src/verif/common/cxp_testcase.py` | @cxp_test decorator for unit benches: TESTCASE number, FSM-coverage samplers, teardown checks, finding= tags written to expected_fail.json. PyUVM uses only _publish for TESTCASE. |
| `fsm_coverage` | coverage | `src/verif/common/fsm_coverage.py` | Python FSM state/arc coverage for unit benches (samples state_q each clock, fsm_coverage.json, waivers via unreachable=, extra arcs reported). Not used by PyUVM. |
| `cxp_8b10b` | util | `src/verif/common/cxp_8b10b.py` | Thin view of golden cxp_protocol.enc8b10b (encode/decode, K-code bytes) for benches. |
| `cxp_bus` | util | `src/verif/common/cxp_bus.py` | Single-cycle register-file master for the cxp_ctrl_bootstrap_regs unit bench. Not used by PyUVM. |
| `cxp_gapped` | util | `src/verif/common/cxp_gapped.py` | Word/valid bus driver with random gaps and junk on the bus while valid is low (catches valid-blind RX blocks), idle_check hook. Unit benches only. |
| `cxp_reset` | util | `src/verif/common/cxp_reset.py` | One-sided per-domain reset pulse aligned to falling edges. Unit benches only. |
| `cxp_reglog` | util | `src/verif/common/cxp_reglog.sv` | Opt-in bound SV trace of control commands and register accesses (emu bridge REGLOG=1). Not compiled in PyUVM. |

### 3.3 Scoreboard checks

**`CxpScoreboard / TestChecks`** (`uvm/scoreboards/sb_base.py`) — Base of every scoreboard: err(kind, msg) counts errors per kind (the kind is what EXPECT_FAIL tags name), pending_count() (0 by default) feeds CxpEnv.quiesce, finalize() runs _final_check exactly once. TestChecks (env.sb_test) lets a test report its own checks as scoreboard errors via check(cond, kind, msg).  
Inputs: none

- TestChecks: whatever kinds the tests pass to env.sb_test.check() (spec_tests / conc_tests)

**`ControlScoreboard`** (`uvm/scoreboards/control_scoreboard.py`) — Predicts the acknowledgment and bus accesses of every control command. Prediction: parser codes first (_parser_code: injected CRC/code/disp error 0x80; undefined opcode 0x42; Size 0 or extra words 0x46; > MAX_WORDS data words 0x45; extension-link write D3 code 0x43, or 0x01 for ConnectionReset/MasterHostConnectionID when D3_EXTENSION_MHCID_SILENT), else executed on RegRef (golden register file generated from src/regmap, XML blob from cxp_camera_xml.mem) or on a model of the user-window memory. Matches each reg-bus/APB access to its command, each 0x03 packet to the oldest command (0xFF cancels unanswered commands, D7 allows one waiting command; a third is optional).  
Inputs: uplink_xp <- uplink_ag.mon.ap; reg_xp <- reg_ag.mon.ap; apb_xp <- apb_ag.mon.ap; apb_start_xp <- apb_ag.mon.ap_start; wire_xp <- txwire_ag.mon.ap_pkt; sideband_xp <- sideband_ag.mon.ap. Publishes nothing; cov list, codes_seen, waits, reset_expected, reset_aborted_access, orphans_drained read by coverage model.

- ack_parse — 0x03 packet does not parse with gp.parse_ctrl_ack
- ack_code_unknown — code not in Table 22 set
- ack_replica — type or code word not four equal data bytes (kmask 0)
- ack_length — 0x00 ack not N+6 words; Wait not 7 words/Size 4; other codes not 4 words; read data word count != N
- ack_pad — pad bytes of last data word (Size % 4) not 0
- ack_crc — CRC of 0x00 or Wait ack wrong (golden parser crc_ok)
- ack_wait_time — Wait announces outside 100..10000 ms
- ack_unexpected — ack with no command waiting
- ack_late — ack later than 200 ms (ms_ns) after command's last character (or after previous final ack for a queued command), or later than the announced Wait time
- wait_bootstrap — Wait for a non-user-window (register file) access
- wait_twice — second Wait for one command
- ack_code — final code not the predicted one (user window: 0x40 if PSLVERR or abandoned, else 0x00/0x01)
- access_count — accepted command made fewer/more accesses than its words
- access_rejected — rejected command made accesses
- stray_access — reg/APB access with no command expecting one
- access — access address/direction/bus (reg vs apb) not the expected one
- ack_size — read ack Size != command Size
- ack_data_bus — ack data != data the bus returned (masked by Size)
- ack_data — ack data != RegRef / user-memory model value (live counters and ConnectionReset left open)
- ack_missing (final) — non-optional command never acknowledged
- crc_count (final) — 0x80 nack pulses not within [crc_injected, crc_injected+crc_maybe]
- reset_pulse_count (final) — sb_ctrl_reset_pulse count != 0xFF commands expected

**`RegScoreboard`** (`uvm/scoreboards/reg_scoreboard.py`) — Checks every register-bus access at the register file against cxp_protocol.regref.RegRef (same YAML source as the RTL). Writes merge through byte enables and update the model; reads compare value and Table 22 code. Live counters come from the sideband monitor's last snapshot (±1 tolerance), ConnectionReset read may be 0 or the current active bit.  
Inputs: reg_xp <- reg_ag.mon.ap; sideband_xp <- sideband_ag.mon.ap (drained and discarded); direct handle sb_mon = sideband_ag.mon

- read_code — rb_err != model code (0 / 0x40 undecoded / 0x44 write-only)
- read_value — rdata != model value (counters ±1, ConnectionReset 0 or active bit allowed)
- write_code — rb_err != model write code (0 / 0x41 invalid value / 0x43 read-only)

**`StreamScoreboard`** (`uvm/scoreboards/stream_scoreboard.py`) — Checks every type-0x01 downlink packet (replicas, DsizeP, CRC, PacketTag continuity, gating by StreamPacketSizeMax/TestMode/ConnectionReset) and feeds the idle-stripped payload to a frame reassembler that walks K28.3 image headers (rect Table 37 / arbitrary Table 39) and line markers (rect 2-word / arbitrary 11-word), then compares each frame against the next GoldenFrame (non-blocking; goldens begun before a flush may be skipped). Reference model: GoldenFrame from VideoMonitor/TpgMonitor (golden §9.4.2 packer, cxp_protocol.stream device_bits/dsizel/line_words with quirks.DEVICE), crc32_words (zlib CRC without final XOR, register = wire word), majority_byte.  
Inputs: video_xp <- video_ag.mon.ap and video_ag.tpg_mon.ap; wire_xp <- txwire_ag.mon.ap_pkt; sideband_xp <- sideband_ag.mon.ap (ConnectionReset windows). Env calls flush_window/close_flush_window/device_reset. frame_cov, pkt_cov, tag_events, conn_cfg_writes, frames_flushed/torn read by coverage.

- pkt_short — stream packet < 8 words
- dsizep_vote — DsizeP replica majority failed
- replica — type/StreamID/PacketTag/DsizeP words (or marker byte words) not one byte x4 with kmask 0
- tag_vote — PacketTag majority failed
- tag_continuity — PacketTag not previous+1 mod 256 (0 after ConnectionReset / ConnectionConfig write / device reset)
- sop_spsm_off — packet SOP while StreamPacketSizeMax < 36 for the whole margin
- sop_test_mode — packet SOP with TestMode=1 held
- sop_reset_window — packet SOP inside a ConnectionReset window
- pkt_over_spsm — packet bytes > SPSM in force (with FIFO drain allowance after a change)
- dsizep_mismatch — DsizeP header != non-IDLE payload word count
- crc — CRC word != crc32_words(non-IDLE payload)
- framing — image cut outside a flush window, non-marker before header, header type not 0x01/0x03, bad line-marker type
- vote — header/marker byte majority failed
- kmark_partial — K28.3 in some lanes only
- streamid — image header StreamID != carrying packet's StreamID
- dsizel — header/line-marker DsizeL != golden dsizel(xsize, bits)
- line_marker — arbitrary line marker xsize/xoffs/dsizeL differs from row 0
- header_field — xsize/ysize/xoffs/yoffs/pixfmt/tapg/streamid/sourcetag/flags != golden
- pixel_count — pixel word count != golden
- pixel_data — first differing pixel word
- lost_frame (final) — golden frame never seen on wire (not excused by a flush)
- unmatched_frame (final) — frame on wire with no golden to compare

**`LinkProtocolScoreboard`** (`uvm/scoreboards/link_protocol_scoreboard.py`) — Feeds every non-IDLE downlink beat (with an IDLE pushed for each preceding run) through the golden cxp_protocol.packets.Deframer: framing, legal long-packet types, short packets never split by IDLE, and the v1.1.1 K-code lexicon. Longest non-IDLE run is measured and reported (the 99-word IDLE rule itself is an SVA).  
Inputs: beat_xp <- txwire_ag.mon.ap_beat; run_hist read by coverage

- short_split — IDLE inside a trigger / I/O-ack short packet
- packet_type — long packet type not 0x01/0x03/0x04
- framing — each golden Deframer error (SOP in packet, EOP outside, stray word); final: test ended inside a long packet
- kcode — illegal K character in a lane (K28.0 flagged), or K28.3 not a whole marker word

**`LinkStateScoreboard`** (`uvm/scoreboards/link_state_scoreboard.py`) — Checks sb_link_detected against what the host did: up within UP_WORDS=48 uplink words after a reset (plus 16 words start-up), no drop outside declared windows, required drops when a test makes the uplink illegal (expect_drop), relock within UP_WORDS (expect_up), up at end unless end_down.  
Inputs: sideband_xp <- sideband_ag.mon.ap; env.reset_starts calls allow_drop, env.reset_models calls device_reset; history/drops read by coverage

- link_drop — link detected fell outside an allowed window
- link_up_late — link came up after its deadline
- link_drop_late — link lost after the expect_drop deadline
- link_no_drop (final) — expected drop never happened
- link_not_up (final) — link still down past its up deadline, or test ended with link down

**`LinkErrorScoreboard`** (`uvm/scoreboards/link_error_scoreboard.py`) — Explains every receiver error pulse (code, disparity, packet framing, trigger glitch) by an injected error: injected kinds counted from UplinkTxn (inject_code_at on non-trigger, inject_disp_at, unrepairable trigger) must each be seen at least once; non-injected kinds must stay 0 except allowed cascades; pulses inside allow() windows are excused.  
Inputs: uplink_xp <- uplink_ag.mon.ap; sideband_xp <- sideband_ag.mon.ap; env.reset_starts calls allow(); injected dict read by coverage

- code_missing / disp_missing / pkt_missing / glitch_missing — fewer pulses (seen+excused) than injected
- code_unexpected / disp_unexpected / pkt_unexpected / glitch_unexpected — pulses with nothing injected (disp/pkt tolerated if any code/disp injected; code tolerated if disp injected)

**`LinkResetScoreboard`** (`uvm/scoreboards/link_reset_scoreboard.py`) — Checks §10.3.28 ConnectionReset sequencing: host writes of 1 to 0x4000 (ignored on an extension link per D3) against link_reset_active windows and done pulses, rate_to_discovery mirroring the active bit, window duration.  
Inputs: uplink_xp <- uplink_ag.mon.ap; sideband_xp <- sideband_ag.mon.ap; active_windows read by coverage

- rate_mirror — sb_rate_to_discovery != sb_link_reset_active in an event
- window_long — ConnectionReset held > 200 ms (device time)
- done_no_window — done pulse without an open window
- window_done (final) — windows != done pulses
- no_window (final) — requests sent but no window
- excess_windows (final) — more windows than requests

**`LinktestScoreboard`** (`uvm/scoreboards/linktest_scoreboard.py`) — Connection test both directions. RX: every host LINKTEST owes len(lt_error_indices) + \|1024 - lt_n_data\| error counts and one packet count; compared with sideband counters at end and with register reads (±1 packet race). TX: every type-0x04 downlink packet must carry the Table 23 payload (gp.linktest_errors, 1024 words), be >= 16 words after the previous, start only with TestMode 1 (margin 400 ns); TestPacketCountTx matches packets on the wire. Clears on counter write-0 and ConnectionReset.  
Inputs: uplink_xp <- uplink_ag.mon.ap; sideband_xp <- sideband_ag.mon.ap; pkt_xp <- txwire_ag.mon.ap_pkt; reg_xp <- reg_ag.mon.ap

- tx_payload — device test packet not 1024 words or differs from Table 23
- tx_gap — < 16 words between device test packets
- tx_after_testmode — device test packet started with TestMode 0
- reg_err_count — TestErrorCount read outside expected range
- reg_pkt_count_rx — TestPacketCountRx (low word) read outside range
- reg_pkt_count_tx — TestPacketCountTx (low word) read outside range
- sideband_dead (final) — host sent packets but no sideband events
- err_count (final) — TestErrorCount != expected
- pkt_count_rx (final) — TestPacketCountRx != host packets
- tx_no_packet (final) — expect_tx_linktest set but no test packet sent
- tx_count (final) — TestPacketCountTx != test packets on the wire

**`IoAckScoreboard`** (`uvm/scoreboards/io_ack_scoreboard.py`) — §8.3.3: every repairable host trigger on the master link (not from_extension_link) must get one K28.6 I/O acknowledgment with code 0x01 x4, paired in order; latency from the trigger's last character to the ack's code word is recorded and bounded by D6 (1 host character time).  
Inputs: uplink_xp <- uplink_ag.mon.ap; short_xp <- txwire_ag.mon.ap_short; char_ns set by env.start_of_simulation_phase from uplink_drv.char_ns

- code — I/O-ack payload not 0x01 in all four lanes
- unexpected — I/O ack with no host trigger waiting
- latency — ack later than D6 x char_ns after trigger's last character (measured to the code word, one word after the K28.6 leader)
- count (final) — host triggers never acknowledged

**`RxTriggerScoreboard`** (`uvm/scoreboards/rx_trigger_scoreboard.py`) — Pairs host Table 15 triggers with sb_trigger_out_app pulses: repairable level changes matching cfg_trig_polarity owe one pulse, resends and wrong-polarity edges none, extension-link triggers none, unrepairable ones a glitch pulse; ConnectionReset drops pending triggers and owes a falling-edge pulse at polarity 1 when the host was asserted. Checks the §8.3.2.1 Delay law: latency from first character minus Delay x (bit/24) must be constant per clock ratio.  
Inputs: uplink_xp <- uplink_ag.mon.ap; sideband_xp <- sideband_ag.mon.ap; jitter_ui set by env from uplink_drv; device_reset() from env.reset_models

- glitch_unexpected — glitch pulse with no damaged trigger
- trig_unexpected — trig_out pulse with no trigger waiting
- trig_missing (final) — owed trig_out pulses never came
- glitch_missing (final) — damaged triggers gave no glitch pulse
- latency_spread (final) — normalised latency spread above limit (Delay law)

**`TxTriggerScoreboard`** (`uvm/scoreboards/tx_trigger_scoreboard.py`) — Device->host trigger packets against the edges IoAgent drove: every K28.4/K28.2 packet must change the host-held level, never more packets than edges of a kind, final host level equals pin level, Delay word 0, and §8.3.3 gating — no new trigger packet before the host's I/O ack (from UplinkTxn IOACK t_done_ns) or the TRIG_ACK_TIMEOUT_TX=4096 tx-cycle timeout. ConnectionReset forces pin level 0; excuse() windows let a sense change move the level without counting.  
Inputs: io_xp <- io_ag.mon.ap; short_xp <- txwire_ag.mon.ap_short; uplink_xp <- uplink_ag.mon.ap; sideband_xp <- sideband_ag.mon.ap

- before_ack — trigger packet within the timeout with no I/O ack since the previous one
- sequence — packet does not change the host's level
- delay — Delay word non-zero
- rise_count / fall_count (final) — more packets than edges of that kind
- final_level (final) — host level != pin level at end

### 3.4 Decisions (`uvm/common/decisions.py`)

Expected values the standard leaves open. Checks that depend on them read them from this table.

| Decision | Value | Meaning |
|---|---|---|
| `D1_UPLINK_BAD_TYPE` | `discard` | REQ-PROT-019: reserved/wrong-direction uplink packet type is silently discarded — no ack, no access, no error pulse (T-12) |
| `D2_TESTMODE_TRIGGERS` | `allowed` | REQ-PROT-027: triggers and I/O acks go out in TestMode, inserted into test packets (C-06, CT-007) |
| `D3_EXTENSION_WRITE_CODE` | `0x43` | REQ-ERR-012: a write on an extension connection is refused 0x43 |
| `D3_EXTENSION_MHCID_SILENT` | `True` | ConnectionReset and MasterHostConnectionID written on an extension link are acknowledged 0x01 and ignored |
| `D3_EXTENSION_CTRL_RESET` | `execute` | 0xFF on an extension link is executed (resets the control channel, not the connection) |
| `D4_SPSM_BAD_VALUE` | `0x41` | REQ-INIT-009: StreamPacketSizeMax not a multiple of 4 is refused 0x41, value kept (T-06) |
| `D4_SPSM_BELOW_MIN` | `accept_hold` | a multiple of 4 below 36 bytes is accepted; no image enters until SPSM >= 36 (stream held, wire IDLE) |
| `D4_SPSM_MIN_BYTES` | `36` | minimum SPSM (header + one data word); used by sb_stream gate checks |
| `D5_SELECTOR_OUT_OF_RANGE` | `0x41` | REQ-PROT-031/BOOT-004: XmlManifestSelector / TestErrorCountSelector out of range refused 0x41, value kept (T-03, T-25) |
| `D6_IOACK_LATENCY_CHARS` | `1` | REQ-TRIG-011: I/O-ack latency guaranteed within one low-speed host character (checked by sb_ioack; C-02, TRIG-006) |
| `D7_OVERLAPPED_COMMANDS` | `serialise` | REQ-CTRL-016: overlapped host commands are serialised; each gets its own ack in order (T-11) |
| `D7_WAITING_COMMANDS` | `1` | one command may wait behind the executing one; a further one is dropped without ack (0xFF always gets through) |
| `D8_CODE_PRIORITY` | `(0x47, 0x42, 0x46, 0x80, 0x45)` | REQ-ERR-010/011: first-match priority of error codes (T-08) |
| `D8_SIZE_ZERO_CODE` | `0x46` | read/write with Size 0 answered 0x46 |
| `D9_SOURCETAG_OWNER` | `passthrough` | pixel-port images carry the sensor metadata SourceTag; the TPG counts its own |
| `D10_FEATURE_PROFILE` | `{'MULTI': False, 'HSUP': False, 'LINESCAN': False, 'INTERLACED': False, 'MSTREAM': False, 'MTAP': False, 'COLOR': False, 'IIDC2': False, 'ECT': True, 'ARB': True, 'D2HTRIG': True, 'LONGOP': True, 'ZIPXML': False}` | DUT feature profile that makes validation-plan rows N/A |
| `D10_GEOMETRY_OUT_OF_RANGE` | `0x41` | Width/Height outside 1..4096 or OffsetX/Y > 4095 refused 0x41, value kept (V-10, T-07) |
| `CONNECTION_RESET_ACKS` | `1` | recorded (not D-numbered): a ConnectionReset write is acknowledged exactly once |

Undecided: none (every D constant is set; undecided() returns []).

### 3.5 Analysis-port wiring (`env.py` connect_phase)

| From | Port | To |
|---|---|---|
| `video_ag.mon` | `ap` | `sb_stream.video_xp` |
| `video_ag.tpg_mon` | `ap` | `sb_stream.video_xp` |
| `txwire_ag.mon` | `ap_pkt` | `sb_stream.wire_xp` |
| `sideband_ag.mon` | `ap` | `sb_stream.sideband_xp` |
| `txwire_ag.mon` | `ap_beat` | `sb_linkpro.beat_xp` |
| `uplink_ag.mon` | `ap` | `sb_control.uplink_xp` |
| `reg_ag.mon` | `ap` | `sb_control.reg_xp` |
| `apb_ag.mon` | `ap` | `sb_control.apb_xp` |
| `apb_ag.mon` | `ap_start` | `sb_control.apb_start_xp` |
| `txwire_ag.mon` | `ap_pkt` | `sb_control.wire_xp` |
| `sideband_ag.mon` | `ap` | `sb_control.sideband_xp` |
| `uplink_ag.mon` | `ap` | `sb_rxtrig.uplink_xp` |
| `sideband_ag.mon` | `ap` | `sb_rxtrig.sideband_xp` |
| `uplink_ag.mon` | `ap` | `sb_linktest.uplink_xp` |
| `sideband_ag.mon` | `ap` | `sb_linktest.sideband_xp` |
| `txwire_ag.mon` | `ap_pkt` | `sb_linktest.pkt_xp` |
| `reg_ag.mon` | `ap` | `sb_linktest.reg_xp` |
| `reg_ag.mon` | `ap` | `sb_reg.reg_xp` |
| `sideband_ag.mon` | `ap` | `sb_reg.sideband_xp (drained, contents unused)` |
| `sideband_ag.mon` | `handle` | `sb_reg.sb_mon (reads .last snapshot)` |
| `uplink_ag.mon` | `ap` | `sb_ioack.uplink_xp` |
| `txwire_ag.mon` | `ap_short` | `sb_ioack.short_xp` |
| `io_ag.mon` | `ap` | `sb_txtrig.io_xp` |
| `txwire_ag.mon` | `ap_short` | `sb_txtrig.short_xp` |
| `uplink_ag.mon` | `ap` | `sb_txtrig.uplink_xp` |
| `sideband_ag.mon` | `ap` | `sb_txtrig.sideband_xp` |
| `sideband_ag.mon` | `ap` | `sb_linkstate.sideband_xp` |
| `uplink_ag.mon` | `ap` | `sb_linkerr.uplink_xp` |
| `sideband_ag.mon` | `ap` | `sb_linkerr.sideband_xp` |
| `uplink_ag.mon` | `ap` | `sb_linkreset.uplink_xp` |
| `sideband_ag.mon` | `ap` | `sb_linkreset.sideband_xp` |
| `txwire_ag.mon` | `ap_pkt` | `stream_dump.pkt_xp` |
| `txwire_ag.mon` | `ap_pkt` | `pkt_log.pkt_xp` |
| `txwire_ag.mon` | `ap_short` | `pkt_log.short_xp` |
| `txwire_ag.mon` | `ap_beat` | `cov.beat_xp` |
| `txwire_ag.mon` | `ap_pkt` | `cov.pkt_xp` |
| `txwire_ag.mon` | `ap_short` | `cov.short_xp` |
| `uplink_ag.mon` | `ap` | `cov.uplink_xp` |
| `sideband_ag.mon` | `ap` | `cov.sideband_xp` |
| `apb_ag.mon` | `ap` | `cov.apb_xp` |
| `reg_ag.mon` | `ap` | `cov.reg_xp` |
| `clkrst_ag` | `handle uplink_drv` | `uplink_ag.drv (retune_to_clock on set_cdc_ratio)` |
| `txwire_ag.mon` | `ap_short` | `trig_resp.short_xp` |
| `trig_resp` | `handle uplink_drv` | `uplink_ag.drv (insert_now IOACK)` |
| `txwire_ag.mon` | `ap_pkt` | `host.pkt_xp` |
| `host` | `handle uplink_drv` | `uplink_ag.drv (send_txn, bypasses uplink_ag.seqr)` |
| `clkrst_ag` | `on_reset_start` | `env.reset_starts (sb_stream.flush_window, sb_linkstate.allow_drop, sb_linkerr.allow)` |
| `clkrst_ag` | `on_reset` | `env.reset_models (sb_control.reset_model, sb_reg.reset_model, sb_rxtrig.device_reset, apb_ag.mon.reset_mem, sb_linkstate.device_reset, sb_stream.device_reset + close_flush_window)` |
| `clkrst_ag` | `ap` | `UNCONNECTED` |
| `vseqr` | `video_seqr/cfg_seqr/uplink_seqr/uplink_trig_seqr/apb_seqr/io_seqr` | `video_ag.seqr / cfg_ag.seqr / uplink_ag.seqr / uplink_ag.trig_seqr / apb_ag.seqr / io_ag.seqr` |
| `uplink_ag (connect_phase)` | `drv.seq_item_port / drv.trig_item_port` | `uplink_ag.seqr / uplink_ag.trig_seqr; drv.mon = uplink_ag.mon (driver-mirrored monitor)` |
| `io_ag (connect_phase)` | `drv.seq_item_port` | `io_ag.seqr; drv.mon = io_ag.mon (driver-mirrored)` |
| `video_ag/cfg_ag/apb_ag (connect_phase)` | `drv.seq_item_port` | `own seqr` |
| `env.start_of_simulation_phase` | `value copy` | `sb_rxtrig.jitter_ui = uplink_ag.drv.jitter_ui; sb_ioack.char_ns = uplink_ag.drv.char_ns` |
| `CxpEnv.quiesce` | `reads` | `txwire_ag.mon.idle_run + every scoreboard pending_count()` |
| `coverage model collect()` | `reads attributes` | `pkt_log, sb_control, sb_stream, sb_linkpro, sb_linkerr, sb_linkstate, sb_linkreset, clkrst_ag.resets_done/ratios, apb_ag.mon, video_ag.drv` |

### 3.6 Run phases

- **build** — run_test sets the dut handle and calls uvm_root().run_test(UVM_TESTNAME). CxpTopTest.build_phase records PLAN/seed/EXPECT_FAIL as results.xml properties, fails at once if REQUIRES build knobs differ from uvm.common.build, publishes cfg_dsizeP (CXP_PKT_DSIZE_P or PKT_DSIZE_P) and host ppm/phase/jitter to ConfigDB, builds CxpEnv and drives apply_defaults (resets asserted).
- **connect** — CxpEnv.connect_phase wires monitors to scoreboards/coverage/subscribers, gives HostCtrl, DeviceTriggerResponder and ClkRstAgent the uplink driver handle, registers reset callbacks, fills VirtualSequencer handles. start_of_simulation copies host jitter and character time into sb_rxtrig / sb_ioack.
- **reset** — run_phase raises an objection, start_clocks(APP/TX/RX_PERIOD, 10 ns each) and clocks.reset(8 rx cycles, all domains). The uplink driver waits 10 IDLE characters before taking items; link_state expects Detected within 48+16 words.
- **bringup** — If BRINGUP: HostCtrl.link_up() polls sb_link_detected, then writes StreamPacketSizeMax = 4 x (PKT_DSIZE_P + 8) bytes and requires ack 0x01 (device powers up with SPSM 0 and sends no stream).
- **main_seq** — Test-specific coroutine: starts virtual/agent sequences, closed-loop HostCtrl commands, ClkRstAgent resets/ratio changes, uplink hold/slip, and declares scoreboard windows (expect_drop, allow, excuse, flush_window, EXPECT_FAIL tags).
- **drain** — CxpEnv.quiesce(QUIESCE_TIMEOUT_NS=400 us): drives cfg_run=0 and s_pix_valid=0, then waits until the downlink has been IDLE for 64 words and every scoreboard pending_count() is 0; on timeout it only warns (the scoreboards' final checks report what was owed). Objection dropped; run_test adds 200 ns grace.
- **check/report** — check_phase calls env.error_kinds(), which finalizes every scoreboard (_final_check once) and collects (scoreboard, kind)->count. report_phase: each component logs its summary, CovSubscriber runs model.collect and writes cov_summary.json, TxWireMonitor closes downlink.bin.gz. final_phase writes sb_errors property and raises AssertionError on untagged errors or EXPECT_FAIL tags that never fired (fired tagged kinds are logged as tolerated). SVA failures (--assert) stop the simulation earlier.
- **results archive** — Makefile `run` deletes stale results.xml/dump.vcd/cov_summary.json/cxp_stream_dump.txt/coverage.dat/downlink.bin.gz, runs `sim`, then always `archive`: copies them to 00_test_results/<UVM_TESTNAME>/ (dump.vcd as <test>.vcd, downlink.bin.gz moved). `report` renders src/verif/uvm_test_report.html via tools/gen_uvm_test_report.py (never decides the verdict).
- **gate** — `make gate TIER_TESTS=...` runs tools/check_results.py --results-dir 00_test_results --expect: every listed test must have a results.xml with at least one passing case and no failure/error; a missing file is MISSING; skipped cases fail unless allowed. This is the tier's exit code.
- **cov_gate** — Only nightly and weekly: python3 -m uvm.coverage.gate merges the tier's cov_summary.json files and fails on any GOALS cell with zero hits or any test with cg_model_error; prints NOT_REACHABLE with reasons. nightly/weekly exit 1 if gate or cov_gate fails.

### 3.7 Other verification levels

- **Unit benches (`src/tb_unit`)** — cocotb 2.x unit benches, one per RTL block, grouped app/cdc/ctrl/lib/rx/top/tx; each dir = Makefile (TOPLEVEL/MODULE + cocotb_sim.mk) + tb_cxp_<m>_top.sv wrapper + test_cxp_<m>.py using @cxp_test, cxp_uplink/cxp_host/cxp_gapped helpers, golden cxp_protocol, Python FSM coverage (13 FSM registrations, all states/arcs covered per README). Repo-root `make tb` runs all in parallel and gates with check_results.py --benches. Counts below are grep of @cxp_test/@cocotb.test decorators at HEAD 0f53a91 (the README inventory table is stale).
- **Assertions (`src/sva`)** — cxp_sva.sv (listed by cxp_sva.f, added as SVA_SOURCES via src/rtl/cxp_ip.mk) holds checkers bound into RTL modules; compiled into every unit bench, the PyUVM build, emu bridge and make lint; Verilator --assert stops on failure, Questa uses +define+CXP_SVA_FATAL.
  - `cxp_arbiter_sva -> cxp_tx_arbiter`: one long-packet source taken per word
  - `cxp_inserter_sva -> cxp_tx_inserter`: 2-word short packet never split; offered I/O ack out within 3 words; <= 99 words between IDLEs
  - `cxp_tx_owner_sva -> cxp_tx_domain`: the long packet owning the arbiter offers a word every cycle
  - `cxp_framer_sva -> cxp_tx_pkt_framer`: SOP/EOP only on valid words, no second SOP in a packet, header/CRC/EOP held while not accepted
  - `cxp_short_pkt_sva -> cxp_tx_short_pkt`: unaccepted word held
  - `cxp_idle_rule_sva -> cxp_tx_domain`: §8.2.5.1 IDLE at least every 100 downlink words
  - `cxp_cdc_stream_fifo_sva -> cxp_cdc_stream_fifo`: never more than p_DEPTH words, no pop when empty
  - `cxp_link_mon_sva -> cxp_rx_link_mon`: uplink framing: bounded bad words before resync, resync flushes
  - `cxp_rxlong_sva -> cxp_rx_packet_parser`: every SOP closed by one EOP before next SOP; err only inside a packet
  - `cxp_ctrl_exec_sva -> cxp_ctrl_bus_master`: command/drained access ends within bus timeout, <= 1 Wait per command, none after 0xFF, response held until taken, waiting command starts when free
  - `cxp_conn_reset_sva -> cxp_ctrl_bootstrap_regs`: ConnectionReset bit clears within its timeout
  - `cxp_reset_order_sva -> cxp_cdc_reset`: domain resets released rx, then tx, then app; asserted together
- **Emulation (`src/emu`)** — Pure SystemVerilog + DPI-C environment (tb/cxp_hw_env.sv + dpi/cxp_fifo_dpi.c): the RTL (cxp_interface_top + cxp_ctrl_bootstrap_regs via inline APB3 bridge, single clock, internal TPG) is the camera, reached over two Linux named pipes (cxp.h2c / cxp.c2h) with 8B/10B, envelope framing and IDLE keep-alive in C. Verilator --binary default, SIM=vsim flow (OPT_LEVEL 0/1), TRACE=1, REGLOG=1 binds cxp_reglog. C++17/Qt host stack (cxp CLI + cxp-gui) speaking the same FIFO wire format to the Python virtual camera or the RTL bridge: discover/read/write/stream-capture/image-extract/compliance/trace/xml-parse, and `cxp validate` — a validation::Campaign running 113 non-electrical validation-plan cases plus the UVM-mirror cases, with per-case protocol logs, results.json and run options (--soak, --host-spsm, --timeout-scale, --ack-latency, --seed ...).

## 4. How to run

```sh
cd src/verif
make UVM_TESTNAME=test_stream_video run     # one test (seed 1)
make UVM_TESTNAME=<test> CXP_SEED=1234 run # reproduce a weekly failure
make smoke                                  # 4 tests
make ci                                     # 8 tests: smoke + read, write, ConnectionReset, TestMode
make nightly                                # 74 tests, then gate and cov_gate
make weekly                                 # 76 tests, random seeds, soak, full clock-ratio matrix
```

- `WAVES=0` for tiers; a change of `WAVES` needs a fresh `sim_build*`. Never run `make -n` on a tier: it runs it.
- Build knobs `OS_RATIO`, `RX_CLK_KHZ`, `RX_LOSS_WORDS`, `FIFO_DEPTH` select their own `sim_build_<cfg>`; `KNOBS_<test>` in the Makefile sets them per test (listed per test below).
- Results are archived per test in `src/verif/00_test_results/<test>/` (results.xml, cov_summary.json, downlink dump).

| Knob | Effect |
|---|---|
| `OS_RATIO` | default 16; -GOS_RATIO + CXP_OS_RATIO; uplink oversampling in cxp_rx_link and the host bit period (OS_RATIO rx periods); non-default -> sim_build_os<N> |
| `RX_CLK_KHZ` | default 20; rx cycles per device ms: 100 ms Wait = 20 us, 900 ms timeout = 180 us, host 200 ms = 40 us at 10 ns; ms_ns() uses it; -> sim_build_khz<N> |
| `RX_LOSS_WORDS` | default 20000 (2 x 10000 low-speed words); words without IDLE before the link is lost; -> sim_build_loss<N> |
| `FIFO_DEPTH` | default 1024; stream FIFO depth, also used by sb_stream SPSM drain allowance; -> sim_build_fifo<N> |
| `ASYNC_CLOCKS` | default 1; -Gp_ASYNC_CLOCKS; 0 builds single-clock variant (sim_build_sync) |
| `WAVES` | default 0 (cocotb_sim.mk); 1 adds --trace --trace-structs (WAVES_FMT vcd\|fst); build stamp wipes sim_build on change; dump.vcd archived as <test>.vcd |
| `DUMP` | default 0 (nightly/weekly set 1); TxWireMonitor writes downlink.bin.gz (cxp_protocol.dump, IDLE runs compressed), capped by DUMP_MAX_MB=16; archived (moved) |
| `COVERAGE / RTL_COV` | The Verilator line-coverage knob is RTL_COV=1 (--coverage-line; coverage.dat archived; `make fsm_cov` merges with verilator_coverage). A COVERAGE env var is interpreted by cocotb as Python-coverage of the testbench; the Makefile's fsm_cov message and comment still say `make COVERAGE=1 nightly`, which is stale. Functional coverage (cov_summary.json) is always on. |
| `CXP_SEED` | default 1; one seed for every rng(tag) stream; weekly draws a random one per test; recorded as cxp_seed property |
| `other` | UVM_TESTNAME (default test_idle_baseline), CXP_PKT_DSIZE_P (psz override of PKT_DSIZE_P), SIM (verilator \| vsim/questa), CXP_COV_JSON, CXP_STREAM_DUMP |

| Tier | Tests | Verdict |
|---|---|---|
| smoke | `test_idle_baseline`, `test_stream_tpg`, `test_discovery_bringup`, `test_conc_ctrl_under_stream_load` | gate |
| ci | smoke + `test_ctrl_cmd_read`, `test_ctrl_cmd_write`, `test_link_reset`, `test_tx_linktest_mode` | gate |
| feature | `test_io_ack`, `test_tx_trigger`, `test_tx_linktest_mode`, `test_router_reject`, `test_link_reset`, `test_tpg_config`, `test_tpg_formats` | gate |
| xifc | `test_xifc_stream_ctrl`, `test_xifc_stream_trigger`, `test_xifc_stream_linkreset`, `test_xifc_full` | gate |
| nightly | 74 tests (all but `test_soak_random`, `test_conc_clock_ratio_matrix_full`), seed 1, DUMP=1 | gate + cov_gate |
| weekly | all 76 tests, random seed per test, DUMP=1 | gate + cov_gate |

Per-test build knobs: `test_uplink_idle_limits`: OS_RATIO=4, `test_linktest_rx_full`: OS_RATIO=4, `test_linktest_tx_rules`: OS_RATIO=4, `test_conc_bidir_linktest`: OS_RATIO=4, `test_uplink_ppm_jitter_os4`: OS_RATIO=4, `test_uplink_ppm_jitter_os4_neg`: OS_RATIO=4, `test_conc_backpressure_frames`: FIFO_DEPTH=256.

## 5. Pass / fail criteria common to every test

A test **passes** when all of the following hold; any one failing makes it **fail**.

1. **build_phase: plan record + REQUIRES build check** — _record_plan() validates PLAN ids against ^CXP-CAM-[A-Z]+-\d{3}$ and that every PLAN_PARTIAL key is in PLAN (ValueError otherwise), then writes results.xml properties uvm_test, cxp_seed, plan, plan_partial, expect_fail. REQUIRES (e.g. {'OS_RATIO': 4}) is compared with uvm/common/build.py (CXP_* env from the Makefile build knobs); a mismatch raises ValueError at once so a test never runs on the wrong build. CXP_PKT_DSIZE_P env overrides PKT_DSIZE_P; packet size and HOST_PPM / HOST_PHASE_PS / HOST_JITTER_UI are published via ConfigDB.
2. **run_phase: reset + bring-up** — Clocks start at APP/TX/RX_PERIOD (default 10/10/10 ns), async reset for RESET_CYCLES=8. If BRINGUP (default True): host.link_up() polls sb_link_detected every 1000 ns and raises AssertionError('the device never detected the link') after 2 000 000 ns; then host.write(StreamPacketSizeMax, 4*(PKT_DSIZE_P+8)) must be acknowledged ACK_OK_WRITE, else AssertionError('bring-up: StreamPacketSizeMax write acknowledged ...'). Tests with BRINGUP=False skip both.
3. **Closed-loop host command timeout** — HostCtrl.command(): per send waits for a type-0x03 ack up to HOST_ACK_TIMEOUT_MS=200 device-ms (ms_ns scaled by RX_CLK_KHZ); a Wait (0x04) ack extends the limit to its announced ms + 200 ms; up to 2 resends (3 sends total), none for a 0xFF reset. No ack after all sends returns code None (tests' read1/write_ok then raise AssertionError).
4. **run_phase: quiesce** — After main_seq, env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS, default 400 000 ns) drives cfg_run=0 and s_pix_valid=0, then polls every 200 ns until the downlink monitor's idle_run >= 64 words AND every scoreboard's pending_count() == 0. On timeout it only logs a warning (quiesce itself never fails the test); whatever is still owed is reported by the scoreboards' final checks.
5. **check_phase / final_phase: scoped EXPECT_FAIL gate** — check_phase calls env.error_kinds(), which finalizes all 12 scoreboards (sb_stream, sb_linkpro, sb_control, sb_rxtrig, sb_linktest, sb_reg, sb_ioack, sb_txtrig, sb_linkreset, sb_linkstate, sb_linkerr, sb_test) exactly once and returns (scoreboard, kind)->count. final_phase writes sb_errors property; fails (AssertionError) if any fired (sb,kind) is not in EXPECT_FAIL ('untagged scoreboard errors') or if any EXPECT_FAIL tag never fired ('tagged but never fired'). Tolerated kinds are logged as 'expect_fail (tolerated)'.
6. **sb_test (TestChecks)** — Test-specific checks: env.sb_test.check(cond, kind, msg) records an error of `kind` when cond is false; gated/tagged exactly like any scoreboard kind.
7. **sb_control final check** — Every command (non-optional) must have been acknowledged (ack_missing); 0x80 CRC-error pulses must lie within [crc_injected, crc_injected + crc_maybe] (crc_count); control-reset pulses must equal 0xFF commands sent (reset_pulse_count). Per-ack (live) kinds: ack_code, ack_code_unknown, ack_unexpected, ack_late, ack_replica, ack_length, ack_pad, ack_crc, ack_size, ack_data, ack_data_bus, ack_parse, ack_wait_time, wait_bootstrap, wait_twice, access, stray_access, access_count, access_rejected. Reference model: cxp_protocol.regref.RegRef + APB slave memory; pending = unanswered commands.
8. **sb_reg (live)** — Every register-bus access vs RegRef generated from src/regmap: read value/code (read_value, read_code), write accept/refuse code (write_code); ConnectionReset loads §10.3.28 values; connection-test counters compared with the sideband value in that cycle. No end-of-test check.
9. **sb_stream final check** — Golden frames (s_pix monitor or TPG monitor) still in the FIFO and not excused by a flush window -> lost_frame; frames reassembled with no golden -> unmatched_frame. Live kinds: tag_vote, tag_continuity, pkt_short, dsizep_vote, dsizep_mismatch, crc, vote, replica, framing, line_marker, kmark_partial, streamid, dsizel, header_field, pixel_count, pixel_data, pkt_over_spsm, sop_spsm_off (packet while StreamPacketSizeMax=0), sop_test_mode, sop_reset_window. Pending = golden frames the wire still owes.
10. **sb_linkpro final check** — Test must not end inside a long packet (framing). Live: framing (deframer errors), packet_type (only 0x01/0x03/0x04 downlink), short_split (IDLE inside a trigger/IO-ack short packet), kcode (lexicon, K28.3 only as a whole marker word). Longest non-IDLE run is measured/reported only (the 99-word rule is the SVA a_idle_interval).
11. **sb_linkstate final check** — Each expect_drop() deadline that never saw a drop -> link_no_drop; an expect_up deadline passed with link still down -> link_not_up; link down at end without end_down -> link_not_up. Live: link_drop (drop outside an allow_drop window), link_up_late (> UP_WORDS=48 words), link_drop_late.
12. **sb_linkerr final check** — For each of code/disp/pkt/glitch: seen+excused < injected -> <k>_missing; pulses of a kind nothing injected -> <k>_unexpected (disp/pkt excused when a code/disp error was injected; code excused when disp injected). allow(t0,t1) windows (e.g. around resets) excuse pulses.
13. **sb_linkreset final check** — ConnectionReset windows (link_reset_active 0->1) must equal link_reset_done pulses (window_done); if the host wrote ConnectionReset=1, at least one window (no_window) and no more windows than requests (excess_windows). Live: rate_mirror (rate_to_discovery must mirror link_reset_active), window_long, done_no_window.
14. **sb_linktest final check** — If host test packets were sent but no sideband events -> sideband_dead; device TestErrorCount must equal expected (clamped at 0xFFFFFFFF) -> err_count; TestPacketCountRx == expected packets -> pkt_count_rx; TestMode enabled but no TX test packet -> tx_no_packet; TestPacketCountTx == test packets on wire -> tx_count. Live: tx_payload (Table 23 payload bit-exact), tx_gap (>=16 words between packets), tx_after_testmode, register-read range kinds.
15. **sb_rxtrig final check** — Host triggers the device can take and whose edge matches cfg_trig_polarity owe one trig_out pulse (trig_missing); damaged triggers owe one glitch pulse (glitch_missing); per clock-ratio, (latency - Delay x unit) spread must be <= 2*rx_ns + 3*jitter_ui*OS_RATIO*rx_ns + 1 ns (latency_spread). Live: trig_unexpected, glitch_unexpected.
16. **sb_ioack final check** — Every accepted host trigger must be answered by one K28.6 I/O ack with code 0x01 (count); live: code (wrong code), unexpected (ack with nothing pending), latency (above decision-D6 bound, in host char_ns units).
17. **sb_txtrig final check** — No more rise/fall packets than io_agent edges (rise_count, fall_count); the host's level must end equal to the pin's (final_level). Live: before_ack (a trigger packet before the host's I/O ack or the device timeout, §8.3.3), delay (Delay must be 0), sequence (level must alternate rise/fall).
18. **Bound SVA contracts (src/sva/cxp_sva.sv)** — Compiled into every run (cocotb_sim.mk: Verilator --assert stops the simulation on the first failing property; Questa build defines CXP_SVA_FATAL). Properties: arbiter a_one_grant; inserter a_trig_contiguous, a_ioack_contiguous, a_ioack_within_3 (IO-ack leader within 3 words of offer), a_run (IDLE at least every 100 words); tx_domain a_owner_valid; framer a_flags_on_valid, a_no_sop_in_packet, a_hold_until_ready; short pkt a_hold_until_ready; idle rule a_idle_interval (§8.2.5.1); stream FIFO a_no_overflow, a_no_underflow; link_mon a_down_bounded, a_resync_flush; rx packet parser a_sop_outside, a_eop_inside, a_err_inside; ctrl bus master a_cmd_bounded, a_one_wait, a_no_wait_after_reset, a_rsp_held, a_no_stranded_cmd; bootstrap regs a_crst_bounded (ConnectionReset bit returns to 0); cdc_reset a_tx_after_rx, a_app_after_tx. A failure errors the cocotb testcase -> results.xml failure.
19. **Functional coverage sample (cov_subscriber)** — report_phase samples uvm/coverage/model.py collect(env) into cov_summary.json (bins <group>.<cell>); an exception in the model adds cg_model_error. Not a per-test pass/fail criterion.
20. **make gate (tier verdict)** — tools/check_results.py --results-dir 00_test_results --expect "$(TIER_TESTS)": every test in the tier must have left 00_test_results/<t>/results.xml (else MISSING -> fail); any testcase with <failure>/<error> fails; a skipped case fails unless allow-listed (tiers pass no --allow-skip). `run` deletes stale artifacts first so a crashed sim cannot inherit the previous test's results.xml. Tiers run every test to completion then gate; smoke/ci/feature/xifc end in gate only.
21. **make cov_gate (nightly/weekly only)** — python3 -m uvm.coverage.gate --tests "$(TIER_TESTS)": merges each tier test's cov_summary.json bins and exits 1 if any cell of model.GOALS was never hit (holes listed) or any test's coverage model failed (cg_model_error). Goal groups: cg_insertion (trig_rise/trig_fall/ioack in stream header/payload/tail, ack/linktest payload, idle), cg_idle_stretch, cg_ctrl (op x region x size, Table 22 codes 00,01,03,04,40-47,80), cg_ctrl_timing, cg_stream (fmt 0x101-0x105, width mod 4, rect/arbitrary, tpg/sensor, packet one/short/full, dsizep buckets), cg_tag (wrap, reset, reset_by connection_reset/connection_config), cg_uplink_err (code/disp/glitch, answered 80/47/46/42), cg_link (up/loss/relock), cg_reset (connection_reset, mid-image, ctrl_reset, domain app/tx/rx), cg_cdc (tx_rx and app_tx slow/fast), cg_backpressure (len 8_63, mid_line, eol). NOT_REACHABLE cells are listed with reasons, not gated. nightly/weekly exit 1 if gate or cov_gate fails.
22. **Weekly seed** — weekly runs every test with a fresh CXP_SEED from /dev/urandom (recorded as cxp_seed property) and DUMP=1; nightly uses CXP_SEED=1 (default) with DUMP=1.

## 6. Coverage closure

`src/verif/uvm/coverage/model.py` — collect(env) at CovSubscriber.report_phase from recorded component state; cells named <group>.<cell>; plus raw subscriber bins (cg_packet_type.*, cg_short_packet.*, cg_uplink_packet.*, cg_errors.*, cg_router.reject.*, cg_link_reset.window_open/done, cg_regbus.*, cg_apb.*) that are not goals. `make cov_gate` (nightly, weekly) merges the tier's `cov_summary.json` files and fails on any `GOALS` cell that no test hit.

Buckets: size_bucket (bytes): 0, 1_3, 4, 5_104, 105_255, max (=256), over; run_bucket (words): lt50, 50_98, 99; stall_bucket (cycles): 1, 2_7, 8_63, ge64.

| Group | Cells sampled | Goal cells (must be hit) |
|---|---|---|
| `cg_insertion` | short packet kind (trig_rise, trig_fall, ioack) x where it sat per PacketLog.where(): idle, or <stream\|ack\|linktest>.<header\|payload\|tail> | trig_rise/trig_fall/ioack x stream x header/payload/tail (9); trig_rise/trig_fall/ioack x ack.payload and linktest.payload (6); trig_rise/trig_fall/ioack .idle (3) |
| `cg_idle_stretch` | inside.<stream\|ack\|linktest> = IDLE inside a long packet; run.<lt50\|50_98\|99> = non-IDLE run lengths before an IDLE (sb_linkpro.run_hist) | inside.stream; inside.linktest; run.lt50; run.50_98 |
| `cg_ctrl` | <read\|write\|reset\|undefined>.<boot_ro\|boot_rw\|user\|unmapped>.<size bucket\|na> per answered command; code.<xx> per final Table 22 code (and 04 for Waits) | read.{boot_ro,boot_rw,user}.{1_3,4,5_104,max} (12); read.user.over; read.unmapped.4; write.{boot_ro,boot_rw,user,unmapped}.4; write.user.5_104; write.user.max; write.user.over; reset.boot_ro.na; undefined.boot_ro.na; code.{00,01,03,04,40,41,42,43,44,45,46,47,80} (13) |
| `cg_ctrl_timing` | stall.<0\|1_15\|16_wait\|gt_wait\|gt_timeout> user-slave latency class; wait.<region>; event.ctrl_reset / reset_aborted_access / reset_drained_access | stall.0; stall.gt_wait; stall.gt_timeout; event.ctrl_reset; event.reset_aborted_access; wait.user |
| `cg_stream` | fmt.<pixfmt>, width_mod4.<0..3>, kind.<rect\|arbitrary>, source.<tpg\|sensor> per frame; packet.<one\|short\|full>, dsizep.<le16\|le256\|gt256> per packet | fmt.{0101,0102,0103,0104,0105}; width_mod4.{0,1,2,3}; kind.rect; kind.arbitrary; source.tpg; source.sensor; packet.one; packet.short; packet.full; dsizep.le16; dsizep.le256; dsizep.gt256 |
| `cg_tag` | wrap, reset, reset_by.connection_reset, reset_by.connection_config | wrap; reset; reset_by.connection_reset; reset_by.connection_config |
| `cg_uplink_err` | code, disp, pkt, glitch (injected counts); answered.<80\|47\|46\|42> (device answered those codes) | code; disp; glitch; answered.{80,47,46,42} |
| `cg_link` | up, loss, relock | up; loss; relock |
| `cg_reset` | connection_reset, connection_reset.stream_mid_image, ctrl_reset, domain.<all\|app\|tx\|rx> | connection_reset; connection_reset.stream_mid_image; ctrl_reset; domain.app; domain.tx; domain.rx |
| `cg_cdc` | tx_rx.<slow\|fast\|eq>, app_tx.<slow\|fast\|eq> per set_cdc_ratio point | tx_rx.slow; tx_rx.fast; app_tx.slow; app_tx.fast |
| `cg_backpressure` | len.<stall bucket> of max stall; at.<eol\|eof\|sof\|mid_line> | len.8_63; at.mid_line; at.eol |

**Not reachable (excluded from the goals):**

- `cg_ctrl_timing.event.reset_drained_access` — APB bridge aborts on a control reset and the register bus answers in one cycle; both finish before the reset ack reaches the wire
- `cg_insertion.*.ack.header / .tail` — a 2-word short packet lands inside a 3-word ack header or its CRC/EOP only by chance; payload cell carries the rule
- `cg_insertion.*.linktest.header / .tail` — Table 23 has a 2-word header and no CRC; covered by payload
- `cg_idle_stretch.inside.ack` — an ack is at most 70 words; the 99-word IDLE rule never falls inside one after a clean IDLE
- `cg_idle_stretch.run.99` — inserter soft threshold (95) yields the IDLE before 99; 99 is the SVA hard limit
- `cg_stream.kind x fmt / source crosses` — arbitrary form and formats are driven from the sensor port; TPG is rectangular Mono8..16
- `cg_cdc.* equal ratio` — the default point; not a goal
- `cg_backpressure.at.eof / .sof` — never held in 1392 stalls (C-11): the port takes an image's first and last pixel at once

## 7. Test case summary

76 PyUVM tests: 31 feature tests (`all_tests.py`), 28 specification-chapter tests (`spec_tests.py`, catalogue T-xx), 17 concurrency tests (`conc_tests.py`, catalogue C-xx). Last archived run: 75 pass, 1 fail.

| VP ID | Test | Cat. | Title | Spec | Tiers | Last |
|---|---|---|---|---|---|---|
| [VP-DSC-01](#vp-dsc-01--test_discovery_bringup) | `test_discovery_bringup` | T-01 | Power-up discovery and bootstrap host sequence | §10.1.2-10.1.5, §10.3.28, Table 43, Table 44 | smoke, ci, nightly, weekly | pass |
| [VP-DSC-02](#vp-dsc-02--test_ctrl_xml_read) | `test_ctrl_xml_read` | T-03 | XML description read via XmlUrl | §10.3.2, §10.3.8-10.3.11, decision D5 | nightly, weekly | pass |
| [VP-LNK-01](#vp-lnk-01--test_idle_baseline) | `test_idle_baseline` |  | IDLE-only baseline, no traffic |  | smoke, ci, nightly, weekly | pass |
| [VP-LNK-02](#vp-lnk-02--test_uplink_reserved_types) | `test_uplink_reserved_types` | T-12 | Reserved/downlink-only uplink packet types | §8.2.3, §8.4, Table 18, decision D1 | nightly, weekly | pass |
| [VP-LNK-03](#vp-lnk-03--test_uplink_ppm_jitter) | `test_uplink_ppm_jitter` | T-14 | Uplink +200 ppm with 10% jitter | §8.2.5 | nightly, weekly | pass |
| [VP-LNK-04](#vp-lnk-04--test_uplink_ppm_jitter_neg) | `test_uplink_ppm_jitter_neg` | T-14 | Uplink -200 ppm with 10% jitter | §8.2.5 | nightly, weekly | pass |
| [VP-LNK-05](#vp-lnk-05--test_uplink_ppm_jitter_os4) | `test_uplink_ppm_jitter_os4` |  | Uplink +200 ppm jitter at OS_RATIO 4 | §8.2.5 | nightly, weekly | pass |
| [VP-LNK-06](#vp-lnk-06--test_uplink_ppm_jitter_os4_neg) | `test_uplink_ppm_jitter_os4_neg` |  | Uplink -200 ppm jitter at OS_RATIO 4 | §8.2.5 | nightly, weekly | pass |
| [VP-LNK-07](#vp-lnk-07--test_link_loss_relock) | `test_link_loss_relock` | T-15 | Link loss and re-lock | §10.1.1, Table 42, §10.2, §8.2.5.1 | nightly, weekly | pass |
| [VP-LNK-08](#vp-lnk-08--test_uplink_idle_limits) | `test_uplink_idle_limits` | T-16 | Minimum IDLE between uplink packets | §8.2.5.1, §8.7.3 | nightly, weekly | pass |
| [VP-LNK-09](#vp-lnk-09--test_conc_link_loss_under_stream) | `test_conc_link_loss_under_stream` | C-10 | Uplink loss and recovery while streaming | §10.2 | nightly, weekly | pass |
| [VP-CTL-01](#vp-ctl-01--test_ctrl_cmd_read) | `test_ctrl_cmd_read` |  | Four open-loop control reads |  | ci, nightly, weekly | pass |
| [VP-CTL-02](#vp-ctl-02--test_ctrl_cmd_write) | `test_ctrl_cmd_write` |  | Four open-loop control writes |  | ci, nightly, weekly | pass |
| [VP-CTL-03](#vp-ctl-03--test_ctrl_reset_op) | `test_ctrl_reset_op` |  | Two control-channel reset commands |  | nightly, weekly | pass |
| [VP-CTL-04](#vp-ctl-04--test_pslverr_burst) | `test_pslverr_burst` |  | User-window writes answered PSLVERR |  | nightly, weekly | pass |
| [VP-CTL-05](#vp-ctl-05--test_ctrl_reset_storm) | `test_ctrl_reset_storm` |  | Three back-to-back 0xFF control resets |  | nightly, weekly | pass |
| [VP-CTL-06](#vp-ctl-06--test_router_reject) | `test_router_reject` |  | Extension-link writes rejected, D3 exceptions | §5.1, §10.3.28, §10.3.30 | nightly, weekly, feature | pass |
| [VP-CTL-07](#vp-ctl-07--test_ctrl_size_matrix) | `test_ctrl_size_matrix` | T-07 | Control command Size matrix | §8.6.2, Table 21, §8.6.3, Table 22 | nightly, weekly | pass |
| [VP-CTL-08](#vp-ctl-08--test_ctrl_invalid_cmds) | `test_ctrl_invalid_cmds` | T-08 | Invalid/malformed control commands | §8.6.1.1, Table 22, decision D8 | nightly, weekly | pass |
| [VP-CTL-09](#vp-ctl-09--test_ctrl_wait_ack) | `test_ctrl_wait_ack` | T-09 | Wait acknowledgment and hung slave | §8.6.1, Figure 24, §8.6.1.1, §8.6.3 | nightly, weekly | pass |
| [VP-CTL-10](#vp-ctl-10--test_ctrl_reset_during_exec) | `test_ctrl_reset_during_exec` | T-10 | Control channel reset mid-command | §8.6.1.2 | nightly, weekly | pass |
| [VP-CTL-11](#vp-ctl-11--test_conc_ctrl_reset_under_load) | `test_conc_ctrl_reset_under_load` | C-08 | Control channel reset under load | §8.6.1.2 | nightly, weekly | pass |
| [VP-REG-01](#vp-reg-01--test_ral_sweep) | `test_ral_sweep` |  | Write/readback sweep of three RW registers |  | nightly, weekly | pass |
| [VP-REG-02](#vp-reg-02--test_bootstrap_ro_map) | `test_bootstrap_ro_map` | T-02 | Bootstrap read-only map and unmapped reads | §10.3.1, §10.3.3, §10.3.4-10.3.18, §10.3.19-27 | nightly, weekly | pass |
| [VP-STR-01](#vp-str-01--test_stream_tpg) | `test_stream_tpg` |  | TPG stream smoke with one ctrl read |  | smoke, ci, nightly, weekly | pass |
| [VP-STR-02](#vp-str-02--test_stream_video) | `test_stream_video` |  | Eight random sensor frames, DsizeP 64 |  | nightly, weekly | pass |
| [VP-STR-03](#vp-str-03--test_stream_video_ragged) | `test_stream_video_ragged` |  | Ragged frame tails and DsizeP shrink | §8.5.2, §8.5.3 | nightly, weekly | pass |
| [VP-STR-04](#vp-str-04--test_arbitrary_image) | `test_arbitrary_image` |  | Arbitrary header plus line markers |  | nightly, weekly | pass |
| [VP-STR-05](#vp-str-05--test_tpg_config) | `test_tpg_config` |  | TPG pattern reprogrammed via registers | §10.3.19, §10.3.27 | nightly, weekly, feature | pass |
| [VP-STR-06](#vp-str-06--test_tpg_formats) | `test_tpg_formats` |  | TPG PixelFormat sweep and StreamID | §9.4.2, §10.3.26, Table 19, Table 37 | nightly, weekly, feature | pass |
| [VP-STR-07](#vp-str-07--test_conn_config_write) | `test_conn_config_write` | T-05 | ConnectionConfig write restarts PacketTag | §10.3.33, §8.5.3 | nightly, weekly | pass |
| [VP-STR-08](#vp-str-08--test_spsm_negotiation) | `test_spsm_negotiation` | T-06 | StreamPacketSizeMax values and mid-image change | §10.1.5, Table 44, §8.5.2, §10.3.32 | nightly, weekly | pass |
| [VP-STR-09](#vp-str-09--test_packet_tag_persistence) | `test_packet_tag_persistence` | T-20 | PacketTag continuity across stream events | §8.5.3 | nightly, weekly | pass |
| [VP-STR-10](#vp-str-10--test_image_geometry_matrix) | `test_image_geometry_matrix` | T-21 | Image geometry and header matrix | §9.4.2, §9.4.6, Table 37, Table 38 | nightly, weekly | pass |
| [VP-STR-11](#vp-str-11--test_pixel_formats) | `test_pixel_formats` | T-23 | Mono8..Mono16 pixel packing | §9.4.1, Table 25, §9.4.2, Figure 30 | nightly, weekly | pass |
| [VP-STR-12](#vp-str-12--test_stream_back_to_back_frames) | `test_stream_back_to_back_frames` | T-24 | 32 back-to-back sensor images | §8.5, §8.5.2, §9.4.3 | nightly, weekly | pass |
| [VP-STR-13](#vp-str-13--test_conc_backpressure_frames) | `test_conc_backpressure_frames` | C-11 | Pixel-port back-pressure with Mono16 |  | nightly, weekly | pass |
| [VP-STR-14](#vp-str-14--test_conc_source_and_format_switch) | `test_conc_source_and_format_switch` | C-12 | Source and format switched under traffic | §9.4, §8.5.2 | nightly, weekly | pass |
| [VP-TRG-01](#vp-trg-01--test_trigger_uplink) | `test_trigger_uplink` |  | Four host triggers to trigger output |  | nightly, weekly | pass |
| [VP-TRG-02](#vp-trg-02--test_tx_trigger) | `test_tx_trigger` |  | Device pin edges to HS trigger packets | §8.3.2, §8.3.3 | nightly, weekly, feature | pass |
| [VP-TRG-03](#vp-trg-03--test_trigger_delay_latency) | `test_trigger_delay_latency` | T-17 | Host trigger Delay latency and polarity | §8.3.2.1, Table 15, Figure 20 | nightly, weekly | pass |
| [VP-TRG-04](#vp-trg-04--test_tx_trigger_ack_rules) | `test_tx_trigger_ack_rules` | T-18 | Device trigger packet ack rules | §8.3.3, §8.3.2 | nightly, weekly | pass |
| [VP-TRG-05](#vp-trg-05--test_trigger_at_discovery) | `test_trigger_at_discovery` | T-19 | Trigger de-assertion at ConnectionReset | §8.3.2, §10.3.28, §8.3.3, §8.3 | nightly, weekly | pass |
| [VP-TRG-06](#vp-trg-06--test_conc_uplink_trigger_in_packet) | `test_conc_uplink_trigger_in_packet` | C-05 | Host trigger inside command and test packet | §8.2.4, Table 15 | nightly, weekly | pass |
| [VP-IOA-01](#vp-ioa-01--test_io_ack) | `test_io_ack` |  | Host triggers answered by K28.6 I/O-ack | §8.3.3, Table 17 | nightly, weekly, feature | pass |
| [VP-LT-01](#vp-lt-01--test_linktest_clean) | `test_linktest_clean` |  | Clean uplink link-test packets, ppm offset | §6.7.4, §6.7 | nightly, weekly | pass |
| [VP-LT-02](#vp-lt-02--test_linktest_inject) | `test_linktest_inject` |  | Link-test packets with injected word errors | §6.7.4 | nightly, weekly | pass |
| [VP-LT-03](#vp-lt-03--test_tx_linktest_mode) | `test_tx_linktest_mode` |  | TestMode on/off device link-test packets | §8.7, §10.3.35, §10.3.38 | ci, nightly, weekly, feature | pass |
| [VP-LT-04](#vp-lt-04--test_linktest_rx_full) | `test_linktest_rx_full` | T-25 | Host test packets and RX counters | §8.7.2, Table 23, §8.7.3, §10.3.36-10.3.39 | nightly, weekly | pass |
| [VP-LT-05](#vp-lt-05--test_linktest_tx_rules) | `test_linktest_tx_rules` | T-26 | Device test packet transmission rules | §8.7.4, §10.3.35, §10.3.38, Table 23 | nightly, weekly | pass |
| [VP-LT-06](#vp-lt-06--test_conc_testmode_under_stream) | `test_conc_testmode_under_stream` | C-06 | TestMode toggled under running stream | §8.7.4, §10.3.35 | nightly, weekly | pass |
| [VP-LT-07](#vp-lt-07--test_conc_bidir_linktest) | `test_conc_bidir_linktest` | C-15 | Bidirectional connection test with polling | §8.7 | nightly, weekly | pass |
| [VP-RST-01](#vp-rst-01--test_link_reset) | `test_link_reset` |  | ConnectionReset clears MasterHostConnectionID | §10.3.28 | ci, nightly, weekly, feature | pass |
| [VP-RST-02](#vp-rst-02--test_xifc_stream_linkreset) | `test_xifc_stream_linkreset` |  | ConnectionReset mid-stream then resume | §10.3.28, §8.5.3 | nightly, weekly, xifc | pass |
| [VP-RST-03](#vp-rst-03--test_conn_reset_postconditions) | `test_conn_reset_postconditions` | T-04 | ConnectionReset post-conditions | §10.3.28, Table 44, §8.5.3, §8.3.2 | nightly, weekly | pass |
| [VP-RST-04](#vp-rst-04--test_conc_conn_reset_everything) | `test_conc_conn_reset_everything` | C-07 | ConnectionReset storm under everything | §10.3.28 | nightly, weekly | pass |
| [VP-RST-05](#vp-rst-05--test_conc_single_domain_reset) | `test_conc_single_domain_reset` | C-13 | Single clock-domain reset under traffic | §10.3.28 | nightly, weekly | pass |
| [VP-ARB-01](#vp-arb-01--test_arbiter_preempt) | `test_arbiter_preempt` |  | Four sources concurrently through arbiter |  | nightly, weekly | pass |
| [VP-ARB-02](#vp-arb-02--test_arbiter_stream_underflow) | `test_arbiter_stream_underflow` |  | I/O-ack survives short final stream packet | §8.3.3 | nightly, weekly | pass |
| [VP-ARB-03](#vp-arb-03--test_arbiter_underflow_ctrl) | `test_arbiter_underflow_ctrl` |  | Ctrl-ack survives short final stream packet |  | nightly, weekly | pass |
| [VP-ARB-04](#vp-arb-04--test_trigger_in_ctrl_packet) | `test_trigger_in_ctrl_packet` |  | Trigger inserted inside control command | §8.2.4, Table 15 | nightly, weekly | pass |
| [VP-ARB-05](#vp-arb-05--test_conc_ioack_inside_stream) | `test_conc_ioack_inside_stream` | C-02 | I/O ack inserted inside stream packets | §8.2.4, Table 13, §8.3.3 | nightly, weekly | pass |
| [VP-ARB-06](#vp-arb-06--test_conc_trigger_insertion_sweep) | `test_conc_trigger_insertion_sweep` | C-03 | Device trigger swept across packet types | §8.2.4, §8.2.5.1, §8.3.2 | nightly, weekly | pass |
| [VP-ARB-07](#vp-arb-07--test_conc_trigger_vs_ioack) | `test_conc_trigger_vs_ioack` | C-04 | Device trigger and I/O ack collide | §8.2.4 | nightly, weekly | fail |
| [VP-ROB-01](#vp-rob-01--test_byte_replication_robust) | `test_byte_replication_robust` |  | One-bit replica error still executed | §8.2.2.1 | nightly, weekly | pass |
| [VP-ROB-02](#vp-rob-02--test_crc_error) | `test_crc_error` |  | CRC-corrupted control commands NACKed 0x80 |  | nightly, weekly | pass |
| [VP-ROB-03](#vp-rob-03--test_uplink_bit_errors) | `test_uplink_bit_errors` | T-13 | Uplink replicated-character and CRC bit errors | §8.2.2.1, §8.2.2.2, Table 15, decision D8 | nightly, weekly | pass |
| [VP-ROB-04](#vp-rob-04--test_conc_uplink_errors_under_stream) | `test_conc_uplink_errors_under_stream` | C-09 | Noisy uplink under running stream | §8.2.2, §8.6.3 | nightly, weekly | pass |
| [VP-CON-01](#vp-con-01--test_xifc_stream_ctrl) | `test_xifc_stream_ctrl` |  | TPG stream with concurrent register traffic |  | nightly, weekly, xifc | pass |
| [VP-CON-02](#vp-con-02--test_xifc_stream_trigger) | `test_xifc_stream_trigger` |  | TPG stream with device HS triggers | §8.3.2 | nightly, weekly, xifc | pass |
| [VP-CON-03](#vp-con-03--test_xifc_full) | `test_xifc_full` |  | Stream, ctrl, host and device triggers |  | nightly, weekly, xifc | pass |
| [VP-CON-04](#vp-con-04--test_ctrl_pipelined_cmds) | `test_ctrl_pipelined_cmds` | T-11 | Pipelined commands without waiting for acks | §8.6.1.1, decision D7 | nightly, weekly | pass |
| [VP-CON-05](#vp-con-05--test_conc_ctrl_under_stream_load) | `test_conc_ctrl_under_stream_load` | C-01 | Control traffic under full stream load | §8.6, §8.2.4, Table 13 | smoke, ci, nightly, weekly | pass |
| [VP-CON-06](#vp-con-06--test_conc_clock_ratio_matrix) | `test_conc_clock_ratio_matrix` | C-14 | C-01 traffic over clock ratios |  | nightly, weekly | pass |
| [VP-CON-07](#vp-con-07--test_conc_clock_ratio_matrix_full) | `test_conc_clock_ratio_matrix_full` | C-14 | Clock ratio matrix, weekly full corners |  | weekly | pass |
| [VP-SOAK-01](#vp-soak-01--test_soak_random) | `test_soak_random` | C-16 | Weighted random soak over all actors |  | weekly | pass |

## 8. Test cases

Each entry: **Description** (what it proves), **How to test** (stimulus, sequences, geometry, knobs), **Pass criteria** (the scoreboards that judge it and the test's own checks), **Fail criteria** (the error kinds and conditions that fail it), and the plan rows it serves with any known partial coverage. Section 5 applies to every test in addition.

### 8.1 Discovery and bootstrap

_Power-up link discovery and the standard host bring-up sequence of §10 against Tables 43-45._

#### VP-DSC-01 — `test_discovery_bringup`

**Title:** Power-up discovery and bootstrap host sequence · **Catalogue:** T-01 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** smoke, ci, nightly, weekly  
**Spec:** §10.1.2-10.1.5, §10.3.28, Table 43, Table 44, §8.5.3  
**Plan rows:** `CXP-CAM-INIT-004`  
**Knobs:** `BRINGUP=False`  
**Command:** `make UVM_TESTNAME=test_discovery_bringup run`

**Description.** Shows the §10.1 host discovery sequence works from power-up: no stream before StreamPacketSizeMax (Table 44), the first stream packet has tag 0, and an acquisition with FrameCount 3 sends exactly 3 images.

**How to test.** BRINGUP=False, so the test does its own bring-up. tpg(1) (cfg_use_tpg=1, cfg_run=1, then 960 ns for the levels to cross), host.link_up() (2 ms limit), ConnectionReset<-1 and poll it up to 20 reads until it reads 0; read Standard, Revision, ConnectionConfigDefault and write it to ConnectionConfig; read ControlPacketSizeMax; wait 20 us, then write StreamPacketSizeMax=spsm_for(PKT_DSIZE_P)=4*(256+8)=1056 B and MasterHostConnectionID=0xA5; read XmlManifestSize, XmlUrlAddress and 64 bytes of URL; read the Width/Height/PixelFormat 0x3000 slots and each feature they point to; write Width=8, Height=4 through the slots. Then cfg_run=0, 40 us, FrameCount=3, TpgRun=0, AcquisitionStart, wait for 3 images (400 us) plus 60 us, AcquisitionStop.

**Pass criteria.**

- judges: sb_control, sb_reg, sb_linkreset, sb_stream, sb_linkpro, sb_linkstate, sb_test
- ack: ConnectionReset<-1 acknowledged 0x01 (ACK_OK_WRITE)
- reset_done: ConnectionReset reads 0 within 20 polls (the sb_linkreset window must be <= 200 ms)
- value: Standard == 0xC0A79AE5; Revision == rm.REVISION_VALUE
- ack: ConnectionConfig <- ConnectionConfigDefault acknowledged 0x01
- value: ControlPacketSizeMax >= 28 (4*6+4)
- stream_before_spsm: sb_stream.crc_checked == 0 after 20 us with StreamPacketSizeMax 0
- write_ok (raises AssertionError on other than 0x01): StreamPacketSizeMax, MasterHostConnectionID, Width, Height, FrameCount, TpgRun, AcquisitionStart/Stop
- value: XmlManifestSize >= 1
- xml_url: URL starts with b'Local:'
- read1 raises unless 0x00 with data: slots and features
- no_stream: >= 1 image within 400 us after StreamPacketSizeMax
- first_tag: sb_stream.tags[:1] == [0] (§8.5.3)
- acq_frames: exactly 3 images between AcquisitionStart and 60 us after the third (FrameCount 3)

**Fail criteria.**

- sb_test/stream_before_spsm, sb_test/first_tag, sb_test/acq_frames, sb_test/reset_done, sb_test/value, sb_test/xml_url, sb_test/no_stream
- sb_stream/sop_spsm_off (stream packet while StreamPacketSizeMax 0), tag_continuity, pixel_data/header_field on the TPG images
- sb_linkreset/window_long (ConnectionReset window > 200 ms) or no_window
- sb_reg/read_value on any bootstrap register mismatch; sb_control ack_* kinds
- link_up raises after 2 ms without sb_link_detected
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 15606, 1388.4 µs simulated._

#### VP-DSC-02 — `test_ctrl_xml_read`

**Title:** XML description read via XmlUrl · **Catalogue:** T-03 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §10.3.2, §10.3.8-10.3.11, decision D5  
**Plan rows:** `CXP-CAM-GEN-001`  
**Command:** `make UVM_TESTNAME=test_ctrl_xml_read run`

**Description.** Proves the XML blob read over the control channel matches the ROM image byte for byte, the last odd-size read is framed right, and an out-of-range manifest selector is refused 0x41.

**How to test.** Read 64 bytes of XmlUrl, parse 'Local:<name>;<addr hex>;<len hex>'. Read the XML in chunks of 4*(CONTROL_PACKET_SIZE_MAX_VALUE//4 - 6) bytes, last chunk the remainder (odd-size read). Write 1 to XmlManifestSelector.

**Pass criteria.**

- judges: sb_control, sb_reg, sb_test
- xml_url: URL starts with 'Local:'
- xml_ack: every chunk read acknowledged 0x00
- xml_blob: assembled blob == load_xml_blob()[:length] (cxp_camera_xml.mem)
- xml_tail: length % 4 != 0 (odd-Size tail exercised)
- selector: XmlManifestSelector<-1 acknowledged 0x41 (ACK_BAD_DATA, D5)
- control scoreboard: last ack Size = B, pad bytes 0

**Fail criteria.**

- sb_test/xml_blob (also fails if load_xml_blob() returns None, i.e. missing .mem)
- sb_test/xml_tail if XML size becomes a multiple of 4
- sb_test/selector
- sb_control/ack_size, ack_pad, ack_length
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 12563, 3090.7 µs simulated._

### 8.2 Uplink reception and link state

_Low-speed uplink sampling, 8B/10B decoding, link Detected / lost, framing recovery and error reporting (§6.7, §8.2, §10.1-10.2)._

#### VP-LNK-01 — `test_idle_baseline`

**Title:** IDLE-only baseline, no traffic · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** smoke, ci, nightly, weekly  
**Command:** `make UVM_TESTNAME=test_idle_baseline run`

**Description.** Sanity baseline: after bring-up the device idles cleanly on the downlink and no scoreboard reports anything with only IDLE on the uplink.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB responder; UplinkIdleSeq(n=8) IDLE words on the packet lane; then Timer 2000 ns of idle wire. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_linkpro (downlink IDLE/framing), sb_linkstate (link stays up), sb_linkerr (no error pulses on clean link), sb_control (bring-up SPSM write ack only)
- no test-specific checks; passes if all scoreboards clean

**Fail criteria.**

- sb_linkstate/link_drop or link_not_up
- sb_linkerr/code_unexpected, disp_unexpected, pkt_unexpected
- sb_linkpro/framing, kcode
- bring-up AssertionError if StreamPacketSizeMax write not acked 0x01 or link never detected within 2 ms
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

_Last result: **pass**, seed 5653, 181.6 µs simulated._

#### VP-LNK-02 — `test_uplink_reserved_types`

**Title:** Reserved/downlink-only uplink packet types · **Catalogue:** T-12 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.2.3, §8.4, Table 18, decision D1  
**Plan rows:** `CXP-CAM-PROT-004`  
**Command:** `make UVM_TESTNAME=test_uplink_reserved_types run`

**Description.** Proves unknown and downlink-only packet types on the uplink are discarded silently: no ack, no access, no trigger, no link error, link stays up.

**How to test.** Six iterations: closed-loop read of Standard, then a RAW well-formed packet (SOP, replicated type, 3 data words 0x01010101*(i+1), EOP) of type 0x00, 0x05, 0x7F, 0xFF, 0x01, 0x03; final read of Revision.

**Pass criteria.**

- judges: sb_control, sb_reg, sb_linkerr, sb_linkstate, sb_rxtrig, sb_ioack
- no sb_test checks; reliance on control scoreboard (no ack_unexpected), no stray access, no trigger pulse, no error pulse, link held

**Fail criteria.**

- sb_control/ack_unexpected, stray_access
- sb_linkerr/*_unexpected
- sb_rxtrig/trig_unexpected
- sb_linkstate/link_drop
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 51075, 665.3 µs simulated._

#### VP-LNK-03 — `test_uplink_ppm_jitter`

**Title:** Uplink +200 ppm with 10% jitter · **Catalogue:** T-14 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.2.5  
**Plan rows:** `CXP-CAM-REC-007`  
**Knobs:** `HOST_PPM=200.0`, `HOST_JITTER_UI=0.1`  
**Command:** `make UVM_TESTNAME=test_uplink_ppm_jitter run`

**Description.** Proves the uplink receiver tolerates a +200 ppm host clock with 10 % UI jitter without errors or link loss.

**How to test.** host bit clock HOST_PPM=+200 ppm, random start phase (HOST_PHASE_PS=None), HOST_JITTER_UI=0.10 edge jitter. 100 closed-loop commands from rng('ppm_cmds'): 50/50 reads of Standard, Revision, DeviceVendorName, 0x1000C or a random user-window word (USER_WORDS=256) vs single-word writes to 0x1000C, 0x1003C or a random user word; after commands 30 and 70 a 1024-word host LINKTEST packet. Product OS_RATIO 16 (build default). Two test packets instead of the catalogue's eight for run time.

**Pass criteria.**

- judges: sb_control, sb_reg, sb_linkerr, sb_linktest, sb_linkstate
- no sb_test checks: every command acknowledged and right (control/reg scoreboards)
- no code/disparity/framing pulse (sb_linkerr)
- TestErrorCount 0 and TestPacketCountRx 2 (sb_linktest)
- link never drops (sb_linkstate)

**Fail criteria.**

- sb_linkerr/code_unexpected, disp_unexpected, pkt_unexpected (lost bits)
- sb_linktest/err_count, pkt_count_rx
- sb_control/ack_missing (host timeout host.command waits HOST_ACK_TIMEOUT_MS=200 ms (device time) per send, with 2 resends)
- sb_linkstate/link_drop
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 40299, 18018.4 µs simulated._

#### VP-LNK-04 — `test_uplink_ppm_jitter_neg`

**Title:** Uplink -200 ppm with 10% jitter · **Catalogue:** T-14 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.2.5  
**Plan rows:** `CXP-CAM-REC-007`  
**Knobs:** `HOST_PPM=-200.0`, `HOST_JITTER_UI=0.1`  
**Command:** `make UVM_TESTNAME=test_uplink_ppm_jitter_neg run`

**Description.** Same as test_uplink_ppm_jitter at -200 ppm.

**How to test.** host bit clock HOST_PPM=-200 ppm, random start phase (HOST_PHASE_PS=None), HOST_JITTER_UI=0.10 edge jitter. 100 closed-loop commands from rng('ppm_cmds'): 50/50 reads of Standard, Revision, DeviceVendorName, 0x1000C or a random user-window word (USER_WORDS=256) vs single-word writes to 0x1000C, 0x1003C or a random user word; after commands 30 and 70 a 1024-word host LINKTEST packet. Subclass of test_uplink_ppm_jitter; only HOST_PPM changed; OS_RATIO 16.

**Pass criteria.**

- judges: sb_control, sb_reg, sb_linkerr, sb_linktest, sb_linkstate
- no sb_test checks: every command acknowledged and right (control/reg scoreboards)
- no code/disparity/framing pulse (sb_linkerr)
- TestErrorCount 0 and TestPacketCountRx 2 (sb_linktest)
- link never drops (sb_linkstate)

**Fail criteria.**

- sb_linkerr/code_unexpected, disp_unexpected, pkt_unexpected (lost bits)
- sb_linktest/err_count, pkt_count_rx
- sb_control/ack_missing (host timeout host.command waits HOST_ACK_TIMEOUT_MS=200 ms (device time) per send, with 2 resends)
- sb_linkstate/link_drop
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 55656, 18051.2 µs simulated._

#### VP-LNK-05 — `test_uplink_ppm_jitter_os4`

**Title:** Uplink +200 ppm jitter at OS_RATIO 4 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.2.5  
**Plan rows:** `CXP-CAM-REC-007`  
**Knobs:** `HOST_PPM=200.0`, `HOST_JITTER_UI=0.1`, `build=OS_RATIO=4 (Makefile only, no REQUIRES)`, `Makefile KNOBS={'OS_RATIO': '4'}`  
**Command:** `make UVM_TESTNAME=test_uplink_ppm_jitter_os4 run`

**Description.** Proves T-14 tolerance at 4x oversampling (sample must sit mid-bit) with +200 ppm and 10 % UI jitter.

**How to test.** host bit clock HOST_PPM=+200 ppm, random start phase (HOST_PHASE_PS=None), HOST_JITTER_UI=0.10 edge jitter. 100 closed-loop commands from rng('ppm_cmds'): 50/50 reads of Standard, Revision, DeviceVendorName, 0x1000C or a random user-window word (USER_WORDS=256) vs single-word writes to 0x1000C, 0x1003C or a random user word; after commands 30 and 70 a 1024-word host LINKTEST packet. Build knob OS_RATIO=4 via Makefile KNOBS_test_uplink_ppm_jitter_os4 (class sets no REQUIRES). Docstring: measured edge 12 % jitter clean, 13 % loses bits.

**Pass criteria.**

- judges: sb_control, sb_reg, sb_linkerr, sb_linktest, sb_linkstate
- no sb_test checks: every command acknowledged and right (control/reg scoreboards)
- no code/disparity/framing pulse (sb_linkerr)
- TestErrorCount 0 and TestPacketCountRx 2 (sb_linktest)
- link never drops (sb_linkstate)

**Fail criteria.**

- sb_linkerr/code_unexpected, disp_unexpected, pkt_unexpected (lost bits)
- sb_linktest/err_count, pkt_count_rx
- sb_control/ack_missing (host timeout host.command waits HOST_ACK_TIMEOUT_MS=200 ms (device time) per send, with 2 resends)
- sb_linkstate/link_drop
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 16779, 4524.7 µs simulated._

#### VP-LNK-06 — `test_uplink_ppm_jitter_os4_neg`

**Title:** Uplink -200 ppm jitter at OS_RATIO 4 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.2.5  
**Plan rows:** `CXP-CAM-REC-007`  
**Knobs:** `HOST_PPM=-200.0`, `HOST_JITTER_UI=0.1`, `build=OS_RATIO=4 (Makefile only, no REQUIRES)`, `Makefile KNOBS={'OS_RATIO': '4'}`  
**Command:** `make UVM_TESTNAME=test_uplink_ppm_jitter_os4_neg run`

**Description.** Same as test_uplink_ppm_jitter_os4 at -200 ppm.

**How to test.** host bit clock HOST_PPM=-200 ppm, random start phase (HOST_PHASE_PS=None), HOST_JITTER_UI=0.10 edge jitter. 100 closed-loop commands from rng('ppm_cmds'): 50/50 reads of Standard, Revision, DeviceVendorName, 0x1000C or a random user-window word (USER_WORDS=256) vs single-word writes to 0x1000C, 0x1003C or a random user word; after commands 30 and 70 a 1024-word host LINKTEST packet. Build knob OS_RATIO=4 via Makefile KNOBS_test_uplink_ppm_jitter_os4_neg (no REQUIRES).

**Pass criteria.**

- judges: sb_control, sb_reg, sb_linkerr, sb_linktest, sb_linkstate
- no sb_test checks: every command acknowledged and right (control/reg scoreboards)
- no code/disparity/framing pulse (sb_linkerr)
- TestErrorCount 0 and TestPacketCountRx 2 (sb_linktest)
- link never drops (sb_linkstate)

**Fail criteria.**

- sb_linkerr/code_unexpected, disp_unexpected, pkt_unexpected (lost bits)
- sb_linktest/err_count, pkt_count_rx
- sb_control/ack_missing (host timeout host.command waits HOST_ACK_TIMEOUT_MS=200 ms (device time) per send, with 2 resends)
- sb_linkstate/link_drop
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 3787, 4513.7 µs simulated._

#### VP-LNK-07 — `test_link_loss_relock`

**Title:** Link loss and re-lock · **Catalogue:** T-15 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §10.1.1, Table 42, §10.2, §8.2.5.1  
**Plan rows:** `CXP-CAM-REC-001`  
**Command:** `make UVM_TESTNAME=test_link_loss_relock run`

**Description.** Proves Detected falls after 32 non-framing words and returns within 48 words of IDLE, the cut write is never executed, and the first command after each re-lock is answered.

**How to test.** 0x1000C <- 0x11111111. Outage 1: uplink held for 100*40 chars (100 words) mid-IDLE. Outage 2: write of 0x22222222 to 0x1000C cut after 4 beats (address word), then line held 100 words. Each outage: sb_linkerr.allow, sb_linkstate.expect_drop by t0+(32+16) words+4 chars, expect_up, wait for sb_link_detected up to 200 words, then read Standard. Slip: drv.slip(1) bit in IDLE, 32 chars, allow_drop/linkerr window 200 words, wait up to 200 words for detect, 60 words, read Standard.

**Pass criteria.**

- judges: sb_linkstate, sb_linkerr, sb_control, sb_reg, sb_linkpro, sb_test
- sb_linkstate: drop by t0+48 words+4 chars; up within 48 words of IDLE
- cut write expect_codes {None, 0x47}
- executed: 0x1000C still reads 0x11111111
- first_after: first Standard read after each re-lock (held, cut write, slip) answered 0x00
- downlink stays legal (sb_linkpro)

**Fail criteria.**

- sb_linkstate/link_no_drop, link_drop_late, link_up_late, link_not_up
- sb_test/executed, first_after
- sb_linkerr/*_unexpected outside windows
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 288, 2130.7 µs simulated._

#### VP-LNK-08 — `test_uplink_idle_limits`

**Title:** Minimum IDLE between uplink packets · **Catalogue:** T-16 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.2.5.1, §8.7.3  
**Plan rows:** `CXP-CAM-CT-004`  
**Knobs:** `REQUIRES={'OS_RATIO': 4}`, `Makefile KNOBS={'OS_RATIO': '4'}`  
**Command:** `make UVM_TESTNAME=test_uplink_idle_limits run`

**Description.** Proves the device keeps lock and counts test packets correctly with the minimum single IDLE between packets.

**How to test.** OS_RATIO 4 build. Nine 1024-word (LT_DATA_WORDS) link test packets posted straight to the wire with exactly one IDLE word between (monitor told of each as LINKTEST lt_n_data=1024), a final IDLE, then 40 closed-loop reads alternating Revision/Standard (each back to back with minimal IDLE).

**Pass criteria.**

- judges: sb_linkstate, sb_linkerr, sb_linktest, sb_control, sb_reg
- no sb_test checks: link never drops, no error pulse, TestErrorCount 0 and TestPacketCountRx 9 (sb_linktest), every read answered

**Fail criteria.**

- sb_linkstate/link_drop
- sb_linkerr/*_unexpected
- sb_linktest/err_count, pkt_count_rx
- sb_control/ack_missing
- build_phase ValueError if OS_RATIO != 4
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 24179, 15277.6 µs simulated._

#### VP-LNK-09 — `test_conc_link_loss_under_stream`

**Title:** Uplink loss and recovery while streaming · **Catalogue:** C-10 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §10.2  
**Plan rows:** `CXP-CAM-REC-001`  
**Command:** `make UVM_TESTNAME=test_conc_link_loss_under_stream run`

**Description.** Proves that when the uplink is cut mid-write and held idle past the link monitor threshold, the downlink stays legal with whole images, the link drops and returns, the cut write never executes and the first read after is answered 0x00.

**How to test.** sensor source; write 0x55550000 to 0x1000C; sensor_frames(10, 'c10_frames', 128x32). After 20 us: sb_linkerr.allow(t0), sb_linkstate.expect_drop(t0 + 300 word_ns); a write of 0x66660000 to 0x1000C truncated to its first 4 beats (expect_codes None or ACK_MALFORMED); drv.hold(120*40 = 4800 bits) at level 0; wait up to 200 word_ns for sb_link_detected=0; then expect_up and wait up to 200 word_ns for relock; close allowances.

**Pass criteria.**

- judges: sb_linkstate, sb_linkerr, sb_stream, sb_linkpro, sb_control, sb_reg, sb_test
- bin loss_mid_stream: enclosing packet at tx_words or stream_packets(env)>0 when link lost
- sb_test 'first_after': read(STANDARD) code == ACK_OK_DATA
- sb_test 'executed': read1(0x1000C) == 0x55550000 (cut write did not take effect; message text says 'the cut write took effect')
- sb_test 'overlap_not_reached'

**Fail criteria.**

- sb_linkstate/link_no_drop, link_drop_late, link_not_up, link_up_late
- sb_test/first_after, executed
- sb_stream/lost_frame, framing; sb_linkpro/framing
- sb_linkerr/*_unexpected outside the allow window
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 29755, 1090.7 µs simulated._

### 8.3 Control channel

_Control command parsing, execution, acknowledgment codes, Wait, timeouts and the 0xFF control-channel reset (§8.6, Tables 21-22)._

#### VP-CTL-01 — `test_ctrl_cmd_read`

**Title:** Four open-loop control reads · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** ci, nightly, weekly  
**Plan rows:** `CXP-CAM-CTRL-001`  
**Command:** `make UVM_TESTNAME=test_ctrl_cmd_read run`

**Description.** Control read commands are executed on the register bus and acknowledged 0x00 with the register's data.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB; UplinkCtrlRandomSeq(n=4, write=False, seed=1) on the packet lane: 4 back-to-back open-loop single-word reads (Size 4) of addresses drawn from SAFE_RW_ADDRS (0x4008, 0x1000C, 0x1003C, 0x20C0). No trailing Timer; run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_control (ack code/data/length/CRC vs model, access count), sb_reg (register reads vs RegRef), sb_linkpro
- no test-specific checks; back-to-back commands subject to decision D7 (one executes, one waits, a third may be dropped -> optional)

**Fail criteria.**

- sb_control/ack_missing, ack_code, ack_data, ack_data_bus, ack_length, ack_crc, access_count
- sb_reg/read_value, read_code
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

_Last result: **pass**, seed 52805, 261.9 µs simulated._

#### VP-CTL-02 — `test_ctrl_cmd_write`

**Title:** Four open-loop control writes · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** ci, nightly, weekly  
**Plan rows:** `CXP-CAM-CTRL-002`  
**Command:** `make UVM_TESTNAME=test_ctrl_cmd_write run`

**Description.** Control write commands are executed and acknowledged 0x01, updating the register model.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB; UplinkCtrlRandomSeq(n=4, write=True, seed=1): 4 open-loop single-word writes of random 32-bit data to addresses from SAFE_RW_ADDRS (0x4008, 0x1000C, 0x1003C, 0x20C0). run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_control, sb_reg (write_code, model update), sb_linkpro
- no test-specific checks

**Fail criteria.**

- sb_control/ack_missing, ack_code, access_count
- sb_reg/write_code
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

_Last result: **pass**, seed 40577, 287.5 µs simulated._

#### VP-CTL-03 — `test_ctrl_reset_op`

**Title:** Two control-channel reset commands · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Plan rows:** `CXP-CAM-CTRL-006`  
**Command:** `make UVM_TESTNAME=test_ctrl_reset_op run`

**Description.** Opcode 0xFF control resets are acknowledged 0x03 and pulse the control-reset sideband once each.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB; inline _CtrlResetSeq(n=2): two back-to-back CTRL_CMD_RESET items (address 0) on the packet lane, no gap. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_control (0x03 ack, reset_pulse_count), sb_linkpro
- no test-specific checks

**Fail criteria.**

- sb_control/ack_code, reset_pulse_count, ack_unexpected
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-CTRL-006`: 0xFF between commands only; never during one

_Last result: **pass**, seed 32447, 172.3 µs simulated._

#### VP-CTL-04 — `test_pslverr_burst`

**Title:** User-window writes answered PSLVERR · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Command:** `make UVM_TESTNAME=test_pslverr_burst run`

**Description.** User-window writes answered by the APB slave with PSLVERR are acknowledged 0x40 and the slave memory keeps its value.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). ApbResponderPslverrSeq (every APB transfer answered with PSLVERR); UplinkCtrlRandomSeq(n=4, write=True, seed=1, addrs=USER_ADDRS) where USER_ADDRS = USER_BASE + 4*{0,1,7,100}: four open-loop random-data writes. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_control (user-window ack = 0x40 when acc_slverr, access count, slave memory model), sb_linkpro
- no test-specific checks

**Fail criteria.**

- sb_control/ack_code, access_count, ack_missing
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

_Last result: **pass**, seed 64274, 287.4 µs simulated._

#### VP-CTL-05 — `test_ctrl_reset_storm`

**Title:** Three back-to-back 0xFF control resets · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Plan rows:** `CXP-CAM-REC-002`  
**Command:** `make UVM_TESTNAME=test_ctrl_reset_storm run`

**Description.** A burst of three back-to-back control-channel resets is each acknowledged 0x03 without disturbing the link.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB; VsLinkResetStorm on vseqr -> _ResetBurstSeq(n=3): three back-to-back CTRL_CMD_RESET items on the packet lane. (Despite the name, no ConnectionReset; the ConnectionReset storm is C-07 elsewhere.) run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_control (0x03 acks, reset_pulse_count), sb_linkpro, sb_linkstate
- no test-specific checks

**Fail criteria.**

- sb_control/ack_code, reset_pulse_count, ack_unexpected
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-REC-002`: three 0xFF control resets; no ConnectionReset, no rediscovery

_Last result: **pass**, seed 14120, 217.2 µs simulated._

#### VP-CTL-06 — `test_router_reject`

**Title:** Extension-link writes rejected, D3 exceptions · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly, feature  
**Spec:** §5.1, §10.3.28, §10.3.30  
**Plan rows:** `CXP-CAM-CTRL-007`  
**Command:** `make UVM_TESTNAME=test_router_reject run`

**Description.** With from_extension_link=1 host writes are rejected 0x43 while reads are forwarded; per decision D3 ConnectionReset and MasterHostConnectionID writes are acked 0x01 but not executed, and 0xFF is executed (0x03).

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Sets sb_control.extension_link_mode=True. Perfect APB; CfgToggleSeq(use_tpg=0, from_extension_link=1). Open-loop UplinkCtrlRandomSeq(n=3, write=True, seed=1) then (n=2, write=False, seed=1) on SAFE_RW_ADDRS. Poll up to 400 x 1000 ns for sb_control.pending_count()==0, then Timer 4000 ns. Closed-loop: read 0x4008; write 0x4008=0x5A5A_0001; read 0x4008; write ConnectionReset 0x4000=1; Timer 20000 ns; host.reset() (0xFF); Timer 4000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_control (0x43 on ext writes except 0x4008/0x4000 -> 0x01; reads 0x00), sb_reg, sb_linkreset (ignored_requests), sb_test
- sb_test ext_mhcid_ack: MasterHostConnectionID write code == 0x01
- sb_test ext_mhcid_value: MasterHostConnectionID read after == read before
- sb_test ext_crst_ack: ConnectionReset write code == 0x01
- sb_test ext_crst_executed: sb_linkreset.active_windows unchanged 20000 ns after the ConnectionReset write
- sb_test ext_ctrl_reset: host.reset() code == 0x03
- pending-ack wait loop: 400 x 1 us max (no failure if it times out; sb_control reports)

**Fail criteria.**

- sb_test/ext_mhcid_ack, ext_mhcid_value, ext_crst_ack, ext_crst_executed, ext_ctrl_reset
- sb_control/ack_code, access_rejected, ack_missing
- sb_linkreset/window_done, excess_windows
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

_Last result: **pass**, seed 24172, 591.8 µs simulated._

#### VP-CTL-07 — `test_ctrl_size_matrix`

**Title:** Control command Size matrix · **Catalogue:** T-07 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.6.2, Table 21, §8.6.3, Table 22, §8.6.4, §10.3.2  
**Plan rows:** `CXP-CAM-CTRL-008`  
**Command:** `make UVM_TESTNAME=test_ctrl_size_matrix run`

**Description.** Proves the control channel honours every Size B (echo, ceil(B/4) words, zero pad, over-limit refusal) and that byte-size writes change only the first B bytes.

**How to test.** APB responder perfect. Reads of B = 1,2,3,4,5,7,8,104,256,260 at user window USER_BASE (0x20000) and at DeviceVendorName. Writes of 1,2,26,64,65 words at _user(100) = 0x20190, each (<=64) read back. DeviceUserID 4-word write + 16 B read. Byte-size writes B=1,2,3,5,6 to DeviceUserID over old [0x50515253,0x54555657] with new [0xA0A1A2A3,0xB0B1B2B3]; 2-byte write of 0 to Width alias + read; DeviceUserID reads of 1,2,3,256 B; write to unmapped 0x5000.

**Pass criteria.**

- judges: sb_control, sb_reg, sb_test
- write: 1..64-word writes acknowledged 0x01; readback: read returns the values
- oversize: 65-word write acknowledged ACK_OVERSIZE (0x45 per docstring) with no bus access
- byte_write: B-byte writes acknowledged 0x01; byte_write_value: DeviceUserID reads new[:B]+old[B:] in wire order
- unmapped_write: write to 0x5000 acknowledged 0x40
- control scoreboard: Size echoes B, pad bytes 0, ack never longer than ControlPacketSizeMax, 260-byte read refused, multi-word write lands word by word

**Fail criteria.**

- sb_control/ack_size, ack_pad, ack_length, access, access_count, stray_access
- sb_test/oversize, readback, byte_write_value, unmapped_write
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 20894, 3634.8 µs simulated._

#### VP-CTL-08 — `test_ctrl_invalid_cmds`

**Title:** Invalid/malformed control commands · **Catalogue:** T-08 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.6.1.1, Table 22, decision D8  
**Plan rows:** `CXP-CAM-NEG-002`, `CXP-CAM-NEG-006`, `CXP-CAM-NEG-007`  
**Command:** `make UVM_TESTNAME=test_ctrl_invalid_cmds run`

**Description.** Proves every malformed or undefined command is acknowledged at once with the Table 22 code and discarded without bus access, and the device answers the next command.

**How to test.** Via host.command with expect_codes, each followed by a good read of Standard: opcodes 0x02, 0x7F, 0xFE; write at 0x1000C with Size 2 words but last data word dropped; Size 1 with an extra word; read Size 0; write Size 0; EOP right after the Cmd/Size word; EOP right after the type word (b[:2]+[EOP]); EOP never sent (link-error allowance open for it); bad CRC; 0xFF reset with address 0x4000, Size 8.

**Pass criteria.**

- judges: sb_control, sb_reg, sb_linkerr, sb_test (via expect_codes)
- expected codes: 0x42 (ACK_BAD_OP) for 3 opcodes; 0x46 (ACK_SIZE_MISMATCH) for short/long write, Size 0 read/write, EOP after Cmd/Size; 0x47 (ACK_MALFORMED) for trailer before command word; {0x47 or none} for the lost trailer; 0x80 (ACK_CRC); 0x03 (ACK_OK_RESET)
- every error ack is the 4-word short form; no register/user-window access for a refused command (control scoreboard)
- the good read after each answered with data
- sb_linkerr.allow/close around the lost trailer: that is the only framing error permitted

**Fail criteria.**

- sb_control/ack_code, ack_length, stray_access, access_rejected
- sb_linkerr/pkt_unexpected (framing error outside the lost-trailer window)
- host.command waits HOST_ACK_TIMEOUT_MS=200 ms (device time) per send, with 2 resends
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 1592, 1145.1 µs simulated._

#### VP-CTL-09 — `test_ctrl_wait_ack`

**Title:** Wait acknowledgment and hung slave · **Catalogue:** T-09 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.6.1, Figure 24, §8.6.1.1, §8.6.3, §10.3.3  
**Plan rows:** `CXP-CAM-CTRL-004`  
**Command:** `make UVM_TESTNAME=test_ctrl_wait_ack run`

**Description.** Proves no Wait under 100 ms, one 0x04 Wait then the final 0x00 over it, a Wait then 0x40 for a hung slave, and never a Wait for a bootstrap register.

**How to test.** RX_CLK_KHZ default 20 rx cycles = 1 device ms. APB waitstate 75*20=1500 cycles, read _user(1); waitstate 150*20=3000 cycles, read _user(2); ApbResponderHangSeq (latency NEVER=0xFFFF), read _user(3); read Standard with slave still hung; APB perfect, read _user(4).

**Pass criteria.**

- judges: sb_control, sb_reg, sb_test
- under: 75 ms read acknowledged 0x00; wait_under: sb_control.waits unchanged
- over: 150 ms read acknowledged 0x00; wait_over: waits == w0+1
- hung: hung slave acknowledged 0x40; wait_hung: waits == w0+2
- control scoreboard: Wait within 200 ms announcing 100..10000 ms (ack_wait_time), final before the announced time (ack_late), no Wait for bootstrap (wait_bootstrap)

**Fail criteria.**

- sb_control/ack_wait_time, ack_late, wait_twice, wait_bootstrap
- sb_test/wait_under, wait_over, wait_hung, hung
- host.command waits HOST_ACK_TIMEOUT_MS=200 ms (device time) per send, with 2 resends; limit extended by announced Wait time
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 10838, 537.3 µs simulated._

#### VP-CTL-10 — `test_ctrl_reset_during_exec`

**Title:** Control channel reset mid-command · **Catalogue:** T-10 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.6.1.2  
**Plan rows:** `CXP-CAM-CTRL-006`  
**Command:** `make UVM_TESTNAME=test_ctrl_reset_during_exec run`

**Description.** Proves a 0xFF reset cancels the running command with exactly one 0x03 and no stray Wait/late final, and the next command gets its own data.

**How to test.** (a) APB hang; open-loop read _user(5); wait 20 ms of device time (20*20 rx cycles); host.reset() (0xFF); APB perfect; read _user(6). (b) APB waitstate 400*20=8000 cycles; open-loop read _user(7); wait until sb_control.waits increments (300 ms limit, 200 ns step); reset; perfect; read _user(8). (c) open-loop read of DeviceVendorName, immediately reset, read Standard. (d) open-loop 0xFF then host.reset() back to back; read _user(9). Then wait 1200 device ms (1200*20 rx cycles).

**Pass criteria.**

- judges: sb_control, sb_reg, sb_test
- control scoreboard only (no sb_test.check): exactly one 0x03 per 0xFF (reset_pulse_count), nothing for the cancelled command after its 0x03 (ack_unexpected), reads after answered with their own data (ack_data / ack_data_bus), abandoned bus access does not complete the next one (stray_access)
- wait_until for the Wait in (b): 300 ms timeout, result not checked

**Fail criteria.**

- sb_control/ack_unexpected, reset_pulse_count, ack_data_bus, stray_access, wait_twice
- host.command waits HOST_ACK_TIMEOUT_MS=200 ms (device time) per send, with 2 resends
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 8429, 874.1 µs simulated._

#### VP-CTL-11 — `test_conc_ctrl_reset_under_load`

**Title:** Control channel reset under load · **Catalogue:** C-08 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.6.1.2  
**Plan rows:** `CXP-CAM-CTRL-006`  
**Command:** `make UVM_TESTNAME=test_conc_ctrl_reset_under_load run`

**Description.** Proves the control-channel reset (opcode 0xFF) issued after a Wait, with the APB bus busy, and while an acknowledgment is framed cancels the command with only an 0x03 packet back and does not disturb the stream.

**How to test.** sensor source; sensor_frames(12, 'c08_frames') default sizes; host_triggers(20, 'c08_trig', gap 8..40 chars). ApbResponderWaitstateSeq(waits=300*RX_CLK_KHZ=6000 rx cycles); open-loop read of user word 1; wait up to ms_ns(300) (60000 ns) for sb_control.waits to rise; h.reset() (0xFF). Open-loop read of user word 2; wait up to 5000 ns for apb_psel; h.reset(). Perfect slave; open-loop 64-byte read of DEVICE_VENDOR_NAME; h.reset() right behind it; closed-loop read of user word 4.

**Pass criteria.**

- judges: sb_control, sb_stream, sb_linkpro, sb_ioack, sb_rxtrig, sb_test
- bin ff_after_wait: sb_control.waits increased within 60 us
- bin ff_bus_busy: apb_psel seen within 5 us
- bin ff_framing: hit unconditionally after sending the DEVICE_VENDOR_NAME read (not measured)
- sb_control: only the 0x03 reset ack for each cancelled command; stream tags/images unaffected (sb_stream)
- sb_test 'overlap_not_reached'

**Fail criteria.**

- sb_control/ack_unexpected, ack_missing, reset_pulse_count, wait_twice, ack_wait_time
- sb_stream/tag_continuity, lost_frame
- sb_test/overlap_not_reached (ff_after_wait or ff_bus_busy not reached)
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 54719, 1074.4 µs simulated._

### 8.4 Register map

_Bootstrap register file (Table 45), use-case features and the manufacturer window through the control channel._

#### VP-REG-01 — `test_ral_sweep`

**Title:** Write/readback sweep of three RW registers · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Plan rows:** `CXP-CAM-BOOT-001`, `CXP-CAM-BND-003`  
**Command:** `make UVM_TESTNAME=test_ral_sweep run`

**Description.** Bit-bash equivalent: each of three full-width RW bootstrap registers is written with 0xFFFFFFFF, 0x00000000 and 0xA5A5A5A5 and read back with the model's value.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB; inline _RalSweepSeq over addrs MasterHostConnectionID 0x4008, StreamPacketSizeMax 0x4010, TestErrorCountSelector 0x4020 (TEST_MODE excluded), patterns (0xFFFF_FFFF, 0x0000_0000, 0xA5A5_A5A5): for each, open-loop write, RAL write_mirror, UplinkIdleSeq(16), read, UplinkIdleSeq(16) -> 9 writes + 9 reads. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_reg (write_code incl. value_ok refusal 0x41, read_value vs RegRef), sb_control (ack code/data vs model)
- RAL mirror is updated locally only; no explicit test compare against it

**Fail criteria.**

- sb_reg/read_value, write_code, read_code
- sb_control/ack_code, ack_data, ack_missing
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-BOOT-001`: 3 hand-picked RW addresses, not the map
- `CXP-CAM-BND-003`: patterns only; StreamPacketSizeMax extremes not driven

_Last result: **pass**, seed 23795, 4627.7 µs simulated._

#### VP-REG-02 — `test_bootstrap_ro_map`

**Title:** Bootstrap read-only map and unmapped reads · **Catalogue:** T-02 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §10.3.1, §10.3.3, §10.3.4-10.3.18, §10.3.19-27, §10.3.40-41, Table 45  
**Plan rows:** `CXP-CAM-BOOT-001`  
**Command:** `make UVM_TESTNAME=test_bootstrap_ro_map run`

**Description.** Proves the bootstrap register map reads its reset values, refuses read-only writes with 0x43 without changing the value, and answers unmapped reads 0x40, never with a Wait.

**How to test.** After standard bring-up, read every grm.BOOTSTRAP row (ZERO blocks: first and last word only; others one read of max(4, nbytes) bytes, so strings whole), every grm.DEVICE slot and feature address, every grm.MANUFACTURER word. For every RegRef().readonly_addrs() address (skipping the interior of the zero block from IMAGE_N_STREAM_ID_ADDRESS+8 to 0x4000) write 0xFFFFFFFF then 0. Read unmapped 0x00000020, 0x00005000, 0x00010080.

**Pass criteria.**

- judges: sb_control, sb_reg, sb_test
- control/register scoreboards predict every code and value: reset values, NUL-padded strings, 0x43 for read-only write (value unchanged), 0x44 for write-only feature, 0x40 unmapped, ElectricalComplianceTest/HsUpconnection/Iidc2Address read 0
- wait: sb_control.waits == 0 at the end (§10.3.3 no Wait for bootstrap)

**Fail criteria.**

- sb_reg/read_value, read_code, write_code
- sb_control/ack_code, ack_data, ack_size, ack_pad, wait_bootstrap
- sb_test/wait
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 63964, 10610.8 µs simulated._

### 8.5 Stream data path

_Pixel sources, packing, image header / line markers, stream packets, PacketTag and StreamPacketSizeMax (§8.5, §9.4, Tables 19, 38)._

#### VP-STR-01 — `test_stream_tpg`

**Title:** TPG stream smoke with one ctrl read · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** smoke, ci, nightly, weekly  
**Plan rows:** `CXP-CAM-DATA-001`, `CXP-CAM-IMG-002`, `CXP-CAM-IMG-011`  
**Command:** `make UVM_TESTNAME=test_stream_tpg run`

**Description.** Smoke test: the internal TPG stream reaches the wire as well-formed, CRC-clean, bit-exact type-0x01 packets while one control read is acknowledged.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8) (PKT_DSIZE_P default 256). VsSmoke on vseqr: ApbResponderPerfectSeq, CfgToggleSeq(use_tpg=1, run=1, dsizeP from ConfigDB=256), UplinkCtrlRandomSeq(n=1, write=False, seed=1) = one open-loop read of a random address from SAFE_RW_ADDRS (0x4008, 0x1000C, 0x1003C, 0x20C0). Then Timer 5000 ns while the TPG free-runs. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns) (TPG golden via TpgMonitor).

**Pass criteria.**

- judges: sb_stream (TPG frames vs TpgMonitor golden), sb_linkpro, sb_control (1 read ack), sb_reg, sb_linkstate
- no test-specific checks

**Fail criteria.**

- sb_stream/lost_frame (golden frame never reassembled off the wire), sb_stream/unmatched_frame
- sb_stream/pixel_data, pixel_count, header_field, crc, dsizep_mismatch, pkt_short, tag_continuity, framing, line_marker, streamid, dsizel, pkt_over_spsm
- sb_linkpro/framing, kcode, packet_type, short_split
- sb_control/ack_missing, ack_code, ack_data
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-DATA-001`: packet format checked only on the packets that reach the wire; Mono8, one DsizeP per run
- `CXP-CAM-IMG-011`: bit-exact on Mono8 frames only; one pixel source and geometry set per run

_Last result: **pass**, seed 1321, 135.1 µs simulated._

#### VP-STR-02 — `test_stream_video`

**Title:** Eight random sensor frames, DsizeP 64 · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Plan rows:** `CXP-CAM-DATA-001`, `CXP-CAM-IMG-002`, `CXP-CAM-IMG-011`  
**Knobs:** `PKT_DSIZE_P=64`  
**Command:** `make UVM_TESTNAME=test_stream_video run`

**Description.** Random-geometry Mono8 frames on the external pixel port are packetised at DsizeP=64 and come back bit-exact with correct headers, CRC and PacketTag continuity.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8) with PKT_DSIZE_P=64 (SPSM=288 bytes). Perfect APB; CfgToggleSeq(use_tpg=0, arbitrary=0, run=1, dsizeP=64). VideoRandomSeq(n_frames=8, rng_seed=0xCAFE): xsize from {4,8,12,16,32,64,128}, ysize from {2,4,8,16,32}, pixfmt forced Mono8 0x0101, per-frame arbitrary=random()<0.25 drawn into the item (but cfg_arbitrary pin=0), dval_density uniform(0.5,1.0), fill_ramp pixels, streamid 1, sourcetag 0. Then Timer 50000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_stream (VideoMonitor golden, adaptive DsizeP=64 check), sb_linkpro, sb_linkstate
- no test-specific checks

**Fail criteria.**

- sb_stream/lost_frame (golden frame never reassembled off the wire), sb_stream/unmatched_frame
- sb_stream/pixel_data, pixel_count, header_field, crc, dsizep_mismatch, pkt_short, tag_continuity, framing, line_marker, streamid, dsizel, pkt_over_spsm
- sb_linkpro/framing, kcode, packet_type, short_split
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-DATA-001`: packet format checked only on the packets that reach the wire; Mono8, one DsizeP per run
- `CXP-CAM-IMG-011`: bit-exact on Mono8 frames only; one pixel source and geometry set per run

_Last result: **pass**, seed 55632, 188.9 µs simulated._

#### VP-STR-03 — `test_stream_video_ragged`

**Title:** Ragged frame tails and DsizeP shrink · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.5.2, §8.5.3  
**Plan rows:** `CXP-CAM-DATA-001`, `CXP-CAM-IMG-002`, `CXP-CAM-IMG-011`  
**Knobs:** `PKT_DSIZE_P=64`  
**Command:** `make UVM_TESTNAME=test_stream_video_ragged run`

**Description.** Frames whose last packet is 1, 2 or DsizeP-1 words long, widths not a multiple of 4, and a DsizeP change 64->16 between images: every DsizeP header equals its payload, tags run on without a gap, and every frame is bit-exact.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8) with PKT_DSIZE_P=64. Perfect APB; CfgToggleSeq(use_tpg=0, arbitrary=0). Five _OneFrameSeq frames (Mono8, dval_density=1.0, fill_ramp) at FRAMES_64=(4,4),(5,4),(29,4),(49,7),(57,6) (per code comment tails 49,53,1,2,63 words; 25 header words + Ysize*(2+ceil(Xsize/4))). Timer 20000 ns. Then closed-loop host.write_ok(StreamPacketSizeMax 0x4010, spsm_for(16)=96 bytes). Three frames FRAMES_16=(21,1),(25,1),(13,1) (tails 1,2,15). Timer 30000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns). Note cfg_dsizeP pin stays 64; only the register changes.

**Pass criteria.**

- judges: sb_stream (DsizeP header vs payload, tag continuity, bit-exact), sb_linkpro, sb_control/sb_reg (SPSM write), sb_linkstate
- host.write_ok(0x4010, [96]) raises AssertionError unless acknowledged 0x01
- no sb_test checks

**Fail criteria.**

- sb_stream/dsizep_mismatch, pkt_short, pkt_over_spsm, tag_continuity, pixel_data, lost_frame, header_field
- sb_linkpro/framing, kcode, packet_type, short_split
- AssertionError from write_ok on non-0x01 ack
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-DATA-001`: packet format checked only on the packets that reach the wire; Mono8, one DsizeP per run
- `CXP-CAM-IMG-011`: bit-exact on Mono8 frames only; one pixel source and geometry set per run

_Last result: **pass**, seed 58440, 196.8 µs simulated._

#### VP-STR-04 — `test_arbitrary_image`

**Title:** Arbitrary header plus line markers · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Plan rows:** `CXP-CAM-IMG-007`, `CXP-CAM-DATA-001`, `CXP-CAM-IMG-011`  
**Command:** `make UVM_TESTNAME=test_arbitrary_image run`

**Description.** With cfg_arbitrary=1 a sensor frame goes out with the arbitrary image header and arbitrary line markers and is reassembled bit-exact.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8) (PKT_DSIZE_P 256). Perfect APB; CfgToggleSeq(use_tpg=0, arbitrary=1). VideoRandomSeq(n_frames=1, rng_seed=0xCAFE): one random Mono8 frame (xsize {4..128}, ysize {2..32}, dval_density 0.5-1.0). No trailing Timer; run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_stream (arbitrary-header path, line markers), sb_linkpro, sb_linkstate
- no test-specific checks

**Fail criteria.**

- sb_stream/line_marker, header_field, dsizel, kmark_partial
- sb_stream/lost_frame (golden frame never reassembled off the wire), sb_stream/unmatched_frame
- sb_stream/pixel_data, pixel_count, header_field, crc, dsizep_mismatch, pkt_short, tag_continuity, framing, line_marker, streamid, dsizel, pkt_over_spsm
- sb_linkpro/framing, kcode, packet_type, short_split
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-IMG-007`: constant geometry on every line; per-line geometry never driven
- `CXP-CAM-DATA-001`: packet format checked only on the packets that reach the wire; Mono8, one DsizeP per run
- `CXP-CAM-IMG-011`: bit-exact on Mono8 frames only; one pixel source and geometry set per run

_Last result: **pass**, seed 40374, 89.0 µs simulated._

#### VP-STR-05 — `test_tpg_config`

**Title:** TPG pattern reprogrammed via registers · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly, feature  
**Spec:** §10.3.19, §10.3.27  
**Plan rows:** `CXP-CAM-IMG-002`, `CXP-CAM-IMG-011`  
**Command:** `make UVM_TESTNAME=test_tpg_config run`

**Description.** Host writes of the TestPattern register while the TPG free-runs propagate to the datapath without breaking framing; every TPG frame stays bit-exact against the TPG output.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB; CfgToggleSeq(use_tpg=1). For pat in (1,2,3,0) (bars, flat, grey-bars, gradient): open-loop UplinkRegSeq write MFR_TESTPATTERN 0x1001C=pat, then UplinkIdleSeq(6). Timer 6000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_stream (TpgMonitor golden: framing/CRC/bit-exact, not pattern content), sb_control (4 write acks), sb_reg, sb_linkpro
- pattern content itself is not checked (left to TPG unit bench)

**Fail criteria.**

- sb_stream/lost_frame (golden frame never reassembled off the wire), sb_stream/unmatched_frame
- sb_stream/pixel_data, pixel_count, header_field, crc, dsizep_mismatch, pkt_short, tag_continuity, framing, line_marker, streamid, dsizel, pkt_over_spsm
- sb_linkpro/framing, kcode, packet_type, short_split
- sb_control/ack_missing, ack_code
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

_Last result: **pass**, seed 35176, 596.9 µs simulated._

#### VP-STR-06 — `test_tpg_formats`

**Title:** TPG PixelFormat sweep and StreamID · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly, feature  
**Spec:** §9.4.2, §10.3.26, Table 19, Table 37  
**Plan rows:** `CXP-CAM-IMG-002`, `CXP-CAM-IMG-011`  
**Command:** `make UVM_TESTNAME=test_tpg_formats run`

**Description.** PixelFormat changes while the TPG runs: each frame is packed at its header's pixel width and matches the golden §9.4.2 packer; StreamID in packet and header equals Image1StreamID=5.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB; UplinkRegSeq write IMAGE1_STREAM_ID_ALIAS 0x1002C=5; UplinkIdleSeq(6); CfgToggleSeq(use_tpg=1). For fmt in (0x0101, 0x0102, 0x0103, 0x0104, 0x0105, 0x0101): UplinkRegSeq write FEAT_PIXFMT 0x10008=fmt; UplinkIdleSeq(6). Timer 6000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns). TpgMonitor skips (does not publish) frames whose format the device does not support.

**Pass criteria.**

- judges: sb_stream (per-format reassembly, streamid, header_field), sb_control, sb_reg, sb_linkpro
- no test-specific checks

**Fail criteria.**

- sb_stream/streamid, header_field, pixel_data, pixel_count, dsizel
- sb_linkpro/framing, kcode, packet_type, short_split
- sb_reg/write_code (unsupported PixelFormat value)
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

_Last result: **pass**, seed 40866, 980.7 µs simulated._

#### VP-STR-07 — `test_conn_config_write`

**Title:** ConnectionConfig write restarts PacketTag · **Catalogue:** T-05 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §10.3.33, §8.5.3  
**Plan rows:** `CXP-CAM-DATA-003`  
**Command:** `make UVM_TESTNAME=test_conn_config_write run`

**Description.** Proves a valid ConnectionConfig write (even of the current value) restarts PacketTag at 0 and a refused one (0x41) does not.

**How to test.** tpg(1), wait for 2 images (400 us); write ConnectionConfig <- CONNECTION_CONFIG_DEFAULT_VALUE (same value) while streaming; wait 2 images; write unsupported ConnectionConfig 0x00000001; wait 2 images.

**Pass criteria.**

- judges: sb_stream, sb_control, sb_reg, sb_test
- setup: fewer than 128 tags seen before the write (so a restart is distinguishable)
- ack: same-value write acknowledged 0x01
- tag_restart: a tag 0 appears in sb_stream.tags after the write
- ack: 0x00000001 acknowledged 0x41 (ACK_BAD_DATA)
- sb_stream checks tag continuity around the restart and that a refused write does not restart

**Fail criteria.**

- sb_test/tag_restart, setup, ack
- sb_stream/tag_continuity
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 955, 200.3 µs simulated._

#### VP-STR-08 — `test_spsm_negotiation`

**Title:** StreamPacketSizeMax values and mid-image change · **Catalogue:** T-06 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §10.1.5, Table 44, §8.5.2, §10.3.32, decision D4  
**Plan rows:** `CXP-CAM-BND-003`  
**Command:** `make UVM_TESTNAME=test_spsm_negotiation run`

**Description.** Proves no stream packet leaves at StreamPacketSizeMax 0, every packet respects the value in force (including a mid-image change), and a non-multiple-of-4 value is refused 0x41 with the register kept.

**How to test.** sensor() (cfg_use_tpg=0, cfg_run=1). StreamPacketSizeMax<-0 and offer a 128x16 Mono8 _Frame (seed 1) for 60 us. Then StreamPacketSizeMax 128, 1024, 4096 B, each followed by another 128x16 frame (seed=v) and wait for 1 image (800 us). Then a 128x16 frame (seed 7) with StreamPacketSizeMax<-256 written 8 us into it. Finally write 130 and read back.

**Pass criteria.**

- judges: sb_stream, sb_control, sb_reg, sb_test
- spsm_zero: no stream packet in 60 us with StreamPacketSizeMax 0
- sb_stream: every packet K27.7..K29.7 total <= value in force (pkt_over_spsm); images bit-exact
- spsm_odd: StreamPacketSizeMax<-130 acknowledged 0x41
- spsm_kept: StreamPacketSizeMax reads 256 afterwards

**Fail criteria.**

- sb_stream/pkt_over_spsm, sop_spsm_off, pixel_data, lost_frame
- sb_test/spsm_zero, spsm_odd, spsm_kept
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 18675, 1388.3 µs simulated._

#### VP-STR-09 — `test_packet_tag_persistence`

**Title:** PacketTag continuity across stream events · **Catalogue:** T-20 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.5.3  
**Plan rows:** `CXP-CAM-DATA-003`  
**Knobs:** `PKT_DSIZE_P=8`  
**Command:** `make UVM_TESTNAME=test_packet_tag_persistence run`

**Description.** Proves PacketTag increments mod 256 through geometry changes, acquisition stop/start and pixel-source switches, and wraps 255 -> 0.

**How to test.** PKT_DSIZE_P=8 (64-byte packets). tpg(1) until 60 tags (2 ms limit); Width alias<-6, Height alias<-3; to 120 tags; cfg_run=0, TpgRun<-1, AcquisitionStart; to 180 tags; AcquisitionStop, 20 us, AcquisitionStart; to 220 tags; AcquisitionStop, 20 us; sensor() and four 16x4 Mono8 _Frames (seeds 0..3); tpg(1) again until 330 tags (3 ms limit).

**Pass criteria.**

- judges: sb_stream, sb_control, sb_reg, sb_test
- sb_stream: tag +1 mod 256 on every packet (tag_continuity)
- wrap: a 255 -> 0 transition present in sb_stream.tags
- wait_until timeouts (2 ms / 3 ms) not checked directly

**Fail criteria.**

- sb_stream/tag_continuity, lost_frame, pixel_data
- sb_test/wrap (e.g. if < 256 packets reached)
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 46508, 509.3 µs simulated._

#### VP-STR-10 — `test_image_geometry_matrix`

**Title:** Image geometry and header matrix · **Catalogue:** T-21 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §9.4.2, §9.4.6, Table 37, Table 38  
**Plan rows:** `CXP-CAM-IMG-001`, `CXP-CAM-IMG-004`  
**Command:** `make UVM_TESTNAME=test_image_geometry_matrix run`

**Description.** Proves every geometry is bit-exact, lines are zero-padded to whole words, header fields are as sent, DsizeL in words, and packet StreamID equals the header's.

**How to test.** sensor(); 24 Mono8 _Frames: Width 1,2,3,5,7,13,64,127 x Height 1,2,33, meta xoffs=W, yoffs=H, sourcetag=0x100+i, flags=0x01, streamid=7; pixel-valid density 0.05 when W*H < 64 and i%3==0, else 1.0.

**Pass criteria.**

- judges: sb_stream
- no sb_test checks; stream scoreboard judges pixel data, padding, header_field, dsizel, streamid

**Fail criteria.**

- sb_stream/pixel_data, header_field, dsizel, streamid, line_marker, lost_frame
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 31074, 177.9 µs simulated._

#### VP-STR-11 — `test_pixel_formats`

**Title:** Mono8..Mono16 pixel packing · **Catalogue:** T-23 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §9.4.1, Table 25, §9.4.2, Figure 30  
**Plan rows:** `CXP-CAM-PIX-001`, `CXP-CAM-IMG-002`  
**Command:** `make UVM_TESTNAME=test_pixel_formats run`

**Description.** Proves each Mono format is packed per Figure 30 at its bit width with zero padding and the header PixelF equals the code, for sensor and TPG sources.

**How to test.** sensor(); for PixelFormat 0x0101/8, 0x0102/10, 0x0103/12, 0x0104/14, 0x0105/16 bits: write PixelFormat alias, 2 us, sensor frames 5x3 and 7x3 (seed code+w). 20 us; cfg_run=0; Width<-7, Height<-2; tpg(0); for each format: write PixelFormat, cfg_run=1, wait 2 images (400 us), cfg_run=0, 20 us.

**Pass criteria.**

- judges: sb_stream, sb_control, sb_reg
- no sb_test checks; stream scoreboard golden pack_line: packing, padding, header PixelF

**Fail criteria.**

- sb_stream/pixel_data, header_field, pixel_count
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 30547, 871.7 µs simulated._

#### VP-STR-12 — `test_stream_back_to_back_frames`

**Title:** 32 back-to-back sensor images · **Catalogue:** T-24 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.5, §8.5.2, §9.4.3  
**Plan rows:** `CXP-CAM-IMG-012`  
**Knobs:** `PKT_DSIZE_P=16`  
**Command:** `make UVM_TESTNAME=test_stream_back_to_back_frames run`

**Description.** Proves back-to-back images arrive bit-exact and in order, including one-word final packets, with every DsizeP equal to the words that follow.

**How to test.** PKT_DSIZE_P=16. sensor(); 32 Mono8 _Frames with no gap, rng('b2b'): H in {1,2,3,4}, W in {4,8,12,16,20,5,9}; every 4th image W is the smallest in 1..63 with (25 + H*(2+ceil(W/4))) % 16 == 1 so the image ends with a one-word packet; sourcetag=i.

**Pass criteria.**

- judges: sb_stream
- no sb_test checks; stream scoreboard: 32 images bit-exact and in order, DsizeP matches payload (dsizep_mismatch)

**Fail criteria.**

- sb_stream/dsizep_mismatch, pixel_data, lost_frame, unmatched_frame, header_field
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 51648, 105.5 µs simulated._

#### VP-STR-13 — `test_conc_backpressure_frames`

**Title:** Pixel-port back-pressure with Mono16 · **Catalogue:** C-11 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** nightly, weekly  
**Plan rows:** `CXP-CAM-IMG-012`  
**Knobs:** `PKT_DSIZE_P=200`, `APP_PERIOD=5`, `REQUIRES={'FIFO_DEPTH': 256}`, `Makefile KNOBS={'FIFO_DEPTH': '256'}`  
**Command:** `make UVM_TESTNAME=test_conc_backpressure_frames run`

**Description.** Proves s_pix_ready throttles the pixel port when pixel supply exceeds downlink capacity, with no pixel lost, repeated or reordered and no FIFO overflow.

**How to test.** Build FIFO_DEPTH=256 required. PixelFormat 0x0105 (Mono16), APP_PERIOD=5, 200-word packets, sensor source. host_traffic(200, 'c11_host') until frames done and host_triggers(15, 'c11_trig') in background. Overridden sensor_frames: 60 Mono16 (bits 16) frames from rng 'c11_frames' with sizes (8x64, 16x32, 12x40, 16x2, 8x1, 4x1) (docstring says 120 images).

**Pass criteria.**

- judges: sb_stream, sb_linkpro, sb_control, sb_reg, sb_ioack, sb_rxtrig, sb_test
- bin stall_ge8: video driver max_stall >= 8 cycles
- bin stall_eol: drv.stalls_at_eol > 0
- stall_eof / stall_sof recorded but not required
- sb_stream bit-exact pixels (pixel_data, pixel_count)
- sb_test 'overlap_not_reached'

**Fail criteria.**

- sb_stream/pixel_data, pixel_count, lost_frame, header_field
- sb_test/overlap_not_reached
- ValueError at build if FIFO_DEPTH != 256
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 12675, 912.8 µs simulated._

#### VP-STR-14 — `test_conc_source_and_format_switch`

**Title:** Source and format switched under traffic · **Catalogue:** C-12 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §9.4, §8.5.2  
**Plan rows:** `CXP-CAM-IMG-007`, `CXP-CAM-DATA-001`  
**Knobs:** `PKT_DSIZE_P=64`  
**Command:** `make UVM_TESTNAME=test_conc_source_and_format_switch run`

**Description.** Proves source (TPG/sensor), rectangular/arbitrary mode, PixelFormat, Width/Height and StreamPacketSizeMax changes take effect only at image boundaries, with every emitted image self-consistent and no oversize packet.

**How to test.** 64-word packets; host_traffic(400, 'c12_host', gap 5 us) throughout. tpg(1) first, then changes in order src, fmt, size, spsm, arb, src, fmt, size, spsm. When TPG is source: wait 1 frame (400 us timeout), change 'between', wait 1 frame, 100..400 ns, change 'mid'. When sensor is source: start a 64x8 frame, 300..1500 ns, change 'mid', finish frame, change 'between'. Changes: src toggles tpg/sensor; fmt 0x101/0x102/0x103; Width 3/5/8 and Height 1/2/4; SPSM 48/96/288 bytes; cfg_arbitrary toggled. Each change inside an sb_stream flush window (+4 us).

**Pass criteria.**

- judges: sb_stream, sb_linkpro, sb_control, sb_reg, sb_test
- bins src_between, src_mid, fmt_between, fmt_mid, size_mid, spsm_mid, arb_between (MUST_HIT) are hit unconditionally by change() -- intent, not measured
- frames_after(env, 1, 400000 ns) return value is ignored
- sb_stream: header matches content, tags continue, no pkt_over_spsm
- sb_test 'overlap_not_reached'

**Fail criteria.**

- sb_stream/header_field, pixel_count, pixel_data, pkt_over_spsm, dsizep_mismatch, tag_continuity, line_marker
- write_ok AssertionError on config writes
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 48462, 2188.1 µs simulated._

### 8.6 Triggers

_Host trigger (Table 15, Delay law, Figure 20) and device trigger (Table 16, ack gating, §8.3.3)._

#### VP-TRG-01 — `test_trigger_uplink`

**Title:** Four host triggers to trigger output · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Plan rows:** `CXP-CAM-TRIG-007`  
**Command:** `make UVM_TESTNAME=test_trigger_uplink run`

**Description.** Host low-speed trigger packets (Table 15, Delay 0) each produce one trigger_out_app edge with a fixed latency, and each is I/O-acknowledged.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB; UplinkTriggerRandomSeq(n=4, seed=3, delay=0) on trig_seqr (insertion lane): 4 triggers, each randomly TRIGGER_RISE or TRIGGER_FALL, sent serially (each waits until on the wire). run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_rxtrig (trig_out edge per trigger, latency spread), sb_ioack (K28.6 ack per trigger, D6 latency <= 1 host char), sb_linkerr, sb_linkpro
- no test-specific checks

**Fail criteria.**

- sb_rxtrig/trig_missing, trig_unexpected, latency_spread
- sb_ioack/count, code, latency, unexpected
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-TRIG-007`: 4 triggers, spaced, Delay 0; no overlap, retrigger or rate stress

_Last result: **pass**, seed 59690, 127.4 µs simulated._

#### VP-TRG-02 — `test_tx_trigger`

**Title:** Device pin edges to HS trigger packets · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly, feature  
**Spec:** §8.3.2, §8.3.3  
**Plan rows:** `CXP-CAM-TRIG-004`  
**Command:** `make UVM_TESTNAME=test_tx_trigger run`

**Description.** Edges on trigger_in_app reach the host as K28.4 (rise) / K28.2 (fall) trigger packets with Delay 0, gated by the host's I/O acks, and the host ends at the pin's level.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB; IoTriggerSeq(n_edges=6, gap=24 default tx_clk cycles) after sb_link_detected: pin toggles 1,0,1,0,1,0 (ends de-asserted). DeviceTriggerResponder answers each device trigger with an I/O ack (mode ack, delay 0). Timer 4000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_txtrig (sequence, delay=0, before_ack gating, counts, final level), sb_linkpro
- no test-specific checks

**Fail criteria.**

- sb_txtrig/rise_count, fall_count, final_level, sequence, delay, before_ack
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-TRIG-004`: Delay 0; pacing against the acknowledgment checked in the unit and device_top benches

_Last result: **pass**, seed 8508, 101.8 µs simulated._

#### VP-TRG-03 — `test_trigger_delay_latency`

**Title:** Host trigger Delay latency and polarity · **Catalogue:** T-17 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.3.2.1, Table 15, Figure 20  
**Plan rows:** `CXP-CAM-TRIG-002`  
**Command:** `make UVM_TESTNAME=test_trigger_delay_latency run`

**Description.** Proves the trig_out pulse latency minus Delay x (1/24 bit) is constant within one rx cycle, the non-selected edge gives no pulse, Delay > 239 glitches with no trigger and no I/O ack, and accepted triggers are acked 0x01.

**How to test.** For polarity 0 and 1 (cfg_trig_polarity, device pin held de-asserted = pol, sb_txtrig excused for 60 us): TRIGGER_RISE and TRIGGER_FALL with Delay 0, 1, 120, 238, 239 inserted, 4 chars apart (20 triggers total). Back to polarity 0, then Delay 240 and 255 rising.

**Pass criteria.**

- judges: sb_rxtrig, sb_ioack, sb_linkerr, sb_txtrig (excused around polarity changes)
- sb_rxtrig: latency spread within 1 rx cycle, no pulse for non-selected edge, glitch for Delay 240/255
- sb_ioack: every accepted trigger acknowledged 0x01
- coverage: len(sb_rxtrig.norm_latency) >= 10

**Fail criteria.**

- sb_rxtrig/latency_spread, trig_unexpected, trig_missing, glitch_missing
- sb_ioack/count, code, unexpected
- sb_linkerr/glitch_missing
- sb_test/coverage
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 19866, 616.6 µs simulated._

#### VP-TRG-04 — `test_tx_trigger_ack_rules`

**Title:** Device trigger packet ack rules · **Catalogue:** T-18 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.3.3, §8.3.2  
**Plan rows:** `CXP-CAM-TRIG-004`  
**Command:** `make UVM_TESTNAME=test_tx_trigger_ack_rules run`

**Description.** Proves the device never sends a trigger packet before the previous one is acknowledged or times out, each packet toggles the host level, and the final host level equals the pin.

**How to test.** Four modes of the host responder env.trig_resp, each with IoTriggerSeq(n_edges=6) toggling trigger_in_app then 60 us: (a) ack, delay 0, gap 400 tx cycles; (b) drop (no acks), gap 400; (c) ack delayed 40 chars, gap 400; (d) ack at once, gap 8 tx cycles (faster than acks return). Restore ack/0.

**Pass criteria.**

- judges: sb_txtrig, sb_linkpro
- no sb_test checks: sb_txtrig before_ack, sequence (each packet changes level), final_level == pin

**Fail criteria.**

- sb_txtrig/before_ack, sequence, final_level, rise_count, fall_count, delay
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 27100, 394.9 µs simulated._

#### VP-TRG-05 — `test_trigger_at_discovery`

**Title:** Trigger de-assertion at ConnectionReset · **Catalogue:** T-19 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.3.2, §10.3.28, §8.3.3, §8.3  
**Plan rows:** `CXP-CAM-TRIG-005`, `CXP-CAM-TRIG-001`  
**Command:** `make UVM_TESTNAME=test_trigger_at_discovery run`

**Description.** Proves ConnectionReset de-asserts triggers both ways: device sends one K28.2 and nothing for a held pin, host-side level falls (one pulse at polarity 1), resends are recognized, and extension-link triggers are ignored.

**How to test.** pin(1) (20 us), ConnectionReset, 60 us; pin 0/1/0; StreamPacketSizeMax=1056; host TRIGGER_RISE, 10 us, ConnectionReset, 60 us. Polarity 1 (pin held 1, 60 us excuse): two TRIGGER_RISE 10 us apart, ConnectionReset, 60 us; StreamPacketSizeMax; TRIGGER_RISE. from_extension_link=1: TRIGGER_FALL, TRIGGER_RISE 10 us apart; restore extension 0 and polarity 0 (60 us excuse).

**Pass criteria.**

- judges: sb_txtrig, sb_rxtrig, sb_ioack, sb_linkreset, sb_control, sb_reg, sb_test
- discovery_fall: after reset exactly one K28.2 and zero K28.4 in sb_txtrig.packets
- trig_out: sb_rxtrig.trig_matched == trig0+1 around host trigger + reset (polarity 0)
- resend: two rising at polarity 1 -> 0 pulses and trig_resent +1
- deassert_pulse: reset at polarity 1 -> trig_matched == trig0+1
- level_after_reset: rising after reset is not a resend (trig_resent still resent0+1)
- extension: 0 pulses and 0 I/O acks with from_extension_link=1

**Fail criteria.**

- sb_test/discovery_fall, trig_out, resend, deassert_pulse, level_after_reset, extension
- sb_txtrig/before_ack, sequence, final_level
- sb_rxtrig/trig_unexpected, trig_missing
- sb_ioack/unexpected, count
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 58811, 854.5 µs simulated._

#### VP-TRG-06 — `test_conc_uplink_trigger_in_packet`

**Title:** Host trigger inside command and test packet · **Catalogue:** C-05 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.2.4, Table 15  
**Plan rows:** `CXP-CAM-PROT-005`  
**Command:** `make UVM_TESTNAME=test_conc_uplink_trigger_in_packet run`

**Description.** Proves a low-speed host trigger inserted at any character boundary inside a control write or a host test packet fires with constant latency less Delay, is acknowledged, and does not corrupt the write or the test packet.

**How to test.** No stream source is started (PKT_DSIZE_P default 256). For off=0..27: an open-loop write of 0xC0DE0000+off to 0x1000C; a TRIGGER_RISE with delay (off*9)%240 inserted off host chars after the write's first character; 10 us then read-back. Then one host LINKTEST with lt_n_data=1024 with TRIGGER_FALL (delay 100) inserted at 0.1 %, 50 % and 99.8 % of 1027*4 chars; cfg_trig_polarity forced 0.

**Pass criteria.**

- judges: sb_rxtrig, sb_ioack, sb_control, sb_reg, sb_linktest, sb_linkpro, sb_test
- sb_test 'write' x28: read1(0x1000C) == 0xC0DE0000+off
- bin in_cmd: cmd.t_start_ns < trig.t_start_ns < cmd.t_done_ns
- bin in_test_packet: lt.t_start_ns < trig.t_start_ns
- wait_until cmd/linktest start within 100 us (step 50 ns)
- sb_test 'overlap_not_reached' (docstring header/data/CRC bins are not separately collected)

**Fail criteria.**

- sb_test/write (a write lost with an embedded trigger)
- sb_rxtrig/latency_spread, trig_missing, trig_unexpected
- sb_ioack/count, latency
- sb_linktest/err_count, pkt_count_rx
- sb_control/ack_missing; read1 AssertionError if read not acked 0x00
- sb_test/overlap_not_reached
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 14537, 9831.3 µs simulated._

### 8.7 I/O acknowledgment

_Table 17 acknowledgments for accepted host triggers._

#### VP-IOA-01 — `test_io_ack`

**Title:** Host triggers answered by K28.6 I/O-ack · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly, feature  
**Spec:** §8.3.3, Table 17  
**Plan rows:** `CXP-CAM-TRIG-001`, `CXP-CAM-TRIG-006`  
**Command:** `make UVM_TESTNAME=test_io_ack run`

**Description.** Every host trigger packet is answered by one K28.6 I/O-acknowledgment packet with code 0x01 replicated four times.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB; UplinkTriggerRandomSeq(n=6, seed=3, delay=0) on trig_seqr: six random rise/fall triggers. Timer 4000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_ioack (count, code 0x01x4, latency <= D6_IOACK_LATENCY_CHARS=1 host char), sb_rxtrig, sb_linkpro
- no test-specific checks

**Fail criteria.**

- sb_ioack/count, code, unexpected, latency (note: D6=1 is enforced by the scoreboard although PLAN_PARTIAL says no latency bound)
- sb_rxtrig/trig_missing
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-TRIG-006`: acks counted; no latency bound (D6 open)

_Last result: **pass**, seed 4636, 153.4 µs simulated._

### 8.8 Connection test

_Device Test Generator and Test Receiver, TestMode, test counters (§8.7, Table 23, §10.3.35-39)._

#### VP-LT-01 — `test_linktest_clean`

**Title:** Clean uplink link-test packets, ppm offset · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §6.7.4, §6.7  
**Plan rows:** `CXP-CAM-CT-004`  
**Knobs:** `HOST_PPM=200.0`, `HOST_JITTER_UI=0.02`  
**Command:** `make UVM_TESTNAME=test_linktest_clean run`

**Description.** Host connection-test packets received under a 200 ppm host rate offset and 0.02 UI jitter are counted in TestPacketCountRx with only the expected error count.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Host runs +200 ppm, 0.02 UI jitter. Perfect APB; UplinkLinktestSeq(n=4): four type-0x04 packets with lt_n_data default 64 data words (no injected errors). Timer 2000 ns settle for sideband republish. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_linktest (TestErrorCount / TestPacketCountRx vs model), sb_linkerr, sb_linkstate, sb_linkpro
- sb_linktest expects TestPacketCountRx=4 and TestErrorCount = 4 x |1024-64| = 3840 (each 64-word packet counts the 960 missing Table 23 words as errors); docstring's 'sb_lt_err_count = 0' is stale
- no sb_test checks

**Fail criteria.**

- sb_linktest/err_count, pkt_count_rx, sideband_dead
- sb_linkstate/link_drop (ppm/jitter)
- sb_linkerr/*_unexpected
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-CT-004`: 64-word bodies, not 1024; the last two words of a packet never compared

_Last result: **pass**, seed 61376, 1822.4 µs simulated._

#### VP-LT-02 — `test_linktest_inject`

**Title:** Link-test packets with injected word errors · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §6.7.4  
**Plan rows:** `CXP-CAM-CT-004`  
**Command:** `make UVM_TESTNAME=test_linktest_inject run`

**Description.** Corrupted words inside host connection-test packets are each counted once in TestErrorCount.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB; UplinkLinktestSeq(n=1) clean prefill (64 words) to prime the parser's link-test gate; then 3x [UplinkIdleSeq(n=4) + UplinkErrorInjectSeq(n=1, mode='lt_word', kind=LINKTEST, n_word_errors=3, seed=4)]: each packet 64 data words with 3 random word indices bit-0 flipped. Timer 2000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_linktest, sb_linkerr, sb_linkpro
- sb_linktest expects TestPacketCountRx=4 and TestErrorCount = 4 x 960 (short bodies) + 3 x 3 = 3849; docstring says 9 (only the injected part)
- no sb_test checks

**Fail criteria.**

- sb_linktest/err_count, pkt_count_rx, sideband_dead
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-CT-004`: 64-word bodies, not 1024; the last two words of a packet never compared

_Last result: **pass**, seed 5122, 1976.2 µs simulated._

#### VP-LT-03 — `test_tx_linktest_mode`

**Title:** TestMode on/off device link-test packets · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** ci, nightly, weekly, feature  
**Spec:** §8.7, §10.3.35, §10.3.38  
**Plan rows:** `CXP-CAM-CT-001`  
**Command:** `make UVM_TESTNAME=test_tx_linktest_mode run`

**Description.** A host write of 1 to TestMode makes the device send Table 23 type-0x04 packets with correct payload and >=16-word gaps; a write of 0 stops them.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Sets env.sb_linktest.expect_tx_linktest=True. Perfect APB; UplinkRegSeq write TestMode 0x401C=1; UplinkIdleSeq(n=8); UplinkRegSeq write TestMode=0; Timer 20000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_linktest (tx_payload vs Table 23 1024 words, tx_gap >= LT_GAP_WORDS=16, tx_after_testmode, tx_no_packet), sb_control (two write acks), sb_reg, sb_stream (sop_test_mode)
- expect_tx_linktest=True -> sb_linktest/tx_no_packet if no type-0x04 packet seen
- no register read of TestPacketCountTx is made, so reg_pkt_count_tx is not exercised despite the docstring

**Fail criteria.**

- sb_linktest/tx_no_packet, tx_payload, tx_gap, tx_after_testmode
- sb_control/ack_missing, ack_code (acks queued behind 1027-word packets)
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

_Last result: **pass**, seed 13423, 302.1 µs simulated._

#### VP-LT-04 — `test_linktest_rx_full`

**Title:** Host test packets and RX counters · **Catalogue:** T-25 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.7.2, Table 23, §8.7.3, §10.3.36-10.3.39, decision D5  
**Plan rows:** `CXP-CAM-CT-004`, `CXP-CAM-CT-005`  
**Knobs:** `REQUIRES={'OS_RATIO': 4}`, `Makefile KNOBS={'OS_RATIO': '4'}`  
**Command:** `make UVM_TESTNAME=test_linktest_rx_full run`

**Description.** Proves the device counts received test packets and word errors (sum of k), each counter clears independently on a 0 write, and an out-of-range selector is refused 0x41.

**How to test.** OS_RATIO 4 build. For TestMode 0 then 1: write TestMode, clear TestErrorCount and TestPacketCountRx; send 4 (tm 0) / 1 (tm 1) clean 1024-word LINKTEST packets; then errored packets with flipped words [3,500,1022], [1023], [0,1,2,3,4] (tm 0: all three; tm 1: first only); read TestErrorCount and TestPacketCountRx (8 B); clear TestErrorCount, read TestPacketCountRx; clear TestPacketCountRx; read TestErrorCount; write TestErrorCountSelector 1. Finally TestMode<-0.

**Pass criteria.**

- judges: sb_linktest, sb_control, sb_reg, sb_linkerr, sb_test (+ sb_linktest TX side with TestMode 1)
- sb_linktest: TestErrorCount = sum of flipped words (9 then 3), packet count by register read
- own_clear: TestPacketCountRx low word d[1] == 7 (tm 0) / 2 (tm 1) after clearing TestErrorCount
- clear: TestErrorCount reads 0 after clearing
- selector: TestErrorCountSelector<-1 acknowledged 0x41

**Fail criteria.**

- sb_linktest/err_count, pkt_count_rx
- sb_test/own_clear, clear, selector
- build_phase ValueError if OS_RATIO != 4
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 38569, 15177.5 µs simulated._

#### VP-LT-05 — `test_linktest_tx_rules`

**Title:** Device test packet transmission rules · **Catalogue:** T-26 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.7.4, §10.3.35, §10.3.38, Table 23  
**Plan rows:** `CXP-CAM-CT-001`, `CXP-CAM-CT-002`  
**Knobs:** `REQUIRES={'OS_RATIO': 4}`, `Makefile KNOBS={'OS_RATIO': '4'}`  
**Command:** `make UVM_TESTNAME=test_linktest_tx_rules run`

**Description.** Proves device test packets are Table 23 1024-word packets >= 16 words apart, no stream packet starts in TestMode, acks interleave between test packets, the in-flight packet completes at 1 -> 0 and none starts after, and TestPacketCountTx equals wire count.

**How to test.** OS_RATIO 4 build. tpg(1), wait 1 image (200 us); TestPacketCountTx<-0; sb_linktest.expect_tx_linktest=True. For cut in 0.1, 0.5, 0.97: TestMode<-1; 3 reads of TestPacketCountTx (8 B); wait for a new TX test packet (400 us, 50 ns step); wait cut*1027 tx periods; TestMode<-0; wait 1 image (200 us). Final TestPacketCountTx read.

**Pass criteria.**

- judges: sb_linktest, sb_stream, sb_linkpro, sb_control, sb_reg
- no sb_test checks; sb_linktest: tx_payload (Table 23, 1024 words), tx_gap >= 16 words, tx_after_testmode, tx_count vs register reads and final
- sb_stream: sop_test_mode (no stream SOP during TestMode)
- sb_linkpro: framing, no torn packet
- wait_until/frames_after timeouts not checked directly

**Fail criteria.**

- sb_linktest/tx_payload, tx_gap, tx_count, tx_after_testmode, tx_no_packet
- sb_stream/sop_test_mode
- sb_linkpro/framing
- build_phase ValueError if OS_RATIO != 4
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 63139, 393.4 µs simulated._

#### VP-LT-06 — `test_conc_testmode_under_stream`

**Title:** TestMode toggled under running stream · **Catalogue:** C-06 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.7.4, §10.3.35  
**Plan rows:** `CXP-CAM-CT-003`  
**Knobs:** `PKT_DSIZE_P=256`, `APP_PERIOD=5`  
**Command:** `make UVM_TESTNAME=test_conc_testmode_under_stream run`

**Description.** Proves TestMode switched on mid-stream lets the in-flight stream packet complete, starts no new stream packet until TestMode=0, drops caught images whole, and keeps triggers/I/O acks flowing (decision D2).

**How to test.** PixelFormat 0x0105 (Mono16), sensor source, APP_PERIOD=5, 256-word packets; 30 frames 128x16 Mono16. host_triggers(30, 'c06_htrig', gap 10..60 chars) and pin_edges(20, 'c06_pin', gap 10..40 us) in background. 4 times: random 5..40 us wait, sb_stream.flush_window, TestMode=1, 3 reads of TEST_PACKET_COUNT_TX (8 bytes), TestMode=0, close_flush_window. RisingEdge watcher on bs_test_mode records switch-on times.

**Pass criteria.**

- judges: sb_stream, sb_linkpro, sb_linktest, sb_ioack, sb_txtrig, sb_rxtrig, sb_control, sb_reg, sb_test
- bin testmode_mid_packet: bs_test_mode rise between a 0x01 packet's sop_ns and eop_ns
- bin ioack_in_testmode / trig_in_testmode: short packets with t_ns in [t_on, t_off]
- sb_stream sop_test_mode (no SOP while TestMode), flush window excuses dropped images
- sb_test 'overlap_not_reached'

**Fail criteria.**

- sb_stream/sop_test_mode, framing, lost_frame, tag_continuity
- sb_linktest/tx_after_testmode, tx_gap, tx_count
- sb_linkpro/framing
- sb_test/overlap_not_reached
- write_ok AssertionError on TestMode writes
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 45351, 2643.8 µs simulated._

#### VP-LT-07 — `test_conc_bidir_linktest`

**Title:** Bidirectional connection test with polling · **Catalogue:** C-15 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.7  
**Plan rows:** `CXP-CAM-CT-001`, `CXP-CAM-CT-004`  
**Knobs:** `REQUIRES={'OS_RATIO': 4}`, `Makefile KNOBS={'OS_RATIO': '4'}`  
**Command:** `make UVM_TESTNAME=test_conc_bidir_linktest run`

**Description.** Proves host->device and device->host connection tests run at once with counter polling: counters monotonic and matching, device test packets keep their spacing with acks in the gaps.

**How to test.** Build OS_RATIO=4 required. TestMode=1, sb_linktest.expect_tx_linktest=True. Background poll loop reading TEST_ERROR_COUNT (4 bytes), TEST_PACKET_COUNT_RX (8 bytes), TEST_PACKET_COUNT_TX (8 bytes) until stop. 4 host LINKTEST packets of lt_n_data=1024, the third (i==2) with lt_error_indices [5, 900]. Then TestMode=0.

**Pass criteria.**

- judges: sb_linktest, sb_control, sb_reg, sb_linkpro, sb_test
- sb_test 'monotonic': each counter read (last data word d[-1]) >= previous for err/rx/tx
- bin ack_in_lt_gap: a 0x03 ack between two consecutive 0x04 device test packets
- bin rx_during_tx: device 0x04 packets exist and sb_linktest.packets_seen > 0
- sb_linktest: counters match packets so far, 16-word gaps, final equality
- sb_test 'overlap_not_reached'

**Fail criteria.**

- sb_test/monotonic
- sb_linktest/err_count, pkt_count_rx, tx_count, tx_gap, tx_payload, tx_no_packet
- ValueError at build if OS_RATIO != 4
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 21988, 6742.3 µs simulated._

### 8.9 Resets and ConnectionReset

_ConnectionReset (§10.3.28), device resets and the flush / restart behaviour they require._

#### VP-RST-01 — `test_link_reset`

**Title:** ConnectionReset clears MasterHostConnectionID · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** ci, nightly, weekly, feature  
**Spec:** §10.3.28  
**Plan rows:** `CXP-CAM-INIT-002`  
**Command:** `make UVM_TESTNAME=test_link_reset run`

**Description.** A host write of 1 to ConnectionReset opens one link-reset window with a matching done pulse, clears MasterHostConnectionID to 0, and the reset-done ack is sent.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB; open-loop UplinkRegSeq write 0x4008=0xA5A5_A5A5; UplinkIdleSeq(8); UplinkRegSeq write 0x4000=1; UplinkIdleSeq(8); UplinkRegSeq read 0x4008; Timer 4000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_linkreset (window/done pairing, rate_mirror, no_window, excess_windows, window_long 200 ms), sb_reg (post-reset read value 0 via RegRef §10.3.28 load), sb_control, sb_linkstate, sb_stream (tag reset / sop_reset_window)
- no test-specific checks: the readback-is-0 is judged by sb_reg/sb_control model

**Fail criteria.**

- sb_linkreset/no_window, window_done, done_no_window, rate_mirror, excess_windows
- sb_reg/read_value
- sb_control/ack_code, ack_missing
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-INIT-002`: one §10.3.28 post-condition read back; no rate fallback, no 200 ms deadline

_Last result: **pass**, seed 44420, 434.7 µs simulated._

#### VP-RST-02 — `test_xifc_stream_linkreset`

**Title:** ConnectionReset mid-stream then resume · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly, xifc  
**Spec:** §10.3.28, §8.5.3  
**Plan rows:** `CXP-CAM-DATA-003`, `CXP-CAM-DATA-001`, `CXP-CAM-IMG-002`, `CXP-CAM-IMG-011`  
**Command:** `make UVM_TESTNAME=test_xifc_stream_linkreset run`

**Description.** A ConnectionReset during a running stream does not corrupt the wire; the packet in flight keeps its tag, the device holds the stream until SPSM is rewritten, and resumes with PacketTag 0.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). VsStreamPlusLinkReset(seed=13, host=env.host, spsm=spsm_for(pkt_dsize)=4*(256+8)=1056): Perfect APB, CfgToggleSeq(use_tpg=1); UplinkIdleSeq(24); closed-loop host.write(0x4000,[1]) (code not checked); UplinkIdleSeq(24); host.write_ok(StreamPacketSizeMax 0x4010, [1056]); UplinkIdleSeq(24). Timer 10000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_stream (tag_continuity with reset to 0, sop_spsm_off, sop_reset_window, flushed frames), sb_linkreset, sb_control, sb_reg, sb_linkpro
- write_ok on the SPSM rewrite raises AssertionError unless 0x01

**Fail criteria.**

- sb_stream/tag_continuity, sop_spsm_off, sop_reset_window, lost_frame
- sb_stream/pixel_data, pixel_count, header_field, crc, dsizep_mismatch, pkt_short, tag_continuity, framing, line_marker, streamid, dsizel, pkt_over_spsm
- sb_linkpro/framing, kcode, packet_type, short_split
- sb_linkreset/no_window, window_done
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-DATA-001`: packet format checked only on the packets that reach the wire; Mono8, one DsizeP per run
- `CXP-CAM-IMG-011`: bit-exact on Mono8 frames only; one pixel source and geometry set per run

_Last result: **pass**, seed 49696, 1112.4 µs simulated._

#### VP-RST-03 — `test_conn_reset_postconditions`

**Title:** ConnectionReset post-conditions · **Catalogue:** T-04 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §10.3.28, Table 44, §8.5.3, §8.3.2  
**Plan rows:** `CXP-CAM-INIT-002`  
**Command:** `make UVM_TESTNAME=test_conn_reset_postconditions run`

**Description.** Proves ConnectionReset returns every §10.3.28 register and the test counters to reset values, silences the stream until StreamPacketSizeMax is rewritten, and restarts PacketTag at 0.

**How to test.** tpg(1); MasterHostConnectionID<-0x12345678; two host LINKTEST packets with lt_n_data=64 (priming counters); TestMode<-1, 30 us; device trigger pin trigger_in_app=1 (IoEvent trig_rise), 10 us; TestMode<-0, 40 us; ConnectionReset<-1 and poll up to 20 reads. Read MasterHostConnectionID, StreamPacketSizeMax, ConnectionConfig, TestMode, TestErrorCountSelector, XmlManifestSelector, ElectricalComplianceTest; read TestErrorCount (4 B), TestPacketCountTx (8 B), TestPacketCountRx (8 B); wait 40 us; drop pin; write StreamPacketSizeMax=1056 and wait for 1 image (400 us).

**Pass criteria.**

- judges: sb_control, sb_reg, sb_linkreset, sb_stream, sb_linktest, sb_txtrig, sb_linkpro, sb_test
- prime: TestErrorCount > 0 before the reset (via read1)
- reset_done: ConnectionReset reads 0 within 20 polls
- reset_window: len(sb_linkreset.windows_ns) == 1
- register reset values predicted by sb_reg/sb_control for the 7 registers read
- counter: TestErrorCount, TestPacketCountTx, TestPacketCountRx each ack 0x00 and all words 0
- stream_after_reset: no new stream packet (crc_checked unchanged) over the reads + 40 us before StreamPacketSizeMax
- first_tag: first stream packet after the rewrite has tag 0

**Fail criteria.**

- sb_test/prime, counter, stream_after_reset, first_tag, reset_window, reset_done
- sb_stream/sop_reset_window, sop_spsm_off, tag_continuity
- sb_linkreset/window_long, excess_windows
- sb_reg/read_value
- sb_txtrig kinds around the pin held through reset
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 885, 1902.5 µs simulated._

#### VP-RST-04 — `test_conc_conn_reset_everything`

**Title:** ConnectionReset storm under everything · **Catalogue:** C-07 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §10.3.28  
**Plan rows:** `CXP-CAM-REC-002`  
**Command:** `make UVM_TESTNAME=test_conc_conn_reset_everything run`

**Description.** Proves ConnectionReset issued with a stream packet in flight, a control command pending on a hung slave, the device trigger asserted and a host test packet arriving tears no packet, restores §10.3.28 reset values, and streaming resumes from tag 0 only after StreamPacketSizeMax is rewritten.

**How to test.** sensor source; sensor_frames(6, 'c07_frames', 128x32). After 30 us: ApbResponderHangSeq (latency NEVER=0xFFFF), open-loop read of user word 3; trigger_in_app=1; host LINKTEST lt_n_data=64 started; after 16 host chars bins sampled; sb_stream flush window opened; open-loop ConnectionReset write (waits behind hung read, D7 serialise, ends at its 900 ms timeout). Wait up to ms_ns(1500) (=300000 ns at RX_CLK_KHZ=20, 10 ns rx) for no pending; ApbResponderPerfectSeq; 5 closed-loop ConnectionReset<-1 writes spaced 200 rx cycles; up to 20 reads until ConnectionReset reads 0; reads MasterHostConnectionID, StreamPacketSizeMax, TestMode, TestErrorCount; trigger released; SPSM rewritten spsm_for(256)=1056; one more sensor frame.

**Pass criteria.**

- judges: sb_stream, sb_linkpro, sb_control, sb_reg, sb_linkreset, sb_linktest, sb_txtrig, sb_ioack, sb_test
- bins: reset_mid_stream (enclosing packet at tx_words or crc_checked>0), reset_ctrl_pending (sb_control.pending_count()>0), reset_trig_asserted (trigger_in_app=1), reset_linktest_rx (lt started, not done)
- register values after reset judged by sb_reg (reset values, counters 0)
- wait_until a new stream tag within 200 us (step 500 ns)
- sb_test 'tag0': sb_stream.tags[tags0:tags0+1] == [0]
- sb_test 'overlap_not_reached'

**Fail criteria.**

- sb_test/tag0
- sb_linkreset/excess_windows, no_window, window_long, window_done, done_no_window
- sb_reg/read_value, read_code
- sb_stream/sop_reset_window, sop_spsm_off, tag_continuity
- sb_linkpro/framing (torn packet)
- sb_control/ack_missing, ack_unexpected (hung read, 900 ms timeout)
- sb_test/overlap_not_reached
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 42584, 1241.9 µs simulated._

#### VP-RST-05 — `test_conc_single_domain_reset`

**Title:** Single clock-domain reset under traffic · **Catalogue:** C-13 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §10.3.28  
**Plan rows:** `CXP-CAM-REC-004`  
**Command:** `make UVM_TESTNAME=test_conc_single_domain_reset run`

**Description.** Proves a reset of any one clock domain (app, tx or rx) under traffic resets the whole device cleanly: link returns, nothing stale replayed, no torn packet, stream resumes from tag 0 and control answers.

**How to test.** sensor source. For dom in app, tx, rx: sensor_frames(3, 'c13_<dom>', 128x16); after 15 us, sb_linkerr.allow; clkrst_ag.do_reset(cycles=20, domains=dom); await frames; host.link_up(); sb_linkerr.close(); bringup() (link + SPSM 1056 for 256 words); read STANDARD.

**Pass criteria.**

- judges: sb_stream, sb_linkpro, sb_linkstate, sb_linkerr, sb_control, sb_reg, sb_test
- bins reset_app/tx/rx: hit only if sb_stream.crc_checked > 0 at reset time
- bringup: SPSM write must ack ACK_OK_WRITE (AssertionError otherwise); link_up within 2 ms
- sb_test 'overlap_not_reached'

**Fail criteria.**

- sb_stream/replica, tag_continuity, lost_frame, framing
- sb_linkpro/framing
- sb_linkstate/link_not_up
- AssertionError from link_up/bringup
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 14108, 972.0 µs simulated._

### 8.10 Downlink scheduling

_Table 13 priorities: long-packet arbitration, trigger / I/O-ack insertion, IDLE rules (§8.2.4, §8.2.5)._

#### VP-ARB-01 — `test_arbiter_preempt`

**Title:** Four sources concurrently through arbiter · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Plan rows:** `CXP-CAM-DATA-001`, `CXP-CAM-IMG-002`, `CXP-CAM-IMG-011`  
**Command:** `make UVM_TESTNAME=test_arbiter_preempt run`

**Description.** Stream, control acks, I/O acks and link-test traffic run concurrently and the arbiter keeps every stream packet and every acknowledgment intact.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8) (PKT_DSIZE_P 256). VsStressConcurrent(seed=2): Perfect APB, CfgToggleSeq(use_tpg=0); concurrently VideoRandomSeq(n_frames=2, rng_seed=2), UplinkCtrlRandomSeq(n=4, seed=3, mixed read/write on SAFE_RW_ADDRS), UplinkTriggerRandomSeq(n=2, seed=5) started on the packet-lane uplink_seqr (between packets, not inserted), UplinkLinktestSeq(n=1) (64 words). Then Timer 10000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_stream, sb_control, sb_reg, sb_rxtrig, sb_ioack, sb_linktest, sb_linkpro
- no test-specific checks

**Fail criteria.**

- sb_stream/lost_frame (golden frame never reassembled off the wire), sb_stream/unmatched_frame
- sb_stream/pixel_data, pixel_count, header_field, crc, dsizep_mismatch, pkt_short, tag_continuity, framing, line_marker, streamid, dsizel, pkt_over_spsm
- sb_linkpro/framing, kcode, packet_type, short_split
- sb_control/ack_missing, ack_code
- sb_ioack/count, latency
- sb_rxtrig/trig_missing
- sb_linktest/err_count, pkt_count_rx
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-DATA-001`: packet format checked only on the packets that reach the wire; Mono8, one DsizeP per run
- `CXP-CAM-IMG-011`: bit-exact on Mono8 frames only; one pixel source and geometry set per run

_Last result: **pass**, seed 25725, 746.4 µs simulated._

#### VP-ARB-02 — `test_arbiter_stream_underflow`

**Title:** I/O-ack survives short final stream packet · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.3.3  
**Plan rows:** `CXP-CAM-DATA-001`, `CXP-CAM-IMG-002`, `CXP-CAM-IMG-011`  
**Knobs:** `PKT_DSIZE_P=64`  
**Command:** `make UVM_TESTNAME=test_arbiter_stream_underflow run`

**Description.** After a frame whose last stream packet is shorter than DsizeP, the arbiter is not wedged: every subsequent host trigger still gets its K28.6 I/O-ack.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8) with PKT_DSIZE_P=64. Perfect APB; CfgToggleSeq(use_tpg=0, arbitrary=0); _OneFrameSeq default 64x8 Mono8, dval_density 1.0, ramp (docstring: 169 merged words = 2x64 + 41-word tail); UplinkIdleSeq(256); UplinkTriggerRandomSeq(n=4, seed=3) on trig_seqr. Timer 6000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_ioack (4 acks), sb_rxtrig, sb_stream (frame bit-exact, pkt_short), sb_linkpro
- no test-specific checks

**Fail criteria.**

- sb_ioack/count, latency
- sb_stream/lost_frame, dsizep_mismatch, pkt_short
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-DATA-001`: packet format checked only on the packets that reach the wire; Mono8, one DsizeP per run
- `CXP-CAM-IMG-011`: bit-exact on Mono8 frames only; one pixel source and geometry set per run

_Last result: **pass**, seed 19857, 3411.2 µs simulated._

#### VP-ARB-03 — `test_arbiter_underflow_ctrl`

**Title:** Ctrl-ack survives short final stream packet · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Plan rows:** `CXP-CAM-DATA-001`, `CXP-CAM-IMG-002`, `CXP-CAM-IMG-011`  
**Knobs:** `PKT_DSIZE_P=64`  
**Command:** `make UVM_TESTNAME=test_arbiter_underflow_ctrl run`

**Description.** After the same short last stream packet, spaced host control reads are all acknowledged (type 0x03 ack packets), proving no arbiter dead-lock.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8) with PKT_DSIZE_P=64. Perfect APB; CfgToggleSeq(use_tpg=0, arbitrary=0); _OneFrameSeq default 64x8; UplinkIdleSeq(256); 4x [UplinkRegSeq read 0x4008 + UplinkIdleSeq(16)]. Timer 6000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_control (4 read acks), sb_reg, sb_stream, sb_linkpro
- no test-specific checks

**Fail criteria.**

- sb_control/ack_missing, ack_code, ack_data
- sb_stream/lost_frame, pkt_short
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-DATA-001`: packet format checked only on the packets that reach the wire; Mono8, one DsizeP per run
- `CXP-CAM-IMG-011`: bit-exact on Mono8 frames only; one pixel source and geometry set per run

_Last result: **pass**, seed 27646, 4364.9 µs simulated._

#### VP-ARB-04 — `test_trigger_in_ctrl_packet`

**Title:** Trigger inserted inside control command · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.2.4, Table 15  
**Command:** `make UVM_TESTNAME=test_trigger_in_ctrl_packet run`

**Description.** A Table 15 trigger inserted at a character boundary inside a long control packet both fires (with I/O-ack) and leaves the command intact so it is executed and acknowledged 0x00.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB; UplinkIdleSeq(8); start_soon _OneCtrlReadSeq(addr=0x4008) on the packet lane; after Timer 20000 ns (~3 word times into the 6-word command) start_soon _OneTriggerSeq (one TRIGGER_RISE, Delay 0) on trig_seqr; await both; Timer 40000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_control (read 0x4008 acked 0x00 with data), sb_rxtrig (trig_out edge), sb_ioack (K28.6 ack), sb_linkerr, sb_linkpro
- no test-specific checks

**Fail criteria.**

- sb_control/ack_missing, ack_code (command corrupted by insertion)
- sb_rxtrig/trig_missing
- sb_ioack/count
- sb_linkerr/pkt_unexpected, code_unexpected
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

_Last result: **pass**, seed 8840, 274.0 µs simulated._

#### VP-ARB-05 — `test_conc_ioack_inside_stream`

**Title:** I/O ack inserted inside stream packets · **Catalogue:** C-02 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.2.4, Table 13, §8.3.3  
**Plan rows:** `CXP-CAM-TRIG-006`  
**Knobs:** `PKT_DSIZE_P=24`, `APP_PERIOD=5`  
**Command:** `make UVM_TESTNAME=test_conc_ioack_inside_stream run`

**Description.** Proves the I/O acknowledgment (priority 1) is inserted into stream-packet headers, payload, tail and control-ack payload without corrupting the enclosing packet's CRC/DsizeP, and within one host character of its trigger (decision D6).

**How to test.** PixelFormat alias written 0x0105 (Mono16); sensor source; APP_PERIOD=5 ns (pixel clock twice tx) with 24-word packets so packets run back to back. Continuous 128x32 Mono16 (bits=16) frames until stop, while host_triggers(150, 'c02_trig', gap 3..9 host chars) inserts random TRIGGER_RISE/FALL with delay 0..239. Then, stream stopped, 20 us wait and _into_ctrl_acks: for usr_latency L = 0..63 rx cycles/word, an open-loop 256-byte user-window read at USER_BASE (0x20000) immediately followed by an alternating host trigger; waits up to 200x1 us for sb_control pending to clear + 2 us; stops once the last IOACK landed in '0x03:payload' twice; usr_latency reset to 0.

**Pass criteria.**

- judges: sb_stream, sb_linkpro, sb_control, sb_ioack, sb_rxtrig, sb_test
- bins from pkt_log.where(tx_cycle-1) of every IOACK: ioack_0x01:header, ioack_0x01:payload, ioack_0x01:tail, ioack_0x03:payload, ioack_idle (all MUST_HIT)
- sb_test 'ioack_latency': max(sb_ioack.latency_ns) <= D6_IOACK_LATENCY_CHARS (=1) * uplink char_ns
- position (word, of) of IOACKs inside packets logged only
- sb_test 'overlap_not_reached'

**Fail criteria.**

- sb_test/ioack_latency (>1 host character)
- sb_test/overlap_not_reached (e.g. no IOACK in ctrl-ack payload after 64 latency steps)
- sb_stream/crc, dsizep_mismatch, framing; sb_control/ack_crc, ack_length
- sb_linkpro/short_split, framing
- sb_ioack/latency, count, unexpected
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 10717, 7802.5 µs simulated._

#### VP-ARB-06 — `test_conc_trigger_insertion_sweep`

**Title:** Device trigger swept across packet types · **Catalogue:** C-03 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.2.4, §8.2.5.1, §8.3.2  
**Plan rows:** `CXP-CAM-TRIG-004`  
**Knobs:** `PKT_DSIZE_P=16`  
**Command:** `make UVM_TESTNAME=test_conc_trigger_insertion_sweep run`

**Description.** Proves device trigger packets inserted into stream packets, control acks and test packets go out a fixed number of words after the pin edge, are never split and leave the enclosing packet's CRC intact.

**How to test.** tpg(1) free-running test pattern, 16-word packets. Phase 1: host_traffic(30, 'c03_host') concurrently with sweep(60, phase 0): each pin toggle after (rt + 7*(k%97) + phase) tx cycles, rt = 20 host chars in tx cycles. Phase 2: tpg(0), 20 us, one 256-byte user-window read measures ack start delta; then 16 reads with the pin toggled at delta+4+4k words into the 70-word ack, 20 chars apart; pin released if high. Phase 3: tpg(1), TestMode=1, sweep(40, phase 3), TestMode=0. Every edge is also written to io_ag monitor.

**Pass criteria.**

- judges: sb_stream, sb_linkpro, sb_linktest, sb_control, sb_txtrig, sb_reg, sb_test
- bins: trig_0x01, trig_0x03, trig_0x04 from pkt_log.where of each TRIG_RISE/TRIG_FALL (MUST_HIT)
- sb_test 'trig_latency': edge-to-packet word latencies non-empty and max-min <= 3 words (docstring says 2)
- SVA (not in this file): at most 99 non-IDLE words in a row
- sb_test 'overlap_not_reached'

**Fail criteria.**

- sb_test/trig_latency spread > 3 or empty
- sb_linkpro/short_split
- sb_stream/crc, sb_linktest/tx_payload or tx_gap, sb_control/ack_crc
- sb_txtrig/sequence, delay, rise_count, fall_count, before_ack
- sb_test/overlap_not_reached
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 39008, 5404.6 µs simulated._

#### VP-ARB-07 — `test_conc_trigger_vs_ioack`

**Title:** Device trigger and I/O ack collide · **Catalogue:** C-04 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.2.4  
**Plan rows:** `CXP-CAM-TRIG-007`  
**Knobs:** `PKT_DSIZE_P=1016`  
**Command:** `make UVM_TESTNAME=test_conc_trigger_vs_ioack run`

**Description.** Proves a device trigger (priority 0) and an I/O acknowledgment (priority 1) due in the same words inside a stream packet both go out whole, trigger first, with the stream packet intact.

**How to test.** sensor source; continuous 128x32 Mono8 frames with 1016-word packets. After 20 us, up to 96 iterations: host TRIGGER_RISE (delay 0), then 0..80 ns random wait (rng 'c04'), pin toggled, 20 host chars wait; from k>=23 stops early once an IOACK and a TRIG are within 4 words in the same enclosing packet. Pin released, stream stopped.

**Pass criteria.**

- judges: sb_stream, sb_linkpro, sb_ioack, sb_txtrig, sb_rxtrig, sb_test
- bin adjacent: an IOACK and a TRIG_RISE/FALL within 4 tx cycles
- bin both_in_one_packet: same enclosing stream packet
- trigger-first ordering only via scoreboards (no explicit check in the test)
- sb_test 'overlap_not_reached'

**Fail criteria.**

- sb_test/overlap_not_reached
- sb_linkpro/short_split, framing
- sb_stream/crc
- sb_ioack/latency, count; sb_txtrig/sequence, delay
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **fail**, seed 30176, 545.7 µs simulated — ValueError: Timer argument time must be positive._

### 8.11 Robustness and error injection

_Behaviour under injected uplink, bus and protocol errors._

#### VP-ROB-01 — `test_byte_replication_robust`

**Title:** One-bit replica error still executed · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.2.2.1  
**Plan rows:** `CXP-CAM-PROT-006`  
**Command:** `make UVM_TESTNAME=test_byte_replication_robust run`

**Description.** A single bit flipped in one of the four replicas of the control command's TYPE word is outvoted 3-of-4 and the command is executed normally.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB; UplinkErrorInjectSeq(n=4, mode='replica', kind=CTRL_CMD_READ, seed=4): four open-loop reads of address 0x0000, each with one bit flipped in replica randrange(0,4) of the TYPE word (beats[1]). run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_control (reads acked 0x00 with data), sb_reg, sb_linkerr (no pulses expected: replica damage is not a code/disp error)
- no test-specific checks

**Fail criteria.**

- sb_control/ack_missing, ack_code, ack_data
- sb_linkerr/pkt_unexpected
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-PROT-006`: one bit in one replica of the type word only

_Last result: **pass**, seed 62039, 261.9 µs simulated._

#### VP-ROB-02 — `test_crc_error`

**Title:** CRC-corrupted control commands NACKed 0x80 · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly  
**Plan rows:** `CXP-CAM-NEG-001`  
**Command:** `make UVM_TESTNAME=test_crc_error run`

**Description.** Control commands with a corrupted CRC are refused with ack 0x80, make no register access, and raise one CRC pulse each.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). Perfect APB; UplinkErrorInjectSeq(n=4, mode='crc', kind=CTRL_CMD_READ, seed=4): four open-loop reads of 0x0000 with inject_crc_err=True. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_control (predicts 0x80 per command, crc_count 0x80 pulses == 4), sb_linkerr (pkt pulses), sb_linkpro
- no test-specific checks

**Fail criteria.**

- sb_control/ack_code, crc_count, access_rejected, ack_missing
- sb_linkerr/pkt_unexpected
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

_Last result: **pass**, seed 20623, 261.9 µs simulated._

#### VP-ROB-03 — `test_uplink_bit_errors`

**Title:** Uplink replicated-character and CRC bit errors · **Catalogue:** T-13 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.2.2.1, §8.2.2.2, Table 15, decision D8  
**Plan rows:** `CXP-CAM-PROT-006`  
**Command:** `make UVM_TESTNAME=test_uplink_bit_errors run`

**Description.** Proves the 3-of-4 majority vote repairs single-lane damage, 2-of-4 damage is never executed wrong, CRC-covered errors give 0x80, opcode errors 0x42, repairable triggers fire and unrepairable ones glitch, and error pulses match the injection plan.

**How to test.** 1-of-4 damage: Revision reads with SOP lane 2 and EOP lane 1 turned to data 0x00, type word lane 3 = 0x07. With sb_linkerr allowance: 2-of-4 damage (type lanes 1,2; SOP lanes 1+2; EOP lanes 1+2) sent open loop with may_drop, 40 us each + Standard read. CRC errors: address bit 5 of beat 3, data bit 17 of beat 4 (write 0x5A5A5A5A to 0x1000C), CRC bit 30; opcode bit 7 flip. Five TRIGGER_RISE Delay 10 inserted with inject_code_at 0/1/2, trig_delays (10,10,11) and (10,20,30), 20 chars apart. IDLE with inject_code_at 2 and with inject_disp_at 1 (each + clean IDLE + Standard read); Revision read with inject_code_at 13; final Standard read.

**Pass criteria.**

- judges: sb_control, sb_reg, sb_linkerr, sb_rxtrig, sb_ioack, sb_linkstate
- 1-of-4: command executes (control model predicts)
- 2-of-4: executed as sent, 0x47, or dropped (may_drop=True), never executed wrong
- expect_codes 0x80 (ACK_CRC) x3, 0x42 (ACK_BAD_OP) for opcode bit 7
- repairable triggers acknowledged (sb_ioack) and pulse; (10,20,30) delays give a glitch (sb_rxtrig glitch)
- sb_linkerr: error pulses equal injection plan (code/disp/pkt/glitch)
- link never drops (sb_linkstate)

**Fail criteria.**

- sb_linkerr/code_missing, disp_missing, glitch_missing, *_unexpected
- sb_rxtrig/trig_missing, glitch_missing, glitch_unexpected
- sb_control/ack_code, access
- sb_ioack/count, code
- sb_linkstate/link_drop
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 3387, 1359.7 µs simulated._

#### VP-ROB-04 — `test_conc_uplink_errors_under_stream`

**Title:** Noisy uplink under running stream · **Catalogue:** C-09 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.2.2, §8.6.3  
**Plan rows:** `CXP-CAM-NEG-001`  
**Command:** `make UVM_TESTNAME=test_conc_uplink_errors_under_stream run`

**Description.** Proves uplink CRC errors, code errors, disparity flips, truncated and reserved-type packets mixed with good commands and host triggers are answered/discarded as the model allows, raise receiver error pulses only for injected kinds, and leave the downlink undisturbed.

**How to test.** sensor source; sensor_frames(12, 'c09_frames'); pin_edges(10, 'c09_pin', gap 2..30 us). sb_linkerr.allow opened at start. Deterministic prefix: one each of crc (REVISION read with inject_crc_err via h.command), code (inject_code_at=10), trunc (beats b[:3]+[b[-1]], expect ACK_SIZE_MISMATCH), type (RAW type 0x05) and disp (inject_disp_at=10), each followed by a STANDARD read. Then 60 random actions from rng 'c09' weighted good 2/8 (STANDARD or user word 0..63 read), crc, code (at 8..15), disp (at 8..13), trunc, type (0x05/0x7F), trig (TRIGGER_RISE delay 0..239), each followed by 0..4 chars gap. 60 us settle, sb_linkerr.close().

**Pass criteria.**

- judges: sb_control, sb_linkerr, sb_stream, sb_linkpro, sb_rxtrig, sb_ioack, sb_txtrig, sb_reg, sb_test
- bins crc>cmd, code>cmd, trunc>cmd, type>cmd, disp>cmd: hit unconditionally by the deterministic prefix (also by random error->good transitions)
- sb_linkerr: seen >= injected per kind (code/disp/pkt/glitch); non-injected kinds must stay 0 (cascade disp/pkt allowed after code/disp)
- sb_control: good commands answered with data; bad ones with their code or not at all
- sb_test 'overlap_not_reached'

**Fail criteria.**

- sb_linkerr/<kind>_missing, <kind>_unexpected
- sb_control/ack_code, ack_missing, ack_unexpected, ack_data
- sb_stream/crc, lost_frame; sb_linkpro/framing
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 33433, 3156.7 µs simulated._

### 8.12 Concurrency

_Several channels active at once: the cross-feature interactions the per-feature tests cannot see._

#### VP-CON-01 — `test_xifc_stream_ctrl`

**Title:** TPG stream with concurrent register traffic · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly, xifc  
**Plan rows:** `CXP-CAM-CTRL-010`, `CXP-CAM-DATA-001`, `CXP-CAM-IMG-002`, `CXP-CAM-IMG-011`  
**Command:** `make UVM_TESTNAME=test_xifc_stream_ctrl run`

**Description.** Control commands are executed and acknowledged while the TPG stream occupies the downlink, without corrupting stream packets.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). VsStreamPlusCtrl(n_cmds=8, seed=11): Perfect APB, CfgToggleSeq(use_tpg=1) free-running stream, then UplinkCtrlRandomSeq(n=8, seed=11, mixed read/write on SAFE_RW_ADDRS) open-loop. Timer 10000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_stream, sb_control (ack code/data, ack_late 200 ms), sb_reg, sb_linkpro
- no test-specific checks

**Fail criteria.**

- sb_stream/lost_frame (golden frame never reassembled off the wire), sb_stream/unmatched_frame
- sb_stream/pixel_data, pixel_count, header_field, crc, dsizep_mismatch, pkt_short, tag_continuity, framing, line_marker, streamid, dsizel, pkt_over_spsm
- sb_linkpro/framing, kcode, packet_type, short_split
- sb_control/ack_missing, ack_code, ack_data
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-CTRL-010`: acks under stream load counted; no latency bound
- `CXP-CAM-DATA-001`: packet format checked only on the packets that reach the wire; Mono8, one DsizeP per run
- `CXP-CAM-IMG-011`: bit-exact on Mono8 frames only; one pixel source and geometry set per run

_Last result: **pass**, seed 19143, 479.2 µs simulated._

#### VP-CON-02 — `test_xifc_stream_trigger`

**Title:** TPG stream with device HS triggers · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly, xifc  
**Spec:** §8.3.2  
**Plan rows:** `CXP-CAM-DATA-001`, `CXP-CAM-IMG-002`, `CXP-CAM-IMG-011`  
**Command:** `make UVM_TESTNAME=test_xifc_stream_trigger run`

**Description.** Device trigger packets inserted into the running stream at word boundaries keep stream packets CRC-clean and leave the host at the pin's level.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). VsStreamPlusTrigger(n_edges=8, seed=12): Perfect APB, CfgToggleSeq(use_tpg=1), IoTriggerSeq(n_edges=8, gap=200 tx_clk cycles) -> 4 rises + 4 falls. Host auto-acks each device trigger. Timer 10000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_stream, sb_txtrig, sb_linkpro (short_split), sb_ioack not exercised (no host triggers)
- no test-specific checks

**Fail criteria.**

- sb_stream/lost_frame (golden frame never reassembled off the wire), sb_stream/unmatched_frame
- sb_stream/pixel_data, pixel_count, header_field, crc, dsizep_mismatch, pkt_short, tag_continuity, framing, line_marker, streamid, dsizel, pkt_over_spsm
- sb_linkpro/framing, kcode, packet_type, short_split
- sb_txtrig/final_level, sequence, rise_count, fall_count, before_ack
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-DATA-001`: packet format checked only on the packets that reach the wire; Mono8, one DsizeP per run
- `CXP-CAM-IMG-011`: bit-exact on Mono8 frames only; one pixel source and geometry set per run

_Last result: **pass**, seed 64585, 110.1 µs simulated._

#### VP-CON-03 — `test_xifc_full`

**Title:** Stream, ctrl, host and device triggers · **Source:** `src/verif/uvm/tests/all_tests.py` · **Tiers:** nightly, weekly, xifc  
**Plan rows:** `CXP-CAM-DATA-001`, `CXP-CAM-IMG-002`, `CXP-CAM-IMG-011`  
**Command:** `make UVM_TESTNAME=test_xifc_full run`

**Description.** Four interfaces active at once (TPG stream, host control, host triggers, device HS triggers): arbitration, preemption and data integrity hold.

**How to test.** Base BRINGUP: host.link_up() then closed-loop write StreamPacketSizeMax = 4*(PKT_DSIZE_P+8). VsFullConcurrent(seed=14): Perfect APB, CfgToggleSeq(use_tpg=1); concurrently UplinkCtrlRandomSeq(n=4, seed=14), UplinkTriggerRandomSeq(n=2, seed=15) on the packet-lane uplink_seqr (between packets), IoTriggerSeq(n_edges=6, gap=300). Timer 15000 ns. run ends with env.quiesce (stop cfg_run/s_pix_valid, wait >=64 IDLE words + all scoreboards pending_count()==0, timeout QUIESCE_TIMEOUT_NS=400000 ns).

**Pass criteria.**

- judges: sb_stream, sb_control, sb_reg, sb_rxtrig, sb_ioack, sb_txtrig, sb_linkpro
- no test-specific checks

**Fail criteria.**

- sb_stream/lost_frame (golden frame never reassembled off the wire), sb_stream/unmatched_frame
- sb_stream/pixel_data, pixel_count, header_field, crc, dsizep_mismatch, pkt_short, tag_continuity, framing, line_marker, streamid, dsizel, pkt_over_spsm
- sb_linkpro/framing, kcode, packet_type, short_split
- sb_control/ack_missing
- sb_ioack/count, latency
- sb_txtrig/final_level, before_ack
- sb_rxtrig/trig_missing
- final_phase: any scoreboard error kind fails the test (no EXPECT_FAIL tags in this class)

**Known partial coverage.**

- `CXP-CAM-DATA-001`: packet format checked only on the packets that reach the wire; Mono8, one DsizeP per run
- `CXP-CAM-IMG-011`: bit-exact on Mono8 frames only; one pixel source and geometry set per run

_Last result: **pass**, seed 64121, 343.2 µs simulated._

#### VP-CON-04 — `test_ctrl_pipelined_cmds`

**Title:** Pipelined commands without waiting for acks · **Catalogue:** T-11 · **Source:** `src/verif/uvm/tests/spec_tests.py` · **Tiers:** nightly, weekly  
**Spec:** §8.6.1.1, decision D7  
**Command:** `make UVM_TESTNAME=test_ctrl_pipelined_cmds run`

**Description.** Proves that overlapped commands are each answered with their own data in order, a dropped command (D7: one executes, one waits, a third dropped) is never half-answered, and the device stays responsive.

**How to test.** Open loop via uplink driver: 2 reads (Standard, Revision) back to back; burst of 8 reads (Standard, Revision, ControlPacketSizeMax, ConnectionConfigDefault, XmlUrlAddress, DeviceVendorName, Height slot, Width slot); 8 write+read pairs at _user(20..27) with 0xC0DE0000+i; wait 100 us. Then APB waitstate int(1.5*cmd_ns/rx_period) where cmd_ns = 6*40*char_ns/10, 8 open-loop reads _user(40..47), wait 16*cmd_ns; APB perfect; closed-loop read of Standard.

**Pass criteria.**

- judges: sb_control, sb_reg, sb_test
- no_overlap: fewer than 8 non-Wait acks for the 8 overlapped reads (proves overlap actually occurred)
- responsive: closed-loop Standard read acknowledged 0x00
- control scoreboard: ack data against bus and register map, in order

**Fail criteria.**

- sb_test/no_overlap (slave not slow enough)
- sb_control/ack_data, ack_unexpected, ack_parse
- sb_test/responsive
- Any untagged scoreboard error kind fails the test in final_phase (EXPECT_FAIL is empty)
- env.quiesce(timeout_ns=400000, base default) after main_seq: expectations still owed are reported by the owning scoreboard (e.g. sb_control/ack_missing, sb_stream/lost_frame)

_Last result: **pass**, seed 13648, 2412.5 µs simulated._

#### VP-CON-05 — `test_conc_ctrl_under_stream_load`

**Title:** Control traffic under full stream load · **Catalogue:** C-01 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** smoke, ci, nightly, weekly  
**Spec:** §8.6, §8.2.4, Table 13  
**Plan rows:** `CXP-CAM-CTRL-010`  
**Knobs:** `PKT_DSIZE_P=1016`  
**Command:** `make UVM_TESTNAME=test_conc_ctrl_under_stream_load run`

**Description.** Proves closed-loop control commands on both targets (register file and user window) are answered correctly and within a bounded latency while the downlink carries back-to-back 1016-word stream packets, with acknowledgments never nested inside a stream packet.

**How to test.** sensor() source (cfg_use_tpg=0, cfg_run=1). Background slave_stalls('c01_slave'): user APB slave latency re-randomised 0..40 rx cycles every 2-20 us until done. Up to 5 attempts: sensor_frames(10, 'c01_frames_<n>') with sizes drawn from (64x8, 128x16, 37x5) Mono8, concurrently host_traffic(60, 'c01_host_<n>') (30 % reads of STANDARD/REVISION/CONNECTION_CONFIG/0x1000C/DEVICE_VENDOR_NAME, 20 % user-window reads of 4..64 bytes, 30 % user-window writes of 1..8 words, 20 % single-word writes to 0x1000C/0x1003C); stops early once an 0x03 ack is found starting <=2 cycles after a 0x01 stream packet's EOP. Slave profile restored to latency 0 afterwards.

**Pass criteria.**

- judges: sb_stream, sb_linkpro, sb_control, sb_reg, sb_test
- bin ack_after_stream_eop: a control ack (type 0x03) with pkt_log.gap_after_eop(sop) <= 2 whose preceding long packet is a stream packet (0x01)
- bin stall_under_stream: apb_ag.mon.max_wait > 0 and sb_stream.crc_checked > 0
- sb_test 'ack_latency': sb_control.max_latency_ns <= (1016+8+16)*tx_period + 1000 ns + 16*apb max_wait*rx_period (= 11400 ns + slave term at 10 ns clocks)
- sb_test 'overlap_not_reached': both MUST_HIT bins hit

**Fail criteria.**

- sb_test/ack_latency if an ack exceeds the bound
- sb_test/overlap_not_reached if no queued-behind-EOP ack in 5 attempts or no slave stall with stream
- sb_linkpro/framing or packet_type if an ack is nested in a stream packet
- sb_control/ack_data, ack_crc, ack_missing, ack_late; sb_stream/crc, pixel_data, lost_frame
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 35379, 10175.1 µs simulated._

#### VP-CON-06 — `test_conc_clock_ratio_matrix`

**Title:** C-01 traffic over clock ratios · **Catalogue:** C-14 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** nightly, weekly  
**Knobs:** `HOST_PPM=150.0`, `POINTS=[[7, 13, 10], [13, 7, 10]]`  
**Command:** `make UVM_TESTNAME=test_conc_clock_ratio_matrix run`

**Description.** Proves the device with unrelated clocks (p_ASYNC_CLOCKS=1) handles stream, control and trigger traffic cleanly at several app/tx/rx period points, host bit clock 150 ppm off.

**How to test.** HOST_PPM=150. POINTS (app, tx, rx ns) = (7,13,10), (13,7,10). Per point: env.quiesce(300 us, stop_source=False); linkerr allow and linkstate allow_drop for 500 us; clkrst_ag.set_cdc_ratio(a,t,x); do_reset(cycles=20, domains='all'); bringup(); 100 us; sensor_frames(3, 64x8), host_triggers(4) and host_traffic(10) concurrently.

**Pass criteria.**

- judges: sb_stream, sb_linkpro, sb_control, sb_reg, sb_ioack, sb_rxtrig, sb_linkstate, sb_linkerr, sb_test
- sb_test 'pre_retune_quiesce': quiesce drained before each ratio change
- bins point_<a>_<t>_<x> for every point (MUST_HIT property)
- error totals logged per point
- sb_test 'overlap_not_reached'

**Fail criteria.**

- sb_test/pre_retune_quiesce
- any scoreboard error at any point (all strict)
- AssertionError from bringup
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 7012, 1557.5 µs simulated._

#### VP-CON-07 — `test_conc_clock_ratio_matrix_full`

**Title:** Clock ratio matrix, weekly full corners · **Catalogue:** C-14 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** weekly  
**Knobs:** `HOST_PPM=150.0`, `POINTS=[[6, 6, 6], [6, 6, 14], [6, 14, 6], [6, 14, 14], [14, 6, 6], [14, 6, 14], [14, 14, 6], [14, 14, 14], [7, 11, 13], [11, 13, 7], [13, 7, 11], [9, 10, 11]]`  
**Command:** `make UVM_TESTNAME=test_conc_clock_ratio_matrix_full run`

**Description.** Weekly variant of C-14: same traffic over all eight corners of 6/14 ns plus four non-integer ratios.

**How to test.** Inherits test_conc_clock_ratio_matrix.actors. POINTS = all (a,t,x) with each in (6,14) (8 corners) + (7,11,13), (11,13,7), (13,7,11), (9,10,11): 12 points. HOST_PPM=150 inherited.

**Pass criteria.**

- judges: sb_stream, sb_linkpro, sb_control, sb_reg, sb_ioack, sb_rxtrig, sb_linkstate, sb_linkerr, sb_test
- sb_test 'pre_retune_quiesce' per point
- bins point_<a>_<t>_<x> for all 12 points
- sb_test 'overlap_not_reached'

**Fail criteria.**

- sb_test/pre_retune_quiesce
- any scoreboard error at any point
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 9428, 9574.9 µs simulated._

### 8.13 Soak and random

_Long constrained-random runs that close coverage._

#### VP-SOAK-01 — `test_soak_random`

**Title:** Weighted random soak over all actors · **Catalogue:** C-16 · **Source:** `src/verif/uvm/tests/conc_tests.py` · **Tiers:** weekly  
**Plan rows:** `CXP-CAM-PERF-003`  
**Knobs:** `PKT_DSIZE_P=128`, `BUDGET_NS=4000000`  
**Command:** `make UVM_TESTNAME=test_soak_random run`

**Description.** Soaks the device with a weighted random mix of every concurrency actor for a fixed time budget with every scoreboard strict.

**How to test.** 128-word packets, sensor source, rng 'soak'. Until 4,000,000 ns (BUDGET_NS) elapse: choose frames (w4: 1..3 frames in background), host (w4: 2..9 closed-loop host_traffic), htrig (w2: 3 host triggers bg), pin (w2: 2 pin edges, gap 5..20 us, bg), ff (w1: h.reset 0xFF), tm (w1: TestMode on 30 us in an sb_stream flush window), stall (w1: user slave latency 0..59); 1..20 us between actions. Trace tail (last 20) logged; slave profile restored.

**Pass criteria.**

- judges: sb_stream, sb_linkpro, sb_control, sb_reg, sb_ioack, sb_rxtrig, sb_txtrig, sb_linktest, sb_test
- MUST_HIT empty: overlap check trivially passes
- all scoreboards strict

**Fail criteria.**

- any scoreboard error kind (untagged)
- write_ok AssertionError on TestMode writes
- run_phase then env.quiesce(timeout_ns=QUIESCE_TIMEOUT_NS=400000 default); final_phase fails on any untagged scoreboard error kind

_Last result: **pass**, seed 57391, 4419.8 µs simulated._

## 9. Traceability: specification clause → tests

| Clause | Tests |
|---|---|
| §5.1 | VP-CTL-06 |
| §6.7 | VP-LT-01 |
| §6.7.4 | VP-LT-01, VP-LT-02 |
| §8.2.2 | VP-ROB-04 |
| §8.2.2.1 | VP-ROB-01, VP-ROB-03 |
| §8.2.2.2 | VP-ROB-03 |
| §8.2.3 | VP-LNK-02 |
| §8.2.4 | VP-ARB-04, VP-ARB-05, VP-ARB-06, VP-ARB-07, VP-CON-05, VP-TRG-06 |
| §8.2.5 | VP-LNK-03, VP-LNK-04, VP-LNK-05, VP-LNK-06 |
| §8.2.5.1 | VP-ARB-06, VP-LNK-07, VP-LNK-08 |
| §8.3 | VP-TRG-05 |
| §8.3.2 | VP-ARB-06, VP-CON-02, VP-RST-03, VP-TRG-02, VP-TRG-04, VP-TRG-05 |
| §8.3.2.1 | VP-TRG-03 |
| §8.3.3 | VP-ARB-02, VP-ARB-05, VP-IOA-01, VP-TRG-02, VP-TRG-04, VP-TRG-05 |
| §8.4 | VP-LNK-02 |
| §8.5 | VP-STR-12 |
| §8.5.2 | VP-STR-03, VP-STR-08, VP-STR-12, VP-STR-14 |
| §8.5.3 | VP-DSC-01, VP-RST-02, VP-RST-03, VP-STR-03, VP-STR-07, VP-STR-09 |
| §8.6 | VP-CON-05 |
| §8.6.1 | VP-CTL-09 |
| §8.6.1.1 | VP-CON-04, VP-CTL-08, VP-CTL-09 |
| §8.6.1.2 | VP-CTL-10, VP-CTL-11 |
| §8.6.2 | VP-CTL-07 |
| §8.6.3 | VP-CTL-07, VP-CTL-09, VP-ROB-04 |
| §8.6.4 | VP-CTL-07 |
| §8.7 | VP-LT-03, VP-LT-07 |
| §8.7.2 | VP-LT-04 |
| §8.7.3 | VP-LNK-08, VP-LT-04 |
| §8.7.4 | VP-LT-05, VP-LT-06 |
| §9.4 | VP-STR-14 |
| §9.4.1 | VP-STR-11 |
| §9.4.2 | VP-STR-06, VP-STR-10, VP-STR-11 |
| §9.4.3 | VP-STR-12 |
| §9.4.6 | VP-STR-10 |
| §10.1.1 | VP-LNK-07 |
| §10.1.2-10.1.5 | VP-DSC-01 |
| §10.1.5 | VP-STR-08 |
| §10.2 | VP-LNK-07, VP-LNK-09 |
| §10.3.1 | VP-REG-02 |
| §10.3.2 | VP-CTL-07, VP-DSC-02 |
| §10.3.3 | VP-CTL-09, VP-REG-02 |
| §10.3.4-10.3.18 | VP-REG-02 |
| §10.3.8-10.3.11 | VP-DSC-02 |
| §10.3.19 | VP-STR-05 |
| §10.3.19-27 | VP-REG-02 |
| §10.3.26 | VP-STR-06 |
| §10.3.27 | VP-STR-05 |
| §10.3.28 | VP-CTL-06, VP-DSC-01, VP-RST-01, VP-RST-02, VP-RST-03, VP-RST-04, VP-RST-05, VP-TRG-05 |
| §10.3.30 | VP-CTL-06 |
| §10.3.32 | VP-STR-08 |
| §10.3.33 | VP-STR-07 |
| §10.3.35 | VP-LT-03, VP-LT-05, VP-LT-06 |
| §10.3.36-10.3.39 | VP-LT-04 |
| §10.3.38 | VP-LT-03, VP-LT-05 |
| §10.3.40-41 | VP-REG-02 |
| Table 13 | VP-ARB-05, VP-CON-05 |
| Table 15 | VP-ARB-04, VP-ROB-03, VP-TRG-03, VP-TRG-06 |
| Table 17 | VP-IOA-01 |
| Table 18 | VP-LNK-02 |
| Table 19 | VP-STR-06 |
| Table 21 | VP-CTL-07 |
| Table 22 | VP-CTL-07, VP-CTL-08 |
| Table 23 | VP-LT-04, VP-LT-05 |
| Table 25 | VP-STR-11 |
| Table 37 | VP-STR-06, VP-STR-10 |
| Table 38 | VP-STR-10 |
| Table 42 | VP-LNK-07 |
| Table 43 | VP-DSC-01 |
| Table 44 | VP-DSC-01, VP-RST-03, VP-STR-08 |
| Table 45 | VP-REG-02 |
| Figure 20 | VP-TRG-03 |
| Figure 24 | VP-CTL-09 |
| Figure 30 | VP-STR-11 |
| decision D5 | VP-DSC-02, VP-LT-04 |
| decision D4 | VP-STR-08 |
| decision D8 | VP-CTL-08, VP-ROB-03 |
| decision D7 | VP-CON-04 |
| decision D1 | VP-LNK-02 |

Plan rows (validation-plan IDs, `PLAN` attributes) → tests:

| Plan row | Tests (* = partial) |
|---|---|
| `CXP-CAM-BND-003` | VP-REG-01*, VP-STR-08 |
| `CXP-CAM-BOOT-001` | VP-REG-01*, VP-REG-02 |
| `CXP-CAM-CT-001` | VP-LT-03, VP-LT-05, VP-LT-07 |
| `CXP-CAM-CT-002` | VP-LT-05 |
| `CXP-CAM-CT-003` | VP-LT-06 |
| `CXP-CAM-CT-004` | VP-LT-01*, VP-LT-02*, VP-LNK-08, VP-LT-04, VP-LT-07 |
| `CXP-CAM-CT-005` | VP-LT-04 |
| `CXP-CAM-CTRL-001` | VP-CTL-01 |
| `CXP-CAM-CTRL-002` | VP-CTL-02 |
| `CXP-CAM-CTRL-004` | VP-CTL-09 |
| `CXP-CAM-CTRL-006` | VP-CTL-03*, VP-CTL-10, VP-CTL-11 |
| `CXP-CAM-CTRL-007` | VP-CTL-06 |
| `CXP-CAM-CTRL-008` | VP-CTL-07 |
| `CXP-CAM-CTRL-010` | VP-CON-01*, VP-CON-05 |
| `CXP-CAM-DATA-001` | VP-STR-01*, VP-STR-02*, VP-STR-03*, VP-STR-04*, VP-ARB-01*, VP-CON-01*, VP-CON-02*, VP-RST-02*, VP-CON-03*, VP-ARB-02*, VP-ARB-03*, VP-STR-14 |
| `CXP-CAM-DATA-003` | VP-RST-02, VP-STR-07, VP-STR-09 |
| `CXP-CAM-GEN-001` | VP-DSC-02 |
| `CXP-CAM-IMG-001` | VP-STR-10 |
| `CXP-CAM-IMG-002` | VP-STR-01, VP-STR-02, VP-STR-03, VP-ARB-01, VP-STR-05, VP-STR-06, VP-CON-01, VP-CON-02, VP-RST-02, VP-CON-03, VP-ARB-02, VP-ARB-03, VP-STR-11 |
| `CXP-CAM-IMG-004` | VP-STR-10 |
| `CXP-CAM-IMG-007` | VP-STR-04*, VP-STR-14 |
| `CXP-CAM-IMG-011` | VP-STR-01*, VP-STR-02*, VP-STR-03*, VP-STR-04*, VP-ARB-01*, VP-STR-05, VP-STR-06, VP-CON-01*, VP-CON-02*, VP-RST-02*, VP-CON-03*, VP-ARB-02*, VP-ARB-03* |
| `CXP-CAM-IMG-012` | VP-STR-12, VP-STR-13 |
| `CXP-CAM-INIT-002` | VP-RST-01*, VP-RST-03 |
| `CXP-CAM-INIT-004` | VP-DSC-01 |
| `CXP-CAM-NEG-001` | VP-ROB-02, VP-ROB-04 |
| `CXP-CAM-NEG-002` | VP-CTL-08 |
| `CXP-CAM-NEG-006` | VP-CTL-08 |
| `CXP-CAM-NEG-007` | VP-CTL-08 |
| `CXP-CAM-PERF-003` | VP-SOAK-01 |
| `CXP-CAM-PIX-001` | VP-STR-11 |
| `CXP-CAM-PROT-004` | VP-LNK-02 |
| `CXP-CAM-PROT-005` | VP-TRG-06 |
| `CXP-CAM-PROT-006` | VP-ROB-01*, VP-ROB-03 |
| `CXP-CAM-REC-001` | VP-LNK-07, VP-LNK-09 |
| `CXP-CAM-REC-002` | VP-CTL-05*, VP-RST-04 |
| `CXP-CAM-REC-004` | VP-RST-05 |
| `CXP-CAM-REC-007` | VP-LNK-03, VP-LNK-04, VP-LNK-05, VP-LNK-06 |
| `CXP-CAM-TRIG-001` | VP-IOA-01, VP-TRG-05 |
| `CXP-CAM-TRIG-002` | VP-TRG-03 |
| `CXP-CAM-TRIG-004` | VP-TRG-02*, VP-TRG-04, VP-ARB-06 |
| `CXP-CAM-TRIG-005` | VP-TRG-05 |
| `CXP-CAM-TRIG-006` | VP-IOA-01*, VP-ARB-05 |
| `CXP-CAM-TRIG-007` | VP-TRG-01*, VP-ARB-07 |

## 10. Known gaps and open items

These findings came out of the review of the test and environment sources that produced this plan. Each one is an item to close; none of them fails a test today.

### 10.1 Checks that cannot fail

| Where | Gap | Action |
|---|---|---|
| `test_soak_random` | `MUST_HIT` is empty, so the soak can never fail on coverage. | List the cells the soak must reach. |
| C-08, C-09, C-12 | Some required coverage bins are set unconditionally. | Set each bin only when its event is observed. |
| several spec / conc tests | The Boolean returned by `wait_until` / `frames_after` is not asserted, so a timeout goes unnoticed. | Wrap each call in `env.sb_test.check(...)`. |
| `test_uplink_ppm_jitter_os4`, `_os4_neg` | No `REQUIRES = {"OS_RATIO": 4}`. On a default build they run at OS 16 instead of failing at once. | Add `REQUIRES`. |
| `test_ctrl_pipelined_cmds`, both clock-ratio matrix tests | `PLAN` is empty, so they trace to no plan row. | Assign plan rows. |

### 10.2 Docstrings that disagree with the code

| Test | Docstring says | Code does |
|---|---|---|
| `test_linktest_clean` | TestErrorCount stays 0 | expects 3840: the 64-word bodies are shorter than the 1024-word Table 23 payload |
| `test_linktest_inject` | 9 errors | expects 3849 |
| C-11 | 120 images | sends 60 |
| C-03 | 2-word latency spread | allows 3 |
| `test_tx_linktest_mode` | reads TestPacketCountTx | never reads it |
| `test_ctrl_reset_storm` | ConnectionReset storm | sends 0xFF control-channel resets only |
| C-10 | check message | states the opposite of the condition it tests |

### 10.3 Environment items

- `EXPECT_FAIL` is empty in every test, although the `all_tests.py` docstring still says eleven tests carry tags. A known RTL defect therefore shows up as a plain failure.
- `clkrst_ag.ap` has no subscriber. `sb_reg.sideband_xp` is drained and discarded.
- `sb_pix_restart_pulse` and `sb_pix_stray_eof_pulse` are published but not checked, and `bs_width`, `bs_height` and `usr_wait` are never read. Sensor framing errors are unverified at this level.
- The shell input `cfg_dsizeP` is not connected to the DUT. Packet size is checked against the StreamPacketSizeMax history.
- `TpgCfgSeq` is unused. The RAL (`CxpRegBlock`) is only written, by `test_ral_sweep`, and some of its reset values are stale (SPSM 0x100 vs a power-up value of 0).
- In `ControlScoreboard`, `extension_link_mode`, `strict_ack_match` and the `nack_expected` / `nack_observed` counters are never compared.
- `IoAckScoreboard` measures D6 latency to the acknowledgment's code word. `decisions.py` defines it to the leader.
- `StreamDumpSubscriber` labels DsizeL as 16-bit, while the scoreboard decodes 24 bits.
- The Makefile text says `make COVERAGE=1 nightly`, but the Verilator line-coverage knob is `RTL_COV=1`. `cov_report` merges `cov_*.xml` files that nothing produces.
- The `PLAN` rows cite the validation plan documents under `docs/verification/validation/`, which are deleted in the current working tree.

### 10.4 Last archived run

The archived results in `src/verif/00_test_results/` come from one weekly-style run with random seeds, made from the old `verif/` path before the move to `src/`. 75 of 76 tests passed. `test_conc_trigger_vs_ioack` failed with a testbench error, `ValueError: Timer argument time must be positive`, at seed 30176, not with a scoreboard error. To reproduce: `make UVM_TESTNAME=test_conc_trigger_vs_ioack CXP_SEED=30176 run`. Rerun the nightly tier from `src/verif` to refresh these results.

