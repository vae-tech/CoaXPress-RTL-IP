//-----------------------------------------------------------------------------
// tb_cxp_app_acq_ctrl_top
//
// Thin cocotb wrapper for `cxp_app_acq_ctrl`.  Top-level `TESTCASE` is written
// by Python.
//-----------------------------------------------------------------------------

`default_nettype none

module tb_cxp_app_acq_ctrl_top (
    input  wire  logic        clk,
    input  wire  logic        rst_n,
    input  wire  logic        acq_start,
    input  wire  logic        acq_stop,
    input  wire  logic [1:0]  acq_mode,
    input  wire  logic [15:0] frame_count,
    input  wire  logic        stream_en,
    input  wire  logic        run,
    output logic              active,
    // Pixel gate: one pixel stream in, the entered images out
    input  wire  logic [15:0] pix_data,
    input  wire  logic        pix_valid,
    input  wire  logic        pix_sof,
    input  wire  logic        pix_eof,
    output logic              pix_ready,
    output logic [15:0]       out_data,
    output logic              out_valid,
    input  wire  logic        out_ready
);

    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */
    initial TESTCASE = 8'h00;

    cxp_pkg::cxp_pix_t s_pix, m_pix;
    always_comb begin
        s_pix       = '0;
        s_pix.data  = pix_data;
        s_pix.valid = pix_valid;
        s_pix.sof   = pix_sof;
        s_pix.sol   = pix_sof;
        s_pix.eof   = pix_eof;
        s_pix.eol   = pix_eof;
    end
    assign out_data  = m_pix.data;
    assign out_valid = m_pix.valid;

    cxp_app_acq_ctrl cxp_app_acq_ctrl_i (
        .clk           (clk),
        .rst_n         (rst_n),
        .acq_start_i   (acq_start),
        .acq_stop_i    (acq_stop),
        .acq_mode_i    (acq_mode),
        .frame_count_i (frame_count),
        .run_i         (run),
        .stream_en_i   (stream_en),
        .src_gated_i   (1'b0),
        .active_o      (active),
        .s_pix_i       (s_pix),
        .s_pix_ready_o (pix_ready),
        .m_pix_o       (m_pix),
        .m_pix_ready_i (out_ready)
    );

endmodule

`default_nettype wire
