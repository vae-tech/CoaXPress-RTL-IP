# cxp_rx_link_mon

Inputs chosen: RTL `src/rtl/rx/cxp_rx_link_mon.sv`; unit TB `src/tb_unit/rx/cxp_rx_link_mon/`; integration TB `src/tb_unit/rx/cxp_rx_link/`; spec JIIA CXP-001-2015 v1.1.1 §8.2.5 / Table 14, §8.2.5.1, §8.7.3, §10.1.1, §10.2.

The uplink IDLE monitor. It sits in `cxp_rx_link` between the 8B/10B decoder register and `cxp_rx_packet_parser`, and owns the link state: it decides when the link is up (§10.1.1 Detected), forwards words to the parser only while it is, declares the link lost when IDLE stops arriving, and decides — in both states — that the sampler's character or word framing is wrong and must be found again (§10.2: "character and word alignment re-established").

| State | Enters when | Words forwarded | Leaves when |
|---|---|---|---|
| DOWN | reset; loss of IDLE; wrong framing; sampler lock lost | no | `p_LOCK_IDLES` error-free IDLE words with the sampler locked |
| UP | as above | yes | `p_LOSS_WORDS` words without IDLE, or wrong framing (both `resync_o`, `flush_o`), or `rx_lock_i` = 0 (`flush_o`) |

Wrong framing, the sampler locked:

| State | Sign | Why it means the framing is wrong |
|---|---|---|
| DOWN | `p_BAD_WORDS` words since the last clean IDLE that are not one | a lock on the wrong bit phase never decodes an IDLE |
| UP | `p_BAD_WORDS` words with a code or disparity error since the last clean IDLE | a bit slip, or a line gone quiet: every character fails to decode |
| UP | one error-free word with K28.5 in P1..P3 | Table 14 sends K28.5 only in P0: the lanes turned by whole characters |

Each sends the sampler back to hunt (`resync_o`); it re-anchors on the next comma.

It replaces the former lane aligner: the sampler frames every word on the K28.5 comma, so the lane offset was always 0, and the aligner's 1024-word timeout was shorter than one Table 23 test packet.

## Interface

| Name | Default | Meaning |
|---|---|---|
| `p_LOCK_IDLES` | `RX_LOCK_IDLES_DEFAULT` = 2 | Error-free IDLE words, sampler locked, to go UP. ≥ 1. |
| `p_LOSS_WORDS` | `RX_LOSS_WORDS_DEFAULT` = 20 000 | Words without an IDLE before the link is lost: twice the §8.2.5.1 low-speed interval (800 000 bits). |
| `p_BAD_WORDS` | `RX_BAD_WORDS_DEFAULT` = 32 | Words since the last clean IDLE that misfit the framing (table above) before a resync. ≥ 1. |
| `p_SHORT_LOSS_OK` | 0 | Benches only. With 0, `p_LOSS_WORDS` < 10 000 is an elaboration `$error` (`g_chk_loss_words`). |

| Name | Dir | Width | Description |
|---|---|---|---|
| `rx_clk`, `rx_rst_n` | in | 1 | Clock; asynchronous active-low reset |
| `rx_lock_i` | in | 1 | Sampler symbol lock |
| `data_i`, `kmask_i`, `valid_i` | in | 32, 4, 1 | Decoded word, P0 in `[7:0]` |
| `err_i` | in | 1 | Code or disparity error on any lane of this word |
| `data_o`, `kmask_o`, `valid_o` | out | 32, 4, 1 | Words forwarded while UP (`valid_o` = `valid_i` & UP & not dropping) |
| `up_o` | out | 1 | UP (exported as `aligned_o` of `cxp_rx_link`) |
| `link_detected_o` | out | 1 | UP AND `rx_lock_i` |
| `resync_o` | out | 1 | Registered one-cycle pulse on IDLE loss or wrong framing; sends the sampler back to hunt |
| `flush_o` | out | 1 | Registered one-cycle pulse whenever the link leaves UP and with every `resync_o`; ends the parser's packet |

## How it works

1. **IDLE detect.** `is_idle` = valid, no error, kmask 0111 and the word equal to `IDLE_WORD` (K28.5 K28.1 K28.1 D21.5, Table 14).
2. **DOWN.** While `rx_lock_i` = 1, count error-free IDLE words; an errored word, lock loss or a resync restarts the count. At `p_LOCK_IDLES` go UP and clear the word counter.
3. **UP.** Every valid non-IDLE word increments `since_q`; an IDLE clears it. When the `p_LOSS_WORDS`-th word without IDLE arrives, `loss` fires: `resync_o` and `flush_o` pulse on the next cycle and the link goes DOWN. `rx_lock_i` = 0 while UP also goes DOWN with `flush_o` (no `resync_o`: the sampler already lost lock). The word on the dropping cycle is not forwarded.
4. **Framing.** `bad_q` counts, with the sampler locked, the words with a code or disparity error since the last clean IDLE (`bad_word`, in both states); it clears on a clean IDLE, lock loss or a resync. `misframed` fires on the `p_BAD_WORDS`-th such word, or at once for an error-free K28.5 outside P0 (both states); it pulses `resync_o` and `flush_o` and, while UP, drops the link. Clean data words between IDLEs never count, so a host that sends IDLE as rarely as §8.2.5.1 / §8.7.3 allow still brings the link up (test_11; before 2026-10-04 every non-IDLE word counted while DOWN and such a host kept the link down for ever).

§8.7.3 asks the host for only one IDLE between 1027-word test packets and §8.2.5.2 forbids IDLE inside a low-speed packet, so the link must survive at least 10 000 IDLE-free words; the default gives a factor of two.

## Verification

### Unit TB — `src/tb_unit/rx/cxp_rx_link_mon/`

Wrapper with `LOSS_WORDS` = 64 and `p_SHORT_LOSS_OK` = 1; 10 ns clock; a monitor task counts `resync`, `flush` and forwarded words.

| Test | Stimulus | Expect |
|---|---|---|
| test_01_reset | reset, 4 data words unlocked | down, nothing forwarded |
| test_02_link_up | lock, 2 IDLE, 5 data | up, `link_detected`, 5 forwarded, no pulse |
| test_03_up_needs_lock_and_clean_idles | IDLE unlocked; IDLE, errored IDLE, IDLE locked; one more IDLE | down until the last IDLE |
| test_04_loss_of_idle | up; 63 data; 1 data | up after 63; one `resync`, one `flush`, down, nothing forwarded after |
| test_05_idle_refreshes | up; 4 × (63 data, IDLE) | stays up, no pulse |
| test_06_lock_loss | up; 3 data; `rx_lock` = 0 | one `flush`, no `resync`, down |
| test_07_down_locked_no_idle | locked, down; 100 data, 100 errored IDLE | at least one `resync`, still down |
| test_08_k28_5_off_p0 | up; 3 data; an IDLE turned by one character (K28.5 in P1) | one `resync` and one `flush` within 2 cycles, down |
| test_09_error_run_while_up | up; 40 errored data words | one `resync` before the 64-word loss, down |
| test_10_sparse_errors_keep_link | up; 10 × (19 words, every other errored; IDLE) | stays up, no pulse (mutant: `bad_q` not cleared by IDLE → red) |

SVA (`cxp_link_mon_sva`, bound in every bench): locked and down, at most `p_BAD_WORDS` words without a clean IDLE before a resync (counted independently of `bad_q`); `resync_o |-> flush_o`.

### Integration — `src/tb_unit/rx/cxp_rx_link/`

test_08_back_to_back_test_packets sends three 1027-word Table 23 packets with one IDLE between them at the default window: `link_detected` never falls, `lt_pkt_count_rx` = 3 and `lt_err_count` = 1 for a flipped word 1023. With the previous 8192-bit sampler window the lock dropped inside the first packet and no packet was counted.

test_14_glitch_before_link_up drops one bit between lock and link-up at each of the four character phases: the link comes up within 200 words each time (before: still down 297 words later). test_15_slip_while_locked inserts one character after link-up: the following read is answered. `cxp_device_top` test_34_lock_loss_mid_cmd holds the line low for 200 words in the middle of a write and brings it back 3 bits off: the cut write is never executed and the next read is answered 0x00 (before: no acknowledgment at all).

## Known issues and recommendations

### Critical

None.

### Medium

- **Fixed 2026-10-04: link never came up under sparse IDLE** (review RX-01). DOWN counted every clean non-IDLE word as misframed, so 32 words after the sampler locked inside a 1027-word test packet the sampler was sent back to hunt. DOWN now judges framing as UP does. test_11 fails on the old RTL ("resync during clean data while down"). The `cxp_link_mon_sva` bound counts errored words too.

### Minor

- A bit error that turns a data character into K28.5 in P1..P3 with no other error on the word (two bit flips) re-hunts a framed link; the cost is one command lost to the flush.
- SVA to add: `valid_o |-> up_o`.
