//-----------------------------------------------------------------------------
// tb_cxp_app_image_header_top
//
// Thin cocotb wrapper around `cxp_app_image_header`.  Re-exposes the
// pre-restyle port names (no `_i`/`_o` suffix) so the cocotb test
// (test_cxp_app_image_header.py) drives plain names.  Top-level `TESTCASE`
// register is written by Python so the current test number is visible in
// waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_app_image_header_top (
    input  wire  logic        app_clk,
    input  wire  logic        app_rst_n,

    input  wire  logic        cfg_arbitrary,

    input  wire  logic [15:0] meta_streamid,
    input  wire  logic [15:0] meta_sourcetag,
    input  wire  logic [23:0] meta_xsize,
    input  wire  logic [23:0] meta_ysize,
    input  wire  logic [23:0] meta_xoffs,
    input  wire  logic [23:0] meta_yoffs,
    input  wire  logic [15:0] meta_pixfmt,
    input  wire  logic [15:0] meta_tapg,
    input  wire  logic [7:0]  meta_flags,
    input  wire  logic        meta_valid,

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

    assign meta = '{arbitrary: cfg_arbitrary,
                    streamid:  meta_streamid[7:0],
                    sourcetag: meta_sourcetag,
                    xsize:     meta_xsize,
                    ysize:     meta_ysize,
                    xoffs:     meta_xoffs,
                    yoffs:     meta_yoffs,
                    pixfmt:    meta_pixfmt,
                    tapg:      meta_tapg,
                    flags:     meta_flags};

    cxp_app_image_header cxp_app_image_header_i (
        .app_clk          (app_clk),
        .app_rst_n        (app_rst_n),
        .meta_i           (meta),
        .meta_valid_i     (meta_valid),
        .m_word_data_o    (m_word_data),
        .m_word_kmask_o   (m_word_kmask),
        .m_word_valid_o   (m_word_valid),
        .m_word_ready_i   (m_word_ready)
    );
endmodule

`default_nettype wire
