//-----------------------------------------------------------------------------
// tb_cxp_app_line_marker_top
//
// Cocotb wrapper for the line-marker generator.  Re-exposes the pre-restyle
// port names (no `_i`/`_o` suffix) so test_cxp_app_line_marker.py can drive
// the metadata / `line_start` inputs and the word-port ready directly; no
// glue logic.  Top-level `TESTCASE` register is written by Python so the
// current test number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_app_line_marker_top (
    input  wire  logic        app_clk,
    input  wire  logic        app_rst_n,
    input  wire  logic        cfg_arbitrary,
    input  wire  logic [23:0] line_xsize,
    input  wire  logic [23:0] line_xoffs,
    input  wire  logic [15:0] line_pixfmt,
    input  wire  logic        line_start,

    output logic [31:0]       m_word_data,
    output logic [3:0]        m_word_kmask,
    output logic              m_word_valid,
    input  wire  logic        m_word_ready
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    cxp_pkg::cxp_meta_t meta;

    always_comb begin
        meta           = '0;
        meta.arbitrary = cfg_arbitrary;
        meta.xsize     = line_xsize;
        meta.xoffs     = line_xoffs;
        meta.pixfmt    = line_pixfmt;
    end

    cxp_app_line_marker cxp_app_line_marker_i (
        .app_clk         (app_clk),
        .app_rst_n       (app_rst_n),
        .meta_i          (meta),
        .line_start_i    (line_start),
        .m_word_data_o   (m_word_data),
        .m_word_kmask_o  (m_word_kmask),
        .m_word_valid_o  (m_word_valid),
        .m_word_ready_i  (m_word_ready)
    );
endmodule

`default_nettype wire
