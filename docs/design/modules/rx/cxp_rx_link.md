# cxp_rx_link

Inputs chosen from the tree: RTL `src/rtl/rx/cxp_rx_link.sv` and its children (`cxp_rx_lspd_sampler`, `cxp_rx_8b10b_decoder`, `cxp_rx_link_mon`, `cxp_rx_packet_parser`, `cxp_rx_trigger_lspd`, `cxp_rx_linktest`); parent `src/rtl/top/cxp_interface_top.sv`; bound SVA `src/sva/cxp_sva.sv`; unit TB `src/tb_unit/rx/cxp_rx_link/`; integration TBs `src/tb_unit/top/cxp_interface_top/`, `src/tb_unit/top/cxp_device_top/`; host model `src/verif/common/cxp_host.py`; spec JIIA CXP-001-2015 v1.1.1 (`docs/spec/CXP-001-2015.pdf`) §8.2, §8.2.4, §8.3.2.1, §8.3.3 (Table 17), §8.7.3; regression `make -C src/tb_unit`; output `docs/design/modules/rx/cxp_rx_link.md`.

Device-side receive wrapper: turns the ~20.83 Mbps low-speed uplink serial line into demuxed trigger, I/O-acknowledgment and connection-test channels plus the stream of long-packet words, `long_o`, that `cxp_ctrl_plane` turns into register accesses.

| Channel | Wire format consumed (§8.2.3) | Output of this wrapper |
|---|---|---|
| IDLE | K28.5 K28.1 K28.1 D21.5 (Table 14) | discarded; drives lock / alignment |
| Trigger | Table 15: K28.2 K28.4 K28.4 / K28.4 K28.2 K28.2 leader (2 of 3), 3×Delay (2 of 3), at any character boundary (§8.3.2.1), taken out by the sampler; only while the link is detected and on the Master connection (`trig_enable_i`) | `trigger_out_app_o` Delay × 1/24 bit after a packet that changes the host's level (a repeated edge is a §8.3.3 resend), `trig_pkt_rcvd_o` for every accepted packet |
| I/O acknowledgment (the host's answer to a device trigger) | Table 17: 4×K28.6 (≥ 3 lanes), 4×Code, at a word boundary, also between two words of a long packet (§8.2.4, §8.3.3); taken out by the parser | `ioack_rcvd_o` (code 0x01) |
| Connection test | K27.7, 4×0x04, 1024 payload words, CRC, K29.7 (Table 23) | `lt_err_count_o`, `lt_pkt_count_rx_o` |
| Every long packet, including control commands (type 0x02, Table 21) | K27.7, 4×TYPE, body, K29.7 | `long_o` (words from the TYPE word to the K29.7 word, with the voted TYPE) |

Source: `src/rtl/rx/cxp_rx_link.sv`. Instantiated once in `cxp_interface_top` (`cxp_rx_link_i`), next to `cxp_ctrl_plane_i`; fed by the `rx_serial` pin; feeds `cxp_ctrl_plane` (`long_o`), `cxp_tx_io_ack` (`trig_pkt_rcvd_o`, through `cxp_cdc_pulse_trig_rcvd_i` when `p_ASYNC_CLOCKS = 1`), `cxp_tx_trigger_hs` (`ioack_rcvd_o` → `ack_i`, through `cxp_cdc_pulse_ioack_rcvd_i`), the device trigger's link gate (`link_detected_o` → `link_up_i`, through `cxp_cdc_sync_link_i`) and the `sb_*` status pins. Children: `cxp_rx_lspd_sampler`, 4× `cxp_rx_8b10b_decoder` (`g_lane[0..3].cxp_rx_8b10b_decoder_i`) `cxp_rx_link_mon`, `cxp_rx_packet_parser`, `cxp_rx_trigger_lspd`, `cxp_rx_linktest`, each with its own document in this folder. The command parser and the executor (with its APB bridge) that follow this wrapper live in `cxp_ctrl_plane` (`cxp_ctrl_plane.md`).

Spec: §8.2 (transport, IDLE, Table 42 connection state), §8.2.4 (insertion), §8.3.2.1/§8.3.3 (trigger, I/O ack source, the host's I/O acknowledgment of device triggers, Table 17), §8.7.3 (host-to-device connection test), §10.3.37/39 (test counters). The §8.6 control channel is handled by `cxp_ctrl_plane`; this module only delivers its packets.

## Interface

| Parameter | Default | Meaning |
|---|---|---|
| `p_OS_RATIO` | 16 | Oversampling ratio of `rx_clk` over the 20.83 Mbps bit rate; even, ≥ 4. One 40-bit word = 40·`p_OS_RATIO` cycles. |
| `p_SAMP_LOCK_HITS` | `cxp_pkg::RX_LOCK_HITS_DEFAULT` = 2 | K28.5 hits, 10 bits apart, before `rx_lock_o`. |
| `p_LINK_LOCK_IDLES` | `cxp_pkg::RX_LOCK_IDLES_DEFAULT` = 2 | Error-free IDLE words, sampler locked, before `aligned_o` (`cxp_rx_link_mon` `p_LOCK_IDLES`). Was `p_SAMP_LOCK_HITS` until 2026-09-27. |
| `p_RX_LOSS_WORDS` | `cxp_pkg::RX_LOSS_WORDS_DEFAULT` = 20 000 | Words without an IDLE before `cxp_rx_link_mon` declares the link lost; twice the §8.2.5.1 low-speed interval (800 000 bits). |
| `p_RX_SHORT_LOSS_OK` | 0 | Benches only: allow `p_RX_LOSS_WORDS` below 10 000 (otherwise an elaboration `$error`). |

The control-buffer and APB parameters (`p_BUF_DEPTH`, `p_APB_AW`, `p_APB_DW`) moved to `cxp_ctrl_plane`.

| Port | Dir | Width | Description |
|---|---|---|---|
| `rx_clk` | in | 1 | Oversample clock; the only clock. |
| `rx_rst_n` | in | 1 | Active-low reset. |
| `rx_serial_i` | in | 1 | Uplink serial line (raw, synchronised inside the sampler). |
| `rx_lock_o` | out | 1 | Sampler symbol lock (`ST_LOCKED`). |
| `aligned_o` | out | 1 | Link up in `cxp_rx_link_mon` (IDLE-aligned). |
| `link_detected_o` | out | 1 | Table 42 Detected; equals `aligned_o`. |
| `cfg_trig_polarity_i` | in | 1 | 0 = pass rising triggers, 1 = falling. |
| `trig_enable_i` | in | 1 | 1 on the Master connection. 0 (an extension connection) drops every Table 15 packet: no pulse, no `trig_pkt_rcvd_o` (§8.3: the I/O channel is the Master's). `cxp_interface_top`: `~from_extension_link`. |
| `trig_deassert_i` | in | 1 | ConnectionReset in progress; its rising edge de-asserts the host's trigger level (§8.3.2), which pulses `trigger_out_app_o` at polarity 1 if the host was asserted. `cxp_interface_top`: `conn_reset_active`. |
| `trigger_out_app_o` | out | 1 | Reconstructed trigger pulse. |
| `trigger_glitch_pulse_o` | out | 1 | Trigger packet rejected: no two Delay characters alike, or Delay > 239. |
| `trig_pkt_rcvd_o` | out | 1 | One-cycle strobe per accepted trigger packet, polarity-independent (§8.3.3 ack source); registered in `cxp_rx_trigger_lspd`. |
| `ioack_rcvd_o` | out | 1 | One-cycle strobe per Table 17 I/O acknowledgment from the host with code 0x01 (the parser's `ioack_o`); the device trigger waits for it (§8.3.3). |
| `clr_lt_err_i` / `clr_lt_pkt_i` | in | 1 each | Clear TestErrorCount / TestPacketCountRx. |
| `lt_err_count_o` | out | 32 | TestErrorCount (§10.3.37). |
| `lt_pkt_count_rx_o` | out | 64 | TestPacketCountRx (§10.3.39). |
| `long_o` | out | `cxp_pkg::cxp_rxlong_t` (47 bits) | Long-packet words for `cxp_ctrl_plane`; fields below. |
| `pkt_err_pulse_o` | out | 1 | Parser framing error. |
| `rx_code_err_pulse_o` | out | 1 | Any lane not in the 8B/10B table, per decoded word. |
| `rx_disp_err_pulse_o` | out | 1 | Any lane with a running-disparity error, per decoded word. |

`long_o` fields (`cxp_rx_link.sv:149-154`, straight from the parser's `long_*` outputs):

| Field | Width | Meaning |
|---|---|---|
| `data` | 32 | Packet word `{P3,P2,P1,P0}`. The TYPE word (with `sop`), every body word, then the K29.7 word (with `eop`). |
| `kmask` | 4 | Per-lane K flag of that word. |
| `valid` | 1 | Word valid; one per decoded word inside a packet, never during IDLE, an I/O acknowledgment or the K27.7 word (a trigger never reaches the words). |
| `sop` | 1 | Set on the TYPE word. |
| `eop` | 1 | Set on the K29.7 word. |
| `ptype` | 8 | 3-of-4 voted TYPE byte; on the `sop` word it is the fresh vote, afterwards the latched value. |

The comments on `cxp_rxlong_t` in `cxp_pkg.sv` describe `data` as "packet word after the TYPE word" and `sop` as "first body word"; both are off by one word (Minor).

Notes:

- Reset is asserted asynchronously (`always_ff … or negedge rx_rst_n` here and in every child); the port comment "Active-low sync reset" is wrong about assertion. Deassertion is not synchronised inside the module.
- Single clock domain: every child runs on `rx_clk`, and so does `cxp_ctrl_plane` beside it. No CDC inside. In `cxp_interface_top`, `trig_pkt_rcvd_o` goes to `cxp_tx_io_ack`, `ioack_rcvd_o` to `cxp_tx_trigger_hs.ack_i` and `link_detected_o` (as `sb_link_detected`) to `cxp_tx_trigger_hs.link_up_i`, all on `tx_clk`: with `p_ASYNC_CLOCKS = 0` as wires (the clocks must then be one clock or phase-locked), with `p_ASYNC_CLOCKS = 1` through `cxp_cdc_pulse_trig_rcvd_i`, `cxp_cdc_pulse_ioack_rcvd_i` (pulses) and `cxp_cdc_sync_link_i` (level). `clr_lt_err_i` / `clr_lt_pkt_i` are driven on `rx_clk` straight from `cxp_interface_top`'s `clr_lt_err` / `clr_lt_pkt_rx` (the register file's pulses; a ConnectionReset pulses both).
- Output registration: `rx_code_err_pulse_o`, `rx_disp_err_pulse_o` are AND/OR of registers. `trig_pkt_rcvd_o` is a register of `cxp_rx_trigger_lspd`; `ioack_rcvd_o`, `pkt_err_pulse_o`, `long_o` and `lt_*` are combinational decodes of child registers (parser outputs are combinational from `d_reg_q`). `aligned_o`, `rx_lock_o` are state decodes.
- No back-pressure anywhere: the datapath is valid-only. A word arrives every 40·`p_OS_RATIO` cycles, which bounds every child's per-word work and that of `cxp_ctrl_plane`.
- `cfg_trig_polarity_i`, `clr_lt_err_i`, `clr_lt_pkt_i` are sampled as `rx_clk`-synchronous levels.

## How it works

The wrapper has no FSM of its own; it owns the decoder output register, the running-disparity chain, the gating between stages and the demux glue.

1. **Serial to word.** `cxp_rx_lspd_sampler` recovers bits, hunts K28.5, and emits one 40-bit word with K28.5 already in P0 plus a one-cycle `sym_valid`; it only emits while in `ST_LOCKED`, and drops back to hunt only when `cxp_rx_link_mon` pulses `resync`. There is no lane aligner: the sampler frames every word on the comma, so the lane offset is always 0.
2. **Decode and register.** Four combinational `cxp_rx_8b10b_decoder` instances (generate loop `g_lane`; lane i decodes `sym_in[10*i +: 10]`) share one running-disparity chain: `rd_chain[0]`, lane i's `rd_out` feeds lane i+1, `rd_chain[4]` is registered into `rd_q`. Until the chain is seeded after lock (`rd_known_q`), `rd_chain[0]` is taken from the K28.5 form in P0 (RD+ form → 1), so the host's disparity is followed from the first word. A word with a code or disparity error on any lane is marked (`word_err`) and reaches the parser with it; the parser forwards a marked body word with `long_o.err` set, `cxp_ctrl_cmd_parser` fails it like a CRC error (0x80) and `cxp_rx_linktest` counts it.
2a. **Link monitor.** `cxp_rx_link_mon` sits after the decoder register. With the sampler locked, 2 error-free Table 14 IDLE words bring the link up (`aligned_o`; `link_detected_o` = sampler lock AND up). While up it forwards words to the parser and counts words since the last IDLE; after `p_RX_LOSS_WORDS` (default 20 000, twice the §8.2.5.1 interval) it pulses `resync` to the sampler and `flush` to the parser and goes down. It also judges the framing in both states: locked and down, 32 words since the last clean IDLE that are not one; up, 32 errored words since the last clean IDLE, or one clean word with K28.5 outside P0 (Table 14) — each pulses `resync` and `flush`. Sampler lock loss also takes the link down and flushes the parser.
3. **Trigger.** The sampler takes the six Table 15 characters out of the character stream (leader voted 2 of 3) and marks the lane of the next character when they changed the running disparity (`sym_rd_flip`); that lane's decoder starts from the chain's RD inverted. Its strobe, with the edge and the three raw Delay characters, goes to `cxp_rx_trigger_lspd` (`p_OS_RATIO` passed down) only while `link_detected_o` is 1 (§10.1.1: before that the framing is unconfirmed) and `trig_enable_i` is 1; that module votes the Delay, rejects (`trigger_glitch_pulse_o`) or accepts (`trig_pkt_rcvd_o`, the I/O-ack request, whatever the polarity), follows the host's level and, for a level change of the selected edge, pulses `trigger_out_app_o` Delay × `p_OS_RATIO` / 24 cycles later (Figure 20). `trig_deassert_i` clears the level (`cxp_rx_trigger_lspd.md`).
4. **Demux.** `cxp_rx_packet_parser` classifies each registered word the same cycle `d_valid_q` is high. Long-packet words go to `cxp_rx_linktest`, gated here by `lt_gate = long_valid & type == 0x04`, and, ungated, to `long_o`. A Table 17 I/O acknowledgment is taken out by the parser in any state, also between two words of a command or test packet, which then continue as if it were absent; its code word with a voted 0x01 is `ioack_rcvd_o` (the parser's `ioack_o`, wired straight through).
5. **Long-packet export.** `long_o` packs the parser's `long_*` outputs into one `cxp_rxlong_t` (lines 149-154). Every long packet of every type leaves on it; `cxp_ctrl_plane` opens its own gate on type 0x02 and executes the command (`cxp_ctrl_plane.md`). Other types are consumed by nobody.

Latency (cycles of `rx_clk`, `W` = 40·`p_OS_RATIO` cycles per word):

| Path | Latency |
|---|---|
| `rx_lock_o` ↑ → `aligned_o` ↑ | 2 IDLE words (`p_LINK_LOCK_IDLES`) |
| `aligned_o` ↑ → first `d_valid_q` | next `sym_valid` + 1 |
| `sym_valid` → `d_valid_q` | 1 |
| `d_valid_q` → `ioack_rcvd_o`, `pkt_err_pulse_o`, `long_o` | 0 (combinational, after the link monitor forwards the word) |
| last Delay character → `trig_pkt_rcvd_o` | 2 cycles (sampler register, trigger register) |
| K29.7 word in `d_reg_q` → `rsp_valid_o` of `cxp_ctrl_plane` (read through the bench bridge, one APB wait state) | 9 [by code reading] |
| Throughput | 1 word per `W`; no stage stalls |

Same-cycle rules: `rx_code_err_pulse_o`/`rx_disp_err_pulse_o` coincide with the `d_valid_q` of the offending word; a word with `code_err` is still delivered to the parser as data 0x00 with `k = 0` (decoder contract), so a corrupted K29.7 becomes a body word on `long_o`.

Link-loss behaviour: when the sampler drops lock, or `cxp_rx_link_mon` sees no IDLE for `p_RX_LOSS_WORDS` words, `aligned_o`/`link_detected_o` fall and the parser is flushed: a packet in flight ends with an abort word (`long_o.eop` and `long_o.err`), which `cxp_ctrl_plane` acks 0x47 and `cxp_rx_linktest` closes. The parser also aborts on an SOP or IDLE inside a body (lost trailer).

## Arbiter integration

Not applicable: the receive path has no arbiter and no `ready` signals. Its outputs become transmit sources only in `cxp_interface_top` (`trig_pkt_rcvd_o` → `cxp_tx_io_ack` → `cxp_tx_inserter`; `long_o` → `cxp_ctrl_plane` → `rsp_*` → `cxp_tx_ctrl_ack` → `cxp_tx_arbiter`); see those documents for ack timing against the §8.3.3 and §8.6.1.1 timeouts. `ioack_rcvd_o` and `link_detected_o` pace and gate the device trigger (`cxp_tx_trigger_hs.md`). On the receive side the parser undoes the host's §8.2.4 insertion of I/O acknowledgments; the sampler undoes that of Table 15 triggers.

## Verification

Verilator 5.046 + cocotb 2.0.1, built with `--assert`. No SVA is bound to this module itself; its parser carries `cxp_rxlong_sva` (every SOP closed by one EOP before the next) and the control plane beside it `cxp_ctrl_exec_sva`. No FSM coverage is collected for this TB (children collect their own in their unit TBs).

### Unit TB — `src/tb_unit/rx/cxp_rx_link/`

Wrapper `tb_cxp_rx_link_top.sv` instantiates the real `cxp_rx_link` and, beside it, the real `cxp_ctrl_plane` fed from `long_o`, with `rbuf_clk = rx_clk`; the wrapper's ports are the old combined `cxp_rx_link` port list, so the tests did not change when the control plane moved out. Parameters: `OS_RATIO = 8`, `SAMP_LOCK_HITS = 2`, `BUF_DEPTH = 16`, `APB_AW = 32`, the link-loss window at its RTL default (20 000 words); `ioack_rcvd` is exposed but no test observes it; the plane's `p_CLK_KHZ` = 50, its user window off, its register port bridged by a bench `cxp_ctrl_apb_bridge` (abort tied 0) to the Python APB slave, and its `nack_*` decoded back into the old `cmd_crc_err_pulse` / `cmd_logical_err_*` / `router_reject_*` outputs; the read buffer is read in the bank of the last response; ports re-exposed without suffixes. The Makefile takes the RTL from `src/rtl/cxp_ip.f` through `common/cocotb_sim.mk`. Clock 10 ns; reset held 5 cycles with `rx_serial = 0`, all inputs 0; `from_extension_link` is never driven (Verilator default 0). Stimulus is bit-serial: `drive_bits` holds each bit for 8 cycles, words are produced by the shared `cxp_8b10b.encode_word` with running disparity chained across helpers; one word = 320 cycles. No shared checkers; each test polls outputs after `RisingEdge`.

| Test | Stimulus | Expect |
|---|---|---|
| `test_01_reset` | reset only | `rx_lock`, `aligned`, `link_detected` = 0 |
| `test_02_link_lock` | 30 IDLE words | `rx_lock` within 4000 cycles, then `link_detected` within 2000 |
| `test_04_trigger` | 20 IDLE, rising Table 15 trigger (Delay 2) inside an IDLE word, 30 IDLE | `trigger_out_app` fires |
| `test_05_ctrl_read` | 20 IDLE, read op 0x00 addr 0x42 size 4, 20 IDLE; APB slave returns 0xDEADBEEF | `rsp_code` = 0x00, `rbuf[0]` = 0xDEADBEEF |
| `test_06_ctrl_reset_op` | 20 IDLE, op 0xFF size 0, 20 IDLE | `ctrl_reset_pulse` seen, `rsp_code` = 0x03 |
| `test_07_trig_pkt_rcvd_for_ioack` | polarity 1, 20 IDLE, rising Table 15 trigger, 40 IDLE | exactly one `trig_pkt_rcvd`, no `trigger_out_app` |
| `test_08_back_to_back_test_packets` | three 1027-word Table 23 packets, one IDLE between them, word 1023 of the third corrupted | lock and link never drop; `lt_pkt_count_rx` = 3, `lt_err_count` = 1 |
| `test_09_lost_trailer` | a write whose K29.7 is replaced by an IDLE, then a read | write acked 0x47 and not executed; read acked 0x00 with 0xDEADBEEF |
| `test_10_rd_seed_and_code_error` | IDLE from RD+, then a read with one invalid 10b symbol | no disparity error before the read; `rx_code_err_pulse`; read acked 0x80, no APB access |
| `test_11_wait_then_timeout` | a read whose APB slave never answers | exactly 0x04 within 200 ms, then 0x40 |
| `test_12_hung_then_reset` | a hung read, then 0xFF | only 0x03 in the first 100 ms; `ctrl_reset_pulse` |
| `test_13_ls_trigger_char_form` | Table 15 triggers at character phases 0–3, in IDLE and inside a Table 21 read | 8 `trig_pkt_rcvd`, 4 `trigger_out_app` pulses, 4 reads acked 0x00, no decode error, no lock drop; without the RD flip every read is 0x80 |
| `test_14_glitch_before_link_up` | one bit lost between sampler lock and link-up, at each of the four character phases | link up within 200 words each time |
| `test_15_slip_while_locked` | one extra character after link-up, then a read | read answered within 100 words |
| `test_16_delay_absolute_time` | Table 15 triggers with Delay 0, 80, 160, 239; then Delay 120 at character phases 0–3 for every pre-roll 0 .. OS_RATIO − 1 cycles, each followed by a falling one | latency to `trigger_out_app` = constant + Delay × 1/24 bit within one cycle; the same over every phase within one cycle (before: spread 319 cycles over Delay) |
| `test_17_leader_bit_error` | Table 15 triggers with one leader character hit by a bit error, rising in IDLE and falling inside a read, all phases | 8 acked, 4 fired, reads 0x00 (before: no response at all) |
| `test_18_trigger_while_down` | Table 15 triggers in the IDLE words around lock, before link-up; one after | none fired or acked while down; one after (before: 2 while down) |
| `test_19_delay_vote` | one Delay character hit (a falling packet before it); three different; Delay 240 | fired at the clean latency; glitch, no ack, twice (before: acked and fired) |
| `test_20_back_to_back_triggers` | a rising and a falling trigger adjacent inside a read, all phases | 8 acked, 4 fired, reads 0x00, no decode error |

#### test_01_reset

- *Stimulus*: reset sequence only; `rx_serial` = 0 throughout.
- *Checks*: `rx_lock` = 0, `aligned` = 0, `link_detected` = 0 one cycle after reset release.
- *Proves*: reset values of the sampler and link-monitor state. No stimulus, so nothing about the RD register or the error pulses.

#### test_02_link_lock

- *Stimulus*: 30 IDLE words encoded from RD− (1200 bits, 9600 cycles).
- *Checks*: `rx_lock` rises within 4000 cycles (observed at 720); `link_detected` rises within a further 2000 cycles (observed 561 later).
- *Proves*: sampler `ST_HUNT → ST_PRELOCK → ST_LOCKED`, the link monitor up after 2 IDLE words, and that `link_detected_o` follows it. It never samples `rx_code_err_pulse` / `rx_disp_err_pulse`, so the RD-seed issue is invisible; with an RD+ stream one `rx_disp_err_pulse` fires on the first decoded word (observed, not in repo).

```wavedrom
{"signal":[
 {"name":"rx_clk","wave":"p|.|.|.|."},
 {"name":"rx_serial (word)","wave":"=|=|=|=|=","data":["IDLE0","IDLE1","IDLE2","IDLE3","IDLE4"]},
 {"name":"rx_lock","wave":"0|1|.|.|."},
 {"name":"sym_valid","wave":"0|.|1|1|1"},
 {"name":"aligned / link_detected","wave":"0|.|.|1|.","node":"......a"},
 {"name":"accept_sym","wave":"0|.|.|.|1"},
 {"name":"d_valid_q","wave":"0|.|.|.|1"}
],"edge":["a wait_for(link_detected)"],"config":{"hscale":1}}
```

#### test_04_trigger

- *Stimulus*: 20 IDLE, a rising Table 15 trigger (Delay 2) after the K28.5 of the next IDLE word, 30 IDLE; `cfg_trig_polarity` = 0.
- *Checks*: `trigger_out_app` seen within 20000 cycles.
- *Proves*: the sampler's extraction and the hookup into `cxp_rx_trigger_lspd`. Does not check the delay value, the pulse width or `trigger_glitch_pulse` (test_16, test_19).

#### test_05_ctrl_read

- *Stimulus*: 20 IDLE, Table 21 control packet SOP / TYPE 0x02 / Cmd 0x00 + Size 4 / Addr 0x42 / CRC / EOP, 20 IDLE. A Python APB slave asserts `pready` with 0xDEADBEEF one cycle after seeing `psel & penable`, then drops it.
- *Checks*: first `rsp_valid` within 40000 cycles has `rsp_code` = 0x00; then `rbuf_addr` = 0 and `rbuf_data` = 0xDEADBEEF sampled in ReadOnly.
- *Proves*: the whole chain `d_reg_q → parser ST_LONG_TYPE/BODY → long_o → ctrl_cmd record → executor slot, ST_REQ → bench APB SETUP/ACCESS → ST_WAIT → response register → rsp_*` and the read-buffer port, i.e. that `long_o` carries everything `cxp_ctrl_plane` needs. Note the address 0x42 is not dword-aligned and nothing rejects it. The slave's one-wait-state timing is the only APB timing exercised; `rsp_size` is not checked.

```wavedrom
{"signal":[
 {"name":"rx_clk","wave":"p........"},
 {"name":"d_valid_q / long_o.valid","wave":"10......."},
 {"name":"d_reg_q","wave":"=x.......","data":["4xK29.7"]},
 {"name":"ctrl_cmd state","wave":"==........","data":["EOP","IDLE"]},
 {"name":"cmd_valid (record, err 0)","wave":"010......."},
 {"name":"executor state_q","wave":"=..==...=.","data":["IDLE","REQ","WAIT","IDLE"]},
 {"name":"reg_req","wave":"0..10....."},
 {"name":"psel","wave":"0...1..0.."},
 {"name":"penable","wave":"0....1.0.."},
 {"name":"pready","wave":"0.....10.."},
 {"name":"rsp_valid","wave":"0........1","node":".........a"},
 {"name":"rsp_code","wave":"x........=","data":["0x00"]}
],"edge":["a check"],"head":{"text":"cycle 0 = K29.7; read from the code, not a trace"},"config":{"hscale":1}}
```

#### test_06_ctrl_reset_op

- *Stimulus*: 20 IDLE, control packet with op 0xFF, size 0, addr 0, 20 IDLE; no APB slave (`pready` stays 0).
- *Checks*: `ctrl_reset_pulse` seen at least once before `rsp_valid`; `rsp_code` = 0x03.
- *Proves*: `long_o → ctrl_cmd → executor` reset branch with no bus access, and the `ctrl_reset_pulse_o` export of `cxp_ctrl_plane`. `from_extension_link` is 0, so the §5.1 / §10.3.29 extension-link checks are not exercised here (see `cxp_ctrl_cmd_parser.md` test_18 and `cxp_ctrl_plane.md` test_04).

#### test_07_trig_pkt_rcvd_for_ioack

- *Stimulus*: `cfg_trig_polarity` = 1 (falling), 20 IDLE, rising Table 15 trigger (Delay 2), 40 IDLE.
- *Checks*: `trig_pkt_rcvd` counted until first seen, must equal 1; then 400 further cycles with `trig_pkt_rcvd` = 0 and `trigger_out_app` never 1.
- *Proves*: `trig_pkt_rcvd_o` (the trigger receiver's `trig_ok_o`) is independent of the polarity filter (§8.3.3 "shall be acknowledged"), and that it is a single-cycle strobe. A rejected packet suppressing it is test_19.


### Integration TB — `src/tb_unit/top/cxp_interface_top/`

20 tests on the real `cxp_interface_top` with the real `cxp_rx_link` inside (`p_OS_RATIO` = 16) and a real `cxp_ctrl_bootstrap_regs` APB slave; all clocks tied together (`p_ASYNC_CLOCKS = 0`); `from_extension_link` tied 0. The device trigger tests start with `uplink()`: a `cxp_host.Host` drives `rx_serial` with IDLE until `link_detected` rises (the device sends triggers only to a detected host) and, except in test 19, answers each device trigger packet with a Table 17 acknowledgment. So these tests exercise lock, alignment, decode, `link_detected_o` and `ioack_rcvd_o` of this module; the other tests hold `rx_serial` at 1 and never bring the link up.

| Test | Through this module |
|---|---|
| `test_08_trigger_rising_edge`, `test_09_trigger_edge_pair`, `test_10_trigger_preempts_stream`, `test_11_trigger_in_testmode`, `test_15_link_reset_clears_trigger_output` | link up; the host's I/O acknowledgments arrive as `ioack_rcvd_o` (the 64-cycle timeout usually paces first at this oversampling) |
| `test_19_trig_phase_sweep_100` | link up; the host drops every acknowledgment |
| `test_20_trigger_held_across_reset` | link up, lost across a device reset and brought up again by the host |

None samples `trig_out`, the `sb_*` error pins or `ioack_rcvd_o` directly; the effect of `ioack_rcvd_o` is seen only as trigger pacing.

### Integration TB — `src/tb_unit/top/cxp_device_top/`

32 tests on `cxp_device_top` (interface top plus register file) built with `p_ASYNC_CLOCKS = 1`, `rx_clk` 10 ns, `tx_clk` 8 ns, `app_clk` 12 ns, `OS_RATIO = 4`. The host model `common/cxp_host.py` drives `rx_serial` bit-serially with the golden `cxp_protocol` codecs in the device's wire format (`cxp_protocol.DEVICE`; commands are Table 21, host triggers Table 15), waits for `sb_link_detected` in every `setup`, decodes the downlink, and answers device triggers with Table 17 acknowledgments inserted at the next uplink word boundary (possibly inside a command). So every test exercises lock, alignment and decode in this module, and the control tests exercise `long_o` into `cxp_ctrl_plane` with unrelated clocks. The entries most specific to this module:

| Test | Through this module |
|---|---|
| `test_02_discovery`, `test_03_registers` | control reads and single-dword writes → `long_o` → correct data |
| `test_07_host_trigger` | one Table 15 trigger → `trig_out` and exactly one I/O ack across `cxp_cdc_pulse_trig_rcvd_i` |
| `test_33_trigger_in_linktest_packet` | Table 15 triggers inside host test packets at words 1, 512, 1023, phases 1–3, TestMode 0 and 1: no test error, packets counted, 9 fired and acked per mode |
| `test_34_lock_loss_mid_cmd` | the line held low for 200 words in the middle of a write, back 3 bits off: the write is never executed, the next read answers 0x00 |
| `test_11_single_domain_reset` | an `rx_rst_n`-only reset after an acked read and an I/O-acked trigger: nothing on the downlink, the link comes back |
| `test_12_idle_cadence_per_packet_type` | 340 device triggers, each released by an I/O acknowledgment through `ioack_rcvd_o` and `cxp_cdc_pulse_ioack_rcvd_i` |
| `test_27_ioack_latency_under_stream` | 200 host triggers → 200 `trig_pkt_rcvd_o` strobes → 200 I/O acks |
| `test_29_ioack_in_testmode` | host triggers during TestMode, each acked |
| `test_30_tx_trigger_ack_rules` | host that acknowledges, drops, or acknowledges late: device trigger pacing follows `ioack_rcvd_o` or the timeout |
| `test_31_nested_preempt` | host trigger and device trigger close together: both acknowledged |
| `test_32_trigger_waits_for_link` | no device trigger before `link_detected_o`; one after the host brings the link up |

No device-level test samples `rx_*_err_pulse`, `pkt_err_pulse` or the `lt_*` counters, and none asserts that an I/O acknowledgment landed inside a command.

### Other

- `src/verif/` (PyUVM): `host_uplink_agent.py` serialises host packets onto `rx_serial` of `cxp_interface_top` and answers device triggers with Table 17; `test_ctrl_cmd_read`, `test_ctrl_cmd_write`, `test_crc_error` exercise this module end to end (see `cxp_ctrl_cmd_parser.md`). `src/verif/uvm/tests/all_tests.py` cites `cxp_rx_link.sv:321` for the link-test gate; it is now line 207. Not run here.
- `src/emu/bridge/tb/cxp_hw_env.sv` instantiates `cxp_device_top` (single clock, `from_extension_link = 0`); not run here.
- Child unit TBs: `src/tb_unit/rx/cxp_rx_lspd_sampler`, `cxp_rx_link_mon`, `cxp_rx_8b10b_decoder`, `cxp_rx_packet_parser` (test_15: I/O-ack extraction), `cxp_rx_trigger_lspd`, `cxp_rx_linktest`. The control chain's unit TBs are listed in `cxp_ctrl_plane.md`.

### Running

```
make -C src/tb_unit/rx/cxp_rx_link WAVES=0                           # all 19
make -C src/tb_unit/rx/cxp_rx_link WAVES=0 COCOTB_TEST_FILTER=test_05_ctrl_read   # one test
make -C src/tb_unit/top/cxp_device_top                               # async-clock integration
make -C src/tb_unit                                              # regression entry point
```

Tip: `make clean` after switching `WAVES`, otherwise the stale `sim_build` is reused.

Results 2026-09-26: unit TB 19/19 pass, none tagged.

### Not covered in-tree

- Loss of sampler lock after alignment, and a link drop inside a packet: by code the link monitor takes the link down and flushes the parser (abort word, 0x47); no test at this level. A lost bit before link-up and a slip while locked recover (test_14, test_15); a line drop mid-write is `cxp_device_top` test_34.
- `ioack_rcvd_o` at this level: no test sends a Table 17 packet into this bench; it is covered by the parser's unit test_15 and, as trigger pacing, by `cxp_interface_top` and `cxp_device_top`.
- Reset during activity and input active at reset release: `rx_serial` is 0 during every reset; a stream already running at release is never tried.
- First decoded word with the line at RD+: no disparity error (test_10).
- Any word with `code_err` or `disp_err` inside a packet; the three `rx_*_err_pulse_o` and `pkt_err_pulse_o` outputs are never sampled by any test above unit level.
- Connection-test packets on the uplink; `lt_err_count_o` / `lt_pkt_count_rx_o` checked by test_08; the clears are not driven here. Uplink packets longer than 12 words (the 13-word single-dword writes) run only in `cxp_device_top`.
- Long packets of types other than 0x02 and 0x04 on `long_o` (by code: delivered, consumed by nobody).
- Control errors on the wire: bad address (0x40), extension-link write (0x43), oversize, CRC mismatch (0x80), PSLVERR (see `cxp_ctrl_plane.md`).
- Back-to-back packets without IDLE between them.
- Counter saturation: `lt_*` counters (see `cxp_rx_linktest.md`), the link monitor's word counter stops at `p_RX_LOSS_WORDS`.
- X-propagation: `from_extension_link` undriven in the unit TB.

## Known issues and recommendations

### Critical

None.

### Medium

1. **Few receive-side error paths are tested**: `pkt_err_pulse_o`, `rx_code_err_pulse_o`, `rx_disp_err_pulse_o`. test_08 covers connection-test packets and the `lt_*` counters, test_10 one code error; still missing: a flipped bit inside an IDLE (`rx_code_err_pulse` once) and a framing error (`pkt_err_pulse`). Also add a Table 17 packet inside a command, asserting one `ioack_rcvd` and the command acked 0x00. The control-side error paths are covered in `cxp_ctrl_cmd_parser` and `cxp_ctrl_plane`; this wrapper still exposes them (decoded from `nack_*`), so they can be added here too. Effort: 1 day.
3. ~~**Unsynchronised crossings at the parent**~~ — resolved: `cxp_interface_top` now has `p_ASYNC_CLOCKS`. At 0 the crossings are wires and the clocks must be one clock (the header states it); at 1, `trig_pkt_rcvd_o` goes through `cxp_cdc_pulse`, the control response through `cxp_cdc_req`, and the read buffer is read on `tx_clk` (`cxp_ctrl_plane.md`). `clr_lt_counter_i` is now driven on `rx_clk`. `cxp_device_top` test 7 runs a trigger across the synchroniser with unrelated clocks; `ioack_rcvd_o` and `link_detected_o` cross the same way (`cxp_cdc_pulse_ioack_rcvd_i`, `cxp_cdc_sync_link_i`, `cxp_device_top` tests 30 and 32).
2. **Resolved: a command arriving while the bus was busy was dropped silently.** The executor now keeps one command waiting and drops only a further one, by design (`cxp_ctrl_bus_master.md`, `cxp_ctrl_plane.md` test_07, test_11).

### Minor

- Port comments "Active-low sync reset" on `rx_rst_n` (and the children's `os_rst_n`/`sys_rst_n`) should read "async assert, sync deassert".
- The header no longer lists any parameter; `p_OS_RATIO` has no comment at its declaration.
- `cxp_pkg::cxp_rxlong_t` field comments: `data` is not only "after the TYPE word" and `sop` is on the TYPE word, not the first body word (Interface).
- `test_cxp_rx_link.py`: `from_extension_link` should be driven in `reset()`; `slave.kill()` is deprecated (`cancel()`); `test_05` reads at the unaligned address 0x42.
- `src/verif/uvm/tests/all_tests.py` cites `cxp_rx_link.sv:321` (now line 207).
- Add SVA: `!(ioack_rcvd_o & long_valid_w)`; `d_valid_q |-> $past(accept_sym)`; `long_o.sop |-> long_o.valid`.

### Open questions

1. Designer: on loss of lock, should the whole receive pipeline (parser, `ctrl_cmd`, trigger, `rd_q`) be flushed, and should the trigger receiver also drop a pending delayed pulse? If so, is an `abort` field on `cxp_rxlong_t` the right carrier to `cxp_ctrl_plane`?
2. Verification: should the wire-level error coverage (Medium 1) live in this TB, in `cxp_device_top` with the golden host, or in the PyUVM control scoreboard?
