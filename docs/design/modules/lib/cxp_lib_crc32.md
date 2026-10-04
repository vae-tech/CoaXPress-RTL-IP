# cxp_lib_crc32

Inputs chosen: RTL `src/rtl/lib/cxp_lib_crc32.sv` (constants in `src/rtl/pkg/cxp_pkg.sv`); unit TB `src/tb_unit/lib/cxp_lib_crc32/`; integration TB `src/tb_unit/tx/cxp_tx_stream_pkt/` (the stream-packet consumer); spec JIIA CXP-001-2015 v1.1.1 (`docs/spec/CXP-001-2015.pdf`) §8.2.1, §8.2.2.2, Tables 19/21/22; regression `make -C src/tb_unit`.

A parallel CRC-32 accumulator. Each clock it folds 1 to `p_IN_W/8` enabled bytes into a reflected 0x04C11DB7 register, P0 first and bit 0 first, and outputs the register itself (no final XOR). It has no FSM and no handshake.

| Setting | RTL | §8.2.2.2 |
|---|---|---|
| Polynomial | 0x04C11DB7 (reflected constant `CRC_POLY` = 0xEDB88320) | same |
| Seed | `CRC_SEED` = 0xFFFFFFFF | same |
| Final XOR | none | none |
| Wire order | `crc_wire()` = the register, `[7:0]` in P0 | MSB of the CRC in P0 bit 0 (reflected register) |
| Fold order | `din_i[7:0]` = P0 … `din_i[31:24]` = P3, bit 0 first, word 0 first | same |

There are two instances, both with `p_IN_W = 32` and `din_be_i = 4'b1111` (the TX one lives in `cxp_tx_pkt_framer`, shared by the stream and ack framers):

| Instance | Clock | `init_i` | `din_valid_i` (words folded) | `crc_o` use |
|---|---|---|---|---|
| `cxp_tx_pkt_framer.g_crc.cxp_lib_crc32_i` | tx_clk | `ST_IDLE & start_i` | header words from the caller's CRC start index, then ST_DATA on `data_fire` | ST_CRC `m_data_o = crc_wire(crc_o)` |
| `cxp_ctrl_cmd_parser.cxp_lib_crc32_i` | rx_clk | `ST_IDLE & type-0x02 & long_sop` | header and data words on `word_fire` | ST_CRC `crc_pass_q <= (long_data_i == crc_wire(crc_o))` |

Spec clauses implemented: §8.2.1 (lane order) and §8.2.2.2 (polynomial, seed, no final XOR, fold order, output bit order, K28.3 treated as D28.3). The §8.2.2.2 worked example (read of address 0 → `56 86 5D 6F`) is a unit test.

## Interface

| Name | Default | Meaning |
|---|---|---|
| `p_IN_W` | 32 | Input width in bits. Must be a positive multiple of 8; bytes per beat = `p_IN_W/8`. In-tree values: 32 (all 3 instances) and 8 (unit TB only). |

| Name | Dir | Width | Description |
|---|---|---|---|
| `clk` | in | 1 | Accumulator clock |
| `rst_n` | in | 1 | Active-low reset. Asserts asynchronously and loads `crc_q = CRC_SEED`, so `crc_o = 0xFFFFFFFF` |
| `init_i` | in | 1 | Synchronous re-seed. Applied before any fold in the same cycle |
| `din_i` | in | `p_IN_W` | Data. Lane `b` = `din_i[8b+7:8b]`; lane 0 = P0 is folded first |
| `din_be_i` | in | `p_IN_W/8` | Per-byte enable. Disabled lanes are skipped; enabled lanes fold in ascending order |
| `din_valid_i` | in | 1 | Fold this beat. 0 = hold |
| `crc_o` | out | 32 | `crc_q`, the register |

Notes:
- Reset: `posedge clk or negedge rst_n`, so assertion is asynchronous. The caller must synchronise deassertion; the port comment only says "sync-deassert".
- Clock domains: one clock per instance (two on tx_clk, one on rx_clk). Nothing crosses domains, so there is no CDC.
- `crc_o` is the register. There is no combinational path from the inputs.
- There is no stall input. The caller qualifies `din_valid_i` with its own handshake. A beat with `din_valid_i = 1` and `din_be_i = 0` is a hold.
- The width check is a sim-time `initial $error` inside `lint_off WIDTH`. `p_IN_W = 12` passes `verilator --lint-only -Wall` and silently ignores `din_i[11:8]`.

## How it works

No FSM.

1. **Next state (combinational):** `c = init_i ? CRC_SEED : crc_q`. If `din_valid_i`, then for each lane `b = 0 … p_IN_W/8−1` with `din_be_i[b]` set: `c = crc32_byte(c, din_i[8b+:8])`. Each `crc32_byte` is 8 unrolled LSB-first shift/XOR steps, so the chain is `p_IN_W` bit-steps deep (32 at `p_IN_W = 32`).
2. **Register:** `crc_q <= crc_n` every clock; `crc_o = crc_q`. Folding the transmitted CRC word after the data leaves `crc_q = 0` (the §8.2.2.2 residue).
3. **Same-cycle rules:**
   - `{init_i, din_valid_i}` = `10` → seed; `crc_o = 0xFFFFFFFF` on the next cycle.
   - `11` → seed, then fold this beat, so the first beat can carry `init_i`.
   - `01` → fold onto `crc_q`.
   - `00` → hold.
   - `rst_n = 0` overrides everything.
4. **Latency and throughput:** a beat sampled at edge *k* appears in `crc_o` right after edge *k* (1 clock). The consumer can emit the CRC word on the cycle after its last fold, and `cxp_tx_stream_pkt` does: ST_DATA → ST_CRC with no bubble. Throughput is `p_IN_W/8` bytes every clock.
5. **K28.3 handling:** there is no K-flag input, so a K28.3 byte (0x7C) folds exactly like D28.3, as §8.2.2.2 requires. This holds only while the consumer folds the data byte regardless of kmask; `cxp_tx_stream_pkt` folds `s_data_i` and ignores `s_kmask_i`.
6. **Consumer same-cycle rule:** in `cxp_tx_stream_pkt`, `data_in_fire` does not include `!suppress_stream_i`. On the first suppressed cycle in ST_DATA, the dropped word is still folded (integration VCD: A3 folded, `crc_o` 0xED131A30 → 0xBBE9D24A). This is harmless because `crc_init` reseeds on the next SOP.
7. **Invariant (not asserted):** `crc_o == CRC_SEED` after reset, or after `init_i` with no data.

## Arbiter integration

Not applicable. `cxp_lib_crc32` has no request, grant or ready. It sits inside `cxp_tx_stream_pkt` and `cxp_tx_ctrl_ack`, which are the `cxp_tx_arbiter` clients. Backpressure and suppression reach it only through the consumer's `din_valid_i` gating (instance table; How it works item 6).

## Verification

Tools: Verilator 5.046 with cocotb 2.0.1. There are no SVA in the RTL or the TBs, and no coverage is collected for this module (`fsm_coverage` samples only registered FSMs; `cxp_tx_stream_pkt` registers its own).

### Unit TB — `src/tb_unit/lib/cxp_lib_crc32/test_cxp_lib_crc32.py`

- **Wrapper:** `tb_cxp_lib_crc32_top` instantiates `cxp_lib_crc32_w_i` (`p_IN_W = 32`, ports `w_*`) and `cxp_lib_crc32_b_i` (`p_IN_W = 8`, ports `b_*`) on a shared `clk`/`rst_n`.
- **Clock and reset:** 10 ns clock, starting low. Bring-up sets all inputs to 0, holds `rst_n = 0` for 4 rising edges, releases it, and waits 1 more edge.
- **Drivers:** `w_feed`/`b_feed` present one valid beat; consecutive calls are back-to-back. `w_seed`/`b_seed` give one `init` cycle with no data. `w_stall(n)` gives n idle cycles.
- **Checks:** there is no shared checker. Each test reads `w_crc`/`b_crc` in `ReadOnly` one idle cycle after the last beat and compares it with the §8.2.2.2 register (`zlib.crc32 ^ 0xFFFFFFFF`; `cxp_protocol.crc` in test_13 and test_16).

| Test | Stimulus | Expect |
|---|---|---|
| test_01_reset_state | bring-up only | `w_crc = b_crc = 0xFFFFFFFF` |
| test_02_empty_init_is_zero | init pulse, no data, both widths | seed |
| test_03_known_vector_check | "123456789" byte-wise | 0x340BC6D9 (= ~0xCBF43926) |
| test_04_wordwise_full_words | 00..07 as 2 words | ref |
| test_05_wordwise_vs_bytewise_random | 20 random buffers, 1–256 B, both widths | ref, w = b |
| test_06_byte_enable_partial_last_word | one word each with BE 0x1/0x3/0x7/0xF | ref of enabled bytes |
| test_07_stalls_dont_perturb_crc | 16 words, 0–4 idle cycles after each | ref |
| test_08_init_and_data_same_cycle | dirty state, then init + data together | fresh ref |
| test_09_back_to_back_packets | 8 packets, 4–40 B | ref per packet |
| test_10_single_bit_flip_detected | 48 B, then 1 bit flipped | both match ref, differ |
| test_11_zero_payload_is_seed | init, 5 idle | seed |
| test_12_disabled_byte_enable_skipped | valid beats with BE = 0 interleaved | ref |
| test_13_stream_packet_shape | N data words (Table 19 coverage), 5 packets | `cxp_protocol.crc` |
| test_14_reset_clears_partial | 2 beats, then reset | seed |
| test_15_long_payload | 1024 B | ref |
| test_16_spec_worked_example | 0x04000000, 0x00000000, then the CRC word | lanes `56 86 5D 6F`, residue 0 |

#### test_01_reset_state
- *Stimulus*: bring-up only.
- *Checks*: `w_crc == 0xFFFFFFFF`; `b_crc == 0xFFFFFFFF`.
- *Proves*: reset loads `CRC_SEED` at both widths.

#### test_02_empty_init_is_zero
- *Stimulus*: `w_seed`, 1 idle cycle, sample; then `b_seed`, 1 idle cycle, sample.
- *Checks*: `w_crc == b_crc == 0xFFFFFFFF`.
- *Proves*: `{init, valid} = 10` → `crc_o = CRC_SEED`. **Weak: both accumulators are already at seed from reset, so the check still passes with `init_i` stuck at 0.** Init from a dirty state is covered by test_05_wordwise_vs_bytewise_random (trials 2–20) and test_09_back_to_back_packets.

#### test_03_known_vector_check
- *Stimulus*: `cxp_lib_crc32_b_i` only. The 9 bytes of "123456789" go in as back-to-back beats, the first with `init = 1`, then 1 idle cycle.
- *Checks*: reference self-check == 0x340BC6D9; `b_crc == 0x340BC6D9`.
- *Proves*: the `p_IN_W = 8` variant produces the CRC-32 check value without the 802.3 final XOR. `cxp_lib_crc32_w_i` is not driven.

#### test_04_wordwise_full_words
- *Stimulus*: words 0x03020100 (with init) and 0x07060504, BE = 0xF, back-to-back, then 1 idle cycle.
- *Checks*: `w_crc == ref(00..07)`.
- *Proves*: lanes fold in P0-first order within a word and across 2 words.

#### test_05_wordwise_vs_bytewise_random
- *Stimulus*: seed 0xC0FFEE, 20 trials. Lengths are 1–256 (drawn values 4 … 213 cover all four residues mod 4). Each trial:
  - `cxp_lib_crc32_w_i`: `w_seed`, then ⌈n/4⌉ back-to-back words with a low-side BE on the last word, 1 idle cycle, sample.
  - `cxp_lib_crc32_b_i`: the same bytes (`b_seed`, then n beats), sample.
  - The two instances are driven one after the other, not in parallel.
- *Checks* (per trial): `got_w == ref`; `got_b == ref`; `got_w == got_b`.
- *Proves*:
  - A partial last word with BE ∈ {0x1, 0x3, 0x7} matches the byte-wise fold.
  - `{init, valid} = 10` reseeds from a dirty accumulator.

#### test_06_byte_enable_partial_last_word
- *Stimulus*: seed 0x12345. For BE = 0x1, 0x3, 0x7, 0xF: `w_seed`, one random word with that BE, 1 idle cycle.
- *Checks*: 4 times, `w_crc == ref(low n bytes)`.
- *Proves*: `din_be_i[b] = 0` skips lane b. Only contiguous low-side masks are tested.

#### test_07_stalls_dont_perturb_crc
- *Stimulus*: seed 0xDEADBEEF, 64 bytes = 16 full words. `w_seed`, then each word followed by 0–4 idle cycles (drawn: 1,3,2,4,1,0,0,4,2,2,1,1,3,0,3,3).
- *Checks*: `w_crc == ref(64 bytes)`.
- *Proves*: `{init, valid} = 00` holds `crc_q`, for both stalled beats and back-to-back beats (gap 0).

```wavedrom
{"signal":[
  {"name":"clk","wave":"p....|...|.."},
  {"name":"w_init","wave":"10...|...|.."},
  {"name":"w_din_valid","wave":"01010|10.|0."},
  {"name":"w_din","wave":"=====|==.|=.","data":["0","daf0da09","0","caa9eec1","0","89c3d0ba","0","0"]},
  {"name":"w_crc","wave":"=.=.=|.=.|=.","data":["00000000","A","B","C","ref(64 B)"],"node":"...........a"}
],
"head":{"text":"test_07_stalls_dont_perturb_crc — first 3 words (stalls 1, 3, 2); a = sample"}}
```

#### test_08_init_and_data_same_cycle
- *Stimulus*:
  1. Dirty the accumulator: 0xDEADBEEF with `init = 1`, then 0xCAFEBABE, then 1 idle cycle.
  2. 0x04030201 with `init = 1` co-asserted, then 0x08070605, then 1 idle cycle.
- *Checks*: `w_crc == ref(01..08)`.
- *Proves*: `{init, valid} = 11` from a non-seed state seeds first, then folds.

```wavedrom
{"signal":[
  {"name":"clk","wave":"p......"},
  {"name":"w_init","wave":"10.10.."},
  {"name":"w_din_valid","wave":"1.01.0."},
  {"name":"w_din","wave":"======.","data":["DEADBEEF","CAFEBABE","0","04030201","08070605","0"]},
  {"name":"w_crc","wave":"===.==.","data":["00000000","dirty1","dirty2","ref(01..04)","ref(01..08)"],"node":"......a"}
],
"head":{"text":"a = sample; the cycle-3 beat folds onto the seed, not onto dirty2"}}
```

#### test_09_back_to_back_packets
- *Stimulus*: seed 0xABCDEF, 8 packets of 4–40 bytes (drawn 9, 15, 4, 22, 22, 21, 32, 26). Each packet: `w_seed`, the words (last one partial), 1 idle cycle, sample.
- *Checks*: 8 times, `w_crc == ref(pkt)`.
- *Proves*: repeated reseeds between packets. **The packets are not back-to-back: each is preceded by one idle cycle and one data-less init cycle. Init co-asserted with the next packet's first beat, straight after the previous packet's last beat, is not exercised.**

#### test_10_single_bit_flip_detected
- *Stimulus*: seed 0x600D, 48 bytes; fold and sample. Then flip bit 4 of byte 2, `w_seed`, fold again and sample.
- *Checks*: `got_orig == ref(orig)`; `got_bad == ref(flipped)`; `got_bad != got_orig`.
- *Proves*: the RTL tracks the reference on the modified message. The third assert tests a property of CRC-32, not of the RTL.

#### test_11_zero_payload_is_seed
- *Stimulus*: `w_seed`, then 5 idle cycles.
- *Checks*: `w_crc == 0xFFFFFFFF`.
- *Proves*: the same as test_02_empty_init_is_zero, with **the same weakness: the accumulator is already at seed from reset.**

#### test_12_disabled_byte_enable_skipped
- *Stimulus*: seed 0xABCD, 12 bytes = 3 words. `w_seed`, then each real word preceded by a valid beat with `din = 0xFFFFFFFF`, BE = 0x0. That makes 6 back-to-back beats, then 1 idle cycle.
- *Checks*: `w_crc == ref(12 bytes)`.
- *Proves*: `din_valid_i = 1` with `din_be_i = 0` holds `crc_q`.

#### test_13_stream_packet_shape
- *Stimulus*: seed 0xCAFEC0DE, 5 packets of DsizeP random data words, BE = 0xF, after `w_seed`.
- *Checks*: 5 times, `w_crc == cxp_protocol.crc.crc32(data words)`.
- *Proves*: the fold over the Table 19 coverage (stream data words 4..N+3).

#### test_14_reset_clears_partial
- *Stimulus*: 0xDEADBEEF (`init = 1`), then 0xCAFEBABE. `rst_n = 0` is written in the step after the second beat's edge and held for 3 edges, then released, then 1 more edge.
- *Checks*: `w_crc == 0xFFFFFFFF`.
- *Proves*: reset returns `crc_q` to seed mid-packet. The sample is taken after clock edges, so a synchronous reset would also pass. An out-of-repo probe showed the asynchronous path: `crc_o` goes to 0 1 ns after `rst_n` falls, between edges.

```wavedrom
{"signal":[
  {"name":"clk","wave":"p......"},
  {"name":"rst_n","wave":"1.0..1."},
  {"name":"w_init","wave":"10....."},
  {"name":"w_din_valid","wave":"1.0...."},
  {"name":"w_din","wave":"===....","data":["DEADBEEF","CAFEBABE","0"]},
  {"name":"w_crc","wave":"===....","data":["00000000","ref(EF BE AD DE)","00000000"],"node":"......a"}
],
"head":{"text":"reset falls in the step of the edge that samples CAFEBABE; a = sample"}}
```

#### test_15_long_payload
- *Stimulus*: seed 0x1024, 1024 bytes = 256 back-to-back words after `w_seed`, then 1 idle cycle.
- *Checks*: `w_crc == ref(1024 bytes)`.
- *Proves*: a long continuous fold. This is the longest vector in-tree.

#### test_16_spec_worked_example
- *Stimulus*: 0x04000000 (Cmd 0x00, Size 4) with `init = 1`, 0x00000000 (Addr), 1 idle cycle, sample; then the sampled CRC word, 1 idle cycle, sample.
- *Checks*: lanes P0..P3 of `w_crc` = `56 86 5D 6F`; the second sample is 0.
- *Proves*: no final XOR and the §8.2.2.2 wire order; the residue property the receiver can rely on. With the old post-XOR register the lanes were `A9 79 A2 90`.

### Integration TB — `src/tb_unit/tx/cxp_tx_stream_pkt/test_cxp_tx_stream_pkt.py`

- **Setup:** 14 tests using the real `cxp_lib_crc32` (the Makefile compiles `src/rtl/lib/cxp_lib_crc32.sv`). tx_clk is 8 ns; reset is 8 edges followed by 4 idle edges.
- **Stimulus:** `PktTxDriver` queues packets on `s_*`. Queued packets are separated by one ST_IDLE cycle, which is the cycle where `crc_init` is high.
- **Shared checker `check_packet()`:**
  - Length = 6 + N + 2.
  - Exact header and data words.
  - The CRC beat equals `expected_crc_word`, the golden `cxp_protocol.crc.crc_word` over the covered words, with kmask/sop/eop = 0.
  - Trailer is 4×K29.7 with kmask 0xF and eop.

| Test | Checks |
|---|---|
| test_02_crc32_random | 5 packets, CRC per packet |
| test_03_packet_tag_wrap | 257 minimum packets, CRC per packet |
| test_05_dsizeP_truncation | drained excess words not folded |
| test_06_backpressure | CRC under random `m_ready` stalls |
| test_09_single_data_word | 1-clock fold → CRC-word latency |
| test_11_dsizeP_under_supply | CRC over a short payload |
| test_12_suppress_abandons_mid_data_and_resyncs | reseed after a mid-data abandon |
| test_13_suppress_mid_header_replays_header_from_idx0 | reseed after a mid-header abandon |

Four more tests also run `check_packet` on plain traffic: test_01_header_field_layout, test_07_per_stream_tag_independent, test_08_idle_between_packets and test_14_suppress_in_idle_preserves_tag_sequence. Two tests do not compare the CRC word: test_04_trailer_k29_7 and test_10_stream_ctrl_reset_restarts_tag.

Two things in this TB assert design decisions rather than spec requirements:
- test_05_dsizeP_truncation and test_11_dsizeP_under_supply assert framer policy.

#### test_02_crc32_random
- *Stimulus*: `cfg_dsizeP = 8`, 5 packets on SID 0x10 (seed 0xC0FFEE), queued back-to-back, `m_ready = 1`.
- *Checks*: 5 packets; `check_packet` on each (tags 0–4).
- *Proves*: the consumer fold schedule (ST_HDR idx 2–5, then ST_DATA), the 1-clock hand-off into ST_CRC, and the reseed between queued packets.

#### test_03_packet_tag_wrap
- *Stimulus*: `cfg_dsizeP = 1`, 257 packets on SID 0xA5 (seed 0xBEEF), `m_ready = 1`.
- *Checks*: 257 packets. Per packet: the tag byte equals `i & 0xFF`, the tag word is 4× replicated, and `check_packet` passes.
- *Proves*: 257 consecutive reseeds with 5 folds each. Tag bytes 0x00–0xFF all pass through the fold.

#### test_05_dsizeP_truncation
- *Stimulus*: `cfg_dsizeP = 4`. A 7-word packet (0x11111111 … 0x77777777), then a 4-word packet, both on SID 0x11.
- *Checks*: 2 packets. `check_packet(first 4 words, tag 0)`; `check_packet(follow-up, tag 1)`.
- *Proves*: words dropped in ST_DRAIN are not folded, because `crc_din_valid` fires only on `data_in_fire`. The next SOP reseeds.

#### test_06_backpressure
- *Stimulus*: `cfg_dsizeP = 6`, 3 packets on SID 0x77 (seed 0x5A5A). `m_ready = 0` on each cycle with p = 0.30 (seed 0xDEADBEEF).
- *Checks*: 3 packets; `check_packet` on each.
- *Proves*:
  - The header fold is gated by `out_fire`.
  - The data fold is gated by `data_in_fire = s_valid & m_ready`.
  - `crc_o` holds while ST_CRC is stalled: packet tag 2 stalls 1 cycle in ST_CRC and `crc_o` stays 0xC7BD2A91.

```wavedrom
{"signal":[
  {"name":"tx_clk","wave":"p.........|."},
  {"name":"state_q","wave":"==.=.===..|=","data":["HDR1","HDR2","HDR3","HDR4","HDR5","DATA","CRC"]},
  {"name":"m_ready","wave":"10101..01.|1"},
  {"name":"m_data","wave":"==.=.===.=|=","data":["01×4","77×4","01×4","00×4","06×4","092b131f","e8818113","bswap32(crc_o)"],"node":"...........a"},
  {"name":"crc_din_valid","wave":"0.101..01.|0"},
  {"name":"crc_o","wave":"=..=.===.=|=","data":["00000000","280d3242","60cfa466","e0a828a6","27117bd9","cdd927e8","c1dc8f73"]}
],
"head":{"text":"packet tag 1, from the run's VCD; a = check_packet CRC compare"}}
```

#### test_09_single_data_word
- *Stimulus*: `cfg_dsizeP = 1`; packets [0xF00DBABE] and [0x01234567] on SID 0x33, `m_ready = 1`.
- *Checks*: 2 packets; `check_packet` on each.
- *Proves*: the last fold (ST_DATA) reaches `m_data` in ST_CRC on the next cycle. All integration tests pin this 1-clock latency; this is the shortest case. The unit tests sample one cycle late.

```wavedrom
{"signal":[
  {"name":"tx_clk","wave":"p.........."},
  {"name":"state_q","wave":"===========","data":["IDLE","HDR0","HDR1","HDR2","HDR3","HDR4","HDR5","DATA","CRC","EOP","IDLE"]},
  {"name":"crc_init","wave":"10........."},
  {"name":"crc_din_valid","wave":"0..1....0.."},
  {"name":"crc_o","wave":"x=..=====..","data":["00000000","+SID","+Tag","+DsizeP_H","+DsizeP_L","9e4b1096"]},
  {"name":"m_data","wave":"x=========x","data":["FB×4","01×4","33×4","00×4","00×4","01×4","f00dbabe","96104b9e","FD×4"],"node":"........a.."}
],
"head":{"text":"first packet, from the run's VCD; a = CRC beat = bswap32(crc_o)"}}
```

#### test_11_dsizeP_under_supply
- *Stimulus*: `cfg_dsizeP = 8`. A 3-word packet with `s_eop` on word 3, then an 8-word packet, both on SID 0x55.
- *Checks*: 2 packets. `check_packet(short packet, header DsizeP = 8, 3 data words, tag 0)`; `check_packet(follow-up, tag 1)`.
- *Proves*: an early `s_eop` takes ST_DATA → ST_CRC, and the CRC covers the 3 words sent with no padding. This is a design decision: on the wire the packet is non-conformant, because DsizeP overstates N.

#### test_12_suppress_abandons_mid_data_and_resyncs
- *Stimulus*: `cfg_dsizeP = 8`; packets A (`0xA0000000|i`) and B (`0xB0000000|i`) on SID 0x55.
  1. Capture 9 beats: 6 header beats plus A0..A2.
  2. Hold `suppress_stream = 1` for 20 cycles.
  3. Release it and run for up to 120 cycles.
- *Checks*: phase 1 is non-empty, starts with SOP and has no EOP; phase 2 produces 0 beats; phase 3 produces exactly 1 packet; `check_packet(B, tag 0)`.
- *Proves*: `crc_init` on B's SOP, after suppress falls, discards the abandoned accumulator. The stray A3 fold (How it works item 6) has no effect.

```wavedrom
{"signal":[
  {"name":"tx_clk","wave":"p..|...|.."},
  {"name":"suppress_stream","wave":"01.|.0.|.."},
  {"name":"state_q","wave":"=.=|..=|==","data":["DATA","IDLE","HDR0","CRC","EOP"]},
  {"name":"s_data","wave":"===|=..|=.","data":["A2","A3","A4","B0 (sop)","0"]},
  {"name":"s_ready","wave":"1..|0..|.."},
  {"name":"crc_init","wave":"0..|.10|.."},
  {"name":"crc_din_valid","wave":"1.0|...|.."},
  {"name":"crc_o","wave":"===|..=|=.","data":["9d5908d9","ed131a30","bbe9d24a","00000000","1e5dd2e5"]},
  {"name":"m_valid","wave":"10.|..1|..","node":"........a."}
],
"head":{"text":"from the run's VCD; A3 folded while suppressed; a = CRC beat of B (e5d25d1e)"}}
```

#### test_13_suppress_mid_header_replays_header_from_idx0
- *Stimulus*: `cfg_dsizeP = 4`; packets A and B on SID 0x77.
  1. Capture 3 header beats (the third one folds SID).
  2. Suppress for 12 cycles.
  3. Release and run for up to 160 cycles.
- *Checks*: at least 3 phase-1 beats, the first being SOP; 0 beats while suppressed; 2 packets; `check_packet(A replay, tag 0)`; `check_packet(B, tag 1)`.
- *Proves*: nothing is folded while ST_HDR is suppressed, because `out_fire = 0` when `m_valid` is muted. The replay reseeds through `crc_init`.

### Other

- `src/tb_unit/tx/cxp_tx_ctrl_ack` (14 tests, 14/14 run): test_08_crc32_random (N ∈ {0, 1, 2, 7, 12}) and every data-ack test compare the CRC word against `cxp_protocol.crc`. test_11_backpressure stalls the fold path.
- `src/tb_unit/ctrl/cxp_ctrl_cmd_parser` (6 tests, 6/6 run):
  - test_04_crc_error flips CRC bit 0 and expects `cmd_crc_err_pulse` (the wrapper's decode of a record with `err` 0x80) with no `cmd_valid`. test_01/02/03/06 exercise the pass compare.
  - The TB and RTL fold 8 byte-replicated header words. Table 21 instead packs Cmd+Size into word 0 and Addr into word 1, so the §8.2.2.2 worked-example packet cannot be replayed through this RX.
- `src/tb_unit/top/cxp_stream_top`: its own `check_packet` uses the same CRC reference. Not run.
- `src/tb_unit/rx/cxp_rx_link`: builds command packets with the same reference. Not run.
- `src/tb_unit/top/cxp_interface_top`: instantiates all three consumers but has no CRC-specific assertion. Not run.
- `src/emu/cxp/protocol/crc.py` (wraps `cxp_protocol.crc`) and `src/verif/uvm/common/cxp_pkg.py` (its own codec, zlib without the final XOR): host-side models in the §8.2.2.2 convention. `src/emu/bridge/Makefile` and `src/verif/Makefile` compile `cxp_lib_crc32.sv`.

### Running

```
make -C src/tb_unit/lib/cxp_lib_crc32                                             # unit TB, Verilator
make -C src/tb_unit/lib/cxp_lib_crc32 COCOTB_TEST_FILTER=test_08_init_and_data_same_cycle
make -C src/tb_unit/tx/cxp_tx_stream_pkt                                     # integration TB
make -C src/tb_unit                                                       # full regression + report
```

Results from 2026-09-14 at commit `bbd6372` (RTL and TB files unmodified), Verilator 5.046:

| TB | Result |
|---|---|
| cxp_lib_crc32 | 15/15 |
| cxp_tx_stream_pkt | 14/14 |
| cxp_tx_ctrl_ack | 14/14 |
| cxp_ctrl_cmd_parser | 6/6 |

Full regression not re-run. If you change `WAVES`, run `make clean` first, because the sim_build directory goes stale.

### Not covered in-tree

- **K28.3 inside the stream payload** (`s_kmask ≠ 0`): untested, because `PktTxDriver` always drives `s_kmask = 0` → Medium 1.
- **Non-contiguous `din_be_i`** (e.g. 0x5): untested. It currently folds P0 then P2, matching zlib of those two bytes (probe, not in repo). Intent undecided → Medium 2.
- **`p_IN_W` ∉ {8, 32}:** untested. A width that is not a multiple of 8 passes lint → Minor 1.
- **X-propagation on `init_i`/`din_valid_i`:** untested; an X would poison `crc_q` until the next init or reset. The consumer drive terms decode reset FSM state, so X can only arrive from an un-reset upstream `s_valid`/`s_sop` → SVA in Minor 3.
- **Async reset between edges:** shown only by an out-of-repo probe → Minor 4.
- **Reset with `din_valid_i = 1`:** untested. Reset overrides everything by construction (no item).
- **Input active at reset release:** untested. The first edge after release folds or reseeds normally, and the consumer FSMs are in ST_IDLE at release (no item).
- **Counter saturation/wrap:** not applicable; there is no counter, and the longest fold in-tree is 1 KiB.
- **Indefinite stall:** tested up to 4 idle cycles (unit TB) and random `m_ready` (integration TB). The hold is by construction (no item).
- **Forced idle mid-transaction:** covered by the two suppress tests.
- **Multi-clock operation:** not applicable (one clock per instance).

## Known issues and recommendations

### Critical

None.

### Medium

1. **K28.3-as-D28.3 is untested.** It holds only by construction (How it works item 5). Add a `cxp_tx_stream_pkt` test with a 4×K28.3 marker word (`s_kmask = 0xF`) in the payload and check the CRC against the D28.3 fold. Effort: 1 h.
2. **The `p_IN_W = 8` variant and `din_be_i` have no in-tree user** (all instances are 32-bit with BE tied to `4'b1111`), and only contiguous low-side masks are tested. Either remove them, or define the rule for non-contiguous masks and add a test. Effort: 1 h.

### Minor

1. **Width check runs only at sim time.** Replace the `initial $error` under `lint_off WIDTH` with an elaboration-time generate `if ((p_IN_W <= 0) || (p_IN_W % 8 != 0)) $error(...)`. Effort: 15 min.
2. **Stale citations.** Some TB docstrings cite v1.0 numbering (§6.2.1, §6.5.1, "table 18"); the v1.1.1 references are §8.2.1, §8.5.1 and Table 19. Effort: 15 min.
3. **Add SVA.** Assert `!$isunknown({init_i, din_valid_i})` out of reset and `init_i && !din_valid_i |=> crc_o == CRC_SEED`; cover `init_i && din_valid_i`. Effort: 30 min.
4. **Weak unit tests.** Effort: 1 h.
   - test_02_empty_init_is_zero and test_11_zero_payload_is_seed: fold a data beat before the seed.
   - test_14_reset_clears_partial: sample `w_crc` before the next edge.
   - test_09_back_to_back_packets: co-assert `init` with the next packet's first beat.
   - All tests: sample directly after the last beat, not one idle cycle later.

### Open questions

1. **Designer:** should `cxp_ctrl_cmd_parser` check by residue (fold the received CRC word and test `crc_q == 0`, as the §8.2.2.2 comment describes) instead of regenerating and comparing?
2. **Designer:** should `p_IN_W = 8` and `din_be_i` stay, given there is no in-tree user? If they stay, are non-contiguous masks legal?

Repository change: added this file (`docs/design/modules/lib/cxp_lib_crc32.md`) only.
