# cxp_ctrl_plane

Inputs chosen from the tree: RTL `src/rtl/ctrl/cxp_ctrl_plane.sv` (+ `cxp_pkg.sv` for `cxp_rxlong_t`, `cxp_ctrl_cmd_t`, `cxp_ctrl_rsp_t`; children `cxp_ctrl_cmd_parser.sv`, `cxp_ctrl_bus_master.sv` and, inside it, `cxp_ctrl_apb_bridge.sv`); parent `src/rtl/top/cxp_interface_top.sv`; bound SVA `src/sva/cxp_sva.sv`; unit TB `src/tb_unit/ctrl/cxp_ctrl_plane/`; integration TBs `src/tb_unit/rx/cxp_rx_link/` (its wrapper instantiates this module beside `cxp_rx_link`), `src/tb_unit/top/cxp_interface_top/`, `src/tb_unit/top/cxp_device_top/`; children's unit TBs `src/tb_unit/ctrl/cxp_ctrl_cmd_parser/`, `src/tb_unit/ctrl/cxp_ctrl_bus_master/`; spec JIIA CXP-001-2015 v1.1.1 (`docs/spec/CXP-001-2015.pdf`) §5.1, §8.6 (Tables 21, 22), §8.6.1.1, §8.6.1.2, §8.6.4, §10.3 (Table 45); regression `make -C src/tb_unit`; output `docs/design/modules/ctrl/cxp_ctrl_plane.md`.

Executes host control commands (§8.6): takes every long packet the uplink receiver delivers, keeps the type-0x02 ones, checks each command, runs it as single-word accesses on the register file port or on an APB3 master for the user window, and holds exactly one final response per command (plus at most one Wait ahead of it, and 0x03 for a control channel reset) until the acknowledgment framer takes it.

| Command outcome | Decided by | Code | Bus activity |
|---|---|---|---|
| Packet aborted by the parser (lost trailer, link loss), or trailer before the command word | parser | 0x47 | none |
| Undefined opcode | parser | 0x42 | none |
| Trailer early / late for the Size, read or write of Size 0 | parser | 0x46 | none |
| CRC mismatch, or a word with a decode error | parser | 0x80 | none |
| Oversize (N > ControlPacketSizeMax/4 − 6, N not wrapped) | parser | 0x45 | none |
| Write on an extension link | parser | 0x43 | none |
| Write of ConnectionReset or MasterHostConnectionID on an extension link | parser | 0x01, ignored | none |
| Op 0xFF | executor | 0x03 after any response already presented | abandons the running command (drains its access); `ctrl_reset_pulse_o` |
| Read, N ≥ 1 | executor | 0x00 with N words, or the first non-zero access code | N reads |
| Write, N ≥ 1 | executor | 0x01, or the first non-zero access code | N writes |
| Command still running after `p_WAIT_AFTER_MS` | executor | 0x04 with `p_WAIT_MS`, once | continues |
| Command still running after `p_TIMEOUT_MS` | executor | 0x40, `rsp_o.timeout` = 1 | user access abandoned |
| A packet that starts while one command executes and one waits | parser + executor | none (dropped), except a valid 0xFF | none |

Source: `src/rtl/ctrl/cxp_ctrl_plane.sv`, structural only (two instances, no logic). Users:

- `cxp_interface_top` (`cxp_ctrl_plane_i`), beside `cxp_rx_link_i`. `long_i` comes from `cxp_rx_link.long_o`; `reg_*` is the top's register-file port; `apb_*` is the top's APB port; `rsp_*` crosses to `cxp_tx_ctrl_ack` (wire or `cxp_cdc_req`, `rsp_ready_i` = `~ack_busy`) with the read-buffer bank in `rsp_o.rbank`; `ctrl_reset_pulse_o` and `nack_*` become `sb_ctrl_reset_pulse` and `sb_ctrl_nack_*`.
- The `cxp_rx_link` bench wrapper (`src/tb_unit/rx/cxp_rx_link/tb_cxp_rx_link_top.sv`), with `rbuf_clk = rx_clk`, `rsp_ready_i = 1`, the user window off and `reg_*` bridged by its own `cxp_ctrl_apb_bridge` (abort tied 0) to a Python APB slave.
- The unit bench `src/tb_unit/ctrl/cxp_ctrl_plane/`.

Children, each with its own document: `cxp_ctrl_cmd_parser_i` (`cxp_ctrl_cmd_parser.md`), `cxp_ctrl_bus_master_i` (`cxp_ctrl_bus_master.md`, with `cxp_ctrl_apb_bridge_i` inside, `cxp_ctrl_apb_bridge.md`).

```
 long_i (cxp_rxlong_t, from cxp_rx_link.long_o)            from_extension_link_i
    |                                                            |
    v                                                            v
 cxp_ctrl_cmd_parser_i   type 0x02: CRC, size, opcode, extension-link check,
    |                two write-buffer banks; one record per packet
    | cmd_valid, cmd (op, size, addr, nwords, err, wbank)    ^ cmd_full
    v                                                        |
 cxp_ctrl_bus_master_i  one command executing + one waiting; response register
    | ^ wbuf_addr {bank, word} / wbuf_data                   (0x03, Wait, final)
    +--> reg_req/we/addr/wdata/wstrb, <-- reg_ack/rdata/err     (register file)
    +--> cxp_ctrl_apb_bridge_i --> apb_psel/penable/pwrite/paddr/pwdata,
    |                     <-- apb_prdata/pready/pslverr          (user window)
    +--> rsp_valid_o / rsp_o (held) <-- rsp_ready_i
    +--> rbuf_data_o <-- rbuf_addr_i {bank, word} (read port on rbuf_clk)
    +--> ctrl_reset_pulse_o, nack_pulse_o / nack_code_o
```

## Interface

| Name | Default | Meaning |
|---|---|---|
| `p_BUF_DEPTH` | 64 | Words per bank of the write buffer (parser) and the read buffer (executor); two banks each. Must hold the largest N of `p_PKT_SIZE_MAX` (elaboration `$error` in the parser). |
| `p_PKT_SIZE_MAX` | `CONTROL_PACKET_SIZE_MAX_VALUE` (280) | ControlPacketSizeMax in bytes; N > `p_PKT_SIZE_MAX/4 − 6` → 0x45. |
| `p_USER_BASE` / `p_USER_SIZE` | 0x2_0000 / 0 | User window [base, base + size): accesses there go to the APB master, all others to `reg_*`. Size 0 = no window and no bridge (`apb_*` tied 0). |
| `p_CLK_KHZ` | 125 000 | `rx_clk` frequency in kHz; the three time limits below are counted in ms of it. |
| `p_WAIT_AFTER_MS` | 100 | Command time before the one Wait (0x04); 1..199 (§8.6.1.1: a response within 200 ms). |
| `p_TIMEOUT_MS` | 900 | Command time before 0x40; must lie between the Wait and its end. |
| `p_WAIT_MS` | 1000 | Time carried by the Wait; 100..10 000 (Table 22). |

The range checks are elaboration `$error`s in `cxp_ctrl_bus_master`. `cxp_interface_top` passes `p_BUF_DEPTH`, `p_USER_BASE`, `p_USER_SIZE` and `p_CLK_KHZ` = `p_RX_CLK_KHZ`; the other three keep their defaults.

| Name | Dir | Width | Description |
|---|---|---|---|
| `rx_clk`, `rx_rst_n` | in | 1 | Clock of both children and the bridge; asynchronous active-low reset. |
| `long_i` | in | `cxp_rxlong_t` | Long-packet words from `cxp_rx_link` (`data`, `kmask`, `valid`, `sop`, `eop`, `err`, `ptype`). Only `ptype == 0x02` is used; `eop & err` aborts the packet. |
| `from_extension_link_i` | in | 1 | Strap: 1 = writes are answered 0x43 (0x01 for ConnectionReset / MasterHostConnectionID) without execution. |
| `reg_req_o`, `reg_we_o`, `reg_addr_o`, `reg_wdata_o` | out | 1, 1, 32, 32 | Register-file access: one `req` cycle per word. |
| `reg_wstrb_o` | out | 4 | Byte enables: 4'hF, except on the last word of a write whose Size is not a multiple of 4. |
| `reg_ack_i`, `reg_rdata_i`, `reg_err_i` | in | 1, 32, 8 | Access done, read data, Table 22 code (0 = OK). Expected one cycle after `req`. |
| `apb_psel_o`, `apb_penable_o`, `apb_pwrite_o`, `apb_paddr_o`, `apb_pwdata_o` | out | 1, 1, 1, 32, 32 | APB3 master of the user window; tied 0 when `p_USER_SIZE` = 0. No PSTRB. |
| `apb_prdata_i`, `apb_pready_i`, `apb_pslverr_i` | in | 32, 1, 1 | APB3 response; PSLVERR answers 0x40; a slave may take any time (Wait, then 0x40 and an abandoned transfer). |
| `rbuf_clk`, `rbuf_addr_i`, `rbuf_data_o` | in, in, out | 1, `$clog2(p_BUF_DEPTH)` + 1, 32 | Read-buffer port `{bank, word}` on the ack framer's clock; data one cycle after the address. |
| `rsp_valid_o`, `rsp_o` | out | 1, `cxp_ctrl_rsp_t` | Held response: `code`, `nwords`, `size` (B), `wait_ms`, `timeout`, `rbank`. |
| `rsp_ready_i` | in | 1 | Framer takes the response; `rsp_o` stays stable until then. |
| `ctrl_reset_pulse_o` | out | 1 | One cycle on a 0xFF. |
| `nack_pulse_o`, `nack_code_o` | out | 1, 8 | One cycle when a command the parser answered starts in the executor (0x42 … 0x47, 0x80; not 0x01). |

Notes:

- **Clocks.** Everything runs on `rx_clk` except the read port of the read buffer (`rbuf_clk`: `tx_clk` in the asynchronous build of `cxp_interface_top`). The buffer array is not synchronised; a read writes one bank, its response names it, and the next read can start only after that response was taken, so it writes the other bank while the framer reads this one.
- **Held response.** The executor's response register is loaded only when empty or taken, so a response can cross a clock boundary unchanged (`cxp_cdc_req` in the parent) and none is dropped while the framer is busy.
- **No back-pressure on `long_i`.** Words arrive at most one per cycle; the parser accepts all of them. Flow control is per packet: `cmd_full` sampled at its SOP.

## How it works

1. **Parse** (`cxp_ctrl_cmd_parser_i`). Table 21 header, write data into the current write bank, CRC per §8.2.2.2, opcode / length / size / extension-link checks. One cycle after the trailer it emits one record: `err` = 0 to execute, else the code to answer. A packet that started while the executor was full stores nothing and emits nothing, unless it is a valid 0xFF.
2. **Queue** (`cxp_ctrl_bus_master_i`). One command executes, one waits in a slot; a record arriving while both are taken is dropped with no answer. The waiting command starts once the executor is idle and everything of the previous command has been handed to the framer.
3. **Execute.** A record with `err` ≠ 0 becomes its response without an access (and a `nack` pulse). A read or write becomes N single-word accesses, each to the APB master if its address is in the user window, else to `reg_*`; the first non-zero code ends it. The command's time runs from its first access: one Wait at `p_WAIT_AFTER_MS`, 0x40 with the timeout flag at `p_TIMEOUT_MS`, the outstanding APB transfer abandoned.
4. **Reset.** A 0xFF drops the waiting command and any Wait or final not yet presented, abandons the running command (an outstanding access is drained until it answers or the command's timeout) and answers 0x03 after whatever is already presented.
5. **Respond.** The response register presents 0x03, then a Wait, then the final; `cxp_tx_ctrl_ack` reads the read data through `rbuf_*` in the bank named by `rsp_o.rbank`.

## Arbiter integration

Not applicable: the response reaches `cxp_tx_arbiter` through `cxp_tx_ctrl_ack` (`TX_PORT_ACK`).

## Verification

Verilator 5.046, cocotb 2.0.1. Bound SVA: `cxp_rxlong_sva` on the long-packet stream (bound to `cxp_rx_packet_parser`) and `cxp_ctrl_exec_sva` on the executor (command bounded by its timeout, one Wait per command and none after a 0xFF, response held, no stranded command; `src/sva/cxp_sva.sv`). FSM coverage only in the children's unit TBs.

### Unit TB — `src/tb_unit/ctrl/cxp_ctrl_plane/`

Wrapper `tb_cxp_ctrl_plane_top.sv`: this module with `BUF_DEPTH` 16, `PKT_SIZE_MAX` 88, `p_CLK_KHZ` 1 (Wait after 20 cycles of a command, timeout after 50, Wait payload 1234 ms), a 64-word user window at 0x0002_0000 served by an APB slave in the wrapper (latency `apb_latency` cycles of ACCESS, 0xFFFF = never; words power up 0xA500_0000 | index), `rbuf_clk` = `clk`. Python models the register file (answers the next cycle from a dict), builds commands with the golden `cxp_protocol` codec (`SPEC`, Table 21), and `take` reads a response's data from its bank while it is held, then pulses `rsp_ready`. The tests of the deleted command router moved here.

| Test | Stimulus | Expect |
|---|---|---|
| test_01_reset | 50 idle cycles | no response, no access |
| test_02_read | read 12 bytes at 0x4000 | three register reads; 0x00, Size 12, data [1, 2, 3] |
| test_03_parser_errors | read with a corrupted CRC; read of 17 words | 0x80 then 0x45; nack 0x80, 0x45; no access |
| test_04_extension_link | extension link: write 0x4000, write 0x4008, write 0x10000, read 0x10000 | 0x01, 0x01, 0x43, 0x00; nack 0x43 only; the only access is the read |
| test_05_reset_op | 0xFF, then a read | 0x03, one reset pulse; the read works |
| test_06_wait | APB latency 30; read USER | 0x04 with 1234 ms, then 0x00 with the slave's word |
| test_07_command_while_busy | APB latency 12; read USER, read 0x4000 at once | both answered in order; the register read starts only after the first response |
| test_08_held | read, `rsp_ready` low 40 cycles | response held unchanged; one response |
| test_09_reset_while_rsp_held | (a) response held, 0xFF; (b) Wait held, 0xFF; (c) as (b) with a read queued | [0x00, 0x03]; [0x04, 0x03]; [0x04, 0x03]; nothing after |
| test_10_wait_then_reset_race | hung slave; 0xFF 8..39 cycles after a read (the Wait is due at 20) | [0x03] or [0x04, 0x03] each round; then a read answers 0x00 |
| test_11_own_rsp_overrun | `rsp_ready` low: CRC-corrupted read, oversize read, Size-0 read | [0x80, 0x45] only: the third found the executor full |
| test_12_start_coincident_cmd | A (slow read), B queued, C landing 0..8 cycles around A's hand-over | A then B, C third or not at all; never out of order or with another's data |
| test_13_write_behind_slow_write | APB latency 2; write W1 to USER, write W2 to USER + 0x40 at once | two 0x01; both read back intact (two write banks) |

### Integration TB — `src/tb_unit/rx/cxp_rx_link/`

The wrapper instantiates `cxp_rx_link` and this module (`rbuf_clk = rx_clk`, `p_CLK_KHZ` 50 so the Wait comes after 5000 and the timeout after 45 000 cycles), with `reg_*` bridged by a bench `cxp_ctrl_apb_bridge` to a Python APB slave; the user window is off. The wrapper decodes `nack_*` into its older status outputs and reads the read buffer in the bank of the last response. Serial host.

| Test | Checks through this module |
|---|---|
| `test_05_ctrl_read` | read at 0x42: 0x00, `rbuf[0]` = APB data |
| `test_06_ctrl_reset_op` | 0xFF: `ctrl_reset_pulse`, 0x03 |
| `test_09_lost_trailer` | write with its trailer lost → 0x47, not executed; next read 0x00 |
| `test_10_rd_seed_and_code_error` | read with one invalid 10b symbol → 0x80, not executed |
| `test_11_wait_then_timeout` | slave never answers: one 0x04 (1000 ms) before 200 ms, then 0x40 |
| `test_12_hung_then_reset` | slave never answers, then 0xFF: only 0x03; the abandoned read never answers |

### Integration TB — `src/tb_unit/top/cxp_device_top/`

25 tests with the real register file on `reg_*`, a 64-word APB slave on the user window at 0x0002_0000 (wrapper `RX_CLK_KHZ` 10, so 100 ms = 1000 `rx_clk` cycles), unrelated clocks (`rbuf_clk` = `tx_clk`) and the golden host (`common/cxp_host.py`, spec wire format). Reads, writes, ConnectionReset (exactly one ack), TestMode and a 2-word counter read all go through this module. `test_08` sends the parser's refusals end to end, `test_13` a read right behind a read (both acknowledgments intact), `test_24` a slow and a hung APB slave (Wait within 200 ms, 0x40 before the announced time ends, a late answer completing nothing, no Wait for a bootstrap register), `test_25` 0xFF during execution, after a Wait, behind a read waiting to be framed and twice back to back.

### Other

- `src/tb_unit/top/cxp_interface_top/`: `rx_serial` held high, no command reaches this module.
- `src/verif/` PyUVM: control scoreboard pairs commands, APB activity and acks (`test_ctrl_cmd_read`, `test_ctrl_cmd_write`, `test_ctrl_reset_op`, `test_crc_error`, `test_pslverr_burst`, `test_router_reject`, `test_link_reset`); the shell now exports `sb_ctrl_nack_*`. Last recorded pass on the working tree of 2026-09-19, before this rework; not re-run here.

### Running

```
make -C src/tb_unit/ctrl/cxp_ctrl_plane
make -C src/tb_unit/ctrl/cxp_ctrl_cmd_parser
make -C src/tb_unit/ctrl/cxp_ctrl_bus_master
make -C src/tb_unit/rx/cxp_rx_link
make -C src/tb_unit/top/cxp_device_top
make tb
```

2026-09-19, working tree on `conformance/cxp111`: `make tb` 304/304, router 8/8, bus master 8/8.

2026-09-26 (uncommitted working tree, router folded into the executor): `cxp_ctrl_plane` 13/13, `cxp_ctrl_cmd_parser` 20/20, `cxp_ctrl_bus_master` 14/14, `cxp_device_top` 25/25; full `make -C src/tb_unit` 389 tests, 0 failing.

### Not covered in-tree

- Undefined opcodes, Size 0, early trailer and over-length packets end to end beyond `cxp_device_top` test_08 (opcode 0x02, Size 0, oversize).
- A non-zero `reg_err_i` end to end is covered by `cxp_device_top` `test_09_access_codes` (0x40, 0x43, 0x41 from the register file); PSLVERR only in `src/verif/`.
- `from_extension_link_i` = 1 above this bench.
- A command crossing from the user window into the register file (unit bench of the executor only, test_04).

## Known issues and recommendations

### Medium

1. **Fixed: read-buffer handover relied on the host.** A read started while its predecessor's acknowledgment was still being framed overwrote the buffer under the reader. The read buffer now has two banks, the bank travels with the response, and the next command starts only after the response was taken (`cxp_device_top` test_13, now green untagged).
2. **Final-response count not asserted.** `cxp_ctrl_exec_sva` bounds a command, allows one Wait and holds the response; that every started command gets exactly one final response or is abandoned by a 0xFF is still unchecked. Effort: 2 h.

### Minor

- No timing constraint for the `rbuf` → read-port path between `rx_clk` and `tx_clk` ships with the IP; an asynchronous integration must add a false path or max-delay.
- ControlPacketSizeMax (0x400C in `src/regmap/cxp_regmap.yaml`) and `p_BUF_DEPTH` are tied by an elaboration check in `cxp_ctrl_cmd_parser`, not derived from one another.
- The executor's command timeout does not gate a register-file request in the cycle it expires, and can abort an APB transfer in its SETUP phase (`cxp_ctrl_bus_master.md` Minor 1, 2).
- The header still says "control-command executor" for the whole plane and lists "(Wait, final, 0x03)" as the response order; the executor loads 0x03 first.

### Open questions

1. Designer: should the register file be allowed to stall (then it relies on the Wait path), or is the one-cycle answer a contract (§10.3.3: no Wait on bootstrap registers)?
