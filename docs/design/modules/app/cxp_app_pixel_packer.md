# cxp_app_pixel_packer

Packs a one-pixel-per-cycle stream into 32-bit P0..P3 words for the frame's pixel format. Each line starts in P0 and its last word is flushed with zero padding (§9.4.2).

| PixelF decoded | Container | Words per line (n px) | §9.4.2 | Status |
|---|---|---|---|---|
| 0x0101 Mono8 | 8 bit | ⌈n/4⌉ | Figure 27 | matches |
| 0x0102 Mono10 | 10 bit | ⌈10n/32⌉ | Figure 28 | matches |
| 0x0103 Mono12 | 12 bit | ⌈12n/32⌉ | Figure 29 | matches |
| 0x0104 Mono14 | 14 bit | ⌈14n/32⌉ | Figure 30 | matches |
| 0x0105 Mono16 | 16 bit | ⌈n/2⌉ | Figure 31 | matches |
| any other code | low 8 bits | ⌈n/4⌉ | — | silent fallback (Critical 1) |

Bits are packed MSB-first: pixel 0 bit (bpp−1) is word bit 31, and P0 = `m_word_data_o[31:24]`. Source: `src/rtl/app/cxp_app_pixel_packer.sv`. There is one instance, `cxp_interface_top.cxp_app_pixel_packer_i`, fed by the TPG/ingress mux through the acquisition gate `cxp_app_acq_ctrl` (whole images only), with `cfg_pixfmt_i = sel_meta.pixfmt`. Its output is `bswap32`'d, so P0 lands in word[7:0], the first lane on the wire. It then goes to `cxp_app_stream`'s pixel-word port, and `m_word_eof_o` closes the image's last stream packet. The accepted SOF and SOL words form `pix_frame_start`/`pix_line_start`; `m_pixfmt_o` is the image header's PixelF and `m_meta_o` the rest of its metadata. `tb_cxp_stream_top` has a second instance. `cxp_app_pixel_ingress.sv` and `cxp_app_tpg.sv` only name the module in comments. Implements §9.4.1.1 Table 25 (Mono subset) and §9.4.2 Figures 27–31.

## Interface

| Name | Default | Meaning |
|---|---|---|
| (none) | — | No parameters. Localparam `ACC_W` = 64 is the accumulator width; the worst-case fill before an emit is 31 + 16 = 47 bits. |

| Name | Dir | Width | Description |
|---|---|---|---|
| `app_clk` | in | 1 | Pixel clock; the only clock. |
| `app_rst_n` | in | 1 | Active-low reset, asserted asynchronously. |
| `cfg_pixfmt_i` | in | 16 | PixelF code. Used live for the SOF pixel, latched on its accept, held for the frame. |
| `s_pix_data_i` | in | 16 | Pixel value, LSB-justified. |
| `s_pix_w_i` | in | 5 | Bits of the sample (latched with the SOF pixel). 0: the value is taken at the format's width (`[bpp−1:0]`, the TPG). 1..16: a sensor sample of that width, MSB-aligned into the format (§9.4.2 Figure 32): a wider format zero-fills the LSBs, a narrower one keeps the MSBs (Mono8 from 12 bits = `[11:4]`). `cxp_app_domain` passes `p_PIX_W` for the sensor, 0 for the TPG. |
| `s_pix_valid_i` | in | 1 | Pixel valid. |
| `s_pix_sol_i`, `s_pix_eol_i` | in | 1 each | First / last pixel of a line. |
| `s_pix_sof_i`, `s_pix_eof_i` | in | 1 each | First / last pixel of a frame. EOF is honoured only together with EOL. |
| `s_pix_ready_o` | out | 1 | `(~m_word_valid_q \| m_word_ready_i) & ~flush_pend_q`, combinational. |
| `m_word_data_o` | out | 32 | Packed word, P0 in [31:24]. |
| `m_word_lane_vld_o` | out | 4 | Bytes touched: bit 0 = P0. `4'b1111` except on a line's last word. |
| `m_word_valid_o` | out | 1 | Word valid, registered. |
| `m_word_sol_o`, `m_word_sof_o` | out | 1 each | On the first word of the line / frame. |
| `m_word_eol_o`, `m_word_eof_o` | out | 1 each | On the last word of the line / frame. |
| `m_word_ready_i` | in | 1 | Downstream accept. |
| `m_pixfmt_o` | out | 16 | `pixfmt_latched_q`: the format of the frame being packed, latched at its SOF pixel. `cxp_interface_top` puts it in the image header (PixelF). |
| `s_meta_i` | in | `cxp_meta_t` | The image's metadata (the TPG's, or the ingress's latched copy), sampled in the SOF-pixel accept cycle. |
| `m_meta_o` | out | `cxp_meta_t` | `meta_latched_q`: that metadata, held for the frame being packed. `cxp_interface_top` gives it to the image header and line markers. |

Notes:
- **Reset:** every register is in the async-reset branch of the single `always_ff`; `pixfmt_latched_q` resets to 0x0101. The port comment "sync-deassert reset" describes the integration's duty; nothing inside synchronises the release.
- **Clocking:** single domain. In `cxp_interface_top`, `cfg_pixfmt_i` is `sel_meta.pixfmt`: the TPG's own latched format, or on the sensor branch `ext_meta_pixfmt` overridden by a non-zero PixelFormat register (`app_clk` copy, through `cxp_cdc_bus` when `p_ASYNC_CLOCKS` = 1). It is sampled only in the SOF-pixel accept cycle.
- **Registration:** all `m_word_*` outputs come from a 1-deep output register. `s_pix_ready_o` depends combinationally on `m_word_ready_i`, so the ready path runs from `cxp_app_stream` through the packer and the ingress to the sensor pins.
- **Stall:** while a word is held and `m_word_ready_i` = 0, no pixel is accepted, even one that would only accumulate. The held word is stable, since the output register is written only when the slot is free.
- **Fixed 2026-10-04 (review TX-02):** sensor pixels used to be taken from the low `bpp` bits, so Mono8 on a 12-bit sensor sent the low byte. With `s_pix_w_i` the sample is MSB-aligned into the format (test_19).

## How it works

1. **Format decode.** `pixfmt_eff = (s_pix_valid_i & s_pix_sof_i) ? cfg_pixfmt_i : pixfmt_latched_q`. Five exact codes select `cbits` of 8/10/12/14/16 and an MSB-aligned 16-bit container; the next pixel is ORed in `cbits` bits later, so Mono14 packs back to back. Every other code takes the Mono8 branch (`default`). A `cfg_pixfmt_i` change inside a frame has no effect (observed, not in repo).
2. **Shift accumulator.** `acc_q` (64 bit) holds `fill_q` valid bits at the top. An accepted pixel ORs its container in at bit `63 − fill_q` and adds `cbits`. The bits below `fill_q` are always 0, so padding is 0 as §9.4.2 requires. Per accepted pixel, one of five cases applies:

| Case | Condition (`fill_post = fill_q + cbits`) | Output register | Accumulator |
|---|---|---|---|
| A | `fill_post ≥ 32`, EOL, no residual | top 32 bits, `lane_vld` 1111, EOL/EOF | cleared |
| B | `fill_post > 32`, EOL | top 32 bits, no EOL | cleared; residual word queued in `flush_*_q`, `flush_pend_q` = 1 |
| C | `fill_post ≥ 32`, not EOL | top 32 bits | shifted left 32 |
| D | `fill_post < 32`, EOL | partial word, `lane_vld` = ⌈fill/8⌉ ones, EOL/EOF | cleared |
| E | `fill_post < 32`, not EOL | none | accumulate; SOL/SOF saved in `sol_pending_q`/`sof_pending_q` |

3. **Flush slot.** In case B the residual word drains on the next free output cycle, with EOL/EOF and no SOL/SOF. `flush_pend_q` holds `s_pix_ready_o` low for that cycle.
4. **Flag carry.** SOL/SOF go on the first word emitted after the flagged pixel (`eff_sol = sol_pending_q | s_pix_sol_i`). EOL/EOF go on the line's last word. EOF without EOL is dropped.
5. **SOF starts empty.** On a SOF pixel the pixel lands in an empty accumulator (`acc_base` = 0, `fill_base` = 0) instead of `acc_q`/`fill_q`. Bits of a line that was cut off without its EOL (a truncated frame) are dropped, so the new frame's first word holds only its own pixels and starts in P0 (§9.4.2). A queued flush word is still sent first (it holds `s_pix_ready_o` low).

There is no FSM; the state is `fill_q`, `flush_pend_q` and the output register.

- **Same-cycle rules:** a word consumed and a new word loaded on one edge (`output_slot_free` includes `output_consumed`) gives back-to-back words. A queued flush wins over a new pixel.
- **Latency:** a pixel accepted in cycle c that completes a word makes `m_word_valid_o` high in cycle c+1 (traced).
- **Throughput:** 1 pixel per cycle while `m_word_ready_i` = 1. One stall cycle follows a case-B pixel if the next line's pixel is already waiting. Case B occurs at line lengths n mod 16 ∈ {4, 7, 10, 13} for Mono10 and n mod 8 ∈ {3, 6} for Mono12, and never for Mono8/14/16.
- **Invariants, not asserted:** `fill_q < 32` between pixels; `fill_q = 0` at every SOL if the source pairs SOL/EOL; `flush_pend_q → ~s_pix_ready_o`; `m_word_eof_o → m_word_eol_o`.
- **Input contract, not checked:** a missing EOL inside a frame packs the next line into the current word. A probe (not in repo) gave `1122b1b2`, where §9.4.2 wants b1 in P0 → Medium 1. EOF without EOL, or a frame cut off mid-line, leaves the residual in the accumulator until the next SOF, which drops it (item 5): the cut frame's last pixels and its EOF are lost, and the next frame starts clean (`test_18_sof_without_prior_eol`).

```wavedrom
{"signal":[
 {"name":"app_clk","wave":"p........."},
 {"name":"s_pix_valid","wave":"1........."},
 {"name":"s_pix_data","wave":"=====..===","data":["10","11","12","13","14","15","16","17"]},
 {"name":"m_word_ready","wave":"1...0.1..."},
 {"name":"s_pix_ready","wave":"1...0.1...","node":"......a..."},
 {"name":"m_word_valid","wave":"0...1..0.."},
 {"name":"m_word_data","wave":"x...=..x..","data":["10111213"]}
],
 "head":{"text":"Stall rule, Mono8 (traced): s_pix_ready follows m_word_ready in the same cycle"}}
```

## Arbiter integration

Not applicable: the packer has no downlink arbiter slot. Its neighbours in the pixel path are:
- **Merger back-pressure.** `cxp_app_stream` gives its header and line-marker words priority over pixel words, so `m_word_ready_i` drops for 25 cycles per frame and 2 per line (`docs/design/modules/app/cxp_app_stream.md`). The packer and its source stall; nothing is lost.
- **Frame/line start pulses** come from this module's SOF/SOL word accept. The header generator samples its metadata at that accept, at least 1 cycle after the packer latched PixelF; its PixelF is `m_pixfmt_o`, already updated then, so the header names the format the payload is packed in whatever the register does meanwhile (Medium 2, fixed).
- **Unused outputs.** `m_word_lane_vld_o` and `m_word_eol_o` have no consumer in `cxp_interface_top`. `m_word_eof_o` closes the image's last stream packet in `cxp_app_stream` (`pix_word_eof_i`).
- **TestMode suppression and the arbiter watchdog** act downstream. The packer only sees `m_word_ready_i` = 0.

## Verification

Verilator 5.046 with cocotb 2.0.1. There are no SVA. No coverage is collected: no FSM is registered and code coverage is off.

### Unit TB — `src/tb_unit/app/cxp_app_pixel_packer/`

Wrapper `tb_cxp_app_pixel_packer_top` renames the ports without `_i`/`_o` and adds `TESTCASE`. Clock 10 ns. `reset()` holds `app_rst_n` low for 4 edges with `cfg_pixfmt` = Mono8 and `m_word_ready` = 1, then runs 2 edges. Shared pieces:
- **`pack_line`** is the reference model: it concatenates containers MSB-first and splits them into big-endian words with `lane_vld = (1 << bytes) − 1`. Its `CBITS` is the Figure 27–31 width (Mono14 14 bits) and `PIXFMT_MONO16` = 0x0105 (Table 25).
- **`drive_pixel`** holds each pixel until ready, giving back-to-back pixels.
- **`collect_words(n)`** returns exactly n accepted words, so a surplus word is never seen and `len(got) == len(expected)` cannot fail.
- **`_run_basic_format_check`** drives random pixels (seed `0x1234 ^ fmt ^ xsize`) and checks data, `lane_vld` and all four flags on every word.

| Test | Stimulus | Expect |
|---|---|---|
| test_01_mono8_packing | Mono8, 16 px × 2 lines | model words and flags |
| test_02_mono10_packing | Mono10, 16 × 2 | same |
| test_03_mono12_packing | Mono12, 16 × 2 | same |
| test_04_mono14_packing | Mono14, 16 × 2 | same, 14-bit dense (7 words per line) |
| test_05_mono16_packing | 0x0105, 8 × 2 | same |
| test_06_mono10_bit_layout_explicit | Mono10, 4 fixed px, EOL/EOF | 2 words, hand-built bytes |
| test_07_mono12_bit_layout_explicit | Mono12, 4 fixed px | 2 words, hand-built bytes |
| test_08_mono8_xsize_not_aligned | Mono8, 1023 × 2 | last word `lane_vld` 0111 |
| test_09_mono8_xsize_5 | Mono8, 5 × 3 | full word + 1-byte flush per line |
| test_10_mono10_xsize_3 | Mono10, 3 px | 1 word, `lane_vld` 1111 |
| test_11_mono16_single_pixel_line | 0x0105, 1 px | 0xBEEF0000, `lane_vld` 0011, 4 flags |
| test_12_pixfmt_change_between_frames | Mono8 frame, then 0x0105 frame | each frame packed in its own format |
| test_13_backpressure_mono10 | Mono10, 23 × 3, ready 1001011 | lossless, flags |
| test_14_backpressure_mono12 | Mono12, 17 × 2, ready 01001 | lossless, flags |
| test_15_input_gaps | Mono12, 32 px, 3 idle cycles between pixels | model words |
| test_16_reset_mid_frame | reset after 3 Mono10 px, then a clean line | idle after reset; clean words and flags |
| test_17_random_multiframe | 6 random trials | model words |
| test_18_sof_without_prior_eol | Mono8: 2 px with SOF+SOL and no EOL/EOF, then a 4-px frame | exactly one word, the 4-px frame's, with SOF+SOL and EOL+EOF |

#### test_01_mono8_packing
- *Stimulus*: Mono8, 2 lines of 16 random 8-bit pixels. SOF+SOL on pixel 0, EOL per line, EOF on the last. Ready = 1.
- *Checks*: 8 words; per word data, `lane_vld` 1111, SOL/SOF on words 0 and 4 as the model expects, EOL on words 3 and 7, EOF on word 7.
- *Proves*: case C then case A for Mono8; the P0-in-MSB byte order; the flag carry across case E.

#### test_02_mono10_packing
- *Stimulus*: as test_01_mono8_packing with 10-bit values; 16 px = 160 bits = 5 words per line.
- *Checks*: 10 words against the model, all flags.
- *Proves*: every Mono10 phase of `fill_q` (0, 10, … 22) and case A at `fill_post` = 32.

#### test_03_mono12_packing
- *Stimulus*: 12-bit values, 16 px = 6 words per line.
- *Checks*: 12 words, all flags.
- *Proves*: Mono12 phases and case A; case B is not reached, since 16 mod 8 = 0.

#### test_04_mono14_packing
- *Stimulus*: 14-bit values, 16 px per line.
- *Checks*: 16 words against the 16-bit-container model.
- *Proves*: 14-bit dense packing (Figure 30) against the independent model.

#### test_05_mono16_packing
- *Stimulus*: `cfg_pixfmt` = 0x0105, 8 random 16-bit px per line.
- *Checks*: 8 words, all flags.
- *Proves*: 16-bit packing with D(0)[15:8] in P0 (Figure 31) under the Table 25 code 0x0105.

#### test_06_mono10_bit_layout_explicit
- *Stimulus*: Mono10 pixels 0x3AB, 0x123, 0x2C9, 0x07E; SOF+SOL on the first, EOL+EOF on the last.
- *Checks*: word 0 equals the hand-built bytes 0–3 with `lane_vld` 1111, SOL = SOF = 1 and EOL = EOF = 0. Word 1 = byte 4 in P0 with `lane_vld` 0001 and EOL = EOF = 1.
- *Proves*: the Figure 28 bit order from an independent byte formula; case B and the flush slot carrying EOL/EOF.

```wavedrom
{"signal":[
 {"name":"app_clk","wave":"p......"},
 {"name":"s_pix_valid","wave":"1...0.."},
 {"name":"s_pix_data","wave":"====x..","data":["3AB","123","2C9","07E"]},
 {"name":"s_pix_eol/eof","wave":"0..10.."},
 {"name":"s_pix_ready","wave":"1...01."},
 {"name":"fill_q","wave":"=====..","data":["0","10","20","30","0"]},
 {"name":"flush_pend_q","wave":"0...10."},
 {"name":"m_word_valid","wave":"0...1.0","node":"....ab."},
 {"name":"m_word_data","wave":"x...==x","data":["ead23b24","7e000000"]},
 {"name":"lane_vld / flags","wave":"x...==x","data":["f SOL SOF","1 EOL EOF"]}
],
 "head":{"text":"Case B (traced): full word at a, queued flush at b, input ready low for one cycle"}}
```

#### test_07_mono12_bit_layout_explicit
- *Stimulus*: Mono12 pixels 0xABC, 0x123, 0xDEF, 0x456, framed as one line.
- *Checks*: word 0 = bytes 0–3 with `lane_vld` 1111; word 1 = bytes 4–5 in P0/P1 with `lane_vld` 0011 and EOL = EOF = 1.
- *Proves*: the Figure 29 bit order; case C at pixel 3 (36 bits) and case D at pixel 4.

#### test_08_mono8_xsize_not_aligned
- *Stimulus*: Mono8, 2 lines of 1023 px.
- *Checks*: 512 words; every last word has `lane_vld` 0111; all flags.
- *Proves*: case D with 24 bits, and that line 2 restarts in P0 after a partial word.

#### test_09_mono8_xsize_5
- *Stimulus*: Mono8, 3 lines of 5 px, back-to-back.
- *Checks*: 6 words; per line a full word with SOL, then a 1-byte flush with EOL; SOF on word 0, EOF on word 5.
- *Proves*: case D with an output word being consumed in the same cycle, and no bubble between lines.

```wavedrom
{"signal":[
 {"name":"app_clk","wave":"p.........."},
 {"name":"s_pix_valid","wave":"1.........0"},
 {"name":"s_pix_data","wave":"==========x","data":["a0","a1","a2","a3","a4","b0","b1","b2","b3","b4"]},
 {"name":"s_pix_sol","wave":"10...10...."},
 {"name":"s_pix_eol","wave":"0...10...10"},
 {"name":"s_pix_ready","wave":"1.........."},
 {"name":"m_word_valid","wave":"0...1.0..1.","node":".....a....b"},
 {"name":"m_word_data","wave":"x...==x..==","data":["a0a1a2a3","a4000000","b0b1b2b3","b4000000"]},
 {"name":"flags","wave":"x...==x..==","data":["SOL SOF","EOL","SOL","EOL EOF"]}
],
 "head":{"text":"Case D (traced, 2 of the 3 lines; the test's last line carries EOF): flush at a and b, line 2 starts in P0"}}
```

#### test_10_mono10_xsize_3
- *Stimulus*: Mono10 pixels 0x355, 0x2AA, 0x1F1 as one line.
- *Checks*: word 0 equals the independent 30-bit pack padded to 32; `lane_vld` 1111.
- *Proves*: case D with `fill` = 30 and zero padding in the two LSBs.

#### test_11_mono16_single_pixel_line
- *Stimulus*: 0x0105, one pixel 0xBEEF with SOL, EOL, SOF and EOF.
- *Checks*: data 0xBEEF0000, `lane_vld` 0011, all four flags set.
- *Proves*: case D on the first pixel; `eff_sol`/`eff_sof` taken from the live pixel.

#### test_12_pixfmt_change_between_frames
- *Stimulus*: frame 1 is Mono8, 8 px, with SOF/SOL/EOL but no EOF flag. `cfg_pixfmt` then becomes 0x0105 for 8 idle cycles, and frame 2 is 8 × 16-bit px with EOF.
- *Checks*: 6 words equal the Mono8 model for frame 1 and the Mono16 model for frame 2.
- *Proves*: the SOF-pixel live override and latch. **The change lands after frame 1's last pixel. A change inside a frame is never driven.**

#### test_13_backpressure_mono10
- *Stimulus*: Mono10, 3 lines of 23 px; `m_word_ready` repeats 1,0,0,1,0,1,1.
- *Checks*: model words, `lane_vld` and all flags.
- *Proves*: the stall rule and, at 23 ≡ 7 (mod 16), case B with the next line's pixel waiting, i.e. the one-cycle `flush_pend_q` stall.

#### test_14_backpressure_mono12
- *Stimulus*: Mono12, 2 lines of 17 px; ready repeats 0,1,0,0,1.
- *Checks*: as above.
- *Proves*: lossless stalls for Mono12; the line ends in case D.

#### test_15_input_gaps
- *Stimulus*: Mono12, one line of 32 px with 3 idle cycles after each accept.
- *Checks*: data and `lane_vld` per word; flags not checked.
- *Proves*: the accumulator holds across `s_pix_valid` = 0.

#### test_16_reset_mid_frame
- *Stimulus*: 3 Mono10 pixels (30 bits, no word out), then `app_rst_n` low for 4 edges.
- *Checks*: after release `m_word_valid` = 0 and `s_pix_ready` = 1. A following 4-px line matches the model with SOL/SOF on the first word and EOL/EOF on the last.
- *Proves*: the async reset clears `acc_q`/`fill_q`. No word is held and no flush is queued at the reset.

#### test_17_random_multiframe
- *Stimulus*: seed 0xDEADBEEF, 6 trials: Mono8 14×4, Mono14 22×1, Mono12 17×2, Mono14 16×4, 0x0105 4×4, Mono12 23×1, with random ready patterns and gaps.
- *Checks*: data and `lane_vld`; flags not checked.
- *Proves*: format changes between frames under random stalls. No trial ends a line in case B.

#### test_18_sof_without_prior_eol
- *Stimulus*: Mono8, `m_word_ready` = 1. Pixels 0xA1 (SOF+SOL) and 0xA2 with no EOL or EOF (a frame cut off mid-line); then a frame 0x01..0x04 with SOF+SOL on the first pixel and EOL+EOF on the last; 40 cycles.
- *Checks*: exactly one output word, equal to `pack_line([1, 2, 3, 4])`, with SOF and EOF set.
- *Proves*: How it works 5: the cut frame's 16 residual bits are dropped at the SOF and the new frame starts in P0. Before the change the first word would have been `A1A20102`, with the new frame's last two pixels in a second word (by code).

### Integration TB — `src/tb_unit/top/cxp_stream_top/`

12 tests, all passing. The wrapper instantiates the real TPG (8 × 4 Mono8) and the real packer on `pix_sel` = 0, with the byte swap hand-coded and pulses gated as in `cxp_interface_top`. `app_clk` 10 ns, `tx_clk` 8 ns, `cfg_dsizeP` 11. The `test_NN_skid_*` tests (04–11) and `test_12_chopper_residual_across_frame_boundary` drive the word path directly and bypass the packer.

| Test | Checks |
|---|---|
| test_02_full_frame_decode | every payload word of two frames against the golden image, including the 16 pixel words |
| test_03_kmask_passthrough | pixel words carry kmask 0 |

#### test_02_full_frame_decode
- *Stimulus*: TPG free-running at 8 px × 4 lines Mono8, pixel (x + y) & 0xFF; `m_ready` = 1; 8000 `tx_clk` cycles.
- *Checks*: framing of every packet; the merged payload equals 2 × (25 header + 4 × (2 marker + 2 pixel)) golden words, with pixel word = {p3, p2, p1, p0}.
- *Proves*: Mono8 packing, `bswap32` and the SOF/SOL accept pulses end to end. Every line ends in case A, and all values are 8 bit.

#### test_03_kmask_passthrough
- *Stimulus*: as above, 4000 `tx_clk` cycles.
- *Checks*: every payload kmask is 0xF (marker) or 0; at least Y_SIZE + 1 markers.
- *Proves*: packer words enter the stream as data.

### Other

- **`src/tb_unit/top/cxp_interface_top/`** (13/13): TPG → packer path with PixelFormat from the in-wrapper regfile (default 0, so the TPG parameter 0x0101 applies). `test_02_stream_from_tpg` checks only the stream type word.
- **`src/verif/` PyUVM:** `video_agent.py:232-240` restricts end-to-end traffic to Mono8 and leaves the other formats to the unit TB. Not run.
- **`src/emu/bridge/Makefile`** compiles the module through `cxp_interface_top`. Not run.

### Running

```
make -C src/tb_unit/app/cxp_app_pixel_packer WAVES=0 COCOTB_TEST_FILTER=test_06_mono10_bit_layout_explicit
make -C src/tb_unit pixel_packer        # regression entry: make -C src/tb_unit
```

2026-09-15, commit 7932349: unit TB 17/17; `src/tb_unit/top/cxp_stream_top` 12/12; `src/tb_unit/top/cxp_interface_top` 13/13. Full regression not re-run. 2026-09-26 (uncommitted working tree): SOF clears the accumulator, `m_pixfmt_o`, test_18 added; not re-run for this document.

### Not covered in-tree

- PixelF codes outside the five decoded ones (currently packed as Mono8 low byte — intent undecided).
- `cfg_pixfmt_i` change inside a frame (currently ignored until the next SOF, observed).
- Pixel bits above `bpp` set (currently ignored, observed).
- Missing EOL inside a frame, i.e. SOL without a prior EOL (currently packed across the boundary, observed) → Medium 1. SOF without a prior EOL is covered by test_18.
- `m_pixfmt_o`: no unit check, and no bench compares the header's PixelF with the packing or changes the format between images end to end (every stream bench runs Mono8).
- Mono12 case B: no test ends a line at n mod 8 ∈ {3, 6}.
- `s_pix_ready_o` timing and throughput: never asserted.
- Surplus output words: `collect_words` stops at n.
- Reset with a held output word or a queued flush (currently both cleared, observed).
- Input active at reset release; X on `cfg_pixfmt_i`.
- Indefinite stall: covered only by periodic ready patterns.
- Any format other than Mono8 through `cxp_app_stream`, and 10–16-bit values end to end → Medium 4.
- Multi-clock: single-domain module; the top delivers `cfg_pixfmt_i` on `app_clk`.

## Known issues and recommendations

### Critical

1. **Codes outside the five decoded ones are packed as Mono8 from the low byte.** Every 10–16-bit Planar/Bayer code, for example 0x0212, packs its low byte only while the header still announces the code, so the host decodes garbage. *Fix:* reject unsupported codes at the PixelFormat register write (Table 22 0x41) so they never reach the packer. *Effort:* with the register-file value checks.

### Medium

1. **Framing violations inside a frame are packed silently across boundaries** (How it works, input contract). Fixed 2026-09-26 for a SOF without a prior EOL: the SOF starts on an empty accumulator (test_18), and the ingress reports the cut (`cxp_app_pixel_ingress.md`). Still open: a SOL without a prior EOL packs the new line into the old line's last word, and an EOF without EOL loses the frame's last partial word. The TPG always pairs flags; the ingress forwards sensor flags unchecked. *Fix:* on an accepted SOL with `fill_q ≠ 0`, flush the residual first (reuse the `flush_*_q` slot); treat EOF as EOL, and set a sticky `proto_err_o`. *Effort:* 0.5 day with tests.
2. **Fixed: the latched PixelF was not exported.** `m_pixfmt_o` is the format latched at the SOF pixel, and `cxp_interface_top` puts it in the image header, so the header's PixelF always matches the packing.
3. **Unit-test gaps** (Not covered in-tree). *Fix:* add Mono12 n = 3 and n = 6 lines followed by another line, with an assertion on the stall cycle. Check flags in `test_15_input_gaps`/`test_17_random_multiframe`. Add a real mid-frame `cfg_pixfmt` change, pixels with bits above `bpp`, an unknown code, and reset with a held word and a queued flush. *Effort:* 0.5 day.
4. **End-to-end is Mono8 only, with 8-bit values and case-A lines.** *Fix:* parametrise `tb_cxp_stream_top` with Mono10/12 and a width that is not a multiple of the word. This needs full-width TPG values (`docs/design/modules/app/cxp_app_tpg.md` Medium 2). *Effort:* 1 day.

### Minor

- The `s_pix_data_i` comment "MSB-aligned" should read "LSB-justified in [bpp−1:0]; upper bits ignored".
- Header: "Created 2025-05-27" predates the module's first commit (2d3c7a1, 2026-05-14).
- The byte order is reversed twice: P0 in [31:24] here, then `bswap32` in `cxp_interface_top` and a hand-coded swap in `tb_cxp_stream_top`. Emitting wire order (P0 in [7:0]) directly would remove both.
- The reset literal `16'h0101` should be `PIXFMT_MONO8`. The `default` arms of the second and third `unique case (fmt)` are unreachable.
- `m_word_lane_vld_o`/`m_word_eol_o`/`m_word_eof_o` are unused at the top; keep them for SVA or drop them.
- Unit TB: `pack_line` still cites v1.0 §7.4.2, and `test_12_pixfmt_change_between_frames` drives no EOF on frame 1.
- SVA to add: `s_pix_valid_i & s_pix_ready_o & s_pix_sol_i |-> fill_q == 0`; `m_word_valid_o & ~m_word_ready_i |=> $stable({m_word_data_o, m_word_lane_vld_o})`; `flush_pend_q |-> ~s_pix_ready_o`; `m_word_valid_o & m_word_eof_o |-> m_word_eol_o`.

### Open questions

2. Designer: which PixelF codes must the packer accept: Mono only, or every Table 25 format by component width? What should an unsupported code do: stall, pack as raw, or flag?
3. Designer: should framing violations be repaired here (implicit flush) or rejected in `cxp_app_pixel_ingress`?
4. Answered 2026-09-26: the packer's latch (`m_pixfmt_o`) owns the header's PixelF.
5. Verification: where does multi-format end-to-end checking live, `tb_cxp_stream_top` or the PyUVM video agent, which is Mono8-only today?
