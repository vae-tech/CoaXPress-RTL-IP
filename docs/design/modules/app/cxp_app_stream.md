# cxp_app_stream

Inputs chosen from the tree: RTL `src/rtl/app/cxp_app_stream.sv` (children `cxp_app_image_header`, `cxp_app_line_marker` and their shared `cxp_app_marker_seq`; downstream `cxp_cdc_stream_fifo` in `cxp_cdc_layer` and `cxp_tx_stream_pkt` on `cxp_tx_pkt_framer` and `cxp_lib_crc32` in `cxp_tx_domain`; packages `cxp_pkg.sv`, `cxp_util_pkg.sv`); bound SVA `src/sva/cxp_sva.sv`; stream-path TB `src/tb_unit/top/cxp_stream_top/` (it keeps the name of the former `cxp_stream_top`); integration TBs `src/tb_unit/top/cxp_interface_top/` and `src/tb_unit/top/cxp_device_top/`; spec JIIA CXP-001-2015 v1.1.1 (`docs/spec/CXP-001-2015.pdf`) §8.2.5.2, §8.5.1–8.5.3, §9.2–9.4; regression `make -C src/tb_unit`; output `docs/design/modules/app/cxp_app_stream.md`.

The `app_clk` half of the device's stream path. It merges image headers, line markers and a 32-bit pixel-word stream into one word flow and cuts that flow into `cfg_dsizeP_i`-word blocks, which it writes into the stream FIFO with their boundaries and StreamID. Until 2026-09-27 this module, the FIFO and the framer were one module, `cxp_stream_top`, which straddled `app_clk` and `tx_clk`; the three now sit in `cxp_app_domain`, `cxp_cdc_layer` and `cxp_tx_domain`, wired as before. This page describes the path they form; the FIFO and the framer have their own pages. The packet on the wire (Table 19):

| Word | Content | Produced by |
|---|---|---|
| SOP | 4×K27.7, kmask 0xF | `cxp_tx_stream_pkt` |
| — | 4×0x01 (stream data) | `cxp_tx_stream_pkt` |
| 0 | 4×Stream ID = `meta_i.streamid` of the image the packet opens in, from the FIFO descriptor | `cxp_tx_stream_pkt` |
| 1 | 4×PacketTag, per Stream ID | `cxp_tx_stream_pkt` |
| 2, 3 | 4×DsizeP[15:8], 4×DsizeP[7:0] = the packet's own length from the FIFO descriptor | `cxp_tx_stream_pkt` |
| 4 … N+3 | N merged words: image header, line markers, pixel words | this module (merger + chopper) |
| N+4 | CRC (convention in `docs/design/modules/lib/cxp_lib_crc32.md`) | `cxp_tx_stream_pkt` |
| EOP | 4×K29.7, kmask 0xF | `cxp_tx_stream_pkt` |

Source: `src/rtl/app/cxp_app_stream.sv`. One instance, `cxp_interface_top.cxp_app_domain_i.cxp_app_stream_i`. It is fed by the pixel packer output, byte-swapped with `bswap32`, with `pix_frame_start_i`/`pix_line_start_i` = `pk_word_valid & sof/sol & ready`, `pix_word_eof_i` = `pk_word_eof`, and by `img_meta`, the TPG's or the external `cxp_meta_t` with `arbitrary` taken from `cfg.arbitrary` and PixelF from the packer. Its words reach port `TX_PORT_STREAM` (2, the lowest of the three long-packet ports) of `cxp_tx_arbiter` through the FIFO and the framer. Spec clauses: §8.5.1 (Table 19 framing, via the framer), §8.5.2 (packet size), §8.5.3 (PacketTag reset, via the framer), §9.2/§9.4 (marker order in the stream).

## Interface

| Name | Default | Meaning |
|---|---|---|
| `p_FIFO_DEPTH` | 1024 | Depth of the stream FIFO it writes, in 32-bit entries; sets `PKT_MAX` = `p_FIFO_DEPTH` − 8, the longest packet (store and forward). ≥ 16, checked here (below that `PKT_MAX` is 0, no packet closes by its count and the first image stops the stream — a depth of 8 sent no packet at all); the FIFO itself checks a power of two ≥ 4. `cxp_interface_top` passes its own `p_FIFO_DEPTH` (default 1024) to both; the stream-path TB, the interface_top TB and the device_top TB use 256. |

| Name | Dir | Width | Description |
|---|---|---|---|
| `app_clk`, `app_rst_n` | in | 1 | Pixel-side clock and active-low reset. |
| `cfg_dsizeP_i` | in | 16 | Data words N per packet. Read by the chopper only; the framer takes each packet's DsizeP from the FIFO descriptor. |
| `pix_word_data_i` | in | 32 | Pixel word, first-transmitted pixel in `[7:0]`. |
| `pix_word_valid_i` | in | 1 | Pixel word valid. |
| `pix_word_eof_i` | in | 1 | The word is the image's last pixel word: the chopper closes the packet with it. |
| `pix_word_ready_o` | out | 1 | Combinational `pix_d_take`: 1 only while `pix_word_valid_i` = 1 and the skid is empty or firing. |
| `pix_frame_start_i` | in | 1 | Frame pulse, drives `cxp_app_image_header.meta_valid_i`. |
| `pix_line_start_i` | in | 1 | Line pulse, drives `cxp_app_line_marker.line_start_i`. |
| `meta_i` | in | `cxp_meta_t` (177) | Frame metadata and marker form, sampled on `pix_frame_start_i`: the image header reads it in that cycle, the line markers read the copy held from it (`frame_meta_q`). `arbitrary` selects rectangular or arbitrary headers and markers; `streamid` (8 bit) also goes to the FIFO descriptor. |
| `m_data_o`, `m_kmask_o` | out | 32, 4 | Merged word and per-byte K flag, to `cxp_cdc_stream_fifo.s_data_i` / `s_kmask_i`. |
| `m_valid_o` | out | 1 | `merge_valid & ~drop`. |
| `m_sop_o`, `m_eop_o` | out | 1 | Chopper boundaries: `word_cnt_q` = 0; count reached or the image's last pixel word. |
| `m_streamid_o` | out | 8 | `frame_meta_q.streamid`, taken by the FIFO at the SOP. |
| `m_ready_i` | in | 1 | `cxp_cdc_stream_fifo.s_ready_o` (almost-full backpressure). |
| `flush_i` | in | 1 | `cxp_cdc_stream_fifo.s_flush_o`: the FIFO's write side is flushing; drop until the next image (How it works 5). |

Downstream, in `cxp_tx_domain`: `cxp_tx_stream_pkt.stream_ctrl_reset_i` = ConnectionReset level | ConnectionConfig write (PacketTags to 0), `stream_en_i` = `cfg_tx.stream_en` (Table 44), `suppress_stream_i` = TestMode (§8.7.4), and the FIFO's `flush_i` = the flush request (ConnectionReset, a held ConnectionConfig write, TestMode).

Notes:
- **Reset:** both resets assert asynchronously (`always_ff … or negedge`). There are no reset synchronisers. A reset of one side alone is absorbed by the FIFO: through `cxp_cdc_link` its read side discards and its write side stalls until both sides are up again, so nothing is replayed and the stream does not wedge (`docs/design/modules/cdc/cxp_cdc_stream_fifo.md` How it works 5). A packet the framer is reading is finished before the discard starts; `cxp_device_top` never resets one side alone anyway.
- **Clock domains:** this module is `app_clk` only; the synchronised crossing of the path is the FIFO (gray pointers, 2 flops); each packet's length and StreamID cross with it in the packet descriptor, written before the packet is announced. `cfg_dsizeP_i` and `meta_i` are `app_clk`-only; `suppress_stream_i`, `stream_ctrl_reset_i`, `stream_en_i` and `flush_i`/`flush_ack_o` are `tx_clk`-only. The flush request reaches the app side through the FIFO (`s_flush_o`, one `cxp_cdc_sync`).
- **Output registration:** the path's packet output (`cxp_tx_stream_pkt.m_o`) is combinational. In the framer's data state `m_o.data`/`m_o.valid` come straight from the FIFO's LUTRAM head and FIFO `m_ready` = `m_ready_i`. Because a packet starts only when it is stored whole, `m_o.valid` stays 1 from its SOP to its EOP.
- **Stall:** `m_ready_i` = 0 stalls the framer; the FIFO fills; at depth − 4 `merge_ready` drops; the generators and skid hold; `pix_word_ready_o` = 0.
- **Comments vs code:** the port comment says the pulses fire "one cycle BEFORE the first pixel word", but the only instantiation fires them on the accept cycle of that word (How it works 1). The header says the skid "adds zero observable latency" and "bypasses fully in steady state". There is no bypass: every pixel word spends exactly one `app_clk` cycle in `pix_d_data_q`.

## How it works

1. **Skid register and priority merger.** `pix_d_take = pix_word_valid_i & (~pix_d_valid_q | pix_d_fire)` and `pix_d_fire = pix_d_valid_q & merge_ready & ~hdr_word_valid & ~line_word_valid`. The merger is combinational with priority header > line marker > skid. `hdr_word_ready = merge_ready`; `line_word_ready = merge_ready & ~hdr_word_valid`. Pixel words get kmask `KMASK_NONE`; marker words keep the generator's kmask. Both generators send their words through `cxp_app_marker_seq` (`docs/design/modules/app/cxp_app_marker_seq.md`).
   - Same-cycle rule: a pulse sampled on the edge that accepts a word makes the generator valid in the next cycle, while the skid holds that word. The skid cannot fire until the generator is done, so the markers go first (unit-TB wave below).
   - Pulse-ahead: a pulse on a `valid` = 0 cycle is order-safe only if the skid fires in that lead cycle. If `merge_ready` = 0 there, the marker overtakes the previous line's last pixel. A pulse held high while its word is not accepted re-triggers the generators and livelocks the source. Both hazards are observed in `docs/design/modules/app/cxp_app_image_header.md` and `docs/design/modules/app/cxp_app_line_marker.md` (Medium 1 there); the fix belongs in this module (Medium 1).
2. **DsizeP chopper.** `word_cnt_q` counts accepted merged words (`chop_fire = merge_valid & merge_ready`). The block length is `pkt_words` = `cfg_dsizeP_i`, or `PKT_MAX` = `p_FIFO_DEPTH` − 8 when `cfg_dsizeP_i` is 0 or larger (a block must fit the FIFO whole, How it works 3). `chop_sop = (word_cnt_q == 0)`; `chop_eop = (word_cnt_q >= pkt_words − 1) | merge_eof`. `merge_eof` is the skid word's `pix_word_eof_i`, the packer's mark on the image's last pixel word (`cxp_interface_top` wires `pk_word_eof`), so every image ends its own block and its last packet is as short as the image leaves it (§8.5.2 comment, Figure 21). `>=` closes the open block at once when a smaller size arrives mid-block. A source that never marks its last word gets a residual block that stays open until the next frame's words fill it (`test_12_chopper_residual_across_frame_boundary`).
3. **CDC FIFO, store and forward.** Each entry is `{data, kmask, sop, eop}`; beside each block's SOP entry the FIFO keeps a descriptor `{StreamID, length}`, written when the block's EOP entry is written. The StreamID is `frame_meta_q.streamid`, from the image's `meta_i` latched at its frame-start pulse; the same latch feeds the line-marker generator, so every line marker of an image carries its Xsize/Xoffs even when the source's metadata has moved on while the pipeline still holds the last line. The framer starts a block only when `m_pkt_avail_o` says a whole block is stored and takes DsizeP and the StreamID from the descriptor (`m_len_o`, `m_streamid_o`), so the header always matches the payload and a packet on the wire never waits for its source. §8.2.5.2 expects stream packets to be fully buffered.
4. **Framer.** `cxp_tx_stream_pkt` (see `docs/design/modules/tx/cxp_tx_stream_pkt.md`), built on the shared `cxp_tx_pkt_framer`, sends 6 header words, N data words, the CRC and K29.7, and a started packet always runs to its trailer. It keeps one PacketTag per Stream ID. `stream_ctrl_reset_i` clears the tags. `suppress_stream_i` only stops the next packet from starting: its SOP entry waits unconsumed at the FIFO head, and the FIFO fills behind it until TestMode's flush empties it. Its `busy_o` (a packet being framed, or one being dropped whole while `stream_en_i` is low) goes to the FIFO's `m_busy_i`.
5. **Flush** (`flush_i` / `flush_ack_o`, `tx_clk`). The FIFO discards what it holds once the packet the framer is reading (if any) has left, and passes the request to `app_clk` as `s_flush_o` (`fifo_s_flush`). On `app_clk`, `drop = fifo_s_flush | drop_q`, with `drop_q <= fifo_s_flush | (drop_q & ~pix_frame_start_i)`: from the cycle the request is seen until an image starts after it is released, nothing reaches the FIFO (`s_valid_i = merge_valid & ~drop`) while `merge_ready` stays 1, so the skid, the header and line-marker generators drain into nothing (pixel words, image headers and line markers are all dropped), and `word_cnt_q` is held at 0 so the next word kept opens a block. The image-start cycle itself still drops: the skid may still hold a word of the dropped image, and the new image's header follows one cycle later. An image that starts while the request is still high is dropped whole. So the first packet after a flush opens a whole image with its header (§9.4). `flush_ack_o` rises once both sides are empty; `cxp_interface_top` then releases the request (ConnectionReset echo, ConnectionConfig hold) or holds it for as long as TestMode lasts.

No FSM in this module; the children's FSMs are in their own documents.

Latency and throughput:
- Pixel accept → FIFO write: 1 `app_clk` (skid). First merged word presented to the FIFO → K27.7 presented on `m_o.data`: 31 ns (about 4 `tx_clk`) at 10/8 ns, measured. The first data word follows 6 header words later.
- The merger moves 1 word per `app_clk`. Per rectangular frame, the source is stalled 25 cycles for the header plus 2 cycles per line for the marker (unit-TB trace, `merge_ready` = 1). Wire efficiency is N/(N + 8).

Invariants: `chop_eop` fires every N accepted words or on the image's last pixel word, and the merged order is header, then (marker, line pixels) × Y provided the pulses are accept-gated; neither is asserted. The FIFO never holds more than `p_FIFO_DEPTH` words and never pops when empty; both are asserted by the bound `cxp_cdc_stream_fifo_sva`.

## Arbiter integration

- **Slot and policy:** stream is port `TX_PORT_STREAM` (2), the lowest of the three long-packet ports of `cxp_tx_arbiter` (control acknowledgment > connection test > stream). The arbiter picks a port only between packets, on a valid SOP; once `m_o.sop` is taken the stream owns the arbiter until `m_o.eop` is taken.
- **Handshake:** `m_ready_i` is the arbiter's combinational `ready_o[TX_PORT_STREAM]`, which passes `cxp_tx_inserter.long_ready_o`; `m_o.valid`/`m_o.data` are combinational from the FIFO head. A word taken in cycle *k* is on `cxp_if_data_o` in cycle *k*+1 (registered in the inserter).
- **What is inserted into the stream:** triggers, I/O acknowledgments and IDLE words, at any word boundary, by `cxp_tx_inserter` (§8.2.4, §8.2.5); this module sees them only as cycles with `m_ready_i` = 0, and the packet resumes afterwards (`cxp_device_top` test_27, test_31).
- **What waits for the stream:** control acknowledgments and connection-test packets wait for the stream packet's EOP (N + 8 words, N ≤ `p_FIFO_DEPTH` − 8).
- **Store-and-forward contract:** there is no watchdog and no way to drop a packet. The owner must offer a word in every cycle it holds the arbiter (`cxp_tx_owner_sva`, bound in `cxp_interface_top`); this module meets it because the framer starts a packet only when the FIFO holds all of it (How it works 3).
- **TestMode:** `suppress_stream_i` stops new stream packets from starting; the packet already on the wire completes with its CRC and EOP. TestMode also raises `flush_i`: the FIFO discards everything stored once that packet has left, and the app side drops the pixel source's output for as long as TestMode lasts, so the source is not stalled and nothing produced during TestMode goes out afterwards; the stream resumes with the next whole image (`cxp_device_top` test_22, test_28).

## Verification

Verilator 5.046 with cocotb 2.0.1, built with `--assert`. The bound checkers in `src/sva/cxp_sva.sv` that sit inside this module run in every bench below: `cxp_cdc_stream_fifo_sva` (the FIFO never holds more than `p_DEPTH` words; no pop when empty) and `cxp_framer_sva` inside `cxp_tx_pkt_framer` (SOP/EOP only on valid words, no second SOP inside a packet, header, CRC and EOP words held while not accepted). None fired. No FSM coverage is collected: this module has no FSM and the TB registers none of its children's.

### Unit TB — `src/tb_unit/top/cxp_stream_top/`

Wrapper `tb_cxp_stream_top` instantiates `cxp_app_stream`, `cxp_cdc_stream_fifo` and `cxp_tx_stream_pkt` wired as in `cxp_interface_top` (until 2026-09-27: `cxp_stream_top`), with `p_FIFO_DEPTH` 256, ties `stream_ctrl_reset_i`, `suppress_stream_i` and `flush_i` to 0 and `stream_en_i` to 1, leaves `flush_ack_o` open, and drives `cfg_dsizeP_i` from its `cfg_dsizeP`. The TPG path ties `pix_word_eof_i` to the packer's end-of-frame mark; the ext path drives it from `ext_pix_word_eof`. `pix_sel` = 0 routes a local 8×4 Mono8 `cxp_app_tpg` → `cxp_app_pixel_packer` → byte swap, with ready-gated `sof`/`sol` pulses and the TPG's `meta_o`: a copy of the `cxp_interface_top` glue. `pix_sel` = 1 routes Python-driven `ext_pix_*` ports and `ext_meta_*` ports packed into a `cxp_meta_t`. The wrapper's `cfg_arbitrary` sets `meta_i.arbitrary` on both paths. `app_clk` 10 ns, `tx_clk` 8 ns, `cfg_dsizeP` 11, `cfg_arbitrary` 0 except in test_16. `bringup()` holds both resets low for 8 `tx_clk` cycles, releases them together, and waits 8 more. Shared checkers:
- `capture()` sets `m_ready` each `tx_clk` cycle and records accepted beats.
- `split_packets()` asserts no SOP inside an open packet and no EOP outside one.
- `check_packet_framing()` asserts length N + 8, the 6 header words (kmask 0xF only on K27.7), sop/eop = 0 on data, the CRC word against the golden §8.2.2.2 CRC over the covered words (register, LSByte in P0), and K29.7 with kmask 0xF.
- The ext drivers hold `fs`/`ls` with the word until it is accepted. Unless a test marks the last word with `ext_pix_word_eof`, they append pad words (`flush_chopper`) so that the last packet closes.

| Test | Stimulus | Expect |
|---|---|---|
| test_01_packet_framing | TPG, 2000 cycles | ≥ 4 packets, envelope OK, tags 0,1,2,… |
| test_02_full_frame_decode | TPG, 8000 cycles | ≥ 10 packets; payload = golden frames with SourceTag 0 then 1 |
| test_03_kmask_passthrough | TPG, 4000 cycles | data-slot kmask ∈ {0, 0xF}; ≥ 5 marker words |
| test_04_skid_same_cycle_pulse_first_marker | ext, 1 frame, same-cycle pulses | packet 0 payload word 0 = 4×K28.3 |
| test_05_skid_same_cycle_full_frame_decode | same | 41 payload words = golden |
| test_06_skid_line_marker_lead | same | 5 K28.3; each line marker → 0x02 → line's first pixel |
| test_07_skid_holds_under_backpressure | same, `m_ready` 0 for 200 cycles | packet 0 payload word 0 = K28.3 |
| test_08_skid_both_pulse_conventions_equivalent | same-cycle frame, then pulse-ahead frame | both = golden and equal |
| test_09_skid_bursty_producer | 2 idle cycles between words | 41 words = golden |
| test_10_skid_back_to_back_frames | 2 frames, one pad | 82 words = golden × 2; 10 K28.3 |
| test_11_skid_idle_no_packets | no valid, 1500 cycles | 0 packets |
| test_12_chopper_residual_across_frame_boundary | 2 frames, no end-of-frame mark, one pad | frame 2 header at payload index 41, packet offset 8 |
| test_13_eof_closes_short_packet | 2 frames, end-of-frame mark, no pad | 8 packets (11, 11, 11, 8 per frame), headers at packets 0 and 4 |
| test_14_tail_packet_keeps_streamid | StreamID 0x05, changed to 0 after the last word | 4 packets, all StreamID 0x05 |
| test_15_dsizeP_lowered_mid_packet | `cfg_dsizeP` 11 → 3 with a packet open, 9 frames | DsizeP = payload ≤ 11 on every packet; payload = golden × 9 |
| test_16_line_markers_keep_frame_meta | arbitrary form, Xsize/Xoffs → 0 after line 0 | 4 line markers with Xsize 8, Xoffs 4, DsizeL 2 |

#### test_01_packet_framing
- *Stimulus*: `pix_sel` 0, `cfg_run` = 1 after bring-up, `m_ready` = 1, capture 2000 `tx_clk` cycles.
- *Checks*: `split_packets` rules; ≥ 4 packets; `check_packet_framing` on every packet with tag = packet index.
- *Proves*: the Table 19 envelope around chopper blocks, and per-Stream-ID tag increment from 0.

#### test_02_full_frame_decode
- *Stimulus*: as above, 8000 cycles.
- *Checks*: ≥ 10 packets (2 frames of 4 packets + 2); framing of all with tag = index mod 256; the concatenated payload equals the golden 41-word frame with SourceTag 0 followed by the same frame with SourceTag 1 (the TPG counts images), word by word including kmask, over min(captured, 82) ≥ 41 words.
- *Proves*: merger order and chopper continuity across packet and frame boundaries with the ready-gated TPG source.

#### test_03_kmask_passthrough
- *Stimulus*: as above, 4000 cycles.
- *Checks*: framing of every packet; every data-slot kmask is 0 or 0xF; ≥ 5 slots with kmask 0xF; ≥ 1 with kmask 0.
- *Proves*: generator kmask reaches the wire through merger, FIFO and framer; pixel words get kmask 0. It does not check that a 0xF slot carries 0x7C.

#### test_04_skid_same_cycle_pulse_first_marker
- *Stimulus*: `pix_sel` 1, `ext_meta` = golden metadata. One frame of 8 words; `fs` and `ls` on the first word, `ls` on the first word of each line, each word held until accepted; 3 pad words; 200 `app_clk` cycles of drain. `m_ready` = 1, 8000-cycle capture.
- *Checks*: ≥ 1 packet; packet 0 payload word 0 = 4×K28.3 with kmask 0xF.
- *Proves*: the same-cycle rule. The skid parks the first pixel while 25 header and 2 marker words pass (`pix_d_fire` = 0 while `hdr_word_valid` or `line_word_valid`).

```wavedrom
{"signal":[
 {"name":"app_clk","wave":"p..|......"},
 {"name":"pix_word_valid","wave":"1..|......"},
 {"name":"pix_frame_start","wave":"10.|......"},
 {"name":"pix_line_start","wave":"10.|....10"},
 {"name":"pix_word_ready","wave":"10.|...1.0"},
 {"name":"pix_d_valid_q","wave":"01.|......"},
 {"name":"hdr_word_valid","wave":"01.|.0...."},
 {"name":"line_word_valid","wave":"01.|...0.1"},
 {"name":"merge_data","wave":"x==|======","data":["K28.3","0x01","hdr 24","K28.3","0x02","P0","P1","K28.3"],"node":".a........"},
 {"name":"word_cnt_q","wave":"=.=|======","data":["0","1","2","3","4","5","6","7"]}
],
"head":{"text":"accept cycle, then header words 0-24, line marker, pixels; a = payload word 0 checked"}}
```

#### test_05_skid_same_cycle_full_frame_decode
- *Stimulus*: as test_04_skid_same_cycle_pulse_first_marker.
- *Checks*: framing of the first 4 packets; their first 41 payload words = golden.
- *Proves*: whole-frame order for the accept-cycle pulse convention (the `cxp_interface_top` convention).

#### test_06_skid_line_marker_lead
- *Stimulus*: as test_04_skid_same_cycle_pulse_first_marker.
- *Checks*: exactly 5 K28.3 words in the first 41 payload words; after each of the last 4, the next word is 4×0x02 and the one after is that line's first pixel word.
- *Proves*: line-marker > skid priority on every line.

#### test_07_skid_holds_under_backpressure
- *Stimulus*: as test_04_skid_same_cycle_pulse_first_marker, with `m_ready` = 0 for the first 200 `tx_clk` cycles, then 1; 400 cycles of drain.
- *Checks*: ≥ 1 packet; packet 0 payload word 0 = 4×K28.3, kmask 0xF.
- *Proves*: order survives a wire stall. **The docstring says the FIFO fills while the skid is occupied. It cannot: 44 words never reach the 252-entry threshold of a 256-deep FIFO, so `merge_ready` stays 1 and the merger sees no back-pressure.**

#### test_08_skid_both_pulse_conventions_equivalent
- *Stimulus*: bring-up and one same-cycle frame as above, then a second bring-up and one pulse-ahead frame. The pulse-ahead frame asserts `fs`/`ls` on a `valid` = 0 cycle and the word on the next cycle. `m_ready` = 1.
- *Checks*: the first 41 payload words of each run equal the golden frame; the two runs are equal.
- *Proves*: the pulse-ahead lead cycle is safe when the skid fires in it (`merge_ready` = 1). The `merge_ready` = 0 case is not covered (Medium 1).

```wavedrom
{"signal":[
 {"name":"app_clk","wave":"p......"},
 {"name":"pix_word_valid","wave":"101..01"},
 {"name":"pix_line_start","wave":"010..10"},
 {"name":"pix_word_ready","wave":"1010101"},
 {"name":"merge_ready","wave":"1......"},
 {"name":"pix_d_valid_q","wave":"1.01..0"},
 {"name":"line_word_valid","wave":"0.1.0.1"},
 {"name":"merge_data","wave":"=======","data":["P0","P1","K28.3","0x02","L1 P0","L1 P1","K28.3"],"node":".a....."}
],
"head":{"text":"pulse-ahead, line 0 → line 1; a = skid must fire in the lead cycle"}}
```

#### test_09_skid_bursty_producer
- *Stimulus*: one same-cycle frame with 2 idle `app_clk` cycles (`valid` = 0, pulses 0) after every accepted word; pad; 400 cycles of drain.
- *Checks*: framing of the first 4 packets; 41 payload words = golden.
- *Proves*: the skid emits nothing while `pix_word_valid_i` = 0 (`pix_d_take` gated by valid) and loses no word across gaps.

#### test_10_skid_back_to_back_frames
- *Stimulus*: two same-cycle frames with no pad between them, then 6 pad words (82 mod 11 = 5); 400 cycles of drain.
- *Checks*: framing of 8 packets; 82 payload words = golden × 2; 10 K28.3 marker words.
- *Proves*: a frame pulse arriving right after the previous frame's last pixel produces one header, in order.

#### test_11_skid_idle_no_packets
- *Stimulus*: `pix_sel` 1, metadata driven, `valid` and pulses 0, 1500 `tx_clk` cycles.
- *Checks*: 0 packets.
- *Proves*: no word enters the merger without `pix_word_valid_i` or a pulse.

#### test_12_chopper_residual_across_frame_boundary
- *Stimulus*: two same-cycle frames back to back with no end-of-frame mark and no padding between them, then one `flush_chopper` pad; 400 drain cycles.
- *Checks*: residual 41 mod 11 = 8 ≠ 0; ≥ 8 packets; framing of 8; ≥ 10 K28.3; the 6th K28.3 (frame 2 header) at payload index 41; offset 41 mod 11 = 8 = residual; offset ≠ 0.
- *Proves*: without `pix_word_eof_i` the chopper ignores frame boundaries (How it works 2). This pins what an unmarked source gets; the TPG and the pixel ingress mark their last word.

#### test_13_eof_closes_short_packet
- *Stimulus*: two same-cycle frames back to back, the last pixel word of each marked with `ext_pix_word_eof`, no padding; 400 drain cycles; `m_ready` = 1.
- *Checks*: exactly 8 packets (11, 11, 11, 8 per frame) with correct framing, tags 0..7 and DsizeP = payload; the payload equals the golden frame × 2; each frame's image-header K28.3 is payload word 0 of packets 0 and 4.
- *Proves*: `merge_eof` closes the block on the image's last word, the short tail leaves without waiting for the next image, and its DsizeP is its own length (§8.5.2).

#### test_14_tail_packet_keeps_streamid
- *Stimulus*: ext frame with StreamID 0x05 and `ext_pix_word_eof` on its last word; `ext_meta_streamid` set to 0x00 right after that word; 400 drain cycles.
- *Checks*: 4 packets (11, 11, 11, 8) framed with StreamID 0x05.
- *Proves*: the StreamID comes from `frame_meta_q` through the FIFO descriptor, not from the live metadata when the packet leaves.

#### test_15_dsizeP_lowered_mid_packet
- *Stimulus*: `cfg_dsizeP` = 11; 3 lines of an ext frame (37 merged words, packet 3 open at 4 words); `cfg_dsizeP` = 3; the rest of the frame and 7 more frames without an end-of-frame mark (more than the 256-word FIFO holds), then one marked frame; 600 drain cycles.
- *Checks*: every packet framed with DsizeP = its payload (≤ 11); the concatenated payload equals the golden frame × 9.
- *Proves*: `chop_eop`'s `>=` closes the open block at once when the size drops, so the stream does not stop, and DsizeP follows each packet's own length.

#### test_16_line_markers_keep_frame_meta
- *Stimulus*: `cfg_arbitrary` = 1; ext frame 8×4 with Xoffs 4; line 0 pushed, then `ext_meta_xsize` / `ext_meta_xoffs` set to 0; the rest of the frame with `ext_pix_word_eof` on its last word; 400 drain cycles.
- *Checks*: 4 arbitrary line markers, each Xsize 8, Xoffs 4, DsizeL 2.
- *Proves*: the line markers read `frame_meta_q`, held from the image's frame-start pulse (How it works 3).

### Integration TB — `src/tb_unit/top/cxp_interface_top/`

16 tests on the real `cxp_interface_top` with the real stream path (`p_FIFO_DEPTH` 256), arbiter and inserter; `cxp_tx_owner_sva` and `cxp_idle_rule_sva` are bound. `p_ASYNC_CLOCKS` = 0 and all three clocks are tied to one 10 ns `clk`. The source is always the TPG (8×4), `cfg_dsizeP` 8.

| Test | Checks |
|---|---|
| test_02_stream_from_tpg | first K27.7-framed packet's type word = 4×0x01, kmask 0 |
| test_04_linktest_packets_under_testmode | under TestMode, packets after the first are type 0x03/0x04; ≥ 1 type 0x04 |
| test_10_trigger_preempts_stream | trigger HDR within 20 samples of an edge inside a stream packet; a later stream SOP |
| test_18_connection_config_write_resets_tag | after a ConnectionConfig write, PacketTag 0 then 1 within the next packets |

#### test_02_stream_from_tpg
- *Stimulus*: TPG on, `cfg_run` 1, `cfg_arbitrary` 0, `cfg_dsizeP` 8; `collect_packet(max_idle=4000)`.
- *Checks*: a SOP and an EOP within 1024 words; word 1 kmask 0; all 4 lanes 0x01.
- *Proves*: the TPG → packer → this module → arbiter → inserter path reaches the wire. Header content, payload and CRC are not checked.

#### test_04_linktest_packets_under_testmode
- *Stimulus*: TPG on, `cfg_dsizeP` 8; backdoor TestMode bit = 1, which raises `suppress_stream_i` and `flush_i`; 5 × `collect_packet(max_idle=8000, max_data=2400)`.
- *Checks*: packet 0 may be type 0x01; packets 1–4 are 0x03 or 0x04; ≥ 1 is 0x04.
- *Proves*: suppression keeps new stream packets off the wire. It does not clear TestMode, so resumption after TestMode is not checked here (`cxp_device_top` test_22 and test_28 do).

#### test_10_trigger_preempts_stream
- *Stimulus*: uplink brought up (`uplink()`); TPG on, `cfg_dsizeP` 8, 200 cycles; wait for a stream SOP, sample 2 more words, then raise the trigger pin.
- *Checks*: trigger HDR K28.4 within 20 samples, Delay word kmask 0; another stream SOP within 2000 samples.
- *Proves*: a trigger can be inserted into the stream packet and the stream port is granted again. The bound cannot tell insertion from waiting for the EOP (13 words remain), and the resumed packet's length and CRC are not checked; `cxp_device_top` test_27 and test_31 check insertion into 200-word packets with a full reassembly.

### Integration TB — `src/tb_unit/top/cxp_device_top/`

`cxp_device_top` built with `p_ASYNC_CLOCKS = 1` (TPG 64×32 maximum, FIFO 256) at unrelated `app_clk`, `tx_clk` and `rx_clk` periods, so the stream FIFO, the `app_clk` copy of DsizeP and `meta_i` run on unrelated clocks. DsizeP comes from the StreamPacketSizeMax register, or from `cfg_dsizeP_i` while it reads 0.

#### test_04_stream
- *Stimulus*: Width = 12, Height = 6 written over the uplink; `cfg_run` = 1; run until the golden reassembler has three images.
- *Checks*: image 1 is 12×6 with 6 lines of 3 words; the golden `cxp_protocol` reassembler (`DEVICE` wire format) reports no CRC, tag or DsizeP error.
- *Proves*: whole packets (header, payload, CRC, PacketTag sequence, DsizeP against payload length) across a real app→tx crossing.

The flush, the image-granular gate ahead of this module and the transmit scheduler behind it are checked by later tests of the same bench (`cxp_device_top.md`), each with the golden reassembler (whole images, no CRC, tag or DsizeP error):
- `test_18_whole_image_after_conn_reset`: ConnectionReset mid-image; the first packet after StreamPacketSizeMax is written again opens a whole image.
- `test_19_stop_tpg_mid_packet`: `cfg_run` dropped 0–70 `tx_clk` cycles after an image header and restarted after 0, 1, 600 or 2000 cycles; every packet closes.
- `test_22_testmode_exit_whole_image`: TestMode for 30 000 cycles while the TPG runs; the stream resumes with a whole image.
- `test_23_spsm_negotiation`: StreamPacketSizeMax changed between and inside images; every packet fits the value in force.
- `test_27_ioack_latency_under_stream`, `test_31_nested_preempt`: I/O acknowledgments and device triggers inserted into 200-word stream packets; the stream reassembles.
- `test_28_testmode_vs_stream`: TestMode written at random points inside stream and test packets until it has taken effect inside a stream packet five times; no framing error, every stream packet reassembles.

### Other

- `src/tb_unit/app/cxp_app_image_header`, `src/tb_unit/app/cxp_app_line_marker`, `src/tb_unit/cdc/cxp_cdc_stream_fifo`, `src/tb_unit/tx/cxp_tx_stream_pkt`: unit TBs of the children. `cxp_app_marker_seq` is tested through the first two (`docs/design/modules/app/cxp_app_marker_seq.md`).
- `src/verif/uvm` (PyUVM, not run here): `test_stream_tpg`, `test_stream_video`, `test_arbitrary_image`, `test_cdc_sweep` (8/12/10 clock ratio), `test_arbiter_stream_underflow` and `test_soak` drive this module through `cxp_interface_top`. A comment in `test_stream_video` limits it to one frame because back-to-back frames "overrun the tx-side stream pipeline" [unverified].

### Running

```
make -C src/tb_unit/top/cxp_stream_top WAVES=0                          # unit TB
COCOTB_TEST_FILTER=test_09_skid_bursty_producer make -C src/tb_unit/top/cxp_stream_top WAVES=0
make -C src/tb_unit/top/cxp_interface_top WAVES=0                       # integration TB
make -C src/tb_unit/top/cxp_device_top WAVES=0                          # three unrelated clocks
make -C src/tb_unit                                                 # regression
```

2026-09-27: the stream-path bench 16/16 pass on the split modules, its top-level trace identical to the one of `cxp_stream_top` (fixed seed); bound SVA silent. Full regression not re-run.

### Not covered in-tree

- A sensor image cut short (SOF inside an open image) through this module: its tail block stays open until the next image's words close it → Medium 2. The unit TBs of the ingress and the packer cover their side.
- `flush_i` in this module's own TB (tied 0); a flush released before `flush_ack_o` (the ConnectionConfig give-up); a flush while the framer drops a stored packet with the stream disabled → Medium 3.
- `suppress_stream_i` in this TB: a started packet completing under TestMode is covered at unit level in `cxp_tx_stream_pkt` (test_12, test_13) and end to end by `cxp_device_top` test_28 → Medium 3.
- FIFO back-pressure reaching the merger at a frame or line boundary, and a long `m_ready_i` = 0 with the FIFO full → Medium 3.
- Pulse-ahead at `merge_ready` = 0, and a pulse held while its word is not accepted → Medium 1.
- Reset of one domain during activity at this level (the FIFO unit TB covers it with no packet in flight).
- Input active at reset release; `cfg_dsizeP_i` = 1.
- `stream_ctrl_reset_i` in this TB; PacketTag wrap past 0xFF; `app_clk` faster than `tx_clk` → Medium 3.
- X-propagation: not checked (Verilator is 2-state).

## Known issues and recommendations

### Critical

None.

### Medium

1. **Pulse contract.** The port comment promises the pulse-ahead convention, which reorders under back-pressure, and a held pulse livelocks (How it works 1). *Fix:* either make the merger robust, with a `frame_pending`/`line_pending` flag cleared on accept and no preemption of a skid word older than the pulse, or document "pulse only on the accept cycle" and add `pix_frame_start_i |-> pix_word_valid_i && pix_word_ready_o` (same for line). *Effort:* 0.5 day.
2. **An image cut short by the source leaves its block open.** The TPG always completes the image it has begun and `cxp_app_acq_ctrl` passes whole images only, so AcquisitionStop, `cfg_run` = 0, a ConnectionReset or StreamPacketSizeMax below one packet end the stream on an image boundary (`cxp_device_top` test_19, test_20). A sensor that cuts an image short (a SOF inside an open image) never delivers the `pix_word_eof_i` of that image: the device reports it (`sb_pix_restart_pulse`, `cxp_app_pixel_ingress.md`), and the packer and the header start the next image cleanly, but the cut image's last block stays open until the next image's words fill it, so the next image's header goes out inside that packet. *Fix:* close the open block on `pix_frame_start_i` (the chopper sees the pulse), or have the ingress raise `eof` on the restart. *Effort:* 2 h plus a test.
3. **Coverage gaps at this level.** The unit TB ties `suppress_stream_i`, `stream_ctrl_reset_i` and `flush_i`, so TestMode, the tag reset and the flush with the framer mid-packet or at the image-start cycle are covered only through `cxp_tx_stream_pkt` and `cxp_device_top`; no unit test fills the FIFO at a frame and a line boundary for both pulse conventions (only test_15 pushes more words than the FIFO holds); no tag-wrap or fast-`app_clk` test. Arbitrary mode is covered only by test_16. `cxp_interface_top` checks only the type word of one stream packet. *Fix:* expose the tied inputs in the wrapper and add the tests. *Effort:* 1 day.

### Minor

1. Header comment: replace "adds zero observable latency" and "bypasses fully in steady state" with "1 `app_clk` of latency per word". Fix the pulse-timing port comment per Medium 1. The `stream_ctrl_reset_i` port comment calls it a "1-cycle strobe"; `cxp_interface_top` drives the ConnectionReset level on it too.
2. `meta_i.streamid` is 8 bits (§9.3: 0–255) in `cxp_meta_t`; the sensor metadata reaches the top as a `cxp_meta_t` too (2026-09-27), so no truncation is left.
3. Unit TB references: a comment in the header checker says "table 18" and the test_01 docstring "Table 18/19" (1.1.1: Table 19).
4. SVA to add (next to the FIFO and framer checkers already bound): `chop_fire && chop_eop |-> word_cnt_q >= pkt_words - 1 || merge_eof`; `word_cnt_q < PKT_MAX`; `merge_valid && !hdr_word_valid && !line_word_valid |-> merge_kmask == 0`; framer start `|-> fifo_m_pkt_avail`.

### Open questions

1. Designer / verification: which pulse convention is normative for `pix_frame_start_i`/`pix_line_start_i`, the port comment's pulse-ahead or the accept-cycle pulse `cxp_interface_top` uses?
2. Designer: should the chopper close an open block on a frame-start pulse, so a cut sensor image cannot carry the next header inside its packet (Medium 2)?
