# cxp_rx_8b10b_decoder

Inputs chosen from the tree: RTL `src/rtl/rx/cxp_rx_8b10b_decoder.sv` (+ `cxp_util_pkg.sv` for `bitrev4/6`); unit TB `src/tb_unit/rx/cxp_rx_8b10b_decoder/`; integration TB `src/tb_unit/rx/cxp_rx_link/` (the only instantiator is `cxp_rx_link`); spec JIIA CXP-001-2015 v1.1.1 (`docs/spec/CXP-001-2015.pdf`) §2.1 (8B/10B normative reference), §8.2.1 (Table 11, Figure 15 bit order); the code set itself is IEEE 802.3 Clause 36 Tables 36-1a/b and 36-2, which the RTL header cites; regression `make -C src/tb_unit`; output `docs/design/modules/rx/cxp_rx_8b10b_decoder.md`.

A combinational single-character 8B/10B decoder: 10-bit symbol plus entering running disparity (RD) in, decoded byte, K flag, disparity-error and code-error flags, and leaving RD out. Four instances are chained per word in `cxp_rx_link`.

| `din_i` bit | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|---|
| Label | a | b | c | d | e | i | f | g | h | j |
| Sub-block | 6b (`sub6`, bit-reversed) | | | | | | 4b (`sub4`, bit-reversed) | | | |
| Decodes to | x = `dout_o[4:0]` | | | | | | y = `dout_o[7:5]` | | | |

Source: `src/rtl/rx/cxp_rx_8b10b_decoder.sv`. Instantiated 4× in `cxp_rx_link` (`cxp_rx_8b10b_decoder_i0..i3`) on `sym_in[10n+9:10n]` from `cxp_rx_lspd_sampler` (K28.5 in lane P0); lane 0 takes `rd_in_i = rd_q`, each lane's `rd_out_o` feeds the next, and lane 3's `rd_out_o` is registered back into `rd_q`. The four `dout_o`/`k_out_o` are registered into `d_reg_q`/`k_reg_q` for `cxp_rx_packet_parser`; the error flags are registered and ORed into `rx_code_err_pulse_o`/`rx_disp_err_pulse_o`, exported by `cxp_interface_top` as `sb_rx_code_err_pulse`/`sb_rx_disp_err_pulse`.

Spec clauses: §8.2.1 (8B/10B per §2.1; Figure 15 "a first" bit order; Table 11 K-codes). The K-code set accepted here is the IEEE one (12 codes), a superset of Table 11 (8 codes).

## Interface

Parameters: none. Internal `pol_t` enum (`POL_NEUT/POL_MIN/POL_PLU`) labels each sub-block's intrinsic disparity.

| Name | Dir | Width | Description |
|---|---|---|---|
| `din_i` | in | 10 | Symbol, bit 0 = `a` (first on the wire) |
| `rd_in_i` | in | 1 | RD entering the symbol, 0 = RD−, 1 = RD+ |
| `dout_o` | out | 8 | Decoded byte HGFEDCBA; 0x00 when `code_err_o` |
| `k_out_o` | out | 1 | 1 = K-character; 0 when `code_err_o` |
| `disp_err_o` | out | 1 | A non-neutral sub-block entered at the same-sign RD |
| `code_err_o` | out | 1 | 6b or 4b pattern not in the table |
| `rd_out_o` | out | 1 | RD leaving the symbol, for chaining |

Notes:

- No clock, reset or state; latency 0. The caller owns the RD register (`cxp_rx_link.rd_q`: asynchronous active-low `rx_rst_n`, resets to RD−, seeded from the first K28.5 after lock, loads only on `accept_sym = sym_valid & rx_lock`). Single `rx_clk` domain, no CDC.
- No handshake or stall. In `cxp_rx_link` the 4-lane RD chain is a serial combinational path `rd_q` → lane 0 → … → lane 3 → `rd_q`, completed in one `rx_clk` (not timing-analysed).
- Header mismatch: "code error … `rd_out_o` forced to unchanged-RD" holds only when both sub-blocks are unknown. With one valid non-neutral sub-block the RD still moves (184 of the 704 code-error inputs, see How it works).
- `disp_err_o` is not masked by `code_err_o`; both can assert together.
- `verilator --lint-only -Wall` on the file plus `cxp_util_pkg.sv`: only `TIMESCALEMOD` (the package has no `timescale`, the module does), waived in the builds.

## How it works

1. **Bit reverse.** `sub6 = bitrev6(din_i[5:0])`, `sub4 = bitrev4(din_i[9:6])`, so the case literals read as the IEEE strings `abcdei` / `fghj`.
2. **5b/6b lookup** (`always_comb`, 52 patterns): value `dec5`, polarity `pol5` (`POL_MIN` = the +2 form used when entering at RD−, `POL_PLU` = the −2 form used at RD+, `POL_NEUT` = disparity 0), `k28_marker` for `001111`/`110000`. Unknown pattern → `dec5_err`.
3. **3b/4b lookup** (14 patterns): `dec3`, `pol3`; `0111`/`1000` set `k_y7_alt` (the A7 / Kx.7 form). Unknown → `dec3_err`. `rd_mid` is `rd_in_i` through a neutral 6b, else the sign the 6b block leaves; `rd_out_o` likewise through the 4b block.
4. **Assembly.** `disp_err_o` = a `POL_MIN` block entered at RD+ or a `POL_PLU` block entered at RD−, for either sub-block. `code_err_o = dec5_err | dec3_err | y7_err`. K28.{1,2,5,6} at RD+ use the complemented 4b form, which aliases `dec3` to `7 − y`; `dec3_corr` undoes it when `sub6 = 110000`, `k28_marker` and `sub4 ∈ {0110,1010,0101,1001}`. `k_out_o = k28_marker | (k_y7_alt & x ∈ {23,27,29,30})`. **y = 7 check** (IEEE 802.3 Table 36-1): `a7_due` = x ∈ {17,18,20} after RD− or x ∈ {11,13,14} after RD+ (`rd_mid`); `y7_err` = an A7 4b (`0111`/`1000`) that is neither K28.7, a Kx.7 nor a due D.x.A7, or a P7 4b (`1110`/`0001`) where A7 is due or after a K28 6b block.

No FSM, no counters, no same-cycle rules. Throughput 1 symbol per evaluation; `cxp_rx_link` decodes 4 per `rx_clk`.

Exhaustive behaviour over all 2048 `{din_i, rd_in_i}` inputs against an independent IEEE Table 36-1/36-2 model (the flag split below is from the earlier probe; the legal and silent rows are test_08):

| Input class | Count | Decoder result |
|---|---|---|
| Legal code-group at its RD (256 D + 12 K, × 2) | 536 | byte, K flag and `rd_out_o` correct, no flags |
| Legal code-group at the other RD | 346 | `disp_err_o`, byte and K flag still correct |
| Legal code-group at the other RD, RD-dependent neutral forms (D.7 `111000`/`000111`, D.x.3 `1100`/`0011`) | 46 | `disp_err_o` (the neutral form is checked against the entering RD) |
| Invalid code-group | 520 | `code_err_o`, `rd_out_o = rd_in_i` |
| Invalid code-group | 184 | `code_err_o`, `rd_out_o` moved |
| Invalid code-group | 318 | `disp_err_o` only |
| Invalid code-group, silent | 0 | (58 before 2026-09-26, all y = 7 pairings: A7 on another data x, P7 where A7 is due, P7 after K28; now `code_err_o`) |

Invariants by construction, not asserted: `code_err_o` ⇒ `dout_o = 0 ∧ k_out_o = 0`; `k28_rdp_form` ⇒ `k28_marker`; a non-neutral sub-block always sets the leaving RD to its own sign, so the RD chain re-locks within one non-neutral sub-block after any error.

## Arbiter integration

Not applicable: the decoder is on the receive path. The only "arbitration" is the RD chain in `cxp_rx_link`, which advances only on `accept_sym`; while the sampler is unlocked the decoders still evaluate but nothing is registered.

## Verification

Verilator 5.046, cocotb 2.0.1, `cxp_test` wrapper; no SVA; no FSM coverage (no FSM). Reference model: the golden `cxp_protocol.enc8b10b` (repo root, via `src/verif/common/cxp_8b10b.py`): IEEE 802.3 tables with the D.x.A7 rule and a table-inverse `decode_symbol`, tested against IEEE vectors, not against this RTL. (Until 2026-09-19 the model mirrored the retired `cxp_8b10b_encoder.sv` and its P7-only y = 7 rule.)

### Unit TB — `src/tb_unit/rx/cxp_rx_8b10b_decoder/`

Wrapper `tb_cxp_rx_8b10b_decoder_top.sv` (ports without `_i/_o`, `TESTCASE` register, a free-running `tb_clk` the DUT does not use). No reset. `apply()` writes `din`/`rd_in` and waits 2 ns before sampling. Shared checkers: none beyond the inline asserts.

| Test | Stimulus | Expect |
|---|---|---|
| test_01_d_code_roundtrip | 256 bytes × 2 RD through `encode_byte` | byte, `rd_out`, no flags, `k_out = 0` |
| test_02_k_codes | 12 K bytes × 2 RD | byte, `k_out = 1`, `rd_out`, no flags |
| test_03_rd_chain | 8-symbol sequence, RD carried forward | byte and `rd_out` each step |
| test_04_code_error | sub6 = `000000` and `111111` | `code_err = 1` |
| test_05_disparity_error | D.0 RD− form at RD+ | `disp_err = 1`, `dout = 0x00` |
| test_06_neutral_rd_forms | D.7 / D.x.3 forms at the wrong RD | `disp_err = 1`; at the right RD 0 |
| test_08_exhaustive_2048 | all 1024 symbols at both RD against Tables 36-1 / 36-2 typed in the bench (not the golden encoder) | the 536 legal pairs exact (byte, K, `rd_out`, no flag); every other pair `code_err` or `disp_err`. Red before the y = 7 check: 58 illegal pairs silent |

#### test_01_d_code_roundtrip

- *Stimulus*: for every byte 0..255 and `rd ∈ {0,1}`: `sym, rd_exp = encode_byte(byte, k_flag=False, rd_in=rd)`; apply. 512 vectors.
- *Checks*, per vector: `code_err = 0`; `disp_err = 0`; `k_out = 0`; `dout = byte`; `rd_out = rd_exp`.
- *Proves*: every D row of both lookups and the RD chain for legal data, including the six mandatory D.x.A7 forms (0xF1/0xF2/0xF4 at RD−, 0xEB/0xED/0xEE at RD+), which the golden encoder emits.

#### test_02_k_codes

- *Stimulus*: K bytes 0x1C, 0x3C, 0x5C, 0x7C, 0x9C, 0xBC, 0xDC, 0xFC, 0xF7, 0xFB, 0xFD, 0xFE, × 2 RD, encoded with `k_flag=True`. 24 vectors.
- *Checks*: `code_err = 0`; `disp_err = 0`; `k_out = 1`; `dout = kbyte`; `rd_out = rd_exp`.
- *Proves*: `k28_marker`, the K28 RD+ y-alias correction (K28.1/2/5/6), and the Kx.7 alternate forms for x ∈ {23,27,29,30}. Includes K28.7 (0xFC), which the retired `cxp_8b10b_encoder.sv` rejected but the Python model encodes.

#### test_03_rd_chain

- *Stimulus*: bytes 0x00, 0xBC(K), 0xFF, 0xAA, 0x55, 0x7C(K), 0x1C(K), 0xFD(K), each encoded with the previous `rd_exp`, starting at RD−.
- *Checks*: `dout` and `rd_out` after each symbol.
- *Proves*: `rd_out_o` is usable as the next `rd_in_i`; the sequence crosses both RD signs.

#### test_04_code_error

- *Stimulus*: `din = 0b0000_001111` (sub6 = `000000`) and `0b0001_111111` (sub6 = `111111`), `rd_in = 0`.
- *Checks*: `code_err = 1` for both.
- *Proves*: `dec5_err`. `dec3_err`, `dout = 0` and `k_out = 0` on error are not checked.

#### test_05_disparity_error

- *Stimulus*: D.0.0 encoded at RD− (`100111 0100`: 6b is the +2 form, 4b the −2 form), applied with `rd_in = 1`.
- *Checks*: `disp_err = 1`; `dout = 0x00`.
- *Proves*: the `POL_MIN`-at-RD+ term of `disp_err_o` on the 6b block (the 4b block is legal at `rd_mid` = RD+) and that data still decodes. The `POL_PLU` at RD− term and the 4b-only terms are untested in-tree (covered by the probe: 346/346 flagged).

### Integration TB — `src/tb_unit/rx/cxp_rx_link/`

19 tests on the real `cxp_rx_link` (serial bitstream → sampler → 4 decoders → link monitor → parser). Every test except test_01 passes symbols through the decoders; test_10 reads `rx_code_err_pulse` for one invalid symbol, tests 13 and 20 require no decode error across Table 15 triggers inside commands. The trigger receiver decodes the Table 15 Delay characters with six more instances (both RDs, `cxp_rx_trigger_lspd.md`).

| Test | Checks |
|---|---|
| test_02_link_lock | `rx_lock`, then `link_detected` on an IDLE stream (K28.5/K28.1/D21.5 decoded for the link monitor) |
| test_04_trigger | `trigger_out_app` after a Table 15 trigger |
| test_05_ctrl_read | `rsp_code = 0x00`, `rbuf[0] = 0xDEADBEEF` after a control read (SOP/type/CRC/EOP all decoded) |
| test_06_ctrl_reset_op | `ctrl_reset_pulse`, `rsp_code = 0x03` |
| test_07_trig_pkt_rcvd_for_ioack | one `trig_pkt_rcvd` strobe, no `trigger_out_app` |

#### test_02_link_lock

- *Stimulus*: 30 IDLE words as a serial bitstream from `cxp_8b10b.py`.
- *Checks*: `rx_lock` within 4000 cycles, `link_detected` within 2000 more.
- *Proves*: IDLE words decode to the K28.5/K28.1/K28.1/D21.5 pattern the link monitor and parser need; RD alternates across IDLE words.

#### test_04_trigger, test_07_trig_pkt_rcvd_for_ioack

- *Stimulus*: 20 IDLE words, a Table 15 trigger (Delay 2) inside the next IDLE word, 30–40 IDLE words; polarity 0 (test_04) or 1 (test_07).
- *Checks*: `trigger_out_app` fires (test_04); exactly one `trig_pkt_rcvd` strobe and no `trigger_out_app` (test_07).
- *Proves*: the Delay characters decode in the trigger receiver's instances; the words around the trigger decode without error.

#### test_05_ctrl_read, test_06_ctrl_reset_op

- *Stimulus*: a CRC-correct control packet (read 4 bytes at 0x42, or op 0xFF) between IDLE words; an APB slave model in test_05.
- *Checks*: `rsp_code` 0x00 / 0x03; `rbuf[0]` (test_05); `ctrl_reset_pulse` (test_06).
- *Proves*: K27.7/K29.7 and a payload including the CRC bytes decode correctly across all four lanes. Asserts nothing about the error pulses.

### Other

- `src/tb_unit/rx/cxp_rx_lspd_sampler/` uses `cxp_pkg::is_k28_5` on raw 10-bit symbols; they do not instantiate the decoder.
- `docs/archive/retired_modules/cxp_8b10b_encoder.md` (encoder retired 2026-09-19) records that all 534 inputs the encoder accepts round-trip through this decoder and that the decoder accepts both y = 7 forms; this document's exhaustive table extends that to all 2048 inputs.
- `src/verif/` PyUVM env compiles `cxp_rx_link` and drives uplink symbols through `host_uplink_agent.py` (same Python encoder); not run here.
- `src/emu/bridge/Makefile` compiles it; no emulation test targets it.

### Running

```
make -C src/tb_unit/rx/cxp_rx_8b10b_decoder
COCOTB_TEST_FILTER=test_05 make -C src/tb_unit/rx/cxp_rx_8b10b_decoder
make -C src/tb_unit/rx/cxp_rx_link
make -C src/tb_unit
```

2026-09-26: unit TB 7/7 pass; `cxp_rx_link` 19/19.

### Not covered in-tree

- Kx.7 for x ∈ {23,27,29,30} at both RDs: covered in test_02 only via the encoder. (The six D.x.A7 rows are applied by test_01 since the golden encoder landed.)
- Wrong-RD detection beyond one vector: the `POL_PLU`-at-RD− term, the two 4b terms, and the 46 silent RD-dependent neutral forms (currently no flag — intent undecided).
- Illegal 4b sub-blocks (`0000`, `1111`) → `dec3_err`; only 6b errors tested.
- `dout_o = 0` / `k_out_o = 0` on `code_err_o`; `rd_out_o` behaviour on `code_err_o` (currently moves in 184 cases, header says held).
- Reset during activity, input active at reset release: the module has no state; `cxp_rx_link` seeds its RD register from the first K28.5 after lock, so the host's RD at lock does not matter.
- Counter saturation/wrap, indefinite stall, forced idle, multi-clock: not applicable (combinational, single domain).
- End-to-end: no TB injects a bit error and checks `rx_code_err_pulse`/`rx_disp_err_pulse`.
- X-propagation: not tested; pure lookup, X in gives X out.

## Known issues and recommendations

### Critical

None. All 536 legal inputs decode correctly and the RD chain is exact (probe not in repo).

### Medium

1. **Resolved: invalid y = 7 code-groups were accepted silently.** 58 illegal (symbol, RD) pairs decoded as data with no flag (A7 on another data x, P7 where A7 is due, P7 after K28); `y7_err` now flags them (`code_err_o`). Proven by test_08 (red before, 2048 pairs exact now).
2. **Error pulses are never observed above the unit level.** Add an rx_top test that flips one bit inside a data character and one inside the IDLE K28.5, and asserts `rx_disp_err_pulse` / `rx_code_err_pulse` fire once while `rsp`/trigger outputs stay quiet. Effort: 2 h.

### Minor

- Header text "rd_out_o … unchanged-RD" on code error is wrong for 184 inputs; either hold `rd_out_o = rd_in_i` whenever `code_err_o` (one mux) or fix the comment.
- The accepted K set (12 IEEE codes) exceeds Table 11 (8). K28.0, K28.7, K23.7 and K30.7 reach `cxp_rx_packet_parser` with `k_out = 1`; it is not documented what the parser does with them. Same as the retired `cxp_8b10b_encoder.md` Open question 2.
- Add `timescale` to `cxp_util_pkg.sv` (or a `TIMESCALEMOD` waiver) so a standalone lint is clean.
- test_05 comment says "D.0 … polarity 'P'"; the RTL name for the +2 RD− form is `POL_MIN`. Align the wording.
- SVA to bind: `code_err_o |-> dout_o == 0 && !k_out_o`; popcount(`din_i`) ∈ {4,5,6} when no `code_err_o`; `rd_out_o` equals the sign of the last non-neutral sub-block.

### Open questions

1. Spec owner: same as the encoder document — restrict K decoding to Table 11, or keep the IEEE set and let the parser reject the extras?
2. ~~Verification: should `cxp_8b10b.py` grow a `decode_symbol` written from IEEE tables?~~ Done: `cxp_protocol.enc8b10b.decode_symbol` (2026-09-19).

