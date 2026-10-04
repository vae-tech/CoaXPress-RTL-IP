# cxp_pkg

Inputs chosen from the tree: RTL `src/rtl/pkg/cxp_pkg.sv` (a SystemVerilog package, no module) and, for its sibling, `src/rtl/gen/cxp_regmap_pkg.sv` (generated); unit TB: none exists; integration TB `src/tb_unit/top/cxp_interface_top/` (the top that compiles the most consumers); golden Python model `cxp_protocol/` (spec values and the named RTL deviations in `quirks.py`); spec JIIA CXP-001-2015 v1.1.1 (`docs/spec/CXP-001-2015.pdf`) §8.2.1 (Table 11), §8.2.2.2, §8.2.4 (Table 13), §8.2.5 (Table 14), §8.3.2.1 (Tables 15–16), §8.3.3 (Table 17), §8.4 (Table 18), §8.6.2–8.6.3 (Tables 21–22), §8.7 (Table 23), §9.4.1.1 (Table 25), §9.4.6.2, §9.4.7.2–9.4.7.3 (Tables 38, 40, 41); regression `make -C src/tb_unit`; output `docs/design/modules/pkg/cxp_pkg.md`.

Single definition of the CoaXPress on-wire constants (K-character bytes, K28.5 comma symbols, packet TYPE bytes, stream-marker sub-types, control opcodes, acknowledge codes, CRC-32 setup and wire order, GenICam PixelF codes), the transmit port table and scheduling limits, six bus structs and six functions, shared by 29 of the 39 modules in `src/rtl/cxp_ip.f` through `import cxp_pkg::*` or `cxp_pkg::` references.

| Group | Symbols | Wire field / use |
|---|---|---|
| K-characters | `K27_7 K28_1 K28_2 K28_3 K28_4 K28_5 K28_6 K29_7 D21_5` | 8-bit character with K flag (Table 11, Table 14) |
| Comma and LS trigger leaders | `K28_5_NEG K28_5_POS K28_2_NEG K28_2_POS K28_4_NEG K28_4_POS`, `is_k28_5() is_k28_2() is_k28_4()` | 10-bit symbol, `din` layout (Table 15 leaders) |
| IDLE and masks | `KMASK_IDLE IDLE_WORD KMASK_ALL KMASK_NONE IDLE_MAX_INTERVAL IDLE_SOFT_RUN LS_IDLE_MAX_WORDS LS_BITS_PER_WORD` | Table 14, §8.2.5.1 |
| Packet TYPE | `PKT_TYPE_*` | Table 18 word 1 |
| Connection test | `LT_DATA_WORDS LT_PKT_WORDS LT_GAP_WORDS`, `RX_MAX_BODY_WORDS` | Table 23; longest uplink packet body accepted |
| Marker sub-type | `HDR_TYPE_* LINE_TYPE_*` | word after 4×K28.3 (Tables 38–41) |
| Control | `CTRL_OP_* ACK_*` | Table 21 Cmd, Table 22 Code |
| CRC | `CRC_SEED CRC_POLY`, `crc_wire()` | §8.2.2.2 |
| Trigger / I/O ack | `TRIG_EDGE_* TRIG_DELAY_MAX TRIG_UNITS_PER_BIT IOACK_CODE_OK TRIG_ACK_TIMEOUT` | internal edge code, Table 15 Delay, Table 17, §8.3.3 acknowledgment wait |
| RX defaults | `RX_LOCK_HITS_DEFAULT RX_LOSS_WORDS_DEFAULT RX_BAD_WORDS_DEFAULT` | design defaults |
| Pixel format | `PIXFMT_MONO*`, `pixfmt_bits()`, `dsizel_words()` | Table 38/40 PixelF, Table 25; DsizeL (Tables 38/41) |
| TX port table | `cxp_txw_t`, `TX_PORTS`, `TX_PORT_ACK/LT/STREAM` | the Table 13 priority-2 (long packet) sources and their order |
| Bus structs | `cxp_meta_t`, `cxp_rxlong_t`, `cxp_pix_t`, `cxp_ctrl_cmd_t`, `cxp_ctrl_rsp_t` | frame metadata, uplink long-packet words, single-pixel stream, control command record and response |

Source: `src/rtl/pkg/cxp_pkg.sv`. Not instantiated; compiled first in every build: it heads the single file list `src/rtl/cxp_ip.f` (followed by `cxp_util_pkg.sv` and `cxp_regmap_pkg.sv`), which every unit TB and `src/verif/` (through `src/verif/common/cocotb_sim.mk`), `src/emu/bridge/Makefile` and `make lint` read. Generic helpers (`rep4`, `bswap32`, `cnt_w`, `idx_w`) live in `cxp_util_pkg`. Register addresses, reset values and the XML ROM geometry live in `cxp_regmap_pkg`, which `src/regmap/gen_regmap.py` generates from `src/regmap/cxp_regmap.yaml` and which is documented by the generated `docs/design/modules/pkg/cxp_regmap.md` and by `cxp_ctrl_bootstrap_regs.md`; it is not described here.

## Interface

No module parameters and no ports. The package's contents (consumers found by name in the `src/rtl/cxp_ip.f` sources):

| Name | Value | Spec | Consumers |
|---|---|---|---|
| `K27_7` | 0xFB | Table 11 SOP | pkt_framer (for ctrl_ack_tx, stream_pkt_tx, tx_linktest), rx_packet_parser |
| `K28_1` | 0x3C | Table 14 P1/P2 | rx_packet_parser; `IDLE_WORD` |
| `K28_2` | 0x5C | Table 16 falling edge | rx_packet_parser, tx_trigger_hs |
| `K28_3` | 0x7C | Table 11 stream marker | marker_gen (for image_header_gen, line_marker_gen), rx_packet_parser |
| `K28_4` | 0x9C | Table 16 rising edge | rx_packet_parser, tx_trigger_hs |
| `K28_5` | 0xBC | Table 14 P0 | rx_packet_parser; `IDLE_WORD` |
| `K28_6` | 0xDC | Table 17 I/O ack | tx_io_ack (sent), rx_packet_parser (the host's, decoded) |
| `K29_7` | 0xFD | Table 11 EOP | pkt_framer, rx_packet_parser |
| `D21_5` | 0xB5 | Table 14 P3 | `IDLE_WORD` only |
| `K28_5_NEG` | 10'b0101111100 | §8.2.1 figure, RD− | rx_lspd_sampler |
| `K28_5_POS` | 10'b1010000011 | RD+ | rx_lspd_sampler |
| `K28_2_NEG` / `K28_2_POS`, `K28_4_NEG` / `K28_4_POS` | 10'b1010111100 / 10'b0101000011, 10'b0100111100 / 10'b1011000011 | Table 15 leaders, RD− / RD+ | `is_k28_2()`, `is_k28_4()` only |
| `KMASK_IDLE` | 4'b0111 | Table 14 (K K K D, lane 0 = P0) | tx_inserter, rx_link_mon, rx_packet_parser; `cxp_idle_rule_sva` |
| `IDLE_WORD` | {D21_5, K28_1, K28_1, K28_5} | Table 14 | tx_inserter, rx_link_mon; `cxp_idle_rule_sva` |
| `KMASK_ALL` / `KMASK_NONE` | 4'b1111 / 4'b0000 | whole-word K / data | pkt_framer, tx_short_pkt, marker_gen / also ctrl_ack_tx, tx_linktest, stream_top, rx_packet_parser, rx_linktest |
| `IDLE_MAX_INTERVAL` | 100 | §8.2.5.1 high speed | tx_inserter (run limits 99 / 97 / 95 derived from it); `cxp_idle_rule_sva`, `cxp_inserter_sva` |
| `IDLE_SOFT_RUN` | 95 (`IDLE_MAX_INTERVAL` − 5) | design choice: run after which the IDLE goes as soon as no two-word packet is half sent | tx_inserter (`p_IDLE_SOFT` default) |
| `LS_IDLE_MAX_WORDS`, `LS_BITS_PER_WORD` | 10000, 40 | §8.2.5.1 low speed | rx_link_mon (guard) |
| `RX_LOCK_HITS_DEFAULT` / `RX_LOSS_WORDS_DEFAULT` / `RX_BAD_WORDS_DEFAULT` | 2 / 20 000 / 32 | design defaults; 20 000 words is twice the §8.2.5.1 interval; 32 misfit words since the last clean IDLE re-hunt the sampler | rx_lspd_sampler, rx_link_mon, rx_top, interface_top, device_top |
| `PKT_TYPE_STREAM` | 0x01 | Table 18 | stream_pkt_tx |
| `PKT_TYPE_CTRL` | 0x02 | Table 18 | rx_ctrl_cmd |
| `PKT_TYPE_ACK` | 0x03 | Table 18 | ctrl_ack_tx |
| `PKT_TYPE_LT` | 0x04 | Table 18 | rx_top, tx_linktest |
| `LT_DATA_WORDS` / `LT_PKT_WORDS` | 1024 / 1027 | Table 23 | tx_linktest / — (documentation only) |
| `RX_MAX_BODY_WORDS` | 2048 (2 × `LT_DATA_WORDS`) | design choice: a longer uplink body is a lost trailer | rx_packet_parser |
| `LT_GAP_WORDS` | 16 | device choice (§8.7.3 asks the Host for >= 1 IDLE) | tx_linktest |
| `HDR_TYPE_REC` | 0x01 | Table 38 word 2 | image_header_gen |
| `HDR_TYPE_ARB` | 0x03 | Table 40 word 2 | image_header_gen |
| `LINE_TYPE_RECT` | 0x02 | Table 39 word 2 | line_marker_gen |
| `LINE_TYPE_ARB` | 0x04 | Table 41 word 2 | line_marker_gen |
| `CTRL_OP_READ` | 0x00 | Table 21 | rx_ctrl_cmd |
| `CTRL_OP_WRITE` | 0x01 | Table 21 | ctrl_bus_master, rx_ctrl_cmd |
| `CTRL_OP_RESET` | 0xFF | Table 21 | ctrl_bus_master, rx_ctrl_cmd |
| `ACK_OK` | 0x00 | Table 22 | ctrl_ack_tx, ctrl_bus_master |
| `ACK_WRITE_OK` | 0x01 | Table 22 | ctrl_bus_master, rx_ctrl_cmd (ignored extension-link write) |
| `ACK_RESET_DONE` | 0x03 | Table 22 | ctrl_bus_master |
| `ACK_WAIT` | 0x04 | Table 22 | ctrl_ack_tx, ctrl_bus_master |
| `ACK_ERR_BAD_ADDR` | 0x40 | Table 22 invalid address | ctrl_bus_master (command timeout, APB PSLVERR), bootstrap_regs |
| `ACK_ERR_BAD_DATA` | 0x41 | Table 22 | bootstrap_regs |
| `ACK_ERR_BAD_OP` | 0x42 | Table 22 | rx_ctrl_cmd |
| `ACK_ERR_RO_WRITE` | 0x43 | Table 22 | rx_ctrl_cmd (extension link), bootstrap_regs |
| `ACK_ERR_WO_READ` | 0x44 | Table 22 | bootstrap_regs |
| `ACK_ERR_OVERSIZE` | 0x45 | Table 22 size field too large | rx_ctrl_cmd |
| `ACK_ERR_SIZE_MISMATCH` | 0x46 | Table 22 | rx_ctrl_cmd |
| `ACK_ERR_MALFORMED` | 0x47 | Table 22 | rx_ctrl_cmd |
| `ACK_ERR_CRC` | 0x80 | Table 22 | rx_ctrl_cmd |
| `CRC_SEED` | 0xFFFFFFFF | §8.2.2.2 | crc32 |
| `CRC_POLY` | 0xEDB88320 | §8.2.2.2, reflected 0x04C11DB7 | crc32 |
| `crc_wire(crc)` | the register unchanged, `[7:0]` in P0 | §8.2.2.2 (MSB of the CRC in P0 bit 0) | pkt_framer (every CRC sent), rx_ctrl_cmd (the CRC checked) |
| `TRIG_EDGE_NONE/RISE/FALL` | 2'b00/01/10 | internal encoding, not a wire value | rx_packet_parser, rx_trigger_lspd |
| `TRIG_DELAY_MAX`, `TRIG_UNITS_PER_BIT` | 239, 24 | §8.3.2.1 / Table 15, Figure 20 | rx_trigger_lspd (Delay range; the wait is Delay × `p_OS_RATIO` / 24 cycles) |
| `IOACK_CODE_OK` | 0x01 | Table 17 | tx_io_ack (sent), rx_packet_parser (the host's acknowledgment of a device trigger) |
| `TRIG_ACK_TIMEOUT` | 4096 | §8.3.3 transmission timeout, device trigger on the high-speed link (the value is the device's choice) | tx_trigger_hs (`p_ACK_TIMEOUT` default), `p_TRIG_ACK_TIMEOUT` default of interface_top and device_top |
| `PIXFMT_MONO8` | 0x0101 | Table 25 | pixel_packer; `p_PIXFMT` default of test_pattern_gen, `p_PIXEL_FORMAT_RESET` default of device_top |
| `PIXFMT_MONO10` | 0x0102 | Table 25 | pixel_packer, `pixfmt_bits()` |
| `PIXFMT_MONO12` | 0x0103 | Table 25 | pixel_packer, `pixfmt_bits()` |
| `PIXFMT_MONO14` | 0x0104 | Table 25 | pixel_packer, `pixfmt_bits()` |
| `PIXFMT_MONO16` | 0x0105 | Table 25 | pixel_packer, `pixfmt_bits()` |
| `cxp_txw_t` | struct {data[31:0], kmask[3:0], valid, sop, eop} | one TX source word | tx_arbiter (`src_i` array, `m_o`), tx_inserter (`trig_i`, `ioack_i`, `long_i`), interface_top (`tx_src`, `tx_long`, `tx_trig`, `tx_ioack`) |
| `TX_PORTS` | 3 | Table 13 priority-2 (long packet) sources | tx_arbiter (`p_PORTS` default), interface_top; `cxp_tx_owner_sva` bind |
| `TX_PORT_ACK` / `_LT` / `_STREAM` | 0 / 1 / 2 | order inside Table 13 priority 2 (the device's choice): control acknowledgment, connection test, stream | interface_top |
| `cxp_meta_t` | struct, 11 fields, `streamid` 8 bit | Tables 38/40 fields | test_pattern_gen, image_header_gen, line_marker_gen, stream_top, interface_top, device_top (`s_meta_i`) |
| `cxp_rxlong_t` | struct {data, kmask, valid, sop, eop, err, ptype} | uplink long-packet word after the TYPE word | rx_top (`long_o`), ctrl_plane (`long_i`), interface_top |
| `cxp_pix_t` | struct {data[15:0], valid, sof, sol, eol, eof} | single-pixel stream | interface_top (TPG / ingress / selected) |
| `cxp_ctrl_cmd_t` | struct {op, size[23:0], addr, nwords[15:0], err[7:0], wbank} | one control command per packet; `err` 0 = execute, else the Table 22 code to answer; `wbank` = write-buffer bank | rx_ctrl_cmd (`cmd_o`), ctrl_bus_master (`cmd_i`) |
| `cxp_ctrl_rsp_t` | struct {code, nwords, size[23:0], wait_ms, timeout, rbank} | the held control response; `rbank` = read-buffer bank of a 0x00 | ctrl_bus_master (`rsp_o`), ctrl_plane, interface_top (crossed to `tx_clk`), ctrl_ack_tx inputs |
| `is_k28_5(s)` | function | `s == K28_5_NEG \|\| s == K28_5_POS` | rx_lspd_sampler |
| `is_k28_2(s)` / `is_k28_4(s)` | functions | either disparity of the Table 15 leader | rx_lspd_sampler |
| `pixfmt_bits(pixfmt)` | function | bits per pixel on the wire: 10/12/14/16 for Mono10..16, 8 for Mono8 and every other code | `dsizel_words()` only (pixel_packer keeps its own case on the codes) |
| `dsizel_words(xsize, pixfmt)` | function | ⌈xsize · bits / 32⌉ | image_header_gen, line_marker_gen |

Notes:

- Every symbol has at least one RTL consumer, except `LT_PKT_WORDS`; the six Table 15 10-bit leader codes are read only by `is_k28_2()` / `is_k28_4()`. `D21_5`, `K28_1` and `K28_5` reach the transmitter only through `IDLE_WORD`. Values that coincide across fields (0x01..0x04 as TYPE, sub-type, opcode and ack code) are deliberately separate names; the header says not to merge them.
- No clocks, resets, registers or CDC: everything is an elaboration-time constant, a type or a pure function.
- `make lint` waives `UNUSEDPARAM` for this file in `src/rtl/cxp_ip.vlt`; with that waiver lint of `cxp_device_top` and `cxp_interface_top` is clean.

## How it works

1. **K-character bytes** follow Kx.y → y·32 + x (K28.5 = 5·32 + 28 = 0xBC). All nine values match Table 11 and the §8.2.1 example row.
2. **Comma symbols** are the K28.5 10-bit codes in the receiver's `din` layout: bit 0 is the first bit on the wire (`a`), so RD− `abcdei fghj = 001111 1010` becomes `10'b0101111100` and RD+ is its complement. `is_k28_5` compares against both.
3. **`KMASK_IDLE`** uses lane 0 = P0, so IDLE = {P3 D21.5, P2 K28.1, P1 K28.1, P0 K28.5} gives K flags 0111. All `kmask` ports in the tree use the same lane order.
4. **CRC** constants describe the §8.2.2.2 CRC-32: reflected polynomial, all-ones seed, no final XOR. `crc_wire()` is the one place that turns the register into the wire word; for the reflected register §8.2.2.2's bit order is the register itself with `[7:0]` in P0 (worked example `56 86 5D 6F`). Every framer (through `cxp_tx_pkt_framer`) and the command checker in `cxp_ctrl_cmd_parser` call it.
5. **TX port table and scheduling limits.** `cxp_txw_t` is the word every transmit source offers: the three long-packet sources into `cxp_tx_arbiter` (array index = `TX_PORT_*` = order, 0 first) and the trigger and I/O-acknowledgment sources into `cxp_tx_inserter`. The trigger (Table 13 priority 0) and the I/O acknowledgment (priority 1) have no port numbers: the inserter places them between the words of whatever long packet is on the wire. There is no pre-emption or suppression policy in the package any more: a started long packet always completes, and TestMode acts only on the stream source (`cxp_tx_linktest.suppress_traffic_o`). `IDLE_MAX_INTERVAL` gives the inserter its hard limit (at most 99 non-IDLE words, `MAX_RUN`), from which it derives the last run at which a trigger may start (97) and an I/O acknowledgment may start (95); `IDLE_SOFT_RUN` = 95 is the run from which the IDLE goes as soon as no two-word packet is half sent. `TRIG_ACK_TIMEOUT` is how long, in `tx_clk` cycles, `cxp_tx_trigger_hs` waits for the host's I/O acknowledgment before it may send the next trigger level (§8.3.3); the package comment sizes it against two low-speed uplink words (26 µs at 156.25 MHz). Changing the long-packet order is a change here, not in the arbiter.
6. **`cxp_meta_t`** carries all image-header and line-marker fields; the TPG produces it, the sensor's arrives as one on `s_meta` (8-bit `streamid`, Table 38 word 3 is one byte), `cxp_interface_top` selects between the two, and `cxp_app_stream` hands it to both marker generators.
7. **`cxp_rxlong_t`** is the stream of long-packet words that `cxp_rx_link` hands to the control plane and the connection-test checker, with the voted packet TYPE in `ptype`. **`cxp_pix_t`** is the single-pixel stream between a pixel source and `cxp_app_pixel_packer`, LSB-justified in 16 bits.

No FSM, no latency.

## Arbiter integration

`TX_PORTS` and `TX_PORT_*` define the arbiter's port count and order; `IDLE_MAX_INTERVAL` and `IDLE_SOFT_RUN` define the inserter's run limits (How it works, item 5). `cxp_tx_arbiter.md` and `cxp_interface_top.md` describe the behaviour (`cxp_tx_inserter` has no page yet; its header comment is the description). The bind of `cxp_tx_owner_sva` in `cxp_interface_top` takes `p_PORTS` from `TX_PORTS`, but its `owner` port is 2 bits and its `valid` vector is written out as `{tx_src[2].valid, tx_src[1].valid, tx_src[0].valid}`, so changing `TX_PORTS` also needs an edit in `src/sva/cxp_sva.sv` (Minor).

## Verification

Verilator 5.046, cocotb 2.0.1; the bound SVA use `IDLE_WORD`, `KMASK_IDLE`, `IDLE_MAX_INTERVAL` and `TX_PORTS`; no FSM coverage. No test targets the package itself. Its values are checked indirectly by the consumer TBs, which take their expected bytes from the golden `cxp_protocol` package (written to the specification and tested against spec and IEEE vectors in `cxp_protocol/tests/test_spec_vectors.py`) or from literals in the test. Where the RTL deliberately deviates, the TBs pass `q = cxp_protocol.DEVICE`, so a remaining deviation is visible instead of copied.

### Unit TB — none

`src/tb_unit/` has no `cxp_pkg` directory. The package is compiled into every unit TB from `src/rtl/cxp_ip.f`, so a wrong value shows up only where a consumer TB compares the wire byte against an independent expectation.

### Integration TB — `src/tb_unit/top/cxp_interface_top/`

16 tests on the real `cxp_interface_top` (all consumers except `cxp_device_top` compiled; the bound `cxp_idle_rule_sva`, `cxp_inserter_sva` and `cxp_tx_owner_sva` run in every test). The tests below assert a package value on the 32-bit pre-encoder word; the expected K bytes come from `cxp_8b10b.py` (a view of the golden `cxp_protocol.kcodes`), the TYPE codes are literals in the test.

| Test | Package value observed |
|---|---|
| test_01_idle_after_reset | `K28_5 K28_1 D21_5`, `KMASK_IDLE` |
| test_02_stream_from_tpg | `K27_7`, `K29_7`, `PKT_TYPE_STREAM` |
| test_04_linktest_packets_under_testmode | `PKT_TYPE_LT` (and absence of `PKT_TYPE_STREAM` after the first packet) |
| test_08_trigger_rising_edge | `K28_4` |
| test_09_trigger_edge_pair | `K28_4` then `K28_2` |
| test_10_trigger_preempts_stream | `K28_4`, `K27_7` |
| test_11_trigger_in_testmode | `K28_4` inside a `PKT_TYPE_LT` packet |
| test_15_link_reset_clears_trigger_output | `K28_4` then `K28_2` |
| test_19_trig_phase_sweep_100 | `K28_4` / `K28_2`, `IDLE_WORD`; the run limits derived from `IDLE_MAX_INTERVAL` |
| test_20_trigger_held_across_reset | `K28_4`, `K28_2` |

test_03 and test_17 check that the wire stays IDLE; tests 12, 13, 16 check controller state and bootstrap registers only; test_18 checks the PacketTag sequence.

#### test_01_idle_after_reset

- *Stimulus*: reset, no configuration, 32 wire samples from 2 cycles after reset release.
- *Checks*: every word is the IDLE word: lanes 0..3 = `K28_5 K28_1 K28_1 D21_5`, `kmask = 0b0111`.
- *Proves*: `IDLE_WORD` and `KMASK_IDLE` lane order as produced by `cxp_tx_inserter` (its reset value and its fill word).

#### test_02_stream_from_tpg

- *Stimulus*: `cfg_use_tpg = 1`, `cfg_run = 1`, `cfg_dsizeP = 8`; first packet collected from SOP.
- *Checks*: word 0 is 4×`K27_7` with `kmask = 0xF`; an EOP of 4×`K29_7` follows within 1024 words; word 1 has `kmask = 0` and all four lanes = 0x01.
- *Proves*: `K27_7` / `K29_7` (from `cxp_tx_pkt_framer`) and `PKT_TYPE_STREAM` in `cxp_tx_stream_pkt`.

#### test_04_linktest_packets_under_testmode

- *Stimulus*: TPG streaming; TestMode set by backdoor on the register file; five packets collected.
- *Checks*: packet 0 may be type 0x01; every later packet TYPE is 0x03 or 0x04; at least one 0x04 appeared.
- *Proves*: `PKT_TYPE_LT` in `cxp_tx_linktest`. The stream is held by `suppress_traffic_o` at the stream framer, not by a package policy.

#### test_08_trigger_rising_edge

- *Stimulus*: the uplink brought up; `trigger_in_app` 0→1 with the wire idle.
- *Checks*: no trigger header before the edge; the next short packet's lane-0 byte = `K28_4`, Delay word 0 with `kmask = 0`; none after.
- *Proves*: `K28_4` in `cxp_tx_trigger_hs`.

#### test_09_trigger_edge_pair

- *Stimulus*: rising then falling edge.
- *Checks*: first packet `K28_4`, second `K28_2`.
- *Proves*: both trigger leaders.

#### test_10_trigger_preempts_stream

- *Stimulus*: stream running, rising edge two words into a stream packet.
- *Checks*: a stream SOP was seen; trigger packet `K28_4` within 20 samples; another SOP afterwards.
- *Proves*: `K28_4` and `K27_7` on one wire. The 20-sample bound would also pass if the trigger waited for the packet's EOP (the test's own note), so insertion into the packet is proven by `src/tb_unit/top/cxp_device_top` test_27 and test_31, not here.

#### test_11_trigger_in_testmode

- *Stimulus*: TestMode on; 100 words into a test packet, a rising edge.
- *Checks*: a `K28_4` header with its Delay word within 20 words; the test packet around it keeps its framing.
- *Proves*: `K28_4` inserted into a `PKT_TYPE_LT` packet: TestMode does not hold triggers.

#### test_15_link_reset_clears_trigger_output

- *Stimulus*: rising edge, then a LinkReset with the pin held high.
- *Checks*: `K28_4` packet first, then a `K28_2` packet produced by the reset's forced de-assert.
- *Proves*: `K28_2` on a reset-driven falling level.

#### test_19_trig_phase_sweep_100

- *Stimulus*: a host that never acknowledges; long stream packets, then TestMode test packets back to back; the pin toggling every 3–11 cycles.
- *Checks*: every trigger leader is followed directly by its Delay word; leaders alternate `K28_4` / `K28_2` and end at the pin's level; they land at 80 or more positions of the IDLE cadence.
- *Proves*: the inserter's run limits (from `IDLE_MAX_INTERVAL`) never split a trigger with an IDLE; the bound `cxp_idle_rule_sva` checks the 99-word run at the same time.

#### test_20_trigger_held_across_reset

- *Stimulus*: the pin held asserted through a device reset and through a ConnectionReset; real edges in between.
- *Checks*: no packet for a pin held across a reset; one `K28_4` per real edge; one `K28_2` for the ConnectionReset.
- *Proves*: both leaders under the reset mask of `cxp_tx_trigger_hs`.

### Other

- Every unit TB under `src/tb_unit/` compiles the package; the ones whose DUT imports it exercise the listed symbols indirectly (see the Consumers column). `src/tb_unit/tx/cxp_tx_arbiter` exercises the port table through a wrapper that packs the three long-packet sources into `cxp_txw_t` in `TX_PORT_*` order and puts `cxp_tx_inserter` behind the arbiter, so the run limits from `IDLE_MAX_INTERVAL` / `IDLE_SOFT_RUN` are tested there; `src/tb_unit/tx/cxp_tx_trigger_hs` runs with `p_ACK_TIMEOUT` = 64, not `TRIG_ACK_TIMEOUT`; `src/tb_unit/top/cxp_device_top` checks every acknowledgment CRC and every stream CRC through `crc_wire` against the golden decoder.
- Python copies of the values: the golden `cxp_protocol` (`kcodes.py`, `packets.py` with `ACK_WO_READ = 0x44`, `ACK_OVERSIZE = 0x45`, `stream.py` with `PIXFMT_MONO16 = 0x0105` and `PIXFMT_MONO16_LEGACY = 0x0107`) is the reference the unit TBs and the emulator use; `src/verif/common/cxp_8b10b.py` re-exports its K bytes. Still maintained by hand: `src/verif/uvm/common/cxp_pkg.py` (K bytes, TYPE, IDLE word, bootstrap map; the PyUVM environment keeps its own codecs), `src/emu/cxp/protocol/constants.py` (ack codes with `BAD_SIZE = 0x44`, `BAD_ALIGNMENT = 0x45`), `src/emu/cxp/image/pixel_formats.py` (`MONO16 = 0x0107`), `src/regmap/genicam/cxp_camera.xml` (Mono16 enum = 0x0107).
- `src/verif/uvm/agents/video_agent.py:240` draws from `[0x0101, 0x0102, 0x0105, 0x0106, 0x0108]` and discards the draw; 0x0106 and 0x0108 are not Table 25 codes either.

### Running

```
make -C src/tb_unit/top/cxp_interface_top          # integration TB
make -C src/tb_unit/rx/cxp_rx_packet_parser       # heaviest single consumer
make lint                                 # whole IP, package waivers in cxp_ip.vlt
make -C src/tb_unit                            # regression
```

2026-09-26, commit `7a267e2`: `cxp_rx_packet_parser` 14/14 pass; `make lint` clean (both tops, `cxp_device_top` also with `p_ASYNC_CLOCKS=1`). `cxp_interface_top` and `cxp_device_top`: see their pages. Full regression not re-run.

### Not covered in-tree

- No test compares the SV package values with spec-table literals. The golden `cxp_protocol` does check its own values against the spec, and the consumer TBs compare the RTL's wire bytes with it, so a wrong package value now fails a consumer TB unless it is one of the named `DEVICE` quirks.
- No check that `src/verif/uvm/common/cxp_pkg.py` and the emulator constants equal the SV package.
- `ACK_ERR_OVERSIZE`, `ACK_WAIT`, `ACK_RESET_DONE`, `ACK_ERR_RO_WRITE`, `ACK_ERR_CRC` on the wire are covered by consumer unit TBs (`cxp_tx_ctrl_ack`, `cxp_ctrl_bus_master`, `cxp_ctrl_cmd_parser`) and `cxp_ctrl_plane`; on the wire `cxp_device_top` covers 0x45 (test_08), 0x43 from the register file (test_09), 0x04 (test_24) and 0x03 (test_25), not 0x80.
- `K28_6` (I/O ack) on the downlink appears end to end in `src/tb_unit/top/cxp_device_top` (test_07, test_27, test_29, test_31); the host's `K28_6` acknowledgment on the uplink is decoded in `src/tb_unit/rx/cxp_rx_packet_parser` and answered by the shared host model (`src/verif/common/cxp_host.py`) in the `cxp_interface_top` and `cxp_device_top` trigger tests.
- `PIXFMT_MONO16` through the full pipeline: the integration TBs and the PyUVM env use Mono8 only.
- `HDR_TYPE_ARB` / `LINE_TYPE_ARB` via `cxp_interface_top` (arbitrary mode is unit-tested only, see `cxp_app_line_marker.md`).
- `TRIG_ACK_TIMEOUT` at its default of 4096: every bench overrides it (`cxp_tx_trigger_hs` 64, `cxp_interface_top` 64, `cxp_device_top` 800), so the default value is never simulated.
- Reset, stall, counters, CDC, X-propagation: not applicable to a constants package.

## Known issues and recommendations

### Critical

None.

### Medium

1. **No package-level test.** Add `src/tb_unit/pkg/cxp_pkg/` with a thin SV wrapper that exposes every constant as an output and a cocotb test whose expected values come from the golden `cxp_protocol` with `SPEC` quirks (or from the tables directly), failing on each named deviation until it is fixed. Effort: 2 h.
2. **Hand-maintained Python mirrors.** `cxp_protocol` is the single Python reference for the unit TBs and the emulator's codecs, and `cxp_8b10b.py` is a view of it. Still separate: `src/verif/uvm/common/cxp_pkg.py`, `src/emu/cxp/protocol/constants.py`, `src/emu/cxp/image/pixel_formats.py`. Point them at `cxp_protocol` and add a CI check that the SV package and `cxp_protocol` (with `DEVICE`) agree. Effort: 0.5 day.
3. **Fixed 2026-09-27: `streamid` width at the sensor port.** The metadata travels as `cxp_meta_t` from `cxp_device_top.s_meta_i` through `cxp_interface_top.s_meta` into `cxp_app_pixel_ingress`; no 16-bit StreamID port is left.

### Minor

- `K28_0 = 0x1C` exists in the Python models but not in the package; add it if the RTL ever decodes K28.0 (§8.2.3 lists it as a short-packet start indication), otherwise leave it to the models.
- `src/verif/uvm/common/cxp_pkg.py:252/254` comment "spec table 37/39" for header lengths; the v1.1.1 tables are 38 and 40.
- `cxp_tx_owner_sva` is bound with a 2-bit `owner` and a written-out 3-entry `valid` vector (Arbiter integration); derive both from `TX_PORTS` so that a port-count change cannot silently mis-bind.

### Open questions

1. Verification: should `src/verif/` move onto `cxp_protocol` so that only one Python reference remains (Medium 2)?
2. Designer: `TRIG_ACK_TIMEOUT` is an elaboration constant in `tx_clk` cycles, so the wait in time scales with the downlink rate (26 µs at 156.25 MHz, 131 µs at 31.25 MHz, per the package comment). §8.3.3 recommends, for a device trigger acknowledged on the low-speed link, a register controlling the timeout with a device default that the host can override. Should the constant become the reset value of such a register?
