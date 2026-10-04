//-----------------------------------------------------------------------------
// tb_cxp_rx_linktest_top
//
// Cocotb wrapper for the connection-test receiver.  Re-exposes the DUT
// ports under the pre-restyle names (no `_i`/`_o` suffix) so Python drives
// the gated long-packet stream and the two clears directly and reads both
// counters.  Top-level `TESTCASE` register is written by Python so the
// current test number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_rx_linktest_top (
    input  wire  logic        rx_clk,
    input  wire  logic        rx_rst_n,
    input  wire  logic        gate,
    input  wire  logic [31:0] long_data,
    input  wire  logic [3:0]  long_kmask,
    input  wire  logic        long_sop,
    input  wire  logic        long_eop,
    input  wire  logic        long_err,
    input  wire  logic        clr_err,
    input  wire  logic        clr_pkt,
    output logic [31:0]       err_count,
    output logic [63:0]       pkt_count
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    cxp_rx_linktest cxp_rx_linktest_i (
        .rx_clk        (rx_clk),
        .rx_rst_n      (rx_rst_n),
        .gate_i        (gate),
        .long_data_i   (long_data),
        .long_kmask_i  (long_kmask),
        .long_sop_i    (long_sop),
        .long_eop_i    (long_eop),
        .long_err_i    (long_err),
        .clr_err_i     (clr_err),
        .clr_pkt_i     (clr_pkt),
        .err_count_o   (err_count),
        .pkt_count_o   (pkt_count)
    );
endmodule

`default_nettype wire
