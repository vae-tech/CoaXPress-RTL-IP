# cxp_app_tpg

Inputs chosen from the tree: RTL `src/rtl/app/cxp_app_tpg.sv` (+ `cxp_pkg.sv`, `cxp_util_pkg.sv`), module-level TB `src/tb_unit/app/cxp_app_tpg`, integration TBs `src/tb_unit/top/cxp_interface_top` and `src/tb_unit/top/cxp_device_top`, spec CoaXPress 1.1.1 (JIIA CXP-001-2015).

Synthesisable single-pixel image source: it emits one pixel per `app_clk` on a valid/ready bus with frame and line flags, plus the image-header metadata of the frame in flight as one `cxp_pkg::cxp_meta_t`. Downstream, the pixel packer turns pixels into 32-bit words and `cxp_app_stream` turns those words into image headers, line markers and stream packets.

| Flag | Asserted on the pixel where |
|---|---|
| `m_pix_sol_o` | x = 0 |
| `m_pix_sof_o` | x = 0 and y = 0 |
| `m_pix_eol_o` | x = xsize − 1 |
| `m_pix_eof_o` | x = xsize − 1 and y = ysize − 1 |

Source: `src/rtl/app/cxp_app_tpg.sv`. One RTL instance, `cxp_interface_top.cxp_app_tpg_i` (`cxp_interface_top.sv:531-559`). It is fed by `cfg_app.*`, the `app_clk` copy of the top-level `cfg_*` ports; `cxp_device_top` drives those from the register file's Width, Height, PixelFormat, TestPattern, OffsetX, OffsetY and SourceTag registers; the `cxp_interface_top` TB wrapper takes the first four from its register file and ties the offsets and SourceTag to 0. Its pixels feed the `cfg_use_tpg` pixel mux, then the acquisition gate `cxp_app_acq_ctrl`, `cxp_app_pixel_packer` and `cxp_app_stream`; its `meta_o` becomes `tpg_meta`, one side of the `sel_meta` mux that feeds `cxp_app_stream.meta_i`. The `src/tb_unit/top/cxp_stream_top` wrapper has a second instance.

Spec clauses: §9.4.1 and Table 25 (PixelF codes), §9.4.2 and Figures 27–31 (packing, through DsizeL), Table 38 (the header fields its metadata supplies). The pattern selector register 0x1001C is manufacturer-specific, not a spec register.

## Interface

| Name | Default | Meaning |
|---|---|---|
| `p_X_SIZE` | 64 | Maximum pixels per line. Must be > 0, checked at elaboration by a generate-if `$error` (`cxp_app_tpg.sv:137-142`), which fails the bench builds (`-Werror-USERERROR`) and any synthesis tool that honours elaboration-time `$error`. Held in 16-bit registers, so values above 65535 are truncated with no guard. X counter width is `idx_w(p_X_SIZE)`. |
| `p_Y_SIZE` | 32 | Maximum lines per frame. Same rules as `p_X_SIZE`. |
| `p_PIXFMT` | `cxp_pkg::PIXFMT_MONO8` (0x0101) | Pixel format used when `cfg_pixfmt_i` = 0. Also the reset value of the latched format. |

| Name | Dir | Width | Description |
|---|---|---|---|
| `app_clk` | in | 1 | Only clock. |
| `app_rst_n` | in | 1 | Active-low reset, asynchronous assert. |
| `cfg_run_i` | in | 1 | Level. 1 starts or continues free-run. 0 stops after the current frame's EOF. |
| `cfg_xsize_i`, `cfg_ysize_i` | in | 16 | Active size. 0 or a value above the maximum selects the maximum. Latched at frame start. |
| `cfg_pixfmt_i` | in | 16 | Pixel format. 0 selects `p_PIXFMT`. Any other value is latched as-is at frame start. |
| `cfg_testpat_i` | in | 2 | Pattern: 0 gradient, 1 bars, 2 flat, 3 grey bars. Latched at frame start. |
| `cfg_xoffs_i`, `cfg_yoffs_i` | in | 16 | Image X / Y offset (OffsetX / OffsetY registers). Latched at frame start into `meta_o.xoffs` / `meta_o.yoffs`; no effect on the pixels. |
| `cfg_srctag_i` | in | 16 | SourceTag preset (SourceTag register). A change of the value presets the SourceTag of the next image (How it works 2); a steady value does nothing. |
| `cfg_streamid_i`, `cfg_tapg_i`, `cfg_flags_i` | in | 8, 16, 8 | Image1StreamID, TapGeometry and StreamFlags registers, passed to `meta_o` unlatched; the packer takes them with the image's first pixel. |
| `m_pix_data_o` | out | 16 | Pixel value. Always 8 bits zero-extended, so bits 15:8 are 0. |
| `m_pix_valid_o` | out | 1 | High exactly while the FSM is in `ST_EMIT`. |
| `m_pix_ready_i` | in | 1 | Consumer accepts the pixel. |
| `m_pix_sof_o`, `m_pix_sol_o`, `m_pix_eol_o`, `m_pix_eof_o` | out | 1 | Frame and line flags, gated by valid. |
| `meta_o` | out | `cxp_meta_t` (177) | Frame metadata, combinational from the latched registers and the `cfg_streamid_i` / `cfg_tapg_i` / `cfg_flags_i` inputs (`cxp_app_tpg.sv:176-187`). Fields below. |

| `meta_o` field | Width | Value |
|---|---|---|
| `arbitrary` | 1 | 0. The TPG always describes a rectangular image; `cxp_interface_top` overwrites the field from `cfg_arbitrary` after the mux. |
| `streamid` | 8 | `cfg_streamid_i` (Image1StreamID). |
| `sourcetag` | 16 | `srctag_q`: the SourceTag of the image in flight (How it works 2) |
| `xsize`, `ysize` | 24 | Latched active size, zero-extended |
| `xoffs`, `yoffs` | 24 | Latched `cfg_xoffs_i`, `cfg_yoffs_i`, zero-extended |
| `pixfmt` | 16 | Latched pixel format |
| `tapg`, `flags` | 16, 8 | `cfg_tapg_i`, `cfg_flags_i` (TapGeometry, StreamFlags) |

The ten separate `meta_*_o` outputs of the earlier version are gone; the unit-TB wrapper unpacks `meta_o` back into them, widening `streamid` to 16 bits with a zero upper byte.

Notes:
- **Reset.** `always_ff @(posedge app_clk or negedge app_rst_n)`: assertion is asynchronous. A probe showed valid falling 1 ns after assertion (not in repo). The port comment "sync-deassert" leaves deassertion synchronisation to the integration. `cxp_interface_top` passes its `app_rst_n` straight through.
- **Clock domains.** Everything runs on `app_clk`. In `cxp_interface_top` the `cfg_*` inputs are `rx_clk` ports driven from the register file. With `p_ASYNC_CLOCKS` = 1 they cross to `app_clk` as one `cxp_cdc_bus` snapshot (`cxp_cdc_bus_cfg_app_i`), so the TPG never sees a torn multi-bit value; with 0 the clocks are one clock (Medium 3, resolved at the top). `src/tb_unit/top/cxp_device_top` runs the TPG at 12 ns against a 10 ns register clock; the other TBs tie the clocks together.
- **Registration.** Valid, data and flags are combinational decodes of registered state (`state_q`, `x_q`, `y_q` and the latched config). None depends on `m_pix_ready_i`. `meta_o` is combinational from the latched registers.
- **Stall.** With `m_pix_ready_i` = 0, valid stays high and data and flags hold. A probe ran 3000 cycles with random 40 % ready and saw 0 stability violations over 1268 accepted pixels (not in repo).

## How it works

1. **Frame-start latch.** `load_size` fires on the edge that leaves `ST_IDLE` with `cfg_run_i` = 1, and on the edge that accepts EOF while `cfg_run_i` = 1. On that edge it copies the clamped size, the effective pixel format, the pattern select and the offsets into `xsize_q`, `ysize_q`, `pixfmt_q`, `testpat_q`, `xoffs_q` and `yoffs_q`, and loads the image's SourceTag into `srctag_q`. `cfg_*` changes during a frame therefore take effect on the next SOF, and `meta_o` switches on the same edge that presents the new SOF.
2. **SourceTag (Table 38).** `srctag_next_q` holds the tag of the next image; it resets to 0, so the first image after reset carries 0, and every `load_size` edge moves it on by one, wrapping 0xFFFF → 0x0000. `srctag_cfg_q` is `cfg_srctag_i` one cycle ago; when the two differ (`srctag_set`), the new value becomes the next image's tag: if that cycle is also a `load_size` edge, the starting image takes the new value itself and the count goes on from it. Only a change presets, so writing the SourceTag register with the value it already holds does not. The TPG owns the count: neither ConnectionReset nor acquisition start resets it (the SourceTag register survives a ConnectionReset too); only `app_rst_n` does.
3. **Counters and FSM.** `x_q` and `y_q` advance on every handshake and wrap at the latched size. `last_pix_in_frame` is `x_q == xsize_q−1 && y_q == ysize_q−1`.

   | State | Next | Condition |
   |---|---|---|
   | `ST_IDLE` | `ST_EMIT` | `cfg_run_i` (x, y cleared; `load_size`) |
   | `ST_EMIT` | `ST_EMIT` | not the EOF handshake, or EOF handshake with `cfg_run_i` = 1 (restart, `load_size`) |
   | `ST_EMIT` | `ST_IDLE` | EOF handshake (`m_pix_ready_i & last_pix_in_frame`) with `cfg_run_i` = 0 |

   ```mermaid
   stateDiagram-v2
       [*] --> ST_IDLE
       ST_IDLE --> ST_EMIT : cfg_run_i
       ST_EMIT --> ST_EMIT : handshake / EOF handshake & cfg_run_i
       ST_EMIT --> ST_IDLE : EOF handshake & !cfg_run_i
   ```

4. **Pattern mux.** It is driven by `testpat_q`, `x_q`, `y_q` and the frame counter. `band_idx` is `min(x / (xsize>>3), 7)`. When xsize < 8 the band width is 0, so every pixel is in band 7. Any remainder of xsize / 8 widens band 7.

   | `cfg_testpat_i` | Pattern | Pixel value (8 bit) |
   |---|---|---|
   | 0 | Gradient | (x + y) & 0xFF |
   | 1 | Bars | 0xFF in odd bands, 0x00 in even bands |
   | 2 | Flat | `frame_cnt_q[3:0]` × 16: 0x00, 0x10 … 0xF0, repeating every 16 frames |
   | 3 | Grey bars | Bands 0–7 = 0x10, 0x30, 0x50, 0x70, 0x90, 0xC0, 0xE0, 0xFF |

   `frame_cnt_q` counts EOF handshakes, whether or not `cfg_run_i` is set. It survives stop and start and is cleared only by reset.
5. **Line size.** Not carried: the marker generators derive DsizeL (32-bit words per line) from the latched `xsize` and `pixfmt` through `dsizel_words()` in `cxp_pkg`.

Same-cycle rules:
- An EOF handshake with `cfg_run_i` = 1 latches the new config and presents the next SOF with zero idle cycles. A probe measured a 0-cycle gap (not in repo).
- A `cfg_run_i` 1→0→1 glitch inside a frame has no effect. Only the value sampled at the EOF handshake matters.
- With `cfg_run_i` = 0 and `m_pix_ready_i` = 0 the FSM parks in `ST_EMIT` indefinitely. When ready returns it finishes the stale frame, then goes idle (probe, not in repo).

Latency and throughput:
- SOF is presented in the cycle after the edge that samples `cfg_run_i` = 1 in `ST_IDLE`.
- One pixel per cycle at ready = 1, so a frame takes xsize · ysize cycles.
- After a stop, valid drops in the cycle after the EOF handshake.
- A 1×1 frame carries all four flags on every pixel (probe, not in repo).

Invariants by construction, not asserted: `m_pix_valid_o == (state_q == ST_EMIT)`; `x_q < xsize_q` and `y_q < ysize_q`; `sof` implies `sol`; `eof` implies `eol`; `meta_o` is constant between two `load_size` edges.

## Arbiter integration

Not applicable: the module has no downlink arbiter slot. It meets two selectors in `cxp_interface_top`.
- **Source mux and run.** `cfg_use_tpg` routes `sel_pix_ready` to the TPG or to the ingress and picks `tpg_meta` or the packed `ext_meta` for `sel_meta` (in TPG mode with `sel_meta.streamid` = `cfg_streamid`, the Image1StreamID register). `cfg_run_i` = `acq_active & cfg_app.use_tpg`, where `acq_active` (`cxp_app_acq_ctrl.active_o`) is high while a new image would enter the gate: the acquisition is armed (AcquisitionStart, or `cfg_run` holding it) and a stream packet fits in StreamPacketSizeMax. It drops in the cycle the last image of a SingleFrame / MultiFrame acquisition ends, so the TPG does not roll into another image. Switching to the sensor mid-frame forces ready and run to 0, so the TPG parks mid-frame. Switching back resumes the stale frame's remaining pixels, which carry no SOF: the gate drops them unless an image is open there, in which case they join it. That scenario is untested → `docs/design/modules/top/cxp_interface_top.md` Medium 4.
- **PixelFormat.** In TPG mode `sel_meta.pixfmt` is the TPG's own `meta_o.pixfmt`, latched at its image start from `cfg_pixfmt_i` (the register, or `p_PIXFMT` when it is 0); the register override in `cxp_interface_top` applies only to the sensor branch. The packer latches that format at the SOF pixel and the image header takes PixelF from the packer (`m_pixfmt_o`), so a PixelFormat write during an image changes neither its packing nor its header.
- **Acquisition stop.** AcquisitionStop (while `cfg_run` = 0), `cfg_run` 1→0 without an acquisition, a ConnectionReset or StreamPacketSizeMax below one packet drop `acq_active`; the TPG completes the image it has begun and stops at its EOF, and the chopper closes that image's last packet short (`cxp_device_top` test_19 stops it at 11 points of a packet and restarts it; every image arrives whole).
- **TestMode** suppresses the stream in the arbiter and flushes the stream path (`cxp_app_stream.md` How it works 5). The TPG keeps running; what it produces during TestMode is dropped, and the stream resumes with its next whole image (`cxp_device_top` test_22).

## Verification

Verilator 5.046 with cocotb 2.0.1. No bound SVA covers this module (`src/sva/cxp_sva.sv`). The unit TB collects FSM state and arc coverage through `fsm_coverage.py`.

### Unit TB — `src/tb_unit/app/cxp_app_tpg`

Wrapper `tb_cxp_app_tpg_top` renames ports without the `_i`/`_o` suffixes, unpacks `meta_o` into the old `meta_xsize` … `meta_flags` ports, uses defaults X 64, Y 32, Mono8, and adds the `TESTCASE` byte. The clock is 10 ns. `reset()` drives reset low for 4 edges with every `cfg_*` = 0 and ready = 1, then waits 2 edges. `capture_frame` records handshakes from the first SOF to EOF, calling an optional callback in the active region after each edge. `check_frame` is the shared checker. For every pixel it asserts the pixel count = xsize·ysize, the four flags, the data against a Python mirror of the pattern mux, and `meta_xsize`, `meta_ysize`, `meta_pixfmt` and `meta_dsizeL` against the expected latched values. Every test leaves `cfg_run` = 1 until after its last EOF, so the next frame has already started when it drops.

| Test | Stimulus | Expect |
|---|---|---|
| test_01_baseline_frame | all `cfg_*` = 0, run | 64×32 Mono8 gradient |
| test_02_cfg_xsize_change_mid_frame_ignored | 8×4; xsize → 24 after 12 px | N 8×4, N+1 24×4 |
| test_03_cfg_ysize_change_mid_frame_ignored | 8×4; ysize → 8 after 16 px | N 8×4, N+1 8×8 |
| test_04_cfg_pixfmt_change_mid_frame_ignored | 8×4 Mono8; pixfmt → 0x0105 after 6 px | N Mono8, N+1 0x0105 |
| test_05_all_cfg_change_mid_frame_ignored | 8×4 Mono8; → 16×2 Mono12 after 10 px | N unchanged, N+1 16×2 Mono12 |
| test_06_cfg_change_under_backpressure_ignored | ready low 4 cycles after 10 px, cfg → 32×1 0x0105 during stall | N 8×4 intact, N+1 32×1 |
| test_07_cfg_pixfmt_zero_falls_back_to_parameter | 8×2, pixfmt 0 | PixelF 0x0101 |
| test_08_bars_pattern | bars 32×4; xsize → 48 after 8 px | 8 bars of 4 px, then of 6 px |
| test_09_flat_pattern | flat 8×4, 3 frames | levels 0x00, 0x10, 0x20 |
| test_10_greybars_pattern | grey bars 64×4 | 8 bands of 8 px |
| test_11_cfg_testpat_change_mid_frame_ignored | 16×4 gradient; → bars after 8 px | N gradient, N+1 bars |
| test_12_stop_at_frame_end | 16×4; run drops after 8 px; 200 idle cycles; run again | frame whole, no valid while idle, restart frame whole |
| test_13_sourcetag_and_offsets | 4×2; 3 frames; mid-frame SourceTag 0xFFFE, offsets 12 / 6; 3 frames | tags 0, 1, 2, 3; then 0xFFFE, 0xFFFF, 0x0000 with offsets 12 / 6 |

#### test_01_baseline_frame
- *Stimulus*: after `reset()`, `cfg_run` = 1 with every `cfg_*` = 0 and ready = 1. One frame is captured, then run drops.
- *Checks*: `check_frame` for 64×32, Mono8, gradient: 2048 pixels, flags, data, meta.
- *Proves*: the zero-means-maximum branches of `xsize_eff` and `ysize_eff`, the `p_PIXFMT` fallback, the gradient arm, arc `ST_IDLE → ST_EMIT`, and a full-size wrap of both counters.

```wavedrom
{"signal":[
 {"name":"app_clk","wave":"p...|..."},
 {"name":"app_rst_n","wave":"1...|..."},
 {"name":"cfg_run","wave":"01..|..."},
 {"name":"state_q","wave":"=.=.|..=","data":["IDLE","EMIT","EMIT"]},
 {"name":"m_pix_valid","wave":"0.1.|..."},
 {"name":"x_q,y_q","wave":"=.==|===","data":["0,0","0,0","1,0","62,31","63,31","0,0"]},
 {"name":"m_pix_sof","wave":"0.10|..1","node":"..a....b"},
 {"name":"m_pix_eof","wave":"0...|.10","node":"......c"},
 {"name":"meta_xsize","wave":"=...|...","data":["64"]}
],
"head":{"text":"a: capture starts on first SOF handshake; c: pixel 2048 = EOF; b: next frame already started"}}
```

#### test_02_cfg_xsize_change_mid_frame_ignored
- *Stimulus*: 8×4 Mono8, run. The callback writes `cfg_xsize` = 24 once 12 handshakes have been recorded. Two frames are captured back to back, then run drops.
- *Checks*: the poke fired. `check_frame` for frame N at 8×4 and frame N+1 at 24×4, including `meta_xsize` = 8 on every pixel of N and 24 on every pixel of N+1.
- *Proves*: `xsize_q` holds mid-frame, and the free-run `load_size` branch (`ST_EMIT & ready & last_pix_in_frame & cfg_run_i`) latches on the EOF edge.

```wavedrom
{"signal":[
 {"name":"app_clk","wave":"p......"},
 {"name":"cfg_xsize","wave":"=......","data":["24"]},
 {"name":"m_pix_ready","wave":"1......"},
 {"name":"x_q,y_q","wave":"======.","data":["5,3","6,3","7,3","0,0","1,0","2,0"]},
 {"name":"m_pix_eof","wave":"0.10...","node":"..a"},
 {"name":"m_pix_sof","wave":"0..10..","node":"...b"},
 {"name":"meta_xsize","wave":"=..=...","data":["8","24"]}
],
"edge":["a~>b load_size"],
"head":{"text":"a: last pixel of 8x4 frame (meta 8); b: SOF of 24x4 frame (meta 24)"}}
```

#### test_03_cfg_ysize_change_mid_frame_ignored
- *Stimulus*: 8×4 Mono8. `cfg_ysize` = 8 is written after 16 pixels. Two frames.
- *Checks*: frame N 8×4 and frame N+1 8×8 through `check_frame`.
- *Proves*: `ysize_q` holds mid-frame; `last_pix_in_frame` uses the latched height.

#### test_04_cfg_pixfmt_change_mid_frame_ignored
- *Stimulus*: 8×4 Mono8. `cfg_pixfmt` = 0x0105 is written after 6 pixels. Two frames.
- *Checks*: frame N has PixelF 0x0101. Frame N+1 has PixelF 0x0105.
- *Proves*: the `pixfmt_q` latch.

#### test_05_all_cfg_change_mid_frame_ignored
- *Stimulus*: 8×4 Mono8. After 10 pixels one callback writes xsize 16, ysize 2 and Mono12 together. Two frames.
- *Checks*: frame N at 8×4 Mono8. Frame N+1 at 16×2 Mono12.
- *Proves*: all latched fields update on the same `load_size` edge; the 12-bit arm of `cbits_q`.

#### test_06_cfg_change_under_backpressure_ignored
- *Stimulus*: 8×4 Mono8. After pixel 10 the callback drops ready. Two cycles later it writes 32×1 and 0x0105, and ready returns after 4 low cycles. Two frames.
- *Checks*: the stage machine completed. Frame N at 8×4 has no lost or duplicated pixel. Frame N+1 is 32×1 at 0x0105.
- *Proves*: `x_q` and `y_q` hold while `m_pix_ready_i` = 0. The config write lands mid-frame like the unstalled tests, so it adds nothing to the latch evidence.

```wavedrom
{"signal":[
 {"name":"app_clk","wave":"p......"},
 {"name":"m_pix_ready","wave":"10...1."},
 {"name":"m_pix_valid","wave":"1......"},
 {"name":"x_q,y_q","wave":"==....=","data":["2,1","3,1","4,1"],"node":".....a"},
 {"name":"cfg_xsize","wave":"=..=...","data":["8","32"]},
 {"name":"meta_xsize","wave":"=......","data":["8"]}
],
"head":{"text":"a: pixel (3,1) accepted exactly once after 4 stall cycles"}}
```

#### test_07_cfg_pixfmt_zero_falls_back_to_parameter
- *Stimulus*: 8×2, `cfg_pixfmt` = 0, run, one frame.
- *Checks*: `check_frame` with PixelF 0x0101.
- *Proves*: the `pixfmt_eff` fallback. `test_01_baseline_frame` already covers this.

#### test_08_bars_pattern
- *Stimulus*: bars at 32×4 Mono8. `cfg_xsize` = 48 is written after 8 pixels. Two frames.
- *Checks*: frame A matches the mirror at 32 px (4-pixel bars) and frame B at 48 px (6-pixel bars).
- *Proves*: the bars arm and a `band_idx` that scales with the latched width. Both widths are multiples of 8, so the remainder and narrow-line cases are not exercised (Medium 5).

#### test_09_flat_pattern
- *Stimulus*: flat at 8×4, three free-run frames.
- *Checks*: every pixel equals `frame_idx` × 16: 0x00, 0x10, 0x20.
- *Proves*: `frame_done` increments `frame_cnt_q` once per EOF handshake; the flat arm. The 16-frame wrap is not reached; a probe saw 0xF0 followed by 0x00 (not in repo).

#### test_10_greybars_pattern
- *Stimulus*: grey bars at 64×4, one frame.
- *Checks*: eight 8-pixel bands with the eight levels.
- *Proves*: the `greybar_val` table and its band mapping.

#### test_11_cfg_testpat_change_mid_frame_ignored
- *Stimulus*: gradient at 16×4. `cfg_testpat` = bars is written after 8 pixels. Two frames.
- *Checks*: frame N is gradient; frame N+1 is bars with 2-pixel bars.
- *Proves*: the `testpat_q` latch.

#### test_12_stop_at_frame_end
- *Stimulus*: gradient at 16×4. `cfg_run` is cleared after 8 pixels of the frame. Then 200 idle cycles, then `cfg_run` = 1 again for one more frame.
- *Checks*: the frame completes whole (`check_frame`); `m_pix_valid` stays 0 for the 200 cycles after it; the restarted frame is whole again.
- *Proves*: the stop arc `ST_EMIT → ST_IDLE` — the frame in flight runs to its EOF before the FSM idles — and the restart out of `ST_IDLE`.

#### test_13_sourcetag_and_offsets
- *Stimulus*: 4×2 Mono8 free-run. Three frames with `cfg_srctag` = `cfg_xoffs` = `cfg_yoffs` = 0. In the fourth frame, after 3 pixels, the callback writes `cfg_srctag` = 0xFFFE, `cfg_xoffs` = 12 and `cfg_yoffs` = 6; three more frames, then run drops.
- *Checks*: every pixel of a frame carries the same `meta_sourcetag`, `meta_xoffs` and `meta_yoffs`; the first three frames carry tags 0, 1, 2 with offsets 0 / 0; the frame in flight at the write keeps tag 3 and offsets 0 / 0; the next three carry 0xFFFE, 0xFFFF, 0x0000 with offsets 12 / 6.
- *Proves*: SourceTag starts at 0 after reset, counts images and wraps; a `cfg_srctag_i` change presets the next image, not the one in flight; the offsets latch at frame start. A preset landing on the `load_size` edge itself, and a write of the unchanged value, are not exercised.

FSM coverage (2026-09-19 run): states 2/2 (`ST_IDLE` 77, `ST_EMIT` 3284 samples); arcs 1/2 (`ST_IDLE → ST_EMIT` 11 hits, `ST_EMIT → ST_IDLE` 0). Unchanged by the `cxp_meta_t` change. 2026-09-20, with test_12 added: states 2/2, arcs 2/2.

### Integration TB — `src/tb_unit/top/cxp_interface_top`

14 tests on the real `cxp_interface_top`, with the TPG instantiated at `p_TPG_X_SIZE` 8 and `p_TPG_Y_SIZE` 4. `cfg_xsize` and `cfg_ysize` come from the bootstrap Width and Height registers, which reset to 640 and 480 and clamp to 8×4. PixelFormat resets to 0, so the format is Mono8, and TestPattern resets to 0, so the pattern is gradient. Four tests turn the TPG on. None checks pixel values, header fields or the TPG's configuration inputs; the wrapper ties `cfg_streamid` to 1 and `cfg_xoffs`, `cfg_yoffs`, `cfg_srctag` to 0. For timing diagrams of the preemption and suppression cases see `docs/design/modules/top/cxp_interface_top.md`. Here the TPG is only the load.

| Test | Checks |
|---|---|
| test_02_stream_from_tpg | First packet's TYPE word is 4×0x01 with kmask 0 |
| test_04_linktest_packets_under_testmode | 5 packets under TestMode are 0x03/0x04, at least one 0x04 |
| test_10_trigger_preempts_stream | Trigger header within 20 cycles of `trig_in`; a later stream SOP |
| test_18_connection_config_write_resets_tag | PacketTag restarts at 0 after a ConnectionConfig write strobe (`docs/design/modules/top/cxp_interface_top.md`) |

#### test_02_stream_from_tpg
- *Stimulus*: `cfg_use_tpg` = `cfg_run` = 1, `cfg_arbitrary` 0, `cfg_dsizeP` 8. The first packet is collected within 4000 cycles.
- *Checks*: TYPE word kmask 0 and all four bytes 0x01.
- *Proves*: the TPG → mux → packer → `cxp_app_stream` path reaches the wire. It says nothing about TPG content.

#### test_04_linktest_packets_under_testmode
- *Stimulus*: TPG on as above. TestMode is set by backdoor, then 5 packets are collected.
- *Checks*: packet type is 0x03 or 0x04, with an in-flight 0x01 tolerated at index 0. At least one 0x04 appears.
- *Proves*: suppression holds the TPG-fed stream off the wire. It does not check that the stream recovers after TestMode clears.

#### test_10_trigger_preempts_stream
- *Stimulus*: TPG on and 200 cycles of settling. After a stream SOP plus 2 words, `trig_in` = 1.
- *Checks*: a K28.4 trigger header within 20 cycles, then another stream SOP within 2000 cycles.
- *Proves*: the arbiter preempts a TPG-fed stream packet and the stream resumes. It asserts a design choice; see the trigger docs.

### Other

- `src/tb_unit/top/cxp_device_top` test_04_stream: the TPG (16×8 maximum) inside `cxp_device_top` at `app_clk` 12 ns, `tx_clk` 8 ns, `rx_clk` 10 ns with `p_ASYNC_CLOCKS = 1`. Width = 12 and Height = 6 are written over the uplink; the golden `cxp_protocol` reassembler checks that the second image is 12×6 with 6 lines of 3 words and no CRC, tag or DsizeP error. It is the only test that checks the TPG's header Xsize/Ysize on the wire. Pixel values, DsizeL, SourceTag and PixelF are not compared.
- `src/tb_unit/top/cxp_stream_top`: its wrapper instantiates the TPG at 8×4 Mono8 with `cfg_xsize`, `cfg_ysize`, `cfg_pixfmt`, `cfg_xoffs`, `cfg_yoffs` and `cfg_srctag` tied to 0, and takes `meta_o` into a `cxp_meta_t`. `cfg_testpat_i` is left unconnected, which Verilator reports as PINMISSING and reads as 0. `test_01_packet_framing`, `test_02_full_frame_decode` and `test_03_kmask_passthrough` use this path. Their golden header asserts DsizeL = 8 bytes and SourceTag 0 on every frame; since the TPG counts images, `test_02_full_frame_decode` fails on the second frame's SourceTag (merged word 45 reads 0x01010101, 2026-09-22 run).
- `src/verif/uvm/sv/tb_cxp_top.sv` and `src/verif/uvm/agents/cfg_agent.py` drive the TPG through `cxp_interface_top` with `cfg_run` and the TestPattern register. Not run today.
- `src/emu/bridge/tb/cxp_hw_env.sv` instantiates `cxp_interface_top` with a parameter that no longer exists (`p_LINK_RESET_CLEAR_CYCLES`), so it does not elaborate at HEAD.
- `src/emu/cxp/sim/virtual_camera.py` mirrors the four patterns in Python. Its gradient scrolls one step per frame; the RTL gradient is static.

### Running

```
cd src/tb_unit/app/cxp_app_tpg && make WAVES=0 COCOTB_TEST_FILTER=test_09_flat_pattern
make -C src/tb_unit/top/cxp_device_top WAVES=0   # TPG image decoded from the wire, three clocks
make -C src/tb_unit test_pattern_gen      # this TB; `make -C src/tb_unit` runs every TB
```

2026-09-19, commit `9604050` (RTL, SVA and TB files differ from the commit only in line endings): unit TB 11/11; `src/tb_unit/top/cxp_interface_top` 13/13; `src/tb_unit/top/cxp_stream_top` 12/12; `src/tb_unit/top/cxp_device_top` 7/7. Full regression not re-run. 2026-09-22, working tree with run-time offsets and the SourceTag count: unit TB 13/13; `src/tb_unit/top/cxp_stream_top` `test_02_full_frame_decode` fails on its SourceTag-0 golden (Other).

### Not covered in-tree

- Reset during a frame. A probe shows valid drops asynchronously and the flat level restarts at 0x00 → Medium 5.
- `cfg_run_i` high at reset release. The SOF follows 1 cycle after the first sampling edge, by code → Medium 5.
- Counter wrap: flat level after 16 frames, and `frame_cnt_q` wrap at 256 → Medium 5.
- Widths that are not a multiple of 8, widths below 8, 1×1 frames, size above the maximum, and 65535 → Medium 5 and Minor 1.
- Indefinite stall, and random back-pressure. A probe shows stability holds → Medium 5.
- Codes outside the decode → Critical 1.
- Pixel bits 15:8 set, and bit patterns above 0xFF → Medium 2.
- Multi-clock operation: covered by `cxp_device_top` (12/10 ns app/rx) with the configuration held static during the image; a register write landing on the `load_size` edge is not targeted. The crossing delivers one coherent snapshot by construction.
- End-to-end: `cxp_device_top` test_04_stream checks header Xsize/Ysize and the line structure; no test compares TPG pixel values or the other header fields on the wire → Medium 4.
- X-propagation: `cfg_testpat_i` floating, as in the stream_top wrapper. Only Verilator is run → Minor 3.

## Known issues and recommendations

### Critical

1. **The header announces pixel formats the payload does not carry.** A probe showed RGB8 0x0401 and arbitrary codes such as 0x1234 pass unchanged to `meta_o.pixfmt` while the packer uses 8-bit mono. *Fix:* reject unsupported codes at the PixelFormat register write (Table 22 0x41). *Effort:* with the register-file value checks.

### Medium

1. ~~**Stop path untested.**~~ — fixed 2026-09-20: `test_12_stop_at_frame_end` drops `cfg_run` mid-frame and checks the frame completes, that no pixel is offered for 200 cycles afterwards, and that a restart frames again; the arc `ST_EMIT → ST_IDLE` now has hits. The test does not check `meta_o` while idle, nor that a restart latches *new* configuration — TC 2–5 already cover the latch at frame start.
2. **Only 8-bit values reach the packer.** For Mono10–16 the high pixel bits are always 0, so no TPG-driven test anywhere in the tree toggles the packer's upper bit lanes, and the image uses at most 255 of the range. *Fix:* scale the pattern to the container, for example `pix_val << (cbits_q − 8)`, or add a full-width ramp pattern, which needs a 3-bit `cfg_testpat_i`. *Effort:* 0.5 day with mirror updates.
3. **Multi-bit configuration crossing — resolved at the top.** `cfg_xsize_i`, `cfg_ysize_i`, `cfg_pixfmt_i`, `cfg_testpat_i`, `cfg_xoffs_i` and `cfg_yoffs_i` are sampled on the `load_size` edge, and `cfg_srctag_i` is compared every cycle. `cxp_interface_top` now delivers them on `app_clk` as one handshaked `cxp_cdc_bus` snapshot when `p_ASYNC_CLOCKS` = 1, so a register write can no longer latch a torn value; a write lands on the frame after the snapshot arrives. The module itself still assumes `app_clk` inputs; any other integrator must provide the same crossing.
4. **Metadata consistency at the top.** Fixed 2026-09-26: the PixelFormat override no longer bypasses the TPG latch (TPG mode takes the TPG's latched format, and the header takes PixelF from the packer). Fixed 2026-09-27: StreamFlags (0x10034) and TapGeometry (0x10028, which takes only 0) reach the TPG header through `cxp_device_top` like OffsetX, OffsetY, SourceTag and Image1StreamID; no header field of a TPG image is a parameter any more. StreamFlags takes any byte (the XML describes it as the header flag byte, 0..255), so a host can set the reserved Flags bits 7:2 or interlace code 3 (Table 38); the interface_top TB wrapper ties all of them to constants. Width and Height above the compiled maximum are clamped silently while the register still reads the host value (the register file refuses only values outside 1..4096); the interface_top TB reads 640 against an 8-pixel image. *Fix:* Clamp or reject Width and Height above the compiled maximum in the register file. Add an end-to-end header check to `test_02_stream_from_tpg`. *Effort:* 1 day.
5. **Unit-TB gaps.** No test covers reset mid-frame, run high at reset release, random back-pressure, widths not a multiple of 8 or below 8, 1×1, clamping above the maximum, or the flat 16-frame wrap. Probes showed the RTL behaves as described in How it works. *Fix:* add directed tests with `check_frame`. *Effort:* 0.5 day.

### Minor

1. **Header comment.** It claims "exactly eight equal-width vertical bars (each xsize/8 wide)", but a probe at xsize 12 gave bars 1,1,1,1,1,1,1,5 wide and at xsize 5 one 0xFF band. It describes the Python gradient as `x & 0xFF`, but the Python gradient is `(x+y+frame)`. Its "Created 2025-05-27" predates the repository. *Fix:* reword. *Effort:* 15 min.
2. **Parameter guards.** The `p_X_SIZE`/`p_Y_SIZE` > 0 checks are now generate-if `$error` blocks, so the bench builds (and synthesis tools that honour elaboration-time `$error`) reject them. Nothing yet rejects values above 65535. *Fix:* extend the two blocks to 0 < size ≤ 65535. *Effort:* 5 min.
3. **TB hygiene.**
   - The unit TB docstring says "instantiated directly (no wrapper)", which is false.
   - `test_07_cfg_pixfmt_zero_falls_back_to_parameter` duplicates the baseline.
   - The stream_top wrapper leaves `cfg_testpat_i` unconnected.
   *Effort:* 30 min.
4. **`frame_cnt_q`** is 8 bits but only bits 3:0 are used. *Fix:* shrink it to 4 bits, or document the 16-frame flat period. *Effort:* 5 min.
5. **SVA to add:**
   - `m_pix_valid_o && !m_pix_ready_i |=> $stable({m_pix_data_o, m_pix_sof_o, m_pix_sol_o, m_pix_eol_o, m_pix_eof_o}) && m_pix_valid_o`
   - `m_pix_sof_o |-> m_pix_sol_o`
   - `m_pix_eof_o |-> m_pix_eol_o`
   - `$changed(meta_o.xsize) |-> m_pix_sof_o || state_q == ST_IDLE`

   *Effort:* 1 hour.

### Open questions

1. Designer: should the TPG generate container-width values, the full 10–16-bit range, so that every packer lane and the Mono10–16 image range are exercised?
2. Designer: for a PixelFormat the TPG cannot produce, should it fall back to `p_PIXFMT`, refuse to start, or have the register file reject the write?
3. Designer: should Width and Height writes above the compiled maximum be clamped in the register file, so the host reads back the size actually streamed?
4. Verification: should `src/tb_unit/top/cxp_interface_top` gain a pixel-content and header-field check on the wire, or is that left to `src/verif/`?
