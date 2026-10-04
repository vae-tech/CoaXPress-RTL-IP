# cxp_app_pixel_ingress

Inputs chosen from the tree: RTL `src/rtl/app/cxp_app_pixel_ingress.sv` (no package dependencies); unit TB `src/tb_unit/app/cxp_app_pixel_ingress/`; integration TB `src/tb_unit/top/cxp_interface_top/`; spec JIIA CXP-001-2015 v1.1.1 (`docs/spec/CXP-001-2015.pdf`) §9.4, §9.4.6.1, §9.4.6.2 (Table 38), plus the module plan `docs/design/cxp_camera_ip_modules.md` §2.1; regression `make -C src/tb_unit`; output `docs/design/modules/app/cxp_app_pixel_ingress.md`.

Adapts the sensor-side AXI4-Stream-like single-pixel bus into the IP: one pixel per cycle in, one pixel per cycle out through a 1-entry register, with a derived start-of-line flag, frame gating, and a per-frame latch of the geometry fields that end up in the Table 38 image header.

| Beat (output) | Content |
|---|---|
| `m_pix_data_o` | one pixel, `p_PIX_W` bits, LSB-justified |
| `m_pix_sof_o` / `m_pix_eof_o` | copied from the sensor beat |
| `m_pix_eol_o` | copied from the sensor beat |
| `m_pix_sol_o` | derived: 1 on the SOF pixel and on the first pixel after an EOL that was not also an EOF |
| `m_meta_o` (`cxp_meta_t`) | Xoffs, Yoffs, Xsize, Ysize (24 bit each), PixelF, TapG, SourceTag (16 bit each), StreamID, Flags (8 bit each) and the `arbitrary` bit, latched from `s_meta_i` on the accepted SOF pixel |

Source: `src/rtl/app/cxp_app_pixel_ingress.sv`. One instance, `cxp_interface_top.cxp_app_pixel_ingress_i`, fed by the top-level `s_pix_*` / `s_meta` ports with `s_pix_valid_i = s_pix_valid & ~cfg_use_tpg`. Its pixel outputs go through the TPG/external mux and the acquisition gate `cxp_app_acq_ctrl` into `cxp_app_pixel_packer`. `spurious_eof_o` and `sof_restart_o` leave `cxp_interface_top` as `sb_pix_stray_eof_pulse` and `sb_pix_restart_pulse` (`cxp_device_top` leaves both unconnected). Its `m_meta_*` outputs form the sensor branch's metadata bundle `ing_meta`: the packer latches it with the image's first pixel and the image header and line markers take that copy, so a sensor may present the next image's metadata as soon as this image's last pixel is taken, however short the image (`cxp_device_top` test 36; before 2026-09-27 the raw `ext_meta_*` levels were read at the packer's first word, and an image of four pixels or fewer carried the next image's header). `m_meta_valid_o` is not read.

Spec clauses: none implemented directly. The module supplies the SOF/SOL events that drive the "header before the first line, line marker before each line" rules of §9.4.6.1 and the six geometry fields of Table 38.

## Interface

| Name | Default | Meaning |
|---|---|---|
| `p_PIX_W` | 16 | Pixel width in bits. Any value ≥ 1 standalone; 1–16 inside `cxp_interface_top`, which slices `s_pix_data[p_PIX_W-1:0]` from a 16-bit port. Capacity is fixed at 1 pixel. |

| Name | Dir | Width | Description |
|---|---|---|---|
| `app_clk` | in | 1 | Only clock |
| `app_rst_n` | in | 1 | Active-low. Asynchronous assert in both `always_ff` blocks; deassertion must be synchronised by the caller |
| `s_pix_data_i` | in | `p_PIX_W` | Pixel value |
| `s_pix_valid_i` | in | 1 | Sensor beat valid |
| `s_pix_sof_i` | in | 1 | Start-of-frame flag on the beat |
| `s_pix_eol_i` | in | 1 | End-of-line flag on the beat |
| `s_pix_eof_i` | in | 1 | End-of-frame flag on the beat |
| `s_pix_ready_o` | out | 1 | `~buf_valid_q | m_pix_ready_i`, combinational |
| `s_pix_xoffs_i` | in | 24 | X offset, sampled on the accepted SOF beat |
| `s_pix_yoffs_i` | in | 24 | Y offset, sampled on the accepted SOF beat |
| `s_pix_xsize_i` | in | 24 | X size, sampled on the accepted SOF beat |
| `s_pix_ysize_i` | in | 24 | Y size, sampled on the accepted SOF beat |
| `s_pix_pixfmt_i` | in | 16 | GenICam pixel format, sampled on the accepted SOF beat |
| `s_pix_tapg_i` | in | 16 | Tap geometry, sampled on the accepted SOF beat |
| `s_pix_streamid_i` | in | 8 | StreamID (sampled on the SOF pixel) |
| `s_pix_sourcetag_i` | in | 16 | SourceTag (sampled on the SOF pixel) |
| `s_pix_flags_i` | in | 8 | Image-header flags (sampled on the SOF pixel) |
| `m_pix_data_o` | out | `p_PIX_W` | Registered pixel |
| `m_pix_valid_o` | out | 1 | Registered, `buf_valid_q` |
| `m_pix_sol_o` | out | 1 | Registered, derived start-of-line |
| `m_pix_eol_o` | out | 1 | Registered copy of `s_pix_eol_i` |
| `m_pix_sof_o` | out | 1 | Registered copy of `s_pix_sof_i` |
| `m_pix_eof_o` | out | 1 | Registered copy of `s_pix_eof_i` |
| `m_pix_ready_i` | in | 1 | Downstream ready |
| `m_meta_o` | out | `cxp_meta_t` | The metadata latched with the open frame's SOF pixel |
| `m_meta_valid_o` | out | 1 | Registered 1-cycle pulse, rises on the same edge as `m_pix_sof_o` |
| `spurious_eof_o` | out | 1 | Combinational: a beat with EOF is being dropped because no frame is open |
| `sof_restart_o` | out | 1 | Combinational: a SOF beat is accepted while a frame is open, i.e. the open frame was cut off and a new one starts |

Notes:

- Reset: asynchronous assert, all registers to 0. The port comment says only "sync-deassert reset"; the assert is asynchronous.
- Single clock domain, no CDC.
- All `m_pix_*` and `m_meta_*` outputs are registered. `s_pix_ready_o`, `spurious_eof_o` and `sof_restart_o` are combinational; `s_pix_ready_o` depends directly on `m_pix_ready_i`, so downstream back-pressure reaches the sensor in the same cycle (not a true skid buffer, despite the header comment).
- Stall: with `m_pix_ready_i = 0` the buffer holds one pixel and `s_pix_ready_o` drops the cycle after the first accept. No timeout.
- The header comment "`m_meta_valid_o` leads the skid-delayed `m_pix_sof_o` by one cycle" (`cxp_app_pixel_ingress.sv:166`) is wrong: both rise on the same edge (confirmed by simulation, probe not in repo), see test_01 diagram.

## How it works

1. **Frame gate.** `pix_is_real = in_frame_q | (s_pix_valid_i & s_pix_sof_i)`. A beat is accepted (`accept_in`) when valid, ready and real; otherwise it is dropped (`drop_in`) and, if it carries EOF, `spurious_eof_o` pulses for that cycle. `in_frame_q` sets on an accepted SOF and clears on an accepted EOF (same-cycle SOF+EOF: accepted, `in_frame_q` returns to 0, one-pixel frame). A SOF arriving while `in_frame_q = 1` is accepted as a new frame and pulses `sof_restart_o` (`accept_in & s_pix_sof_i & in_frame_q`); the previous frame's last pixel, held in the one-pixel look-ahead stage, leaves with EOL and EOF set. Every pixel except an EOF waits there until the sensor's next pixel arrives, so the cut is known before the pixel moves on. The cut frame therefore ends like any other: `cxp_app_acq_ctrl` counts it, `cxp_app_pixel_packer` flushes its last word, and the stream closes its last packet even when the next image is not admitted (review TX-01, fixed 2026-10-04; before, the cut image's open packet sat in the store-and-forward FIFO until a later acquisition). The metadata taken with a SOF pixel travels with it through the look-ahead stage, so a one-pixel frame cut at once keeps its own.
2. **SOL derivation.** `sol_in = s_pix_sof_i | pending_sol_q`; `pending_sol_q <= eol & ~eof` on every accept. Invariant, not asserted: `pending_sol_q = 1 ⇒ in_frame_q = 1`, so the `else if (drop_in) pending_sol_q <= 0` branch (`cxp_app_pixel_ingress.sv:159`) is unreachable.
3. **1-entry register.** `skid_free = ~buf_valid_q | m_pix_ready_i`. Same-cycle rule: dequeue (`buf_valid_q & m_pix_ready_i`) and enqueue (`accept_in`) in one cycle keep `buf_valid_q = 1` with the new pixel. Latency accept-to-`m_pix_valid_o`: 1 cycle. Throughput: 1 pixel/cycle while `m_pix_ready_i = 1`.
4. **Metadata latch.** `meta_capture = accept_in & s_pix_sof_i`. The whole `s_meta_i` struct loads on that edge, `m_meta_valid_o` is high for the following cycle only; the fields hold until the next accepted SOF.

No FSM.

## Arbiter integration

Not applicable: the module sits in the `app_clk` pixel path ahead of `cxp_app_acq_ctrl` and `cxp_app_pixel_packer`. The gate takes and drops every pixel of an image that does not enter (acquisition stopped, StreamPacketSizeMax below one packet), so a running sensor is not held off then. The only arbitration it meets is the `cfg_use_tpg` mux in `cxp_interface_top`, which masks `s_pix_valid_i` and forces `m_pix_ready_i = 0` while the TPG is selected. State (`buf_valid_q`, `in_frame_q`, `pending_sol_q`) freezes across such a switch and resumes on switch-back; while TPG is selected with a pixel buffered, `s_pix_ready` to the sensor stays 0.

## Verification

Verilator 5.046, cocotb 2.0.1, `cxp_test` wrapper from `src/verif/common`; no SVA; no FSM coverage (no FSM registered).

### Unit TB — `src/tb_unit/app/cxp_app_pixel_ingress/`

Wrapper `tb_cxp_app_pixel_ingress_top.sv` (parameter `PIX_W = 16`, ports without `_i/_o`), clock 10 ns, reset: all inputs 0, `app_rst_n` low 4 cycles, release, 1 cycle. Shared helpers: `make_frame` (pixel `(x+y) & 0xFF`), `drive_frame` (one beat per cycle honouring `s_pix_ready`, geometry driven on every beat of the frame), `capture` (samples every `m_pix_valid & m_pix_ready` beat), `check_frame` (count + bit-exact data/sol/eol/sof/eof per beat).

| Test | Stimulus | Expect |
|---|---|---|
| test_01_valid_frame_ingest | 8×4 frame, `m_pix_ready = 1` | 32 beats exact; metadata = GEOM |
| test_02_backpressure_no_loss | 8×4 frame, random valid gaps, random `m_pix_ready` | 32 beats exact |
| test_03_spurious_eof_dropped | 3 stray beats (eof 0,1,0), then 8×2 frame | `spurious_eof` seen; no output; frame exact, first beat SOF |
| test_04_meta_change_midframe | two 4×2 frames, geometry g1 then g2 | xoffs latched = [1, 9] |
| test_05_reset_midframe | frame interrupted after 10 cycles, reset, full frame | outputs 0 after reset; frame exact, first beat SOF+SOL |
| test_06_sof_restart | 5 pixels of a frame (no EOL/EOF), then a whole 8×4 frame | `sof_restart` pulses once; second frame exact, SOF+SOL first, EOF last |
| test_07_cut_frame_ends | 10 pixels (no EOF), idle, a 4×2 frame; a one-pixel cut frame, a 4×1 frame | 9 pixels out while the 10th waits; it leaves with EOL+EOF; one-pixel frame keeps its metadata (fails on the old RTL) |

#### test_01_valid_frame_ingest

*Stimulus*: GEOM = xoffs 0x10, yoffs 0x20, xsize 8, ysize 4, pixfmt 0x0101, tapg 0 (the plan's 1024×768 is scaled to 8×4). 32 beats back-to-back, one per cycle, SOF on beat 0, EOL on x = 7, EOF on the last beat. `m_pix_ready = 1` throughout. Capture stops 8 cycles after the last beat. A watcher records `m_meta_*` whenever `m_meta_valid = 1`.

*Checks*: `check_frame` (32 beats, each equal); recorded metadata equals GEOM.

*Proves*: accept path; SOL on SOF and after each EOL (`pending_sol_q`); EOF clears `in_frame_q`; six-field capture on SOF. Does not check the timing of `m_meta_valid` against `m_pix_sof`.

```wavedrom
{ "signal": [
  { "name": "app_clk",        "wave": "p......." },
  { "name": "s_pix_valid_i",  "wave": "01....0." },
  { "name": "s_pix_sof_i",    "wave": "010....." },
  { "name": "s_pix_data_i",   "wave": "x22222x.", "data": ["p0","p1","p2","p3","p4"] },
  { "name": "s_pix_ready_o",  "wave": "1......." },
  { "name": "m_pix_valid_o",  "wave": "0.1....0" },
  { "name": "m_pix_sof_o",    "wave": "0.10....", "node": "..A....." },
  { "name": "m_pix_sol_o",    "wave": "0.10...." },
  { "name": "m_pix_data_o",   "wave": "x.22222x", "data": ["p0","p1","p2","p3","p4"] },
  { "name": "m_meta_valid_o", "wave": "0.10...." },
  { "name": "m_meta_xoffs_o", "wave": "2.3.....", "data": ["0","0x10"] }
], "head": { "text": "A: first captured beat; meta pulse and SOF coincide" } }
```

#### test_02_backpressure_no_loss

*Stimulus*: `random.seed(2)`. 8×4 frame; before each beat, with probability 0.3, `s_pix_valid` is low for 1–3 cycles. `m_pix_ready` is re-randomised (0/1) every cycle by the capture task. After the driver finishes `m_pix_ready = 1` for 20 cycles, then capture stops.

*Checks*: `check_frame` (32 beats, order and flags exact).

*Proves*: stall via `skid_free`, same-cycle dequeue+enqueue, no loss or duplication under both-side gaps.

```wavedrom
{ "signal": [
  { "name": "app_clk",       "wave": "p......." },
  { "name": "s_pix_valid_i", "wave": "1....0.." },
  { "name": "s_pix_data_i",  "wave": "23..4x..", "data": ["A","B","C"] },
  { "name": "m_pix_ready_i", "wave": "10.1.01." },
  { "name": "s_pix_ready_o", "wave": "10.1.01." },
  { "name": "m_pix_valid_o", "wave": "01.....0" },
  { "name": "m_pix_data_o",  "wave": "x2..34.x", "data": ["A","B","C"], "node": "...A...." }
], "head": { "text": "A: B enqueued and A dequeued on the same edge" } }
```

#### test_03_spurious_eof_dropped

*Stimulus*: `m_pix_ready = 1`. Three consecutive beats with `s_pix_valid = 1`, `s_pix_sof = 0`, data 0xAB, `s_pix_eof` = 0, 1, 0, one cycle each; then valid low. A watcher polls `spurious_eof` for 12 cycles. Then an 8×2 frame with GEOM, capture stops 8 cycles after it.

*Checks*: `spurious_eof` was 1 at least once; `m_pix_valid = 0` after the 12-cycle window; `check_frame` (16 beats); `out[0].sof = 1`.

*Proves*: `drop_in` path, `pix_is_real` gating, `spurious_eof_o` on the EOF beat only. Not checked: `m_meta_valid` stays 0 and `m_meta_*` stay 0 during the strays (plan criterion 3), and that the two non-EOF strays raise nothing.

```wavedrom
{ "signal": [
  { "name": "app_clk",       "wave": "p....." },
  { "name": "s_pix_valid_i", "wave": "01110." },
  { "name": "s_pix_sof_i",   "wave": "0....." },
  { "name": "s_pix_eof_i",   "wave": "0010.." },
  { "name": "s_pix_ready_o", "wave": "1....." },
  { "name": "spurious_eof_o","wave": "0010..", "node": "..A..." },
  { "name": "m_pix_valid_o", "wave": "0....." },
  { "name": "in_frame_q",    "wave": "0....." }
], "head": { "text": "A: combinational pulse, sampled by the watcher after this edge" } }
```

#### test_04_meta_change_midframe

*Stimulus*: `m_pix_ready = 1`. g1 = (xoffs 1, yoffs 2, 4×2, pixfmt 0x0101, tapg 0), g2 = (xoffs 9, yoffs 8, 4×2, pixfmt 0x0103, tapg 1). Frame f1 driven with g1, then frame f2 driven with g2 immediately after (no idle beat between f1's EOF and f2's SOF). A watcher records `m_meta_xoffs` at every `m_meta_valid` pulse for 80 cycles.

*Checks*: recorded list equals `[1, 9]`.

*Proves*: one capture per SOF; back-to-back frames (EOF beat followed directly by a SOF beat) re-open the gate. Incidentally covers `in_frame_q` clear→set on adjacent beats.

**The geometry never changes mid-frame.** `drive_frame` presents the frame's own geometry on every beat, so the inputs flip from g1 to g2 exactly on f2's SOF beat, the cycle the latch is meant to re-capture. Because xoffs is only sampled at `m_meta_valid` pulses, a transparent latch (fields following the inputs) would also produce `[1, 9]`. The hold behaviour named in the test docstring is not exercised.

#### test_05_reset_midframe

*Stimulus*: `m_pix_ready = 1`. 8×4 frame driver started as a task; after 10 clock edges the task is killed (about 10 beats accepted: line 0 and the first two beats of line 1, not "the first two lines" as the comment says). `do_reset`: all inputs and `m_pix_ready` to 0, `app_rst_n` low 4 cycles, release, 1 cycle. Then `m_pix_ready = 1` and the same 8×4 frame from a fresh SOF, capture stops 8 cycles after it.

*Checks*: `m_pix_valid = 0` and `m_meta_valid = 0` right after reset; `check_frame` (32 beats); `out[0].sof = 1 and out[0].sol = 1`.

*Proves*: asynchronous reset clears `buf_valid_q` and `m_meta_valid_o`. The reset of `in_frame_q` and `pending_sol_q` is not observable here: a SOF beat is accepted and gets SOL either way. `task.kill()` prints a DeprecationWarning under cocotb 2.0.

#### test_06_sof_restart

*Stimulus*: `m_pix_ready = 1`. The first 5 pixels of a frame (0xE0..0xE4, SOF on the first, no EOL, no EOF), then a whole 8×4 frame with GEOM. `sof_restart` sampled every cycle.

*Checks*: `sof_restart` is 1 in exactly one cycle; the 32 beats after the first 5 equal the second frame; its first beat has SOF and SOL, its last EOF.

*Proves*: `sof_restart_o` fires on a SOF inside an open frame and only there (not on the first SOF), and the new frame passes intact. What the packer and the stream do with the cut frame is covered in their own benches (`cxp_app_pixel_packer` test_18).

### Integration TB — `src/tb_unit/top/cxp_interface_top/`

`src/tb_unit/top/cxp_interface_top`: only `test_19_trig_phase_sweep_100` drives the sensor bus (`cfg_use_tpg` = 0, `cfg_run` = 1, six 128×32 Mono8 frames, one pixel per cycle); it checks trigger timing on the wire, not the stream content.

`src/tb_unit/top/cxp_device_top`: `test_20_acq_start_stop_sensor` drives a free-running 16×8 Mono8 sensor through `cxp_device_top` (the wrapper exposes `s_pix_*` and builds `s_meta_i`). The golden reassembler checks whole images with no CRC, tag or DsizeP error; no stream packet before AcquisitionStart; at most the image in progress after AcquisitionStop. No test cuts a sensor image short or observes `sb_pix_*` above the unit TB.

| Test | Checks |
|---|---|
| `cxp_device_top` test_20_acq_start_stop_sensor | sensor images on the wire, gated by AcquisitionStart / Stop |
| `cxp_interface_top` test_19_trig_phase_sweep_100 | sensor traffic as load for the trigger check |

### Other

- `src/tb_unit/top/cxp_stream_top/` (`test_04_skid_*` … `test_11_skid_*`, 8 tests): drive `cxp_app_stream`'s external word path with the "pulse coincident with data" convention that the ingress+packer pair produces. They model the ingress; they do not instantiate it.
- `src/verif/uvm/agents/video_agent.py` (PyUVM): drives `cxp_interface_top.s_pix_*` through the ingress with random valid gaps and honours `s_pix_ready`; the monitor rebuilds 32-bit words for the stream scoreboard. This is the only in-tree end-to-end exercise of the module. Not run here.
- `src/emu/bridge/Makefile` compiles the module into the emulation build; no test there targets it.

### Running

```
make -C src/tb_unit/app/cxp_app_pixel_ingress                         # unit TB, Verilator
COCOTB_TEST_FILTER=test_04 make -C src/tb_unit/app/cxp_app_pixel_ingress
make -C src/tb_unit/top/cxp_interface_top                         # integration TB
make -C src/tb_unit                                           # regression
```

2026-09-15, commit bbd6372: unit TB 5/5 pass; integration TB 13/13 pass. Full regression not re-run. 2026-09-26 (uncommitted working tree): `sof_restart_o` and test_06 added; not re-run for this document. Tip: after changing `WAVES=` delete `sim_build` before rerunning.

### Not covered in-tree

- Reset during activity: only `m_pix_valid`/`m_meta_valid` checked (test_05); `in_frame_q`, `pending_sol_q`, `m_meta_*` values after reset unobserved.
- Input active at reset release: not tested (by code: valid without SOF is dropped, valid with SOF is accepted on the first cycle).
- Counter saturation/wrap: no counters in the module.
- Indefinite stall: `m_pix_ready_i = 0` held for longer than the 1–3-cycle random gaps of test_02 (by code: holds one pixel forever, no timeout).
- Forced idle mid-transaction: `cfg_use_tpg` toggled while a pixel is buffered or a frame is open (currently freezes state and resumes on switch-back — intent undecided).
- Multi-clock operation: single clock, nothing to cover.
- End-to-end path: covered by `cxp_device_top` test_20 for well-formed Mono8 frames; a cut frame end to end is not covered.
- X-propagation: not tested; every register has a reset value, `s_pix_data_i` is only captured under `accept_in`.
- SOF while a frame is open: covered by test_06 (restart, `sof_restart_o`).
- EOF without EOL on the same beat (currently accepted; downstream gets EOF with `m_pix_eol_o = 0` — intent undecided).
- Single-beat frame (SOF+EOF on one beat).
- Mid-frame geometry change with the latch holding (see test_04).
- `p_PIX_W` other than 16.
- `m_meta_valid_o` timing relative to `m_pix_sof_o` (both rise on the same edge; not asserted by any test).

## Known issues and recommendations

### Critical

1. **Fixed: the latched metadata was dead in `cxp_interface_top`.** Since 2026-09-27 the sensor branch of the mux takes `ing_meta` (this module's latch), the packer latches it again at its SOF pixel and the header and line markers take the packer's copy (`cxp_device_top` test 36). The metadata travels as one `cxp_meta_t` from `cxp_device_top.s_meta_i` to `m_meta_o`. `m_meta_valid_o` is still unconsumed at the top (waived).
2. **test_04 cannot detect a non-holding latch.** See the test's bold note. Fix: keep g1 on beats 0–2 of f1 and switch the geometry inputs to g2 from beat 3 onward; sample all six `m_meta_*` fields on every cycle of f1 and assert they equal g1 until f2's `m_meta_valid`; then assert g2. Effort: 1 h.

### Medium

1. **Fixed: a SOF inside an open frame was accepted silently.** It now pulses `sof_restart_o` (`sb_pix_restart_pulse` at the top), and the packer starts the new frame on an empty accumulator, so the next image is clean (test_06; `cxp_app_pixel_packer` test_18). The cut image stays incomplete on the wire: an integration contract (How it works 1).
2. **Resolved: no `src/tb_unit` test drove the sensor path end to end.** `cxp_device_top` test_20 streams sensor images to the wire and reassembles them. Still missing: a cut image end to end, and a format other than Mono8. Effort: 0.5 day.
3. **`s_pix_ready_o` is a combinational pass-through of `m_pix_ready_i`** (Interface notes). The sensor's ready path runs through the mux and `cxp_app_pixel_packer` in one cycle. If that is a timing problem, replace the register with a 2-entry skid whose ready is registered; test_02 already covers the resulting behaviour. Effort: 0.5 day.
4. **State freezes across a `cfg_use_tpg` switch** (Arbiter integration). Either clear `buf_valid_q`/`in_frame_q`/`pending_sol_q` on a `cfg_use_tpg` edge, or document that the source may only be switched with the sensor idle and no pixel buffered; add a test for the chosen rule. Effort: 2 h.
5. **Narrow checks in test_03 and test_05.** test_03: assert `m_meta_valid = 0` and `m_meta_* = 0` throughout the stray window and that `spurious_eof` is 1 only on the EOF beat. test_05: force reset one beat after an EOL so `pending_sol_q = 1` and `in_frame_q = 1` at reset, then assert the post-reset frame's second line still gets SOL only from its own EOL. Effort: 1 h.
6. **The status pulses reach no register.** Stray non-EOF beats are dropped without any flag. `spurious_eof_o` and `sof_restart_o` are one-cycle combinational pulses; `cxp_interface_top` exports them, but `cxp_device_top` leaves them unconnected, so the host cannot see a cut or stray frame. Register them (sticky bit or counter) and make them readable; add a `spurious_pix_o` (or count) alongside (`cxp_interface_top.md` Minor 6). Effort: 1 h.

### Minor

- Port comment for `app_rst_n`: state "async assert, sync deassert".
- Remove the unreachable `else if (drop_in) pending_sol_q <= 1'b0` branch or replace it with an assertion `pending_sol_q |-> in_frame_q`.
- Add SVA: `m_meta_valid_o |-> m_pix_valid_o && m_pix_sof_o`; `$fell(app_rst_n) |=> !m_pix_valid_o`.
- `docs/design/cxp_camera_ip_modules.md` §2.1 is stale: `PIX_W = 12`, port names without `_i/_o`, no `spurious_eof`, spec ref "§7.4" (v1.0 numbering; the RTL header says §9.4).
- `cxp_app_image_header.sv:13` says `meta_valid_i` is the SOF tick from `cxp_app_pixel_ingress`; in `cxp_interface_top` it is the packer's SOF-word accept.
- test_05: replace `drv.kill()` with `drv.cancel()`; fix the "first two lines" comment.

### Open questions

1. Answered 2026-09-26: a SOF arriving while a frame is open is accepted as a restart and flagged (`sof_restart_o`); the cut image is not repaired.
2. Designer: is `m_meta_*` meant to feed the image header (then Critical 1 is a wiring bug), or is `ext_meta_*` a level contract where the sensor holds geometry stable for the whole frame? If the latter, the ingress latch and `m_meta_valid_o` are dead logic and should be removed.
3. Designer: Table 38 also carries DsizeL, StreamID, SourceTag and Flags, which the ingress does not latch. Should they be latched at SOF too, and should DsizeL be derived from Xsize and pixel format instead of being an input?
4. Designer: is a combinational sensor-side ready acceptable for the target sensor interface, or is a registered ready required?
5. Verification: is the PyUVM sensor-path run the sign-off for this module end-to-end, or does `src/tb_unit` need its own integration test (Medium 2)?

No repository files other than this document were changed.
