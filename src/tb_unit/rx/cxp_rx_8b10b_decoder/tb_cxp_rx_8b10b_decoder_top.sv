//-----------------------------------------------------------------------------
// tb_cxp_rx_8b10b_decoder_top
//
// Cocotb wrapper for the combinational 8B/10B decoder.  A free-running
// clock (`tb_clk`) is provided so cocotb can sequence stimuli; the DUT
// itself is purely combinational.  Top-level `TESTCASE` register is
// written by Python so the current test number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_rx_8b10b_decoder_top (
    input  wire  logic        tb_clk,
    input  wire  logic [9:0]  din,
    input  wire  logic        rd_in,
    output logic [7:0]        dout,
    output logic              k_out,
    output logic              disp_err,
    output logic              code_err,
    output logic              rd_out
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    cxp_rx_8b10b_decoder cxp_rx_8b10b_decoder_i (
        .din_i      (din),
        .rd_in_i    (rd_in),
        .dout_o     (dout),
        .k_out_o    (k_out),
        .disp_err_o (disp_err),
        .code_err_o (code_err),
        .rd_out_o   (rd_out)
    );
endmodule

`default_nettype wire
