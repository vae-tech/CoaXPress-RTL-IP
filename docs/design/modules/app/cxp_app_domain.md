# cxp_app_domain

Inputs chosen from the tree: RTL `src/rtl/app/cxp_app_domain.sv` (children `cxp_app_acq_ctrl`, `cxp_app_tpg`, `cxp_app_pixel_ingress`, `cxp_app_pixel_packer`, `cxp_app_stream`); lint waivers `src/rtl/cxp_ip.vlt`; integration TBs `src/tb_unit/top/cxp_interface_top/`, `src/tb_unit/top/cxp_device_top/`, `src/verif/`; spec JIIA CXP-001-2015 v1.1.1 §9.4, Table 38, §11.2.1.4/5, Table 44; output `docs/design/modules/app/cxp_app_domain.md`.

Everything of `cxp_interface_top` that runs on `app_clk`: the two pixel sources, the source mux, the acquisition gate, the packer and the app side of the stream path. Its configuration and acquisition events arrive already crossed by `cxp_cdc_layer`; its stream words leave into the stream FIFO's write side, also in `cxp_cdc_layer`. Created 2026-09-27 by moving the `app_clk` logic of `cxp_interface_top` unchanged (the top-level traces of every bench are identical before and after).

```
cxp_app_tpg --,
                       +-> mux (cfg_i.use_tpg) -> cxp_app_acq_ctrl -> cxp_app_pixel_packer -> bswap32
s_pix_*, s_meta_i   ---'   pixel + cxp_meta_t      (image gate)                         |
  cxp_app_pixel_ingress                                                     cxp_app_stream -> m_*_o
```

Source: `src/rtl/app/cxp_app_domain.sv`, one instance `cxp_interface_top.cxp_app_domain_i`.

## Interface

| Name | Default | Meaning |
|---|---|---|
| `p_TPG_X_SIZE` / `p_TPG_Y_SIZE` | 64 / 32 | TPG maximum image. |
| `p_FIFO_DEPTH` | 1024 | Depth of the stream FIFO: `cxp_app_stream` cuts packets of at most `p_FIFO_DEPTH` − 8 words. ≥ 16 (checked there). |
| `p_PIX_W` | 16 | Sensor pixel width, 1…16 (elaboration `$error`, `g_chk_pix_w`). |

| Name | Dir | Width | Description |
|---|---|---|---|
| `app_clk`, `app_rst_n` | in | 1 | Pixel clock, asynchronous active-low reset released on `app_clk`. |
| `cfg_i` | in | `cxp_cfg_app_t` | Pixel-path configuration, crossed from `rx_clk`: source, run, acquisition mode / count, stream enable, TPG geometry, PixelFormat, StreamID, offsets, SourceTag, TapG, Flags, pattern, marker form, DsizeP. |
| `acq_start_i`, `acq_stop_i` | in | 1 | AcquisitionStart / AcquisitionStop pulses, crossed. |
| `s_pix_*`, `s_pix_ready_o` | in / out | 16, 1 | Sensor single-pixel bus (`cfg_i.use_tpg` = 0). |
| `s_meta_i` | in | `cxp_meta_t` | Sensor frame metadata, latched with the image's first pixel. |
| `pix_restart_o`, `pix_stray_eof_o` | out | 1 | A sensor SOF inside an open image; an EOF outside one (`cxp_app_pixel_ingress`). |
| `m_data_o`, `m_kmask_o`, `m_valid_o`, `m_sop_o`, `m_eop_o`, `m_streamid_o` | out | 32, 4, 1, 1, 1, 8 | Stream words into `cxp_cdc_stream_fifo`'s write side, packet boundaries marked. |
| `m_ready_i`, `flush_i` | in | 1 | The FIFO takes the word; the FIFO's write side is flushing. |

## How it works

1. **Source mux.** `cfg_i.use_tpg` picks the TPG's or the ingress's `cxp_pix_t` stream and `cxp_meta_t` bundle and routes `ready` back to that source only. `sel_meta.arbitrary` = `cfg_i.arbitrary`; on the sensor branch a non-zero `cfg_i.pixfmt` overrides the sensor's format.
2. **Acquisition gate.** `cxp_app_acq_ctrl` lets an image in only if it starts while the acquisition is armed (or `cfg_i.run`) and `cfg_i.stream_en` is 1; an image that entered completes. The TPG runs only while `acq_active` (it stops itself when no image would enter).
3. **Packer and stream words.** The packer latches format and metadata with the SOF pixel; its word is byte-swapped (`bswap32`), and the frame / line start pulses are its SOF / SOL flags gated by the accept (`pk_word_valid & sof & ready`). `cxp_app_stream` merges header, markers and pixel words and cuts packets (`cxp_app_stream.md`); the header carries the packer's metadata with the packer's format (`img_meta`).

Unconsumed, waived in `cxp_ip.vlt`: `ing_meta_valid` (the ingress's capture pulse), the packer's `lane_vld` and `eol`.

## Verification

No bench of its own; it is covered, unchanged, by every test that streams through `cxp_interface_top` (`src/tb_unit/top/cxp_interface_top`, 16 stream-related tests), `cxp_device_top` (38 tests, `p_ASYNC_CLOCKS` = 1) and the PyUVM tiers. The split was proven by comparing, with a fixed seed, every top-level signal of all 30 benches before and after (value-change digests identical).

## Known issues and recommendations

The findings of its children stay on their pages (`cxp_app_acq_ctrl.md`, `cxp_app_tpg.md`, `cxp_app_pixel_ingress.md`, `cxp_app_pixel_packer.md`, `cxp_app_stream.md`). None is new here.
