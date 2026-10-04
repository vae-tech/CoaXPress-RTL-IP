# cxp_rx_linktest

Inputs chosen from the tree: RTL `src/rtl/rx/cxp_rx_linktest.sv`; unit TB `src/tb_unit/rx/cxp_rx_linktest/`; integration TB `src/tb_unit/rx/cxp_rx_link/` (direct parent); spec JIIA CXP-001-2015 v1.1.1 (`docs/spec/CXP-001-2015.pdf`) §8.7.1–§8.7.3, Table 23, §10.3.28, §10.3.37, §10.3.39; regression `make -C src/tb_unit`; output `docs/design/modules/rx/cxp_rx_linktest.md`.

The §8.7.1 Test Receiver of the Device: compares the body of every host connection-test packet (type 0x04) against the Table 23 sequence, counts differing words into TestErrorCount and counts packets into TestPacketCountRx.

| Word on the `long_*` stream | Content | Handling |
|---|---|---|
| SOP word (`long_sop_i`) | 4 × 0x04 TYPE | word index to 0, not compared |
| Body word i | P0 = 4i mod 256, P1..P3 = +1..+3 (Table 23) | compared on arrival; one error if it differs, has a K lane, or i > 1023 |
| K29.7 word (`long_eop_i`) | trailer | one error per missing word (i < 1024), packet counted |

Source: `src/rtl/rx/cxp_rx_linktest.sv`. One instance, `cxp_rx_link.cxp_rx_linktest_i`, on `rx_clk`. Fed by `cxp_rx_packet_parser`'s `long_*` body stream, qualified in the parent by `lt_gate = long_valid_w & (long_type_w == PKT_TYPE_LT)`. The two counters leave `cxp_rx_link` as `lt_err_count_o` / `lt_pkt_count_rx_o`, `cxp_interface_top` exports them as `sb_lt_err_count` / `sb_lt_pkt_count_rx`, and `cxp_ctrl_bootstrap_regs` serves them at 0x4024 and 0x4030. The clears come from the `cxp_interface_top` inputs `clr_lt_err` and `clr_lt_pkt_rx` (through `cxp_rx_link` `clr_lt_err_i` / `clr_lt_pkt_i`), which the register file pulses on a host write of 0 or on a ConnectionReset.

Spec clauses: §8.7.1 (compare per word, count packets), §8.7.2 / Table 23 (pattern), §8.7.3 (runs regardless of TestMode; write 0 clears), §10.3.37, §10.3.39, §10.3.28 (ConnectionReset clears).

## Interface

No parameters.

| Name | Dir | Width | Description |
|---|---|---|---|
| `rx_clk` | in | 1 | Only clock |
| `rx_rst_n` | in | 1 | Active-low, asynchronous assert; port comment says "sync" |
| `gate_i` | in | 1 | Word qualifier: `long_valid & (long_type == 0x04)` in the parent |
| `long_data_i` | in | 32 | Body word, P0 in [7:0] |
| `long_kmask_i` | in | 4 | Per-lane K flag; any set bit makes the word non-data |
| `long_sop_i` | in | 1 | High on the TYPE word |
| `long_eop_i` | in | 1 | High on the 4 × K29.7 word |
| `clr_err_i` / `clr_pkt_i` | in | 1 each | Level; clears TestErrorCount / TestPacketCountRx and blocks a same-cycle increment of that counter |
| `err_count_o` | out | 32 | TestErrorCount, saturates at 0xFFFFFFFF |
| `pkt_count_o` | out | 64 | TestPacketCountRx, wraps |

Notes:

- Reset: `negedge rx_rst_n` in the sensitivity list, so asynchronous assert. Every register clears, including both counters.
- Single clock. The clears come only from the register file on `rx_clk` (`ctl_test_err_count_clr_o`, `ctl_test_pkt_rx_clr_o` → `clr_lt_err`, `clr_lt_pkt_rx`): a host write of 0, or one pulse of each on a ConnectionReset; no crossing is involved. The counters are read by the register file.
- Both outputs are registers. No combinational path from any input to an output.
- No `ready`: the module accepts one word per cycle and cannot stall the parser.
- `gate_i` must already include `long_valid`; the module never looks at a valid of its own.

## How it works

1. **Packet state.** `gate & sop` sets `in_pkt_q` and clears the word index `idx_q`; the trailer (`gate & eop`) clears `in_pkt_q`. Words outside a packet are ignored.
2. **Compare.** Each body word (`gate & in_pkt_q & ~sop & ~eop`) is compared with `lt_word(idx_q)`; a data difference, any K lane, or `idx_q ≥ 1024` adds one to `err_count_q` (saturating at 0xFFFFFFFF). `idx_q` advances on every body word, so a single corrupted or K-character word costs exactly one error and the words after it compare against the right position.
3. **End of packet.** On the trailer, `1024 − idx_q` missing words (if any) are added to `err_count_q` and `pkt_count_q` increments. A packet the parser aborts (`long_eop_i` with `long_err_i`: lost trailer, link loss) ends the same way. There is no CRC slot: word 1023 is compared like every other (Table 23).

Same-cycle rules: each clear wins over its counter's increment on the same cycle. `sop` overrides everything else on that word. Back-to-back packets with `eop` and the next `sop` on consecutive cycles count correctly.

Latency: a word's error is visible 1 cycle after the word; `pkt_count_o` 1 cycle after the K29.7 word. Throughput 1 word/cycle.

Invariant by construction, not asserted: `err_count_o` never decreases except through `clr_err_i` or reset.

No FSM.

## Arbiter integration

Not applicable: the module sits on the receive side behind `cxp_rx_packet_parser`, has no handshake and cannot be preempted or stalled. Its only side-band input is `clr_counter_i` (host write of 0 or ConnectionReset).

## Verification

Verilator 5.046 + cocotb 2.0.1. No SVA. No FSM or functional coverage is collected for this module (no `fsm_coverage.json` in the TB directory).

### Unit TB — `src/tb_unit/rx/cxp_rx_linktest/`

Wrapper `tb_cxp_rx_linktest_top` with a `TESTCASE` register; 10 ns clock; reset holds `rx_rst_n` = 0 with all inputs 0 for 4 cycles, then 1 more cycle. No shared checker: each test reads `err_count` / `pkt_count` after the last edge. The helper `send_packet(n_data = 1024, error_indices, k_indices)` drives: TYPE word (`long_sop` = 1), n data words from the golden `cxp_protocol` sequence with bit 0 flipped at each listed error index or a 4 × K28.5 word at each K index, a 4 × K29.7 word with `long_eop` = 1 and kmask 0xF, then one cycle with `gate` = 0.

| Test | Stimulus | Expect |
|---|---|---|
| `test_01_reset` | reset only | `err_count` = 0 |
| `test_02_clean` | 3 full packets, no errors | `err_count` = 0 |
| `test_03_inject_errors` | full packet, errors at 1, 5, 1022; then full packet, error at 1023 | 3, then 4 |
| `test_04_clear` | full packet, errors at 1, 2, 3; `clr_err` 1 cycle | 3, then 0; `pkt_count` kept |
| `test_05_saturation` | backdoor `err_count_q` = 0xFFFFFFFE; words 0–9 wrong | 0xFFFFFFFF |
| `test_06_kchar_in_body` | full packet with word 3 = 4 × K28.5; then a clean packet | `err_count` = 1 |
| `test_07_pkt_count_rx` | 3 clean full packets, 1 with 2 errors, `clr_pkt` | `pkt_count` 1, 2, 3, 4 → 0; `err_count` stays 2 |
| `test_08_short_and_long_packets` | 1000-word packet, then 1025-word packet | 24, then 25; `pkt_count` 2 |

#### test_01_reset
- *Checks*: `err_count` == 0 after reset.

#### test_02_clean
- *Stimulus*: 3 full Table 23 packets (1024 words), one idle cycle between packets.
- *Checks*: `err_count` == 0.
- *Proves*: the index restart on `sop` and the 64-word roll-over of the sequence.

#### test_03_inject_errors
- *Stimulus*: packet A with bit 0 flipped in words 1, 5, 1022; packet B with word 1023 flipped.
- *Checks*: `err_count` == 3 after A, 4 after B.
- *Proves*: one count per differing word, including the last word before K29.7 (the old two-slot delay line dropped 1022 and 1023: it read 2 after A).

#### test_04_clear
- *Checks*: 3 errors, then 0 one cycle after a one-cycle `clr_err`; `pkt_count` still 1.

#### test_05_saturation
- *Stimulus*: backdoor `err_count_q` = 0xFFFFFFFE, then a packet with words 0–9 wrong.
- *Checks*: `err_count` == 0xFFFFFFFF.

#### test_06_kchar_in_body
- *Stimulus*: a packet with word 3 replaced by 4 × K28.5, then a clean packet.
- *Checks*: `err_count` == 1.
- *Proves*: a K-character word costs one error and the index keeps advancing.

#### test_07_pkt_count_rx
- *Checks*: `pkt_count` 1, 2, 3 after clean packets, 4 after one with 2 errors (`err_count` 2); after `clr_pkt` `pkt_count` 0 and `err_count` still 2.

#### test_08_short_and_long_packets
- *Stimulus*: a 1000-word packet, then a 1025-word packet.
- *Checks*: `err_count` 24, then 25; `pkt_count` 2.
- *Proves*: missing and surplus words count.

### Integration TB — `src/tb_unit/rx/cxp_rx_link/`

6 tests against the real `cxp_rx_link` (serial input through the sampler, decoders, link monitor and parser). None sends a type-0x04 packet, none reads `lt_err_count` or `lt_pkt_count_rx`, and `clr_lt_counter` is held at 0 throughout, so no test touches this module. 6/6 pass is reported below as evidence that the instance elaborates and stays quiet.

### Other

- `src/tb_unit/top/cxp_interface_top` `test_04_linktest_packets_under_testmode` and `test_11_trigger_suppressed_in_testmode` exercise the transmit side (`cxp_tx_linktest`) only; `sb_lt_err_count` / `sb_lt_pkt_count_rx` are never read.
- `src/tb_unit/ctrl/cxp_ctrl_bootstrap_regs` `test_14_test_block_shifted` drives `test_err_count_link0` / `test_pkt_count_rx_link0` directly and reads 0x4024 / 0x4030; it covers the register side of the path, not this module.
- `src/tb_unit/tx/cxp_tx_linktest`: the Device-side generator. Its packet follows Table 23 (no CRC word), as this receiver expects; no loopback test exists.
- `src/verif/uvm` (PyUVM, `cxp_interface_top` + regfile): `test_linktest_clean` sends 4 packets of 64 data words and expects `sb_lt_err_count` = 0; `test_linktest_inject` sends 1 clean + 3 packets with 3 flipped words each and expects 9. Both packet sizes are short, so `linktest_scoreboard` adds the 960 missing words per packet to its expectation (§8.7.1).

### Running

```
cd src/tb_unit/rx/cxp_rx_linktest && make WAVES=0 COCOTB_TEST_FILTER=test_03_inject_errors   # one test
cd src/tb_unit/rx/cxp_rx_linktest && make WAVES=0 && make clean                                # all 7
make -C src/tb_unit                                                                          # regression
```

2026-09-19: unit TB 8/8; full `make tb` green.

### Not covered in-tree

- Reset during a packet, and a packet already on the stream at reset release (observed: counters clear, the next packet is counted normally).
- `clr_pkt_i` on the `eop` cycle (the packet is not counted) or `clr_err_i` on a mismatch cycle (not counted).
- `err_count_o` saturation through the datapath: `test_05` reaches it only by writing the register.
- `pkt_count_o` wrap (2^64 packets, unreachable).
- Indefinite stall: not applicable, there is no handshake.
- Multi-clock operation: the clears come from the register file's clock → Medium 2.
- End-to-end: serial packet → parser → this module → 0x4024 read, in `src/tb_unit` → Medium 2. Host write-0 clear path: the register file's per-counter clears is unconnected in every integration.
- Packet whose TYPE lane P0 or trailer is corrupted (parser routes or ends the packet without `long_eop_o`; `pkt_count_o` not incremented).
- X-propagation: all inputs are driven from cycle 0 in both TBs.

## Known issues and recommendations

### Critical

None.

### Medium

1. **No end-to-end coverage in `src/tb_unit`.** Fix: a `cxp_rx_link` test that sends one Table 23 packet with two flipped words through the serial driver, checks `lt_err_count` = 2 and `lt_pkt_count_rx` = 1, pulses `clr_lt_counter`, and checks 0. Effort: 2 h.
2. **Resolved: unsynchronised crossings.** The clears now come only from the register file on `rx_clk` (the former link-reset clear from `tx_clk` is gone), and the register file itself runs on `rx_clk` in `cxp_device_top` and `src/verif/`, so neither the clears nor the counters change clock.
3. **`clr_pkt_i` on the `eop` cycle drops the packet from `pkt_count_o`**; a host clear landing on the trailer of a packet loses one count. Fix: apply the clear before the increment (`pkt_count_q <= clr ? 1 : pkt_count_q + 1`) or accept the loss. Effort: 0.5 h after Open question 3.

### Minor

2. `rx_rst_n` comment says "sync reset"; the always block uses `negedge rx_rst_n`. Fix the comment or the block.
3. ~~`slot_t.is_kchar` is never set; drop the field.~~ Done 2026-09-19.
4. `test_05` comment paragraph describes a plan that is not the test; shorten to "backdoor preload".
5. SVA to add: `clr_err_i |=> err_count_o == 0`; `clr_pkt_i |=> pkt_count_o == 0`; `gate_i & long_sop_i |=> idx_q == 0`.

### Open questions

1. Designer: should a packet whose trailer coincides with a clear still be counted?
2. Verification: should `sb_lt_err_count` / `sb_lt_pkt_count_rx` be checked in `src/tb_unit/top/cxp_interface_top` with a host test packet, or is `src/verif/uvm` the only owner of that path?

No repository files other than this document were changed.
