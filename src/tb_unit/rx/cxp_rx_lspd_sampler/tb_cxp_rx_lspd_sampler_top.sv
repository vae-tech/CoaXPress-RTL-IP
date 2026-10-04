//-----------------------------------------------------------------------------
// tb_cxp_rx_lspd_sampler_top
//
// Thin cocotb wrapper around `cxp_rx_lspd_sampler`.  Shrinks OS_RATIO
// (16 -> 8) so lock-acquisition tests complete in a few thousand os_clk
// cycles; loss of lock is driven through `resync` (the link monitor's job).
// Top-level `TESTCASE` register is written by Python so the current test
// number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_rx_lspd_sampler_top #(
    parameter int OS_RATIO  = 8,    // shrunk from RTL default 16 for speed
    parameter int LOCK_HITS = 2
) (
    input  wire  logic        os_clk,
    input  wire  logic        os_rst_n,
    input  wire  logic        rx_serial,
    input  wire  logic        resync,
    output logic [39:0]       sym_out,
    output logic              sym_valid,
    output logic              rx_lock
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    cxp_rx_lspd_sampler #(
        .p_OS_RATIO  (OS_RATIO),
        .p_LOCK_HITS (LOCK_HITS)
    ) cxp_rx_lspd_sampler_i (
        .os_clk       (os_clk),
        .os_rst_n     (os_rst_n),
        .rx_serial_i  (rx_serial),
        .resync_i     (resync),
        .sym_out_o    (sym_out),
        .sym_valid_o  (sym_valid),
        .rx_lock_o    (rx_lock)
    );
endmodule

`default_nettype wire
