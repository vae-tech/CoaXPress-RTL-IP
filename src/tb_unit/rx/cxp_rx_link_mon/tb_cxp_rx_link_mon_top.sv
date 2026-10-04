//-----------------------------------------------------------------------------
// tb_cxp_rx_link_mon_top
//
// Cocotb wrapper for the uplink IDLE monitor.  Shrinks the loss window to
// LOSS_WORDS = 64 (p_SHORT_LOSS_OK = 1; the RTL default is 20 000) so a
// loss of lock fits in a few hundred cycles.  Top-level `TESTCASE`
// register is written by Python so the current test number is visible in
// waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_rx_link_mon_top #(
    parameter int LOCK_IDLES = 2,
    parameter int LOSS_WORDS = 64
) (
    input  wire  logic        rx_clk,
    input  wire  logic        rx_rst_n,
    input  wire  logic        rx_lock,
    input  wire  logic [31:0] data_in,
    input  wire  logic [3:0]  kmask_in,
    input  wire  logic        err_in,
    input  wire  logic        valid_in,
    output logic [31:0]       data_out,
    output logic [3:0]        kmask_out,
    output logic              valid_out,
    output logic              up,
    output logic              link_detected,
    output logic              resync,
    output logic              flush
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    cxp_rx_link_mon #(
        .p_LOCK_IDLES    (LOCK_IDLES),
        .p_LOSS_WORDS    (LOSS_WORDS),
        .p_SHORT_LOSS_OK (1'b1)
    ) cxp_rx_link_mon_i (
        .rx_clk          (rx_clk),
        .rx_rst_n        (rx_rst_n),
        .rx_lock_i       (rx_lock),
        .data_i          (data_in),
        .kmask_i         (kmask_in),
        .err_i           (err_in),
        .valid_i         (valid_in),
        .data_o          (data_out),
        .kmask_o         (kmask_out),
        .valid_o         (valid_out),
        .up_o            (up),
        .link_detected_o (link_detected),
        .resync_o        (resync),
        .flush_o         (flush)
    );

endmodule

`default_nettype wire
