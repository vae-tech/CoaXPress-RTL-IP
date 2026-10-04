# cxp_ctrl_apb_bridge

Inputs chosen: RTL `src/rtl/ctrl/cxp_ctrl_apb_bridge.sv`; parent `src/rtl/ctrl/cxp_ctrl_bus_master.sv`; bench users `src/tb_unit/rx/cxp_rx_link/tb_cxp_rx_link_top.sv`, `src/tb_unit/ctrl/cxp_ctrl_bus_master/`, `src/tb_unit/ctrl/cxp_ctrl_plane/`, `src/tb_unit/top/cxp_device_top/`; spec JIIA CXP-001-2015 v1.1.1 §8.6.1.1, §8.6.3 Table 22; AMBA APB3.

Bridges one register-bus access (`req`/`we`/`addr`/`wdata` → `ack`/`rdata`/`err`) to one APB3 transfer. `cxp_ctrl_bus_master` instantiates it (`cxp_ctrl_apb_bridge_i`, only when `p_USER_SIZE` ≠ 0) for the user window [`p_USER_BASE`, +`p_USER_SIZE`), which appears as `apb_*` on `cxp_ctrl_plane` and `cxp_interface_top` and as `m_apb_*` on `cxp_device_top`. APB carries a single error bit, so `PSLVERR` becomes one fixed Table 22 code. `abort_i` lets the executor give up a transfer whose slave does not answer.

## Interface

| Name | Default | Meaning |
|---|---|---|
| `p_SLVERR_CODE` | 0x40 | Code returned on `err_o` for `pslverr_i` = 1 (invalid address). The executor passes `ACK_ERR_BAD_ADDR` (0x40). |

| Name | Dir | Width | Description |
|---|---|---|---|
| `clk`, `rst_n` | in | 1 | Bus clock (`rx_clk`); asynchronous active-low reset. |
| `abort_i` | in | 1 | Give up the transfer: no ack follows. Latched while PSEL is high. The transfer itself runs on unchanged until PREADY (APB has no abort); only its completion is discarded. |
| `wstrb_i` | in | 4 | Byte enables of a write, driven onto `pstrb_o` at SETUP. |
| `busy_o` | out | 1 | A transfer is open (= PSEL). The executor holds a user-window access in ST_REQ while it is high. |
| `req_i`, `we_i`, `addr_i`, `wdata_i` | in | 1, 1, 32, 32 | Access request (one cycle) and its fields. |
| `ack_o`, `rdata_o`, `err_o` | out | 1, 32, 8 | Registered; `ack_o` one cycle, `err_o` 0 or `p_SLVERR_CODE`. |
| `psel_o`, `penable_o`, `pwrite_o`, `paddr_o`, `pwdata_o`, `pstrb_o` | out | 1, 1, 1, 32, 32, 4 | APB3 master plus APB4 PSTRB, all registered. `pstrb_o[n]` enables `pwdata_o[8n+7:8n]`; 0 during a read. |
| `prdata_i`, `pready_i`, `pslverr_i` | in | 32, 1, 1 | APB3 slave response. |

## How it works

`req_i` while idle → SETUP (`psel`, address, data, direction latched) → ACCESS (`penable`) → wait for `pready_i` → idle, with `ack_o`, `rdata_o` and `err_o` registered in the same edge. A transfer takes at least 3 cycles plus one for the ack. There is no timeout here. The executor counts its command time (Wait after `p_WAIT_AFTER_MS`, 0x40 after `p_TIMEOUT_MS`) and, at the timeout or at the end of a drain after a 0xFF, raises `abort_i` while the transfer is still unanswered: the bridge keeps PSEL, PENABLE, PADDR, PWRITE, PWDATA and PSTRB as they are until PREADY and then returns to idle without an ack. Commands to the register file run meanwhile; a user-window access waits for `busy_o` to drop (or for its own command timeout). Before 2026-10-04 the bridge dropped PSEL and PENABLE without PREADY, an illegal APB cycle (review RX-03).

## Verification

No unit TB of its own. Through its parent:

- `src/tb_unit/ctrl/cxp_ctrl_bus_master/`: the wrapper shows the APB side as the old register bus (SETUP = `usr_req`, PREADY = `usr_ack`). test_04 (window decode), test_05 (Wait), test_06 (never answers: 0x40 and PSEL dropped), test_07 / test_10 (0xFF with a transfer outstanding: drained), test_11 (an answer after the timeout completes nothing).
- `src/tb_unit/ctrl/cxp_ctrl_plane/` and `src/tb_unit/top/cxp_device_top/`: a Verilog APB slave with a set latency (or none) on the user window; `cxp_device_top` test_24 runs the abort in real ms.
- `src/tb_unit/rx/cxp_rx_link/` uses a separate instance between the plane's register port and a Python APB slave, `abort_i` tied 0 (reads, a slave that never answers in test_11/test_12).
- `src/tb_unit/top/cxp_interface_top/` and `src/verif/` keep the register file behind the top's APB port with the default whole-space user window; `src/verif/` `test_pslverr_burst` injects `PSLVERR` and checks 0x40.

## Known issues and recommendations

### Critical

None.

### Medium

1. **Fixed: a hung APB slave kept the bridge busy.** After the executor timed out, the bridge still waited for `pready_i` and ignored the next request, so its late answer could complete another command. `abort_i` now ends the transfer (`cxp_ctrl_bus_master` test_06, test_11; `cxp_device_top` test_24).

- **Fixed 2026-10-04: APB transfer withdrawn without PREADY** (review RX-03) and **no PSTRB** (RX-04: a 1-byte write over 0x11223344 left 0xAA000000). `cxp_ctrl_bus_master` test_06, test_16, test_17 and `cxp_device_top` test_39.

### Minor

- **Fixed: abort in the SETUP cycle.** An `abort_i` while PSEL is high and PENABLE low is latched; the transfer enters ACCESS and ends there, so every SETUP is followed by an ACCESS phase. An abort in the cycle PREADY is high lets the transfer complete on the bus but returns no ack (the command answers 0x40). No test drives either case.
- No unit TB and no SVA (`psel_o && !penable_o && !abort_i |=> penable_o`; `req_i |-> !psel_o`).
- `p_SLVERR_CODE` is one code for every error; a slave that needs 0x41/0x43/0x44 must use the register-file port instead.
