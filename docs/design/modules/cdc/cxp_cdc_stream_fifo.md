# cxp_cdc_stream_fifo

Inputs chosen from the tree: RTL `src/rtl/cdc/cxp_cdc_stream_fifo.sv` (no package imports; children `cxp_cdc_link`, two `cxp_cdc_sync`, `docs/design/modules/cdc/cxp_cdc.md`); bound SVA `src/sva/cxp_sva.sv` (`cxp_cdc_stream_fifo_sva`); unit TB `src/tb_unit/cdc/cxp_cdc_stream_fifo/`; integration TBs `src/tb_unit/top/cxp_stream_top/` (the parent's TB) and `src/tb_unit/top/cxp_device_top/` (unrelated clocks; the `cxp_interface_top` TB ties all clocks together); spec CXP-001-2015 §8.2.5.2, §8.5; regression `make -C src/tb_unit stream_fifo`; output `docs/design/modules/cdc/cxp_cdc_stream_fifo.md`.

Two-clock FIFO that carries the stream-packet payload from `app_clk` to `tx_clk`. Each slot holds one beat; an EOP counter reports whether a complete packet is stored. A reset of one side alone empties both sides (the read side discards, the write side stalls), and a flush request from the tx side does the same on demand, so neither leaves stale or partial packets behind.

| Slot field | Width | Meaning |
|---|---|---|
| `data` | `p_DATA_W` (32) | Payload word, P0 in `[7:0]` |
| `kmask` | 4 | Per-byte K flag (K28.3 image-header / line-marker words) |
| `sop` / `eop` | 1 / 1 | First / last word of a DsizeP chunk |

Source: `src/rtl/cdc/cxp_cdc_stream_fifo.sv`. One instance, `cxp_cdc_layer.cxp_cdc_stream_fifo_i`, with `p_DEPTH = p_FIFO_DEPTH`: 1024 by default in `cxp_app_stream`, `cxp_interface_top` and `cxp_device_top`, 256 in the stream_top, interface_top and device_top TB wrappers, 1 048 576 in `src/verif/uvm/sv/tb_cxp_top.sv`. It is fed by the header > line-marker > pixel priority merger and the DsizeP chopper (`chop_sop`/`chop_eop`) on `app_clk`, and feeds `cxp_tx_stream_pkt` on `tx_clk`. The framer waits for `m_pkt_avail_o` (a whole packet stored) and takes the packet's DsizeP and StreamID from `m_len_o` / `m_streamid_o` (store and forward, `docs/design/modules/app/cxp_app_stream.md` How it works 3); its `busy_o` returns as `m_busy_i`. `flush_i` / `flush_ack_o` are `cxp_app_stream`'s flush ports, driven by `cxp_interface_top` on a ConnectionReset, a ConnectionConfig write or TestMode; `s_flush_o` tells the app-side merger to drop.

Spec clauses: §8.5 (stream data packet payload), §8.2.5.2 (packets may be stretched with IDLE on the high-speed link; its comment expects stream packets to be fully buffered). The header cites §8.5.4, which is "Combining Multiple Streams" and is not implemented in this single-stream build.

## Interface

| Name | Default | Meaning |
|---|---|---|
| `p_DEPTH` | 4096 | Slots; power of two ≥ 4, checked at elaboration by a generate-if `$error` (`cxp_cdc_stream_fifo.sv:197-203`). Pointers are `$clog2(p_DEPTH)+1` bits. Usable capacity `p_DEPTH − p_ALMOST_FULL_MARGIN` beats (60 at 64, 1020 at 1024) |
| `p_DATA_W` | 32 | Data width; slot width `p_DATA_W + 6` |
| `p_ALMOST_FULL_MARGIN` | 4 | 1..`p_DEPTH−1`, checked by the same kind of `$error` block; `s_ready_o` falls when the app-side fill ≥ `p_DEPTH − margin` |

| Name | Dir | Width | Description |
|---|---|---|---|
| `app_clk` | in | 1 | Producer clock |
| `app_rst_n` | in | 1 | Producer reset, active-low, asynchronous assert |
| `tx_clk` | in | 1 | Consumer clock |
| `tx_rst_n` | in | 1 | Consumer reset, active-low, asynchronous assert |
| `s_data_i` | in | `p_DATA_W` | Beat data |
| `s_kmask_i` | in | 4 | Beat K flags |
| `s_valid_i` | in | 1 | Beat valid; written when `s_ready_o = 1` |
| `s_sop_i` | in | 1 | Beat is a packet start (stored; used only by the skip logic) |
| `s_eop_i` | in | 1 | Beat is a packet end (stored; increments the EOP counter) |
| `s_ready_o` | out | 1 | `app_ok & (wr_drop | fill < p_DEPTH − p_ALMOST_FULL_MARGIN)`: 0 while the pair is not up (a reset on either side, and its settle time); 1 while the write side drops for a flush |
| `m_data_o` | out | `p_DATA_W` | Head data, combinational RAM read |
| `m_kmask_o` | out | 4 | Head K flags |
| `m_valid_o` | out | 1 | Head present, not being skipped and not being discarded |
| `m_sop_o` | out | 1 | Head `sop` |
| `m_eop_o` | out | 1 | Head `eop` |
| `m_ready_i` | in | 1 | Consumer accepts the head |
| `s_streamid_i` | in | 8 | StreamID of the packet, taken with its SOP beat |
| `m_pkt_avail_o` | out | 1 | At least one EOP written and not yet read, and not discarding |
| `m_len_o` | out | 16 | Words of the packet whose SOP is at the head (valid with `m_pkt_avail_o`) |
| `m_streamid_o` | out | 8 | StreamID of that packet |
| `m_busy_i` | in | 1 | `tx_clk`. The consumer is in the middle of a packet; no discard (reset or flush) starts until it has finished (`cxp_tx_stream_pkt.busy_o`) |
| `flush_i` | in | 1 | `tx_clk` level. Empty both sides; hold until `flush_ack_o` |
| `flush_ack_o` | out | 1 | `tx_clk`. Both sides are empty: `flush_i` and 7 cycles of discard after the write side was seen dropping |
| `s_flush_o` | out | 1 | `app_clk`. `flush_i` synchronised: the write side is dropping |

Notes:

- Reset: both register blocks use `posedge clk or negedge rst_n`, so assertion is asynchronous. There is no reset synchroniser; the port comment "(async/sync-low)" leaves deassertion to the integrator. The RAM is not reset. Either reset drops `app_ok`/`tx_ok` of `cxp_cdc_link` at once, so `s_ready_o = 0` during `app_rst_n = 0` and a beat offered then is not written.
- Clock domains: `s_*`, `wr_ptr_q`, `wr_eop_cnt_q`, `wr_want_sop_q`, `s_flush_q` and `rd_ptr_gray_sync_wr_q` on `app_clk`; `m_*`, `rd_ptr_q`, `rd_eop_cnt_q`, `skip_q`, `flush_cnt_q` and both `*_sync_rd_q` on `tx_clk`. Three data crossings, each gray-coded from a register through two flops (the EOP count through three): write pointer to tx, EOP count to tx, read pointer to app. Three control crossings: `cxp_cdc_link` (pair up, `app_ok` / `tx_ok`), `flush_i` to `app_clk` and `s_flush_q` back to `tx_clk` (one `cxp_cdc_sync` each). The RAM is written on `app_clk` and read combinationally on `tx_clk`. No `ASYNC_REG` attribute and no timing constraint for these paths exist anywhere in the tree.
- Output registration: `s_ready_o` depends on registers and, while the write side waits for a SOP after a flush, combinationally on `s_sop_i`. `m_pkt_avail_o`, `m_valid_o` and `flush_ack_o` depend combinationally on `flush_i` and `m_busy_i`; `m_valid_o`, `m_sop_o` and `m_data_o` are otherwise combinational from registers and the RAM. There is no `s_valid_i → s_ready_o` or `m_ready_i → m_valid_o` path.
- Stall: `m_ready_i = 0` holds the head indefinitely. A beat offered while `s_ready_o = 0` is not written, and the producer must hold it.
- `verilator --lint-only -Wall` is clean at `p_DEPTH` 4096 and 1024. At 48 it now reports `USERERROR: p_DEPTH (48) must be a power of two >= 4` at elaboration, which fails the bench builds (`-Werror-USERERROR`) and any synthesis tool that honours elaboration-time `$error`; the old `initial $fatal` let 48 elaborate.

## How it works

1. **Write (`app_clk`).** `accept_wr = s_valid_i & s_ready_o & ~wr_drop` writes `mem[wr_ptr_q]`, increments `wr_ptr_q`, and increments `wr_eop_cnt_q` if `s_eop_i`. Both counters are re-encoded to gray in registers (`bin2gray(next)`), so each crossing word changes one bit per `app_clk`.
2. **Flow control.** `fill_wr = wr_ptr_q − gray2bin(rd_ptr_gray_sync_wr_q[1])` over `PTR_W` bits. The synchronised read pointer is 2–3 `app_clk` old, so `fill_wr` over-estimates occupancy and cannot overflow. That holds with margin 0 too; the margin only reserves slots.
3. **Read (`tx_clk`).** `empty_rd = (rd_ptr_gray_q == wr_ptr_gray_sync_rd_q[1])`. The head `mem[rd_ptr_q]` is presented with `m_valid_o = ~empty_rd` (outside discard and skip), and `m_valid_o & m_ready_i` pops it. A popped beat with `eop` increments `rd_eop_cnt_q`.
4. **Packet availability.** `m_pkt_avail_o = (gray2bin(synced wr_eop_cnt) − rd_eop_cnt_q) ≠ 0`, forced to 0 while discarding. The EOP count crosses through three flops, one more than the write pointer, so a packet is not announced before its words are visible. Beside each packet's SOP slot a descriptor RAM holds `{StreamID, length}`: the producer counts the packet's words, takes the StreamID with the SOP beat, and writes both when it writes the EOP beat, on the edge that advances the EOP count. The count is modular over `PTR_W` bits, and there is at most one EOP per slot, so wrap is safe while pointers stay consistent.
5. **Discard.** `discard = (~tx_ok_q[3] | flush_i) & ~m_busy_i`, where `tx_ok_q` is `tx_ok` delayed by 4 `tx_clk` edges (cleared at once when `tx_ok` falls) so the three-stage EOP synchroniser has settled before discard ends. A packet the consumer is reading is never cut: it is whole in the RAM (store and forward), so the reader finishes it first and discard starts when `m_busy_i` falls. While it is high nothing is presented (`m_valid_o = 0`, `m_pkt_avail_o = 0`), and each `tx_clk` edge loads `rd_ptr_q` from the synchronised write pointer and `rd_eop_cnt_q` from the synchronised EOP count, so everything written so far is dropped. The write side meanwhile stalls (`s_ready_o = 0` while `app_ok` is low) or drops (flush, item 7), so the pointers agree again when discard ends. `tx_ok`/`app_ok` come from `cxp_cdc_link`: both fall at once when either reset asserts, and rise only after both sides have settled (about 3 `app_clk` + 3 `tx_clk` edges after the later release, `dst_ok` first; `cxp_cdc.md`); discard then lasts 4 `tx_clk` edges longer.
6. **Skip-until-SOP.** `tx_rst_n` and every discard cycle set `skip_q`. While it is set, a non-SOP head is dropped (`skip_word`: `rd_ptr_q` advances, `m_valid_o = 0`, regardless of `m_ready_i`). The first SOP head clears `skip_q` and is presented normally. So after a discard the rest of a packet whose start was dropped never comes out.
7. **Flush.** `flush_i` crosses to `app_clk` (`cxp_cdc_sync`, then `s_flush_q` = `s_flush_o`). While `s_flush_q` is high, `wr_drop` takes and drops every beat offered (`s_ready_o = 1`); it also sets `wr_want_sop_q`, which keeps dropping non-SOP beats after the release until the next SOP is accepted. On `tx_clk`, discard starts as soon as `flush_i` is high and the consumer is not mid-packet (`m_busy_i = 0`), so a packet already being read goes out whole. `s_flush_q` crosses back (`flush_drop_s`); `flush_cnt_q` counts cycles with `flush_i & discard & flush_drop_s` and `flush_ack_o = flush_i & (flush_cnt_q == 7)`: seven discard cycles after the writer was seen dropping, enough for the pointer and the EOP count (3 stages) to have crossed. The requester then releases `flush_i`, and discard ends at once; the writer stops dropping 2–3 `app_clk` edges later and resumes at a SOP.

No FSM; `skip_q`, `wr_want_sop_q` and `s_flush_q` are one-bit mode flags, `flush_cnt_q` a 3-bit counter.

Same-cycle rules:

- Push and pop are in different domains and interact only through the synchronised pointers. A pop frees space for `s_ready_o` two `app_clk` edges later.
- In skip mode a SOP head clears `skip_q` whether or not it is popped that cycle.
- An accepted `s_eop_i` beat advances `wr_ptr_q` and `wr_eop_cnt_q` on the same edge. The tx side synchronises them independently.
- `flush_i` rising in the cycle the consumer would start a packet: `m_busy_i` is still 0, so discard wins and `m_valid_o = 0`; the packet is not started.
- `flush_i` or a one-sided reset while `m_busy_i = 1`: the reader keeps presenting that packet's words; discard starts in the first cycle `m_busy_i` falls.

One-sided resets (from the code):

| Reset | While the pair is down | Afterwards |
|---|---|---|
| Both | All pointers 0, `skip_q = 1` | Normal start |
| `tx_rst_n` only | Read side reset, `skip_q = 1`; `s_ready_o = 0`; after the release the reader discards until 4 edges after `tx_ok`, following the write side's pointers and EOP count | Nothing delivered before the reset is replayed and nothing written before it is sent; the next packet written comes out alone (`test_12_tx_reset_after_wrap`) |
| `app_rst_n` only | Write pointers 0, `s_ready_o = 0`; the reader first finishes a packet it is reading (whole in the RAM), then discards, its pointers following to 0 | No stale beat, no stuck `s_ready_o`, no cut packet; the next packet written comes out alone (`test_11_app_only_reset`) |

Latency and throughput (probe, equal clocks; with unequal clocks each crossing adds the phase offset between the two edges):

| Path | Cycles |
|---|---|
| Beat accepted at `app_clk` edge N → `m_valid_o` | high after `tx_clk` edge N+2, earliest pop at N+3 |
| EOP accepted → `m_pkt_avail_o` | same edge as that beat's `m_valid_o` in simulation |
| Pop at `tx_clk` edge P (FIFO at threshold) → `s_ready_o` | high after `app_clk` edge P+2 |
| Throughput | 1 beat per cycle on each side |

Invariants: `fill_wr ≤ p_DEPTH` and "no pop while empty" are asserted by the bound `cxp_cdc_stream_fifo_sva` (`src/sva/cxp_sva.sv`):
- `a_no_overflow` (`@(posedge app_clk) disable iff (!app_rst_n || !app_ok)`): `fill_wr <= p_DEPTH`. While one side is in reset the other side's view of its pointer is stale by design (the writer stalls and the reader discards then), so the check is off until the pair is up again.
- `a_no_underflow` (`@(posedge tx_clk) disable iff (!tx_rst_n)`): `emit_word |-> !empty_rd`. `emit_word` is only ever set inside `if (!empty_rd)`, so this holds by construction; it guards against a future edit rather than checking current behaviour.

`pkts_in_fifo ≤` occupancy is not asserted.

## Arbiter integration

The FIFO has no arbiter slot; it sits upstream of `cxp_tx_stream_pkt`, the arbiter's stream source.

- `cxp_tx_stream_pkt` in `ST_DATA` drives `m_valid` straight from the FIFO's `m_valid_o` and returns `s_ready_o = m_ready_i`. Because a packet starts only once all of it is stored, the FIFO never runs empty mid-packet; `cxp_tx_owner_sva` (bound in `cxp_interface_top`) checks that the packet owning the arbiter offers a word every cycle.
- `cxp_tx_stream_pkt` starts a packet only on `m_pkt_avail_o`, so a whole packet is stored before its first word leaves and the payload never pauses on the wire. `cxp_app_stream` bounds a packet to `p_DEPTH` − 8 words so one always fits.
- `suppress_stream_i` (TestMode) only stops `cxp_tx_stream_pkt` from starting a packet; the packet on the wire completes. TestMode also raises `flush_i` (`cxp_interface_top`), so once the framer is idle the FIFO discards everything the pixel source wrote meanwhile.
- `m_busy_i` is `cxp_tx_stream_pkt.busy_o` (framer mid-packet, or reading a stored packet it drops whole while the stream is disabled). Neither a flush nor a one-sided reset cuts the packet on the wire; discard waits for it.
- `cxp_tx_stream_pkt` in `ST_IDLE` never consumes a non-SOP head. After any discard the reader skips to the next SOP, so it is never presented one.

## Verification

Verilator 5.046 + cocotb 2.0.1, built with `--assert`, so `cxp_cdc_stream_fifo_sva` is bound into the FIFO in every bench below; it did not fire. No FSM or functional coverage is collected.

### Unit TB — `src/tb_unit/cdc/cxp_cdc_stream_fifo`

Wrapper `tb_cxp_cdc_stream_fifo_top` sets `DEPTH = 64`, `DATA_W = 32`, `ALMOST_FULL_MARGIN = 4`. The default clocks are `app_clk` 10 ns and `tx_clk` 8 ns, overridden per test. The wrapper exposes `m_busy`, `flush`, `flush_ack` and `s_flush`. `bringup` holds both resets low for 8 `app_clk` edges with all inputs 0 (`m_busy` and `flush` included), releases them together, then waits 24 `app_clk` and 4 `tx_clk` edges, so `cxp_cdc_link` reports the pair up before the first beat is offered. `producer` presents beats and advances when `s_ready` was 1 at the edge, optionally inserting bubbles with probability `gap_prob` (seed 0xC0DE). `consumer` drives `m_ready` each `tx_clk` cycle, dropping it with probability `gap_prob` (seed 0xBABE), and records `m_valid & m_ready` beats. `make_packets` builds random-length packets with random data and kmask, `sop` on the first beat and `eop` on the last. There is no shared checker.

| Test | Stimulus | Expect |
|---|---|---|
| `test_01_cdc_app_faster` | app 4 ns / tx 16 ns, 81 beats, consumer gaps 0.1 | Bit-exact round trip |
| `test_02_cdc_tx_faster` | app 16 ns / tx 4 ns, 81 beats, producer gaps 0.1 | Bit-exact round trip |
| `test_03_cdc_close_ratio` | app 10 ns / tx 11 ns, 81 beats, gaps 0.2 both | Bit-exact round trip |
| `test_04_almost_full_backpressure` | `m_ready = 0`, push until `s_ready` falls | Exactly 60 accepted, `s_ready` stays 0 |
| `test_05_packet_boundaries` | app 10 ns / tx 7 ns, 24 packets, 154 beats, gaps 0.15 | SOP/EOP counts and framing preserved |
| `test_06_tx_only_reset` | Traffic, `tx_rst_n` pulse, more traffic | First beat after reset has `sop`, one EOP seen |
| `test_07_pkt_avail` | 12-beat packet with `m_ready = 0`, then drain | `m_pkt_avail` rises; no mid-packet underrun |
| `test_08_back_to_back_packets` | 20 packets, 52 beats, no gaps | Bit-exact round trip |
| `test_09_kmask_roundtrip` | 16 single-beat packets, kmask 0..15 | kmask preserved |
| `test_10_len_at_head` | 4 of 6 beats held back, then packets of 1, 5, 3 with StreamIDs | `m_pkt_avail` 0 before the EOP; `m_len` / `m_streamid` right at each SOP |
| `test_11_app_only_reset` | 7 beats pushed and drained, `app_rst_n` alone, fresh 5-beat packet | exactly the fresh packet out |
| `test_12_tx_reset_after_wrap` | 70 beats pushed and drained (slot 0 mid-packet), `tx_rst_n` alone, fresh 5-beat packet | exactly the fresh packet out |
| `test_13_flush` | 2½ packets stored, flush while the first is being read | first packet whole; `flush_ack`; afterwards only the fresh packet |

#### test_01_cdc_app_faster

*Stimulus*: `app_clk` 4 ns, `tx_clk` 16 ns. 12 packets of 1–10 beats (seed 0x5EED), 81 beats in total. The producer offers back-to-back; the consumer drops `m_ready` with probability 0.1.
*Checks*: received beat count equals 81; every beat equals the sent one in `data`, `kmask`, `sop` and `eop`.
*Proves*: gray-pointer crossing at 4:1, RAM address wrap (81 > 64), and throttling at the threshold. Instrumented (not in repo), the fill reaches 60 and `s_ready` is low for 25 `app_clk` cycles.

#### test_02_cdc_tx_faster

*Stimulus*: `app_clk` 16 ns, `tx_clk` 4 ns, the same 81 beats. Producer bubbles with probability 0.1; the consumer is always ready.
*Checks*: as `test_01_cdc_app_faster`.
*Proves*: the empty flag crossing app → tx. A beat is not popped twice while the synchronised write pointer lags, since the fill never exceeds 2.

#### test_03_cdc_close_ratio

*Stimulus*: `app_clk` 10 ns, `tx_clk` 11 ns, the same 81 beats, bubbles and ready drops with probability 0.2 on both sides.
*Checks*: as `test_01_cdc_app_faster`.
*Proves*: crossing with slowly drifting phase. **The docstring promises a 1 ns phase offset, but `bringup` starts both clocks at 0 ns and applies `tx_phase` as a delay before reset assertion, so there is no offset.** The 10/11 ratio still sweeps the phase. Fill never exceeds 8.

#### test_04_almost_full_backpressure

*Stimulus*: default clocks, `m_ready = 0` throughout. One beat per `app_clk` cycle, `sop` on the first only and no `eop`, until `s_ready` reads 0 after an edge. Then `s_valid = 0` for 8 `app_clk` cycles.
*Checks*: the loop aborts if more than 72 beats are accepted; `s_ready = 0` on each of the 8 idle cycles; accepted ≤ 60; accepted ≥ 60.
*Proves*: `almost_full_wr = fill_wr ≥ p_DEPTH − p_ALMOST_FULL_MARGIN`, and no glitch while the read pointer is static.

```wavedrom
{"signal":[
  {"name":"app_clk","wave":"p.|......"},
  {"name":"s_valid","wave":"1.|...0.."},
  {"name":"fill_wr","wave":"==|===...","data":["0","1","58","59","60"]},
  {"name":"s_ready","wave":"1.|..0...","node":".....a..."},
  {"name":"m_ready","wave":"0.|......"}
],
"foot":{"text":"a: s_ready falls at fill 60 = 64 - 4; the test then checks accepted == 60 and s_ready = 0 for 8 more cycles"}}
```

#### test_05_packet_boundaries

*Stimulus*: `app_clk` 10 ns, `tx_clk` 7 ns. 24 packets of 1–12 beats (seed 0xBEEF), 154 beats. Bubbles and ready drops with probability 0.15.
*Checks*: EOP count out equals EOP count in; SOP count out equals EOP count in; every packet begins with `sop`; no packet is still open at the end.
*Proves*: `sop`/`eop` travel with their slot, and the full pointer wraps (154 > 128 = 2^`PTR_W`). Data is not compared.

#### test_06_tx_only_reset

*Stimulus*: default clocks. Phase 1 pushes and drains 2 packets, 7 beats. Phase 2 pushes 3 packets, 6 beats, with the consumer idle; after 40 `app_clk` cycles `wr_ptr_q = 13` and `rd_ptr_q = 7`. `tx_rst_n` is then held low for 8 `tx_clk` cycles and followed by 4 idle cycles. Phase 3 pushes a 4-beat packet with `m_ready = 1`.
*Checks*: phase 1 output starts with `sop` and ends with `eop`; after the reset some beat is emitted; the first emitted beat has `sop = 1`; at least one EOP within 4000 `tx_clk` cycles. A final loop drains without asserting.
*Proves*: no deadlock after a tx-only reset. The 6 undelivered phase-2 beats are discarded with the rest (the reader follows the write pointer, How it works 5), so the first beat after the reset is phase 3's SOP. The test does not check which beats come out; `test_12_tx_reset_after_wrap` does.

```wavedrom
{"signal":[
  {"name":"tx_clk","wave":"p..........."},
  {"name":"tx_rst_n","wave":"1.0..1......"},
  {"name":"tx_ok_q[3] (discard = !tx_ok_q[3], m_busy 0)","wave":"1.0.....1..."},
  {"name":"rd_ptr_q","wave":"=.=....=....","data":["7","0","13"]},
  {"name":"m_ready","wave":"0........1.."},
  {"name":"m_valid","wave":"1.0.......1.","node":"..........a."},
  {"name":"m_data","wave":"=.x.......=.","data":["B0","C0"]}
],
"foot":{"text":"drawn from the code, not measured. rd_ptr_q follows the synchronised write pointer (13) while discarding, so B0-B5 are dropped. a: the first beat out is C0, the SOP of the phase-3 packet"}}
```

#### test_07_pkt_avail

*Stimulus*: `app_clk` 8 ns, `tx_clk` 10 ns. One 12-beat packet (seed 0x77) written with `m_ready = 0`. The test polls `m_pkt_avail` for up to 20 `tx_clk` cycles, then sets `m_ready = 1` for up to 120 cycles until `eop`.
*Checks*: `m_pkt_avail` seen high; `m_valid` never drops after the first valid beat; 12 beats received; beats equal the packet.
*Proves*: the EOP counter crosses to tx and a stored packet drains without a gap. **It never checks that `m_pkt_avail` is 0 before the EOP is written. With the output tied to 1, the suite passes 9/9 (mutant, not in repo).** → Medium 1.

```wavedrom
{"signal":[
  {"name":"clk (drawn coincident)","wave":"p.........|."},
  {"name":"s_valid","wave":"1.0.......|."},
  {"name":"s_eop","wave":"010.......|."},
  {"name":"m_pkt_avail","wave":"0...1.....|.","node":"....a......."},
  {"name":"m_ready","wave":"0.....1...|."},
  {"name":"m_valid","wave":"1.........|.","node":"...........b"},
  {"name":"m_eop","wave":"0.........|1"},
  {"name":"m_data","wave":"=......===|=","data":["D0","D1","D2","D3","D11"]}
],
"foot":{"text":"a: pkt_avail rises two tx edges after the EOP write (test polls up to 20 cycles); b: m_valid held for all 12 beats, data compared"}}
```

#### test_08_back_to_back_packets

*Stimulus*: default clocks. 20 packets of 1–4 beats (seed 0xABCD), 52 beats, no bubbles, consumer always ready.
*Checks*: the received list equals the sent list.
*Proves*: sustained one-beat-per-cycle flow across short packets. Fill never exceeds 4.

#### test_09_kmask_roundtrip

*Stimulus*: default clocks. 16 single-beat packets (`sop = eop = 1`), `kmask = k` and `data = 0xDEAD000k` for k = 0..15.
*Checks*: each received `kmask` equals the sent one. Data, `sop` and `eop` are not compared.
*Proves*: all four `kmask` bits are stored per slot.

#### test_11_app_only_reset

*Stimulus*: default clocks. 7 beats (two packets) pushed and drained; `app_rst_n` alone low for 4 `app_clk` cycles; 60 `app_clk` cycles; one fresh 5-beat packet; `m_ready = 1` for 400 `tx_clk` cycles.
*Checks*: the beats received after the reset equal the fresh packet exactly.
*Proves*: the write pointer returning to 0 while the read pointer is at 7 neither leaves `s_ready` stuck low nor lets stale slots out. No packet is being read at the reset, so the wait for `m_busy_i` is not exercised, and `m_pkt_avail` is not checked.

#### test_12_tx_reset_after_wrap

*Stimulus*: default clocks. 14 five-beat packets (70 beats) pushed and drained, so both pointers have wrapped and slot 0 holds the middle of a packet; `tx_rst_n` alone low for 4 `tx_clk` cycles; 60 `tx_clk` cycles; one fresh 5-beat packet; drain 400 cycles.
*Checks*: the beats received equal the fresh packet exactly (its first beat has `sop`).
*Proves*: no replay from slot 0 after a tx-only reset, the case `test_06_tx_only_reset` could not see. `m_pkt_avail` is not checked, so the 4-edge `tx_ok_q` delay (Medium 4) is not observed here.

#### test_13_flush

*Stimulus*: default clocks. Two 5-beat packets and 3 beats of a third pushed with the consumer held; the consumer takes 2 beats of the first packet; `m_busy = 1`, `flush = 1`; the consumer takes the other 3 beats; `m_busy = 0`; wait up to 100 `tx_clk` cycles for `flush_ack`; the last 2 beats of the third packet are offered while `s_flush` is high; `flush = 0`; after 20 `app_clk` cycles one stray non-SOP beat followed by a fresh 4-beat packet; drain 400 cycles.
*Checks*: the first packet came out whole; `flush_ack` rose and `s_flush` was high then; after the flush exactly the fresh packet comes out.
*Proves*: the packet being read survives the flush (`m_busy_i`), the stored second packet and the partial third are discarded, the write side drops while `s_flush_o` is high, and after the release it drops until a SOP (`wr_want_sop_q`).

```wavedrom
{"signal":[
  {"name":"tx_clk","wave":"p.....|......"},
  {"name":"flush","wave":"01....|...0.."},
  {"name":"m_busy","wave":"1..0..|......"},
  {"name":"m_valid","wave":"1..0..|......","node":"..a.........."},
  {"name":"discard","wave":"0..1..|...0.."},
  {"name":"s_flush (app, drawn on tx)","wave":"0.1...|.....0"},
  {"name":"flush_ack","wave":"0.....|..10..","node":".........b..."}
],
"foot":{"text":"drawn from the code, not measured. a: last beat of packet 1 read, busy falls, discard starts; b: 7 discard cycles after the writer was seen dropping"}}
```

### Integration TB — `src/tb_unit/top/cxp_stream_top`

16 tests on the real FIFO inside `cxp_app_stream`, with the TPG and pixel packer in front. Wrapper `p_FIFO_DEPTH = 256`, `app_clk` 10 ns, `tx_clk` 8 ns, `cfg_dsizeP = 11`, `flush_i` tied 0; both resets are released together after 8 `tx_clk` cycles. No test resets one domain, flushes, or runs at another clock ratio.

| Test | Checks |
|---|---|
| `test_02_full_frame_decode` | Framing and CRC of every packet; payload equals two frames of reference |
| `test_07_skid_holds_under_backpressure` | First payload word of packet 0 is K28.3 after a 200-cycle `m_ready = 0` stall |
| `test_09_skid_bursty_producer` | One frame with 2 idle cycles between pixel words equals the reference |
| `test_11_skid_idle_no_packets` | No packet in 1500 cycles with no pixel input |

`test_01_packet_framing` and `test_03_kmask_passthrough` use the same free-running stimulus as `test_02_full_frame_decode` with weaker checks. The six other `test_NN_skid_*` and `test_12_chopper_*` tests target the merger and chopper.

#### test_02_full_frame_decode

*Stimulus*: `cfg_run = 1`, the TPG free-runs 8×4 Mono8 frames, `m_ready = 1` for 8000 `tx_clk` cycles.
*Checks*: at least 2 frames plus 2 packets captured. Each packet passes `check_packet_framing`: 19 words, header, data without `sop`/`eop`, CRC against `zlib.crc32`, and K29.7. The tag equals the packet index. The concatenated payload equals the reference word by word, including kmask.
*Proves*: bit-exact crossing at 10/8 ns with the FIFO held at its threshold (fill 252 of 256, probe, not in repo) and continuous `s_ready` throttling of the merger.

#### test_07_skid_holds_under_backpressure

*Stimulus*: external pixel path, one 8×4 frame with same-cycle pulses plus 3 pad words (44 merged words); `m_ready = 0` for the first 200 `tx_clk` cycles, then 1.
*Checks*: at least one packet; payload slot 0 of packet 0 is `rep4(K28.3)` with kmask 0xF.
*Proves*: ordering is kept across a consumer stall. The FIFO holds at most 44 of 256 entries, so the threshold is not reached.

#### test_09_skid_bursty_producer

*Stimulus*: the same frame with 2 idle `app_clk` cycles after every pixel word; `m_ready = 1`.
*Checks*: the first frame's 41 payload words equal the reference.
*Proves*: a sparse producer does not create or lose beats in the FIFO.

#### test_11_skid_idle_no_packets

*Stimulus*: external pixel path selected, no pixel input, `m_ready = 1` for 1500 `tx_clk` cycles.
*Checks*: zero packets.
*Proves*: an empty FIFO after reset presents nothing; `skip_q = 1` with an empty FIFO has no effect.

### Other

- `src/tb_unit/top/cxp_interface_top` (13 tests) instantiates the FIFO at depth 256 with `app_clk = tx_clk`, so it runs as a synchronous FIFO. No test observes it.
- `src/tb_unit/top/cxp_device_top` test_04_stream runs the FIFO at depth 256 between `app_clk` 12 ns and `tx_clk` 8 ns inside the whole device; the golden reassembler checks every stream packet's CRC, tag and DsizeP over three images. The flush is exercised end to end there: test_18 (ConnectionReset mid-image), test_22 (TestMode while streaming, 30 000 cycles, so the FIFO would fill) and test_17 (ConnectionReset under traffic) check that only whole images follow. test_14 resets one input at a time, which `cxp_cdc_reset` turns into a reset of all three domains, so the one-sided path is not reached there. The SVA is active; the FIFO's fill is not observed.
- `src/verif/` PyUVM env builds `cxp_device_top` with depth 1024 (256 for `test_conc_backpressure_frames`), so the FIFO fills and back-pressures under the concurrency tests; `test_conc_clock_ratio_matrix` and `test_conc_single_domain_reset` run it across clock ratios and one-domain resets.
- `src/emu/bridge/Makefile` compiles it into the hardware-emulation build.

### Running

```
cd src/tb_unit/cdc/cxp_cdc_stream_fifo && make WAVES=0 COCOTB_TEST_FILTER=test_07_pkt_avail   # one test
cd src/tb_unit/top/cxp_stream_top  && make WAVES=0                                     # integration
make -C src/tb_unit stream_fifo                                                    # regression target
```

2026-09-19, commit `9604050` (RTL, SVA and TB files differ from the commit only in line endings): unit TB 9/9, `cxp_app_stream` 12/12, `cxp_interface_top` 13/13, `cxp_device_top` 7/7 pass, bound SVA silent. Full regression not re-run. 2026-09-26 (uncommitted working tree): discard, flush and tests 11–13 added, then the `tx_ok_q` delay and the `m_busy_i` wait on every discard; not re-run for this document.

### Not covered in-tree

- App-only reset while the consumer is reading a packet (Medium 5, fixed by code; no test).
- `m_pkt_avail_o` after a tx-only reset with packets written before it (Medium 4, fixed by code; no test).
- A flush while the write side is in a one-sided reset, and `flush_i` released before `flush_ack_o` (the ConnectionConfig give-up, now 65535 `tx_clk` cycles, in `cxp_interface_top`).
- EOP-counter wrap (at most 24 packets per test, wrap at 128).
- Input active at reset release (a beat offered while the pair is not up is not written; the producer holds it).
- Deployed depth 1024 and any depth other than 64 and 256 → Medium 3.
- Indefinite stall: `m_ready = 0` is held for only 8 cycles at the threshold (unit) and 200 cycles below it (stream_top); the FIFO holds state by construction.
- Multi-clock: covered at 4:1, 1:4, 10:11, 10:8, 8:10 and 10:7 ns. Metastability and synchroniser skew are not modelled (Verilator, 2-state) → Medium 1 and 2.
- End-to-end at unequal clocks above `cxp_app_stream`: `cxp_device_top` at 12/8 ns (app/tx), without back-pressure reaching the threshold and without per-domain resets → Medium 3.
- Non-power-of-two `p_DEPTH`: rejected at elaboration (Minor 1, resolved).
- X-propagation: 2-state simulation only. `m_data_o` shows uninitialised RAM while `m_valid_o = 0`, which is harmless.

## Known issues and recommendations

### Critical

1. **Fixed: an app-only reset wedged the stream path.** The read side now discards while `cxp_cdc_link` reports the pair down and the write side stalls (How it works 5); `test_11_app_only_reset`.
2. **Fixed: a tx-only reset re-transmitted delivered data.** The read pointer follows the write pointer after the reset instead of restarting at slot 0; `test_12_tx_reset_after_wrap`.

### Medium

1. **Resolved: `m_pkt_avail_o` was unverified and unused.** `cxp_tx_stream_pkt` now waits for it; `test_10_len_at_head` checks it stays 0 until the EOP is written; the EOP count has the extra synchroniser flop; `cxp_app_stream` bounds a packet to `p_DEPTH` − 8 words. It is also held low while discarding.
2. **CDC hardening.** Mark the synchroniser registers (the three gray chains, `cxp_cdc_link` and the two flush `cxp_cdc_sync`) `ASYNC_REG`. Add `set_max_delay -datapath_only` (one destination period) on the three gray buses and the RAM read path. Add reset synchronisers for deassertion in the integrating top. Effort: 0.5 day.
3. **Coverage at deployed settings.** No run at `p_DEPTH` 1024. The one unequal-clock run above `cxp_app_stream` (`cxp_device_top`, 12/8 ns) never fills the FIFO. `s_ready` recovery latency and the one-beat-per-cycle rate are not asserted. Add a depth-1024, 3:2-ratio unit run. Effort: 0.5 day.
4. **Fixed: after a tx-only reset `m_pkt_avail_o` was offset by the EOP count written before it.** Discard ended on the edge `tx_ok` rose, before the three-stage EOP synchroniser had settled, so `rd_eop_cnt_q` stayed 0; discard now ends 4 `tx_clk` edges later (`tx_ok_q`). No test checks `m_pkt_avail` after the reset.
5. **Fixed: an app-only reset cut the packet the consumer was reading.** Every discard now waits for `m_busy_i` to fall; the packet is whole in the RAM, so the reader finishes it first. No test resets with a packet in flight.

### Minor

1. Resolved: the power-of-two and margin checks are generate-if `$error` blocks (`cxp_cdc_stream_fifo.sv:197-203`), so `p_DEPTH = 48` is rejected at elaboration (Verilator `USERERROR`) instead of building a RAM indexed past its end.
2. The header's margin rationale is wrong: the stale read pointer only over-estimates fill, so margin 0 is safe. Allow 0 or document the margin as optional slack. Effort: 15 min.
3. Correct the port comments to "asynchronous assert, synchronous deassert required". Fix the header: §8.5.4 citation, the v1.0 name "StreamPacketDataSize" (now StreamPacketSizeMax, §10.3.32, in bytes), the unused 4096 default and "one instance per stream". `docs/design/cxp_camera_ip_modules.md` §2.5 still cites §6.5.4, and its plan item 5 (depth = packet size) has no test. Effort: 30 min.
4. `bin2gray_ptr`/`bin2gray_eop` and the gray2bin pair are identical because `EOP_W == PTR_W`; keep one pair. Effort: 10 min.
5. Fixed: `s_ready_o` was 1 during `app_rst_n`; it is now gated by `cxp_cdc_link`'s `app_ok`, low from reset until the pair is up.
6. TB hygiene: make `tx_phase`/`app_phase` actually delay a clock start; compare data in `test_09_kmask_roundtrip` and `test_05_packet_boundaries`. Effort: 30 min.
7. SVA: `fill_wr <= p_DEPTH` is bound (`a_no_overflow`), together with `a_no_underflow`, which is true by construction. Still to add: `@(posedge tx_clk) skip_q |-> !m_valid_o || m_sop_o`; `@(posedge tx_clk) m_pkt_avail_o |-> !empty_rd`; `@(posedge app_clk) !s_ready_o |=> $stable(wr_ptr_q)`; and, if `a_no_underflow` is to check something, compare the popped count against the synchronised write pointer instead. Effort: 1 h.

### Open questions

1. Answered 2026-09-26: one-sided resets are handled in RTL (discard through `cxp_cdc_link`); `cxp_device_top` also resets both sides together. Medium 4 and 5, found afterwards, are fixed as well.
2. Answered 2026-09-22: `cxp_tx_stream_pkt` waits for `m_pkt_avail_o` (store and forward).
3. Verification: should multi-clock and reset coverage live in the unit TB or in `src/verif/` `test_cdc_sweep`, whose 1 M-entry FIFO never exercises back-pressure?
4. Designer: which RAM style is intended at depth 1024? The 38-bit combinational read maps to distributed RAM [assumption]; the header suggests a registered BRAM plus skid buffer for deep instances.

No repository files other than this document were changed.
