# cxp_cdc_reset

Inputs chosen: RTL `src/rtl/cdc/cxp_cdc_reset.sv`; user `src/rtl/top/cxp_device_top.sv`; bound SVA `src/sva/cxp_sva.sv` (`cxp_reset_order_sva`); system TB `src/tb_unit/top/cxp_device_top/`; related `src/rtl/cdc/cxp_cdc_link.sv` (`cxp_cdc.md`); spec JIIA CXP-001-2015 v1.1.1 §10.3.28 (power-up executes a connection reset).

One reset request in, one synchronised reset per clock domain out. The device keeps state on both sides of every clock crossing, so a reset of one domain alone would leave the crossings disagreeing. Here all three domains reset together and come out of reset in a fixed order.

| Output | Clock | Released | Resets in `cxp_device_top` |
|---|---|---|---|
| `rx_rst_n_o` | `rx_clk` | first, 2 `rx_clk` edges after `rst_n` rises | `cxp_interface_top` rx domain (uplink, control plane), `cxp_ctrl_bootstrap_regs` |
| `tx_rst_n_o` | `tx_clk` | 2 `tx_clk` edges after `rx_rst_n_o` | `cxp_interface_top` tx domain (framers, arbiter, trigger gate) |
| `app_rst_n_o` | `app_clk` | 2 `app_clk` edges after `tx_rst_n_o` | `cxp_interface_top` app domain (TPG, ingress, packer, FIFO write side) |

Source: `src/rtl/cdc/cxp_cdc_reset.sv`. One instance, `cxp_device_top.cxp_cdc_reset_i`, fed by `rst_req_n = app_rst_n & tx_rst_n & rx_rst_n`. The three reset inputs of `cxp_device_top` therefore act as one: asserting any of them resets the whole device, including the register file, which returns to its power-on values (the §10.3.28 ConnectionReset values, `cxp_ctrl_bootstrap_regs.md`). `cxp_interface_top` alone does not instantiate it; benches that use it directly (`tb_cxp_interface_top`, `src/verif/uvm/sv/tb_cxp_top.sv`) drive its three resets themselves.

## Interface

No parameters.

| Name | Dir | Width | Description |
|---|---|---|---|
| `rst_n` | in | 1 | Reset request, active low, asynchronous; no synchronous release needed |
| `rx_clk`, `tx_clk`, `app_clk` | in | 1 | The three domain clocks |
| `rx_rst_n_o` | out | 1 | `rx_clk` reset: asserts with `rst_n`, releases first |
| `tx_rst_n_o` | out | 1 | `tx_clk` reset: asserts with `rst_n`, releases after `rx_rst_n_o` |
| `app_rst_n_o` | out | 1 | `app_clk` reset: asserts with `rst_n`, releases after `tx_rst_n_o` |

- **Outputs:** each is the last flop of a 2-flop chain in its own clock, so its release is synchronous to that clock.
- **Clocking:** the tx chain samples `rx_rst_n_o` and the app chain samples `tx_rst_n_o`; those two inputs are the only crossings, each through the 2-flop chain. No ASYNC_REG attributes or constraints are in the repository (as for `cxp_cdc_*`).

## How it works

1. **Assert.** `rst_n` low clears all three chains (`rx_q`, `tx_q`, `app_q`) asynchronously, so every domain enters reset at once, without a clock.
2. **Release.** With `rst_n` high, `rx_q` shifts in 1 on `rx_clk`; `tx_q` shifts in `rx_q[1]` on `tx_clk`; `app_q` shifts in `tx_q[1]` on `app_clk`. Each output is bit 1 of its chain.
3. **Order.** Because each chain is fed by the previous output, tx cannot leave reset while rx is held, nor app while tx is held. The register file and the control plane (rx) are therefore up before anything that consumes their configuration. Every crossing in `cxp_interface_top` is reset on both sides together; the only one-sided state it sees is the few-cycle gap between two releases, which `cxp_cdc_link` absorbs (an event offered in the first ~10 cycles after both sides are released is not transferred, `cxp_cdc.md`).

Latency: release takes 2 `rx_clk` + 2 `tx_clk` + 2 `app_clk` edges after `rst_n` rises (one more edge per stage when a release lands inside the setup window). Assertion is immediate.

## Arbiter integration

Not applicable: no packet source.

## Verification

Verilator 5.046 + cocotb 2.0.1, `--assert`. No unit bench.

- **SVA `cxp_reset_order_sva`** (bound to every instance): on `tx_clk`, `tx_rst_n_o |-> rx_rst_n_o`; on `app_clk`, `app_rst_n_o |-> tx_rst_n_o`; both disabled while `rst_n` is low. It runs in every `cxp_device_top` test.
- **`src/tb_unit/top/cxp_device_top/`** (three unrelated clocks, 10 / 8 / 12 ns; `setup` releases the three inputs 7 ns and 5 ns apart):
  - `test_11_single_domain_reset`: after one acknowledged read and one acknowledged host trigger, `rx_rst_n` alone for 20 `rx_clk` cycles; no acknowledgment, I/O acknowledgment or trigger on the downlink; a read afterwards is answered once. Before this block and `cxp_cdc_link`, an rx-only reset produced a phantom control acknowledgment.
  - `test_14_single_domain_reset_streaming`: while streaming, `tx_rst_n` alone, then `app_rst_n` alone, 20 cycles each; nothing stray on the wire, no stream packet while StreamPacketSizeMax reads its power-on 0, and after rediscovery the stream restarts at PacketTag 0.
  - Every other test goes through the block at start-up.

Not covered: a reset input pulse shorter than one clock period; a clock that is stopped while its domain is released; release order under other clock ratios.

## Known issues and recommendations

### Critical

None.

### Medium

None.

### Minor

1. **The three inputs are one reset.** A reset of the app or tx domain alone (for example a sensor or SerDes recovery) now also resets the uplink and the register file, so the host loses the link and every register returns to its power-on value. This is intended (the header says so), but the port comments of `cxp_device_top` still read as three independent resets; say there that they are ORed into one device reset.
2. **Release depends on every clock running.** tx leaves reset only after rx, and app only after tx; with `rx_clk` stopped (for example an uplink clock taken from a recovered clock that is not yet valid) the whole device stays in reset. State the requirement for free-running `rx_clk` and `tx_clk` in the integration notes.
3. **The request is a combinational AND of three inputs** (`rst_req_n` in `cxp_device_top`). A glitch on any input asynchronously resets the whole device. Integrators should drive the inputs from glitch-free sources; CDC/RDC tools will want the AND and the three chains declared as the reset synchronisers.
4. **No unit bench.** The order is checked only by the SVA inside `cxp_device_top` runs, at one clock ratio. A small bench (random clock ratios, `rst_n` pulses shorter than a clock period, one clock held) would cover item 2 and the minimum pulse width. Effort: 2 h.
