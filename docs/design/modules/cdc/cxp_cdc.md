# cxp_cdc (clock-crossing primitives)

Inputs chosen from the tree: RTL `src/rtl/cdc/cxp_cdc_sync.sv`, `cxp_cdc_pulse.sv`, `cxp_cdc_bus.sv`, `cxp_cdc_req.sv`, `cxp_cdc_link.sv`; user `src/rtl/top/cxp_interface_top.sv` (`g_cdc`, `p_ASYNC_CLOCKS` = 1); unit TB `src/tb_unit/cdc/cxp_cdc/`; system TB `src/tb_unit/top/cxp_device_top/`; coding rule `docs/style/rtl_coding_style.sv` (CDC section); spec: none (the primitives carry no protocol); regression `make -C src/tb_unit`; output `docs/design/modules/cdc/cxp_cdc.md`.

Four small crossings between unrelated clocks, plus `cxp_cdc_link`, which the three two-sided ones use to survive a reset of one side. Every signal that changes clock in the IP goes through one of them, except the pixel stream, which crosses in `cxp_cdc_stream_fifo`'s gray-code FIFO.

| Module | Carries | Mechanism | Uses in `cxp_interface_top` (`g_cdc`) |
|---|---|---|---|
| `cxp_cdc_sync` | a level, or a bus whose bits may skew by one cycle | two destination flops | ConnectionReset level rx→tx and its echo tx→rx (`:439`, `:448`); inside the other three |
| `cxp_cdc_pulse` | a one-cycle event | source toggle, 2-flop sync, edge detect | `acq_start`, `acq_stop` rx→app (`:428`, `:433`); `conn_cfg_wr`, `clr_lt_pkt_tx`, `trig_pkt_rcvd` rx→tx (`:458`–`:471`) |
| `cxp_cdc_bus` | a multi-bit value that changes rarely | holding register + request/acknowledge toggles | configuration rx→app and rx→tx (`:413`, `:420`); TX test count tx→rx (`:493`) |
| `cxp_cdc_req` | a request with payload, consumed once | request/acknowledge toggles, payload read across | control response rx→tx (`:475`) |
| `cxp_cdc_link` | whether both ends are out of reset and settled | two settle chains, each side's view of the other cleared by either reset | inside `cxp_cdc_pulse`, `cxp_cdc_bus`, `cxp_cdc_req` |

The four crossings are instantiated only by `cxp_interface_top` and only when `p_ASYNC_CLOCKS` = 1; with 0 the crossings are wires. Every flop uses the asynchronous active-low reset of its own domain. No vendor attributes (ASYNC_REG) or timing constraints are in the repository; the headers leave them to the integration.

## Interface

| Module | Parameter | Default | Meaning |
|---|---|---|---|
| `cxp_cdc_sync` | `p_W` | 1 | Bits. At most 64 (`p_RESET` is 64 bits wide; not checked, Minor 1). |
| `cxp_cdc_sync` | `p_RESET` | 0 | Reset value of both stages, low `p_W` bits. |
| `cxp_cdc_bus` | `p_W` | 32 | Bus width. |
| `cxp_cdc_bus` | `p_RESET` | 0 | Reset value of the holding register and of the destination copy. |
| `cxp_cdc_req` | `p_W` | 32 | Payload width. |

| Module | Port | Dir | Width | Domain | Description |
|---|---|---|---|---|---|
| sync | `clk`, `rst_n` | in | 1 | dst | Destination clock and reset. |
| sync | `d_i` | in | `p_W` | src | Asynchronous input. |
| sync | `q_o` | out | `p_W` | dst | `d_i` two destination edges later. |
| pulse | `src_clk`, `src_rst_n`, `dst_clk`, `dst_rst_n` | in | 1 | — | Both clocks and resets. |
| pulse | `pulse_i` | in | 1 | src | Each cycle high flips the source toggle. |
| pulse | `pulse_o` | out | 1 | dst | One-cycle pulse per observed toggle change; combinational XOR of two destination flops. |
| bus | `src_*`, `dst_*` | in | 1 | — | Both clocks and resets. |
| bus | `d_i` | in | `p_W` | src | Source value. |
| bus | `q_o` | out | `p_W` | dst | Last value transferred; a registered copy of `hold_q`. |
| req | `src_*`, `dst_*` | in | 1 | — | Both clocks and resets. |
| req | `src_valid_i`, `src_data_i` | in | 1, `p_W` | src | Request and payload; hold both until `src_ready_o`. |
| req | `src_ready_o` | out | 1 | src | One-cycle pulse: the destination consumed the request, or the request was dropped by a destination reset. |
| req | `dst_valid_o` | out | 1 | dst | Request pending (`req_s != ack_q`). |
| req | `dst_data_o` | out | `p_W` | src (!) | `src_data_i` wired straight across; valid only while `dst_valid_o` = 1 and the source holds it. |
| req | `dst_ready_i` | in | 1 | dst | Consume the pending request (acts when `dst_valid_o` = 1). |
| link | `src_clk`, `src_rst_n`, `dst_clk`, `dst_rst_n` | in | 1 | — | Both clocks and resets. |
| link | `src_ok_o` | out | 1 | src | Both sides out of reset and settled, as seen by the source. |
| link | `dst_ok_o` | out | 1 | dst | The same, in the destination domain; rises before `src_ok_o`. |

Notes:
- **Reset:** each flop resets with its own domain's `rst_n`; releases must be synchronous to their clock (no reset synchronisers inside; `cxp_cdc_reset` provides them in `cxp_device_top`). A reset of one side alone is handled by `cxp_cdc_link` (How it works, item 5): it never produces an event, but an event offered while the pair is down, or within about ten cycles of the release, is not transferred.
- **Registered outputs:** `cxp_cdc_sync.q_o`, `cxp_cdc_bus.q_o` and `cxp_cdc_req.dst_valid_o` come from flops (the last as an XOR of two, gated by `dst_ok`); `cxp_cdc_pulse.pulse_o` and `cxp_cdc_req.src_ready_o` are combinations of flops in their own domain, so glitch-free; `cxp_cdc_req.dst_data_o` is not re-registered.

## How it works

1. **`cxp_cdc_sync`** (`cxp_cdc_sync.sv:39`): `s1_q <= d_i; q_o <= s1_q`. A multi-bit input is safe only if a one-cycle skew between bits is harmless; `cxp_interface_top` uses it only for 1-bit levels. It has no link state: after a one-sided reset it simply samples the other side again.
2. **`cxp_cdc_pulse`** (`cxp_cdc_pulse.sv:62`): `tog_q` flips on every source cycle with `pulse_i` = 1 and `src_ok` = 1; `cxp_cdc_sync` brings it across as `tog_s`; `pulse_o = dst_ok & (tog_s ^ tog_d_q)`. The destination sees one pulse per change of the synchronised toggle, not per source pulse: two source pulses that both land between the same two destination samples leave the toggle where it was and produce no pulse; three produce one.
3. **`cxp_cdc_bus`** (`cxp_cdc_bus.sv:67`): when `src_ok` = 1, the last transfer is acknowledged (`req_q == ack_s`) and `d_i != hold_q` (or `resend_q` = 1), the source loads `hold_q <= d_i` and flips `req_q`. The destination takes `q_o <= hold_q` when the synchronised request differs from `ack_q` and flips `ack_q`, which returns through a second `cxp_cdc_sync`. `hold_q` is stable from the flip until the acknowledge returns, so it is read across without a synchroniser. Values that change faster than one round trip are skipped; the destination only ever shows values the source held, in order, and settles on the last one.
4. **`cxp_cdc_req`** (`cxp_cdc_req.sv:81`): with `busy_q` = 0, `src_valid_i` = 1 and `src_ok` high this cycle and the last, the source flips `req_q` and sets `busy_q`. `dst_valid_o = dst_ok & (req_s != ack_q)`; `dst_ready_i` while valid sets `ack_q <= req_s`. The acknowledge returns through `cxp_cdc_sync`; its edge (`ack_s != ack_s_d_q`) pulses `src_ready_o` and clears `busy_q`. The source must drop `src_valid_i` on the edge that ends the `src_ready_o` cycle (a registered source that clears on `src_ready_o`, like `cxp_interface_top`'s `rsp_pend_q`, does); otherwise a second request starts.
5. **`cxp_cdc_link`** (`cxp_cdc_link.sv:64`): each side runs a 3-flop settle chain on its own reset; each side's view of the other (`src_seen_q` on `dst_clk`, `dst_seen_q` on `src_clk`) is cleared asynchronously by either reset. `dst_ok_o` = destination settled and source seen settled (3 `dst_clk` edges); `src_ok_o` = source settled and `dst_ok_o` seen (3 more `src_clk` edges). So both drop at once on either reset, and on release `dst_ok_o` rises before `src_ok_o`: about 3 S + 3 D + 3 S after the later release. The contract: a reset is held for at least one edge of its own clock; an event offered while `src_ok_o` = 0 is not transferred.

What each primitive does while the pair is down (`src_ok` / `dst_ok` = 0):

| Primitive | Source side | Destination side | Result of a one-sided reset |
|---|---|---|---|
| pulse | `pulse_i` ignored (dropped) | `tog_d_q` follows `tog_s`, `pulse_o` held 0 | a source reset that returns the toggle to 0 is not a pulse (no phantom event) |
| bus | `resend_q` set: the current `d_i` is sent once the pair is back, even if it equals `hold_q` | `ack_q` follows the request, no capture | a destination-only reset re-converges to the source value; a source-only reset leaves the old copy until the resend |
| req | no new request; a request in flight is dropped with a whole-cycle `src_ready_o` pulse (`busy_q && !src_ok_q`) | `ack_q` follows the request, `dst_valid_o` held 0 | a destination reset drops the request rather than delivering it twice; a source-only reset raises no `dst_valid_o` (formerly the phantom control acknowledgment) |

Latency (read from the code; `S` = source cycles, `D` = destination cycles):

| Module | Latency | Throughput |
|---|---|---|
| sync | 2 D | every destination cycle |
| link | ≈ 3 S + 3 D + 3 S from the later reset release to `src_ok_o` | — |
| pulse | 2–3 D from the source edge that flips the toggle | at most one event per ~2 D; closer events are lost in pairs |
| bus | 1 S (load) + 2–3 D (sync + capture) | one value per round trip, ≈ 3 D + 3 S |
| req | 1 S + 2–3 D to `dst_valid_o`; `dst_ready_i` + 1 D + 2–3 S to `src_ready_o` | one request per round trip |

Invariants, by construction, not asserted: `cxp_cdc_bus` — `hold_q` changes only while `req_q == ack_s`; `cxp_cdc_req` — `busy_q` = 1 from the request flip until the acknowledge edge, and `req_q` flips at most once per `busy_q` period.

## Arbiter integration

Not applicable: no packet source. The primitives only move `cxp_interface_top`'s control signals between domains; see `cxp_interface_top.md` (Interface notes) for which signal uses which primitive.

## Verification

Verilator 5.046 + cocotb 2.0.1, `--assert` (no SVA is bound to these modules). No FSM coverage. `cxp_cdc_link` has no bench of its own; tests 6–9 exercise it through the primitives.

### Unit TB — `src/tb_unit/cdc/cxp_cdc/`

Wrapper `tb_cxp_cdc_top.sv`: one of each primitive between `src_clk` and `dst_clk` — `cxp_cdc_sync` 8 bits, `cxp_cdc_pulse`, `cxp_cdc_bus` 32 bits with `p_RESET` = 0x5A5A5A5A, `cxp_cdc_req` 32 bits. `reset(dut, dst_ns)` (re)starts the source clock at 10 ns and the destination clock at `dst_ns`, zeroes the inputs (`bus_d` = 0x5A5A5A5A), holds both resets 50 ns, releases them together and waits 20 source cycles (`SETTLE`) so every `cxp_cdc_link` is up. One-sided resets use `pulse_reset` (asserted and released on a falling edge of that side's clock). Every test runs twice, `dst_ns` = 7 (destination faster) and 13 (slower). Test 5 is not used.

| Test | Stimulus | Expect |
|---|---|---|
| `test_01_sync` | 3 values, each set after a destination edge | old after 1 edge, new after 2 |
| `test_02_pulse` | 40 one-cycle pulses, 4 source cycles apart | 40 one-cycle destination pulses |
| `test_03_bus` | 60 random values held 1–6 source cycles | only held values, in order; ends on the last |
| `test_04_req` | 30 random payloads; consumer waits 0–5 cycles | payloads arrive once, in order; one `req_ready` each |
| `test_06_bus_one_sided_reset` | 2 values crossed (toggle back at 0); destination reset alone 3 cycles; 40 source cycles | `bus_q` = the source value again |
| `test_07_pulse_one_sided_reset` | 3 pulses (toggle left at 1); source reset alone 3 cycles; 300 ns | 3 destination pulses, none after the reset |
| `test_08_req_dst_reset_in_flight` | 1 request consumed; destination reset alone 2 cycles before the ack reaches the source | one delivery; `req_ready` at most once |
| `test_09_req_src_reset` | 1 request completed (toggles at 1); source reset alone 3 cycles; 60 destination cycles | `dst_valid` never rises |

#### test_01_sync
- *Stimulus*: for 0xA5, 0x3C, 0xFF: wait for a destination edge, record `sync_q`, set `sync_d`.
- *Checks*: `sync_q` unchanged after the next destination edge, equal to the new value after the second.
- *Proves*: exactly two destination stages.

#### test_02_pulse
- *Stimulus*: 40 × (`pulse_in` = 1 for one source cycle, 0 for 3); 200 ns; a monitor counts destination cycles with `pulse_out` = 1.
- *Checks*: 40 pulses; none wider than one cycle.
- *Proves*: one destination pulse per source pulse at 40 ns spacing, which is more than 3 destination periods even at 13 ns. The spacing at which pulses are lost is never approached (Minor 3).

#### test_03_bus
- *Stimulus*: `random.Random(3)`; 60 values, each held 1–6 source cycles; 300 ns; a monitor records every change of `bus_q`.
- *Checks*: every recorded value appears in the source sequence at or after the previous one; the last recorded value is the last value sent. The first entry is the reset value 0x5A5A5A5A.
- *Proves*: coherence (no torn value) and order, with values skipped when they change faster than the round trip.

#### test_04_req
- *Stimulus*: `random.Random(4)`; for each of 30 payloads set `req_data` and `req_valid` = 1, wait for `req_ready`, one more edge, `req_valid` = 0, one edge. A consumer takes `dst_data` when `dst_valid` = 1, waits 0–5 + 1 destination cycles and raises `dst_ready` for one cycle.
- *Checks*: the consumed payloads equal the sent ones; `readies == 1` per request.
- *Proves*: one delivery per request, payload intact, order kept. The `readies` count stops at the first `req_ready`, so a second pulse would not be seen; a duplicated request would still show up as an extra payload in `got`.

#### test_06 – test_09 (one-sided resets)
- *Stimulus*: each leaves the toggles in the state that used to be misread (even transfer count for the bus, odd for the pulse and request), then resets one side alone.
- *Checks*: bus re-converges (6); no extra pulse (7); no second delivery and at most one `req_ready` (8); no `dst_valid` (9).
- *Proves*: the `cxp_cdc_link` behaviour in the table above, at both clock ratios. Test 8 checks that the request is not delivered twice; whether the dropped-request `req_ready` fires is allowed either way. No test offers an event inside the settle window to show it is dropped.

### System TB — `src/tb_unit/top/cxp_device_top/`

`cxp_device_top` with `p_ASYNC_CLOCKS` = 1 at `rx_clk` 10 ns, `tx_clk` 8 ns, `app_clk` 12 ns carries real traffic through every instance: configuration (`test_03_registers`, `test_04_stream`), control responses and the read buffer (every read), the ConnectionReset level and its echo (`test_05_connection_reset`, `test_15`–`test_17`), the TX test count (`test_06_test_mode`), `trig_pkt_rcvd` (`test_07_host_trigger`). In `cxp_device_top` the three domains are always reset together by `cxp_cdc_reset` (`test_11`, `test_14`), so one-sided resets happen only in the unit TB. See `cxp_device_top.md`.

### Running

```
make -C src/tb_unit/cdc/cxp_cdc WAVES=0 COCOTB_TEST_FILTER=test_03_bus
make -C src/tb_unit cdc              # one target of the regression; `make -C src/tb_unit` runs all
```

2026-09-25, after commit `61b8395`: 8/8 PASS (tests 6–9 were red before `cxp_cdc_link`). Not re-run for this document.

### Not covered in-tree
- An event offered inside the settle window after a reset (dropped by design; no test shows it).
- Two source pulses within one destination period → Medium 2 (probe below).
- `cxp_cdc_req` with a source that changes `src_data_i` or drops `src_valid_i` before `src_ready_o` (outside the contract; `cxp_interface_top` Minor 2 is such a case).
- Clock ratios beyond 7/10 and 13/10 ns; a destination much slower than the source for `cxp_cdc_bus` updates.
- Metastability: a 2-state simulator resolves every sample; only the 2-flop structure is checked.

## Known issues and recommendations

### Critical

None.

### Medium
1. **`cxp_cdc_bus` stayed at its reset value after a destination-only reset.** Fixed: the source re-sends its value (`resend_q`) once `cxp_cdc_link` reports the pair usable again (`test_06_bus_one_sided_reset`). Phantom events from one-sided resets in `cxp_cdc_pulse` and `cxp_cdc_req` are fixed the same way (`test_07`–`test_09`).
2. **`cxp_cdc_pulse` loses pulses in pairs.** Two source pulses that fall between the same two destination samples flip the toggle twice and produce no destination pulse; three produce one. Observed (probe, not in repo; source 10 ns, destination 30 ns): two pulses one or two source cycles apart gave 0 destination pulses, three back-to-back gave 1. The header now says so ("two closer than that cancel"). The current users are safe because their events are far apart (one per host command or trigger packet), but a burst of TestPacketCountTx clears or a faster `rx_clk` could lose a clear. Fix: a source-side busy/acknowledge (as `cxp_cdc_req`) where an event must never be lost. Effort: 0.5 day.

### Minor
1. `cxp_cdc_sync.p_RESET` is fixed at 64 bits, so `p_W` > 64 fails to elaborate with an out-of-range select; `cxp_cdc_bus.p_RESET` is `p_W` wide. Size `p_RESET` by `p_W`, or add a `generate`-if `$error` for `p_W` > 64 as the other modules do.
2. `cxp_cdc_req.dst_data_o` is `src_data_i` with no register, so its correctness rests on the source contract alone. Add a bound assertion (source side: `busy_q |-> $stable(src_data_i)`) so a user that breaks it fails in simulation.
3. Unit TB: `test_02_pulse` never places two pulses inside one destination period, `test_04_req` cannot see a second `req_ready`, and no test offers an event inside the `cxp_cdc_link` settle window. Add those cases, and a run with the destination slower than half the source rate.
4. The headers ask the integration for ASYNC_REG / false-path constraints, but the repository has no constraint file. Name the synchroniser flops the constraints must match (`s1_q`/`q_o` in `cxp_cdc_sync`, `hold_q` → `q_o` in `cxp_cdc_bus`, `src_data_i` → consumer in `cxp_cdc_req`), or ship an example XDC/SDC.

### Open questions
1. Designer: does any future user need a lossless event crossing (`cxp_cdc_pulse` with a handshake)?
