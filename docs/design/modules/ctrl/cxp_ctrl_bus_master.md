# cxp_ctrl_bus_master

Inputs chosen: RTL `src/rtl/ctrl/cxp_ctrl_bus_master.sv` (+ `cxp_ctrl_apb_bridge.sv`, `cxp_pkg.sv`, `cxp_util_pkg.sv`); bound SVA `cxp_ctrl_exec_sva` in `src/sva/cxp_sva.sv`; unit TB `src/tb_unit/ctrl/cxp_ctrl_bus_master/`; parent `src/rtl/ctrl/cxp_ctrl_plane.sv`; integration TBs `src/tb_unit/ctrl/cxp_ctrl_plane/`, `src/tb_unit/rx/cxp_rx_link/`, `src/tb_unit/top/cxp_device_top/`; spec JIIA CXP-001-2015 v1.1.1 §8.6.1.1 (one acknowledgment per command, Wait, 200 ms), §8.6.1.2 (control channel reset), §8.6.3 Table 22, §10.3.2 (multi-word access), §10.3.3 (no Wait on bootstrap registers); AMBA APB3.

The control-command executor. It takes every command record `cxp_ctrl_cmd_parser` emits — commands to execute and commands the parser already answered (`cmd_i.err` ≠ 0) — in order, runs a read or write as N single-word accesses on the register file port or, inside the user window, on its own APB3 master (`cxp_ctrl_apb_bridge`, instantiated here when `p_USER_SIZE` ≠ 0), stores read data in a two-bank read buffer that `cxp_tx_ctrl_ack` reads on its own clock, and owns the one response register towards the acknowledgment framer. Each command gets exactly one final response and at most one Wait before it; a 0xFF gets 0x03.

| Command / outcome | Response(s) on `rsp_o` | `nwords` / `size` | `timeout` | Bus activity |
|---|---|---|---|---|
| `err` ≠ 0 from the parser (0x42 … 0x47, 0x80, 0x01) | that code; `nack_pulse_o` except for 0x01 | 0 / 0 | 0 | none |
| Read, every access OK | 0x00, `rbank` = its read-buffer bank | N / B | 0 | N reads |
| Write, every access OK | 0x01 | 0 / 0 | 0 | N writes |
| An access answers a non-zero code | that code; the command stops there | 0 / 0 | 0 | up to that word |
| Command running `p_WAIT_AFTER_MS` | 0x04 with `wait_ms` = `p_WAIT_MS`, once, before the final | 1 / 4 | 0 | continues |
| Command running `p_TIMEOUT_MS` | 0x40; a user-window access still outstanding is abandoned (`abort_i` of the bridge) | 0 / 0 | 1 | stops |
| 0xFF (control channel reset) | 0x03, after any response already presented; `ctrl_reset_pulse_o` | 0 / 0 | 0 | the running command is abandoned; an outstanding access is drained |

## Interface

| Name | Default | Meaning |
|---|---|---|
| `p_BUF_DEPTH` | 64 | Words per read-buffer bank (two banks); also the parser's write-buffer bank size. |
| `p_USER_BASE` / `p_USER_SIZE` | 0x2_0000 / 0 | User window [base, base + size): words there go to the APB3 master. Size 0 = no window, no bridge (`g_no_apb`: `apb_*` outputs tied 0). Unsigned compare, so an address below the base is outside. |
| `p_CLK_KHZ` | 125 000 | `clk` frequency in kHz. |
| `p_WAIT_AFTER_MS` | 100 | Command time before the Wait; `$error` unless 1..199. |
| `p_TIMEOUT_MS` | 900 | Command time before 0x40; `$error` unless between `p_WAIT_AFTER_MS` and `p_WAIT_AFTER_MS + p_WAIT_MS`, so the final response comes before the announced Wait time runs out. |
| `p_WAIT_MS` | 1000 | Time carried by the Wait; `$error` unless 100..10 000 (Table 22). |

| Name | Dir | Width | Description |
|---|---|---|---|
| `clk`, `rst_n` | in | 1 | Control-plane clock (`rx_clk`); asynchronous active-low reset. |
| `cmd_valid_i`, `cmd_i` | in | 1, `cxp_ctrl_cmd_t` | One record per packet: `op`, `size`, `addr`, `nwords`, `err`, `wbank`. |
| `cmd_full_o` | out | 1 | A command waits and does not start this cycle (`pend_valid_q & ~start`); the parser then drops the next packet except a valid 0xFF. |
| `wbuf_addr_o`, `wbuf_data_i` | out, in | `$clog2(p_BUF_DEPTH)` + 1, 32 | Parser write buffer `{cur.wbank, word}`; data one cycle after the address. |
| `reg_req_o`, `reg_we_o`, `reg_addr_o`, `reg_wdata_o` | out | 1, 1, 32, 32 | Register-file access; `req` is one cycle per word. |
| `reg_wstrb_o` | out | 4 | Byte enables of the current write word, bit 3 = `wdata[31:24]` (the lowest address): 4'hF, except on the last word when Size mod 4 ≠ 0 (1 → 4'b1000, 2 → 4'b1100, 3 → 4'b1110), so the zero padding is not written. The APB master has no strobes (APB3). |
| `reg_ack_i`, `reg_rdata_i`, `reg_err_i` | in | 1, 32, 8 | Done, read data, Table 22 code (0 = OK). |
| `apb_psel_o` … `apb_pwdata_o` | out | 1, 1, 1, 32, 32 | APB3 master of the user window (from `cxp_ctrl_apb_bridge_i`). |
| `apb_prdata_i`, `apb_pready_i`, `apb_pslverr_i` | in | 32, 1, 1 | APB3 slave response; PSLVERR answers 0x40. |
| `rsp_valid_o`, `rsp_o`, `rsp_ready_i` | out, out, in | 1, `cxp_ctrl_rsp_t`, 1 | The response register: `code`, `nwords`, `size`, `wait_ms`, `timeout`, `rbank`; held unchanged until `rsp_ready_i` (one cycle: handed over). |
| `rbuf_clk`, `rbuf_addr_i`, `rbuf_data_o` | in, in, out | 1, `$clog2(p_BUF_DEPTH)` + 1, 32 | Read-buffer port `{bank, word}` on the ack framer's clock; registered. |
| `ctrl_reset_pulse_o` | out | 1 | One cycle when a 0xFF arrives. |
| `nack_pulse_o`, `nack_code_o` | out | 1, 8 | One cycle when a command answered by the parser starts (0x42 … 0x47, 0x80); not for 0x01. |

## How it works

States: `ST_IDLE` → (`ST_FETCH` for a write) → `ST_REQ` → `ST_WAIT` → back to `ST_FETCH`/`ST_REQ` for the next word, or `ST_IDLE` with the final response pending; `ST_DRAIN` when a 0xFF abandons a command whose access is still outstanding.

1. **One-deep slot.** A record that is not a valid 0xFF enters `pend_q` if the slot is empty or is being emptied this cycle; otherwise it is dropped with no response (one command executes, one waits: a host re-sending after its 200 ms timeout, §8.6.1.1). `start = ST_IDLE & pend_valid_q & free & ~new_reset & ~apb_psel_o`, with `free` = response register empty and no Wait, final or 0x03 still to load into it: the next command starts only once everything of the previous one has been handed to the framer.
2. **Start.** `cur_q <= pend_q`; `to_user_q` = first address in the user window; `cmd_t_q` = 0. A record with `err` ≠ 0 only queues its code as the final response (and pulses `nack_*` unless 0x01). A read flips `rbank_q`, so consecutive reads use alternate read-buffer banks.
3. **Access.** `ST_REQ` raises `reg_req_o` for one cycle, or `req_i` of the APB bridge; `ST_WAIT` waits for the selected `ack`. Write data are `wbuf[{wbank, idx}]`, fetched in `ST_FETCH`. Read data are written to `rbuf[{rbank_q, idx}]` on an error-free ack.
4. **Next word.** Address + 4, window decode again (a command may cross the window edge). The first non-zero code, or the last word, queues the final response.
5. **Time limits** (§8.6.1.1). `cmd_t_q` counts every cycle the command is running (and while it drains), from its first fetch or request, up to `p_CLK_KHZ · p_TIMEOUT_MS`. At `p_CLK_KHZ · p_WAIT_AFTER_MS` it queues one Wait (the count passes that value once). At the timeout the command ends with 0x40 and `timeout` = 1 while an access is unanswered in `ST_WAIT`, or at the next answer if one arrives at that point (no further word is accessed); a user access still outstanding is abandoned through the bridge's `abort_i` (PSEL/PENABLE drop, no ack follows), so a late PREADY completes nothing. The register file answers in one cycle (§10.3.3), so in practice only user-window commands reach either limit.
6. **Response register.** Loaded whenever it is empty or being taken, in the order 0x03, Wait, final; `rsp_valid_o` holds until `rsp_ready_i`.
7. **Control channel reset** (§8.6.1.2). A record with op 0xFF and `err` 0 clears the slot and any Wait or final not yet loaded, queues 0x03 and pulses `ctrl_reset_pulse_o`. A response already in the register stays and goes out before the 0x03. The running command is abandoned: with an access outstanding (`ST_REQ`, or `ST_WAIT` without ack) the FSM waits in `ST_DRAIN` until the access answers or the command's timeout, when a user access is abandoned; otherwise it goes idle at once. No command starts during a drain.
8. **Read banks.** A read writes the bank named in its response; the framer latches `rbank` when it takes the response and reads that bank for the whole packet. Since the next command starts only after the previous response was taken, the next read writes the other bank, never the one being sent.

`cmd_full_o` and `start` are combinational; everything else is registered. A command answered by the parser takes 1 cycle from start to its response; a 1-word register read 4 (`ST_REQ`, `ST_WAIT` with the ack, the final queued, then loaded) [by code reading].

## Verification

Bound SVA `cxp_ctrl_exec_sva` (`src/sva/cxp_sva.sv`, `p_LIMIT` = the timeout in cycles), in every bench built with `--assert`: `a_cmd_bounded` (a command, or a drained access, is not idle for more than the timeout + 4 cycles), `a_one_wait` (one Wait per command), `a_no_wait_after_reset` (no Wait after a 0xFF until the next command starts), `a_rsp_held` (the whole response holds until taken), `a_no_stranded_cmd` (a waiting command starts as soon as the executor is idle and free).

### Unit TB — `src/tb_unit/ctrl/cxp_ctrl_bus_master/`

Wrapper: `p_BUF_DEPTH` 16, user window 0x2_0000..0x2_0FFF, `p_CLK_KHZ` 1 so Wait / timeout are 20 / 50 cycles, `p_WAIT_MS` 1234; 10 ns clock. The wrapper keeps the earlier register-bus view of the user window: a shim shows each APB transfer as one `usr_req` cycle (SETUP) and completes it on `usr_ack` (PREADY; PSLVERR when `usr_err` ≠ 0), and `usr_open` = PSEL. `abort` sends a 0xFF record; `busy` = not idle, or not free, or a command waiting; `wait_pulse` marks a Wait being taken. Python models the write buffer, a register file answering the next cycle from a dict (with per-address error codes), and a user slave with a chosen latency (or none) that forgets its pending answer when PSEL drops.

FSM coverage is collected: the TB registers `ctrl_bus_master` (5 states, 11 arcs, including the timeout and 0xFF exits written after the case statement).

| Test | Stimulus | Expect |
|---|---|---|
| test_01_read | read N 4 | 4 accesses in order, data in `rbuf`, (0x00, 4, 16) |
| test_02_write | write N 3 | write data from the buffer, 0x01 |
| test_03_error_stops | write N 4, word 2 answers 0x43 | two accesses only; (0x43, 0, 0) |
| test_04_user_window | read N 4 starting 8 bytes below the window end | two words on the user port, two on `reg_*`; 0x00 |
| test_05_wait | (a) user slave 30 cycles, read N 1; (b) 8 cycles per word, read N 4 | one Wait (1234 ms) then 0x00 each: the time counts per command, not per access |
| test_06_timeout | user slave never answers | one Wait, then (0x40, 0, 0), `timeout` = 1; PSEL dropped |
| test_07_abort | 0xFF mid-command; 0xFF with a user access outstanding | only 0x03; busy until the access is answered; next read works |
| test_08_response_held | `rsp_ready` low 25 cycles | response and busy held, fall after `rsp_ready` |
| test_09_abort_each_state | write N 4, 0xFF 0..5 cycles after the command | only 0x03; busy falls within a few cycles; a read works after each |
| test_10_abort_in_req | user slave 15 cycles; 0xFF 0..3 cycles after a read (one lands in `ST_REQ`); read USER + 4 | 0x03, then 0x00 with the second word: the drained access completed nothing |
| test_11_late_ack_after_timeout | user slave 60 cycles (> 50); read; then latency 1, read USER + 4 | 0x40 with `timeout`; the second read returns its own word |
| test_12_reset_while_rsp_held | (a) response held, 0xFF; (b) Wait held, a command queued, 0xFF; (c) 0xFF swept around the Wait | (a) [0x00, 0x03]; (b) [0x04, 0x03], queued command never answered; (c) [0x03] or [0x04, 0x03] |
| test_13_parser_codes | `err` 0x80, 0x45, 0x47 with `rsp_ready` low; then `err` 0x01 | [0x80, 0x45] (0x47 found the slot full), then [0x01]; nack 0x80, 0x45; no access |
| test_14_queue_and_banks | read 0x4000, then read 0x4004 while the first response is held 20 cycles | `cmd_full`; no second access before the first response is taken; the two responses name different banks, each holding its own word |

### Integration

- `src/tb_unit/ctrl/cxp_ctrl_plane/`: the executor behind the real parser, with a Wait from a slow APB slave, the 0xFF races (tests 9, 10), the refused-command overrun (test 11), a new command in the cycle the slot empties (test 12) and a write behind a slow write (test 13); see `cxp_ctrl_plane.md`.
- `src/tb_unit/rx/cxp_rx_link/` `test_11_wait_then_timeout` and `test_12_hung_then_reset`: the same limits in ms through the whole control plane, the register port bridged to a Python APB slave by a bench `cxp_ctrl_apb_bridge` whose abort is tied 0.
- `src/tb_unit/top/cxp_device_top/`: every register access of the golden host; `test_13_pipelined_cmds` (a read behind a read keeps both acknowledgments intact), `test_24_ctrl_wait_ack` (the Wait and the 0x40 in real ms of a slow or hung APB slave, a late answer after the timeout, no Wait for a bootstrap register) and `test_25_ctrl_reset_during_exec` (0xFF during a hung access, after a Wait, behind a read waiting to be framed, twice back to back).

### Running

```
make -C src/tb_unit/ctrl/cxp_ctrl_bus_master
make -C src/tb_unit/ctrl/cxp_ctrl_plane
make -C src/tb_unit/rx/cxp_rx_link COCOTB_TEST_FILTER=test_11
make -C src/tb_unit/top/cxp_device_top COCOTB_TEST_FILTER=test_24
```

2026-09-20: 9/9 with the router in front (6 states with the former `ST_DONE`).

2026-09-26 (uncommitted working tree, the executor with the router folded in): 14/14; FSM coverage 5/5 states and 11/11 arcs; `cxp_device_top` 25/25 with test_13 untagged; full `make -C src/tb_unit` 389 tests, 0 failing.

### Not covered in-tree

- N = `p_BUF_DEPTH` in the unit bench (covered end to end by `cxp_device_top` test_08).
- `rsp_ready_i` in the same cycle as a 0xFF.
- A multi-word command whose time runs out between words (Minor 1).
- A timeout that falls in the APB SETUP cycle (Minor 2) or in the cycle PREADY arrives.
- A PSLVERR from the user window through this module (only `src/verif/` `test_pslverr_burst`, not re-run).

## Known issues and recommendations

### Critical

None.

### Medium

None open. Fixed with the executor rework:

- A read that started while the previous read's acknowledgment was still being framed overwrote the buffer under the framer. Reads now alternate banks, the bank travels with the response, and the next command waits until the previous response was taken (`cxp_device_top` test_13, green untagged; unit test_14).
- The Wait and the timeout were counted per access, so a command of many slow accesses never got a Wait and could run past 200 ms. They now count per command (unit test_05 (b), `cxp_device_top` test_24).
- A user access abandoned at the timeout or by a 0xFF left the APB bridge waiting for PREADY, so its late answer could complete the next command. The bridge is now aborted (unit test_06, test_10, test_11; `cxp_device_top` test_24 late case).

### Minor

1. **Fixed: an access issued in the cycle the command timed out.** The timeout now ends a command only in `ST_WAIT` without an answer, or between words (an answered word with the time used up ends the command 0x40 before the next access). An access issued in `ST_REQ` always gets its answer or is abandoned; it is never issued after the 0x40 is decided. No dedicated test (found by review).
2. **Fixed: an abort in the APB SETUP cycle.** The bridge now lets a SETUP enter ACCESS and ends it there (`cxp_ctrl_apb_bridge.md`), and no command starts while PSEL is still high (`start` includes `~apb_psel_o`). A PREADY in the abort cycle still completes the transfer on the bus while the command answers 0x40: at the timeout boundary the device has given up. No dedicated test.
3. **Fixed: a buffer depth that is not a power of two** indexed past the banked arrays; `p_BUF_DEPTH` must now be a power of two ≥ 2 (`$error`, here, in `cxp_ctrl_cmd_parser` and in `cxp_tx_ctrl_ack`).
4. The register file port relies on a one-cycle answer: one that never answers now gets the Wait and the 0x40 of the command time limit, but a Wait on a bootstrap register is what §10.3.3 forbids. Document the one-cycle answer as a contract, or assert it.
5. SVA not yet present: `reg_req_o |-> !usr_req`; exactly one final response per started command.
