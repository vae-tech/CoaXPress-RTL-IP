# cxp_app_acq_ctrl

Inputs chosen: RTL `src/rtl/app/cxp_app_acq_ctrl.sv` (package `cxp_pkg`: `cxp_pix_t`); user `src/rtl/top/cxp_interface_top.sv`, driven from `src/rtl/top/cxp_device_top.sv`; unit TB `src/tb_unit/app/cxp_app_acq_ctrl/`; system TB `src/tb_unit/top/cxp_device_top/`; spec JIIA CXP-001-2015 v1.1.1 §11.2.1.4 (AcquisitionStart), §11.2.1.5 (AcquisitionStop), Table 44 (StreamPacketSizeMax 0 = no stream), §10.3.32; GenICam SFNC AcquisitionMode / AcquisitionFrameCount.

Acquisition control and the pixel gate. Every pixel of either source (test pattern or sensor) passes here on its way to `cxp_app_pixel_packer`, one whole image at a time: at an image's first pixel the gate decides whether the image enters (or, for a source that gates itself on `active_o`, takes it as it comes), and an image that entered always completes. So AcquisitionStart / AcquisitionStop, a ConnectionReset and a StreamPacketSizeMax too small for one packet all act on image boundaries.

| Mode (`acq_mode_i`) | Images per AcquisitionStart | `cxp_device_top` selects it when |
|---|---|---|
| 0 Continuous | until AcquisitionStop | TpgRun bit 0 = 1 |
| 1 SingleFrame | 1 | never |
| 2 MultiFrame | `frame_count_i` (0 counts as 1) | TpgRun bit 0 = 0 |
| 3 | as 0 (`limit` = 0) | never |

Source: `src/rtl/app/cxp_app_acq_ctrl.sv`. One instance, `cxp_interface_top.cxp_app_acq_ctrl_i`, on `app_clk`. Its input is `sel_pix`, the `cfg_use_tpg` mux of the TPG and the ingress; its output `gate_pix` feeds the packer. `active_o` (`acq_active`) runs the TPG (`cfg_run_i = acq_active & cfg_app.use_tpg`), and `src_gated_i` is tied to `cfg_app.use_tpg`. In `cxp_device_top`, `acq_start_i` is the AcquisitionStart write pulse, `acq_stop_i` the AcquisitionStop write pulse ORed with the ConnectionReset write pulse, both crossed from `rx_clk` by `cxp_cdc_pulse`; `run_i` is the top port `cfg_run_i`; `stream_en_i` is StreamPacketSizeMax ≥ 36 bytes; mode and count come from TpgRun and FrameCount.

## Interface

No parameters.

| Name | Dir | Width | Description |
|---|---|---|---|
| `clk` | in | 1 | Pixel clock (`app_clk`) |
| `rst_n` | in | 1 | Active-low, asynchronous assert |
| `acq_start_i` | in | 1 | One-cycle AcquisitionStart: arm, restart the image count |
| `acq_stop_i` | in | 1 | One-cycle AcquisitionStop: disarm |
| `acq_mode_i` | in | 2 | 0 continuous, 1 single, 2 multi (table above) |
| `frame_count_i` | in | 16 | MultiFrame image count, 0 counts as 1 |
| `run_i` | in | 1 | Level. Admit images as if armed, without an acquisition (a free-running test pattern) |
| `stream_en_i` | in | 1 | Level. A stream packet fits (StreamPacketSizeMax); while low no image enters |
| `active_o` | out | 1 | A new image starting now would enter. Combinational |
| `src_gated_i` | in | 1 | Level. The source starts images only while `active_o` is high (the test pattern): every image it starts enters |
| `s_pix_i` | in | `cxp_pix_t` | Pixel of the selected source: data, valid, sof, sol, eol, eof |
| `s_pix_ready_o` | out | 1 | Pixel taken: `m_pix_ready_i` for a pixel of an entered image, 1 for every other pixel (dropped). Combinational |
| `m_pix_o` | out | `cxp_pix_t` | `s_pix_i` with `valid` cleared unless the pixel belongs to an entered image |
| `m_pix_ready_i` | in | 1 | Packer takes the pixel |

- **Reset:** all four registers (`armed_q`, `done_q`, `in_img_q`, `pass_q`) reset asynchronously to 0: disarmed, outside any image.
- **Clocking:** one domain. The start / stop pulses and the configuration levels arrive already crossed to `app_clk` by `cxp_interface_top`.
- **Combinational paths:** `s_pix_ready_o` depends on `m_pix_ready_i`, `s_pix_i.sof`, `run_i`, `stream_en_i` and the registers; `m_pix_o.valid` on `s_pix_i.valid`/`sof` and the same levels; `active_o` on the levels and, in the cycle an image ends, on the pixel handshake. The ready path therefore runs from `cxp_app_stream` through the packer and this gate to the TPG or the ingress in one cycle.

## How it works

1. **Arm.** `acq_start_i` sets `armed_q` and clears `done_q`; `acq_stop_i` clears `armed_q`. `limit` = 1 (mode 1), `frame_count_i` or 1 (mode 2), 0 = unlimited (modes 0 and 3). `last` = the image now ending is the `limit`-th.
2. **Admit.** `admit = (armed_q | run_i) & stream_en_i`. At a pixel with `sof` the decision is `pass = admit | src_gated_i`, stored in `pass_q` when the pixel is taken; inside an image `pass = pass_q`. `in_img = sof | in_img_q`; `in_img_q` is set by a taken pixel of an image and cleared by its `eof`.
3. **Gate.** A pixel of an image that entered (`in_img & pass`) goes to the packer with its handshake: `m_pix_o.valid = s_pix_i.valid`, `s_pix_ready_o = m_pix_ready_i`. Every other pixel, of an image that did not enter or outside any image (a sensor running before its first SOF, or the rest of an image after a reset), is taken (`s_pix_ready_o = 1`) and dropped. A source that cannot be stopped is therefore never held off by a stopped acquisition.
4. **Count.** `img_end` = the last pixel (`eof`) of an entered image is taken. If `armed_q`, `done_q` counts it, and at the `last` image `armed_q` clears: images are counted as they leave the gate, not as a source starts them.
5. **`active_o`** = `admit & ~(img_end & last & ~run_i)`: whether a new image would enter. It drops already in the cycle the last image of a SingleFrame / MultiFrame acquisition ends, so the TPG, which decides at its EOF whether to roll into another image, sees it.
6. **Self-gated source** (`src_gated_i` = 1, the TPG in `cxp_interface_top`). The TPG decides from `active_o` whether to start an image a few cycles before that image's first pixel reaches the gate (through the packer's pipeline back-pressure and its own EOF-to-SOF step). With `src_gated_i` the gate trusts that decision and passes every image the source starts, so an image begun while `active_o` was high is not dropped because the acquisition stopped (or `stream_en_i` fell) in between. Without it such an image was dropped whole (seen in PyUVM `test_xifc_full`). The count and `active_o` are unchanged. With `src_gated_i` = 1 and a source that does not obey `active_o`, the gate would pass everything: it is only for such a source.

No FSM; `in_img_q`/`pass_q` are the image state, `armed_q`/`done_q` the acquisition.

Same-cycle rules:
- `acq_start_i` and `acq_stop_i` together: start wins (assigned last).
- `acq_stop_i` inside an entered image: the image completes (`pass_q` holds); the next SOF finds `admit` = 0 unless `run_i`.
- `acq_start_i` inside an image that did not enter: that image stays dropped; the next SOF enters.
- A SOF while an image is open (a sensor that cut its image short): the gate decides afresh for the new image. The cut image never produced `img_end`, so it is not counted.
- `stream_en_i` falling inside an entered image: the image still passes the gate; the framer drops what reaches it while the stream is disabled (`cxp_device_top.md` Minor 7).

Latency: none; the gate is combinational. Throughput: one pixel per cycle.

## Arbiter integration

Not applicable: no downlink port. It decides which images reach `cxp_app_stream`; the stream framer and the arbiter see only whole images.

## Verification

Verilator 5.046 + cocotb 2.0.1. No SVA is bound to this module; no FSM coverage.

### Unit TB — `src/tb_unit/app/cxp_app_acq_ctrl/`

Wrapper `tb_cxp_app_acq_ctrl_top`: ports without `_i`/`_o`, `src_gated_i` tied to 0 (the sensor behaviour), a 16-bit pixel bus packed into `cxp_pix_t` with `sol` = `sof` and `eol` = `eof`, and `out_data`/`out_valid` unpacked from `m_pix_o`. Clock 10 ns. `reset()` sets mode, count and `stream_en`, `run` = 0, pixels idle, `out_ready` = 1, holds `rst_n` 3 cycles. `end_frame` passes a one-pixel image (SOF and EOF on one pixel) and returns `active` in that cycle.

| Test | Stimulus | Expect |
|---|---|---|
| `test_01_start` | reset; AcquisitionStart | `active` 0, then 1 |
| `test_02_continuous` | mode 0; start; 5 images; stop | `active` 1 through the images, 0 after stop |
| `test_03_single_frame` | mode 1; start; 1 image | `active` 0 already in the image's last cycle |
| `test_04_multi_frame` | mode 2, count 3; then count 0 | drops at the 3rd image; with 0 at the 1st |
| `test_05_stream_disabled` | `stream_en` 0; start; `stream_en` 1 | `active` 0, then 1 |
| `test_06_restart` | mode 2, count 2; one image; start again | two more images allowed |
| `test_07_pixel_gate` | mode 0: 3 pixels without SOF; a 6-pixel image while stopped; start; a 6-pixel image with stop at its 3rd pixel; another 6-pixel image | every pixel taken; only the image begun after the start comes out, all 6 pixels |

#### test_07_pixel_gate
- *Stimulus*: as in the table; a watcher records `out_data` on every `out_valid & out_ready` cycle.
- *Checks*: `pix_ready` = 1 on every pixel; the output is exactly 0x30..0x35.
- *Proves*: stray pixels and an image begun while stopped are taken and dropped; an image that entered completes across AcquisitionStop (§11.2.1.5); the image after the stop does not enter.

Tests 1–6 check `active_o` only; since the change their images also pass the gate (one pixel each), which exercises `img_end` and the count.

### System TB — `src/tb_unit/top/cxp_device_top/`

At `app_clk` 12 ns behind the register file, with the golden reassembler on the downlink (`cxp_device_top.md`):
- `test_10_powerup_is_connection_reset`: AcquisitionStart without StreamPacketSizeMax → no stream packet.
- `test_20_acq_start_stop_sensor`: a free-running sensor (it cannot be stopped); no packet before AcquisitionStart; whole images after; AcquisitionStop mid-frame → at most the image in progress.
- `test_19_stop_tpg_mid_packet`: `run_i` dropped and restored at 11 points of a packet; every image whole.
- `test_21_spsm_below_minimal_packet`, `test_23_spsm_negotiation`: `stream_en_i` held low below 36 bytes.
- `src/tb_unit/top/cxp_interface_top` test_19 runs the sensor with `run_i` = 1.

### Running

```
make -C src/tb_unit/app/cxp_app_acq_ctrl WAVES=0 COCOTB_TEST_FILTER=test_07_pixel_gate
make -C src/tb_unit          # regression
```

2026-09-26 (uncommitted working tree): pixel gate, `run_i` and test_07 added; not run for this document.

### Not covered in-tree

- `m_pix_ready_i` = 0 (the unit wrapper holds `out_ready` at 1), so back-pressure through the gate is checked only end to end.
- `run_i` in the unit TB; `run_i` with SingleFrame / MultiFrame (the image count is not advanced for images entered by `run_i` alone).
- A SOF inside an open image, and `stream_en_i` falling inside an image.
- `src_gated_i` = 1 at unit level (tied 0); in the system it is exercised by every TPG test, but no `src/tb_unit` test stops the acquisition between the TPG's decision and its first pixel reaching the gate.
- Start and stop in the same cycle; MultiFrame end to end (`cxp_device_top` never selects mode 1, and no test there sets TpgRun = 0 with a FrameCount).

## Known issues and recommendations

### Critical

None.

### Medium

1. **Unit-TB gaps** (Not covered). The gate's hold path (`m_pix_ready_i` = 0 inside an entered image) and `run_i` are untested at unit level. *Fix:* add back-pressure to test_07 and a `run_i` test in each mode. *Effort:* 1 h.

### Minor

1. **A cut image is not counted.** With MultiFrame, a sensor image cut short (SOF inside an open image) enters but never ends, so the acquisition sends one more image than FrameCount, the extra one incomplete. Count at the SOF of an entered image, or document it with the sensor contract (`cxp_app_pixel_ingress.md`).
2. **Fixed: the gate could drop an image the TPG had begun.** The TPG decides from `active_o` a few cycles before its first pixel reaches the gate; a stop in between made the gate drop that image whole, and the TPG's SourceTag skipped it. The gate now takes the TPG's images as they come (`src_gated_i`, How it works 6).
3. **Long combinational ready path.** `s_pix_ready_o` passes `m_pix_ready_i` straight through; with the packer and the ingress also combinational, the sensor's ready is a single-cycle path from `cxp_app_stream`. Register it if timing needs to.
4. **SVA to add:** `m_pix_o.valid |-> in_img & pass`; `!(in_img & pass) |-> s_pix_ready_o`; `$fell(armed_q) |-> $past(acq_stop_i || (img_end && last))`.
5. The instance banner in `cxp_interface_top` ("which test-pattern images start") predates the gate; it now covers both sources.

### Open questions

1. Designer: should AcquisitionMode (stored without effect, `cxp_device_top.md` Medium 1) select SingleFrame / MultiFrame instead of TpgRun?
2. Designer: should images admitted by `run_i` alone count toward FrameCount?
