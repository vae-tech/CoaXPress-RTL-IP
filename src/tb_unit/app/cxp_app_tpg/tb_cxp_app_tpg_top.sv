//-----------------------------------------------------------------------------
// tb_cxp_app_tpg_top
//
// Cocotb wrapper for the single-pixel test-pattern generator.  Re-exposes
// the DUT ports without the `_i`/`_o` suffixes so the cocotb test uses the
// pre-restyle names, and forwards the parameters under their un-prefixed
// names; the defaults (X_SIZE=64, Y_SIZE=32, PIXFMT=Mono8) cover every TB
// case.  Top-level `TESTCASE` register is written by Python so the current
// test number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_app_tpg_top #(
    parameter int          X_SIZE    = 64,
    parameter int          Y_SIZE    = 32,
    parameter logic [15:0] PIXFMT    = 16'h0101,
    parameter logic [15:0] TAPG      = 16'h0000,
    parameter logic [15:0] STREAMID  = 16'h0001,
    parameter logic [7:0]  FLAGS     = 8'h00
) (
    input  wire  logic        app_clk,
    input  wire  logic        app_rst_n,

    input  wire  logic        cfg_run,
    input  wire  logic [15:0] cfg_xsize,
    input  wire  logic [15:0] cfg_ysize,
    input  wire  logic [15:0] cfg_pixfmt,
    input  wire  logic [1:0]  cfg_testpat,
    input  wire  logic [15:0] cfg_xoffs,
    input  wire  logic [15:0] cfg_yoffs,
    input  wire  logic [15:0] cfg_srctag,

    output logic [15:0]       m_pix_data,
    output logic              m_pix_valid,
    input  wire  logic        m_pix_ready,
    output logic              m_pix_sof,
    output logic              m_pix_sol,
    output logic              m_pix_eol,
    output logic              m_pix_eof,

    output logic [23:0]       meta_xsize,
    output logic [23:0]       meta_ysize,
    output logic [23:0]       meta_xoffs,
    output logic [23:0]       meta_yoffs,
    output logic [15:0]       meta_pixfmt,
    output logic [15:0]       meta_tapg,
    output logic [15:0]       meta_streamid,
    output logic [15:0]       meta_sourcetag,
    output logic [7:0]        meta_flags
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    cxp_pkg::cxp_meta_t meta;

    assign meta_xsize     = meta.xsize;
    assign meta_ysize     = meta.ysize;
    assign meta_xoffs     = meta.xoffs;
    assign meta_yoffs     = meta.yoffs;
    assign meta_pixfmt    = meta.pixfmt;
    assign meta_tapg      = meta.tapg;
    assign meta_streamid  = 16'(meta.streamid);
    assign meta_sourcetag = meta.sourcetag;
    assign meta_flags     = meta.flags;

    cxp_app_tpg #(
        .p_X_SIZE    (X_SIZE),
        .p_Y_SIZE    (Y_SIZE),
        .p_PIXFMT    (PIXFMT)
    ) cxp_app_tpg_i (
        .app_clk          (app_clk),
        .app_rst_n        (app_rst_n),
        .cfg_run_i        (cfg_run),
        .cfg_xsize_i      (cfg_xsize),
        .cfg_ysize_i      (cfg_ysize),
        .cfg_pixfmt_i     (cfg_pixfmt),
        .cfg_testpat_i    (cfg_testpat),
        .cfg_xoffs_i      (cfg_xoffs),
        .cfg_yoffs_i      (cfg_yoffs),
        .cfg_srctag_i     (cfg_srctag),
        .cfg_streamid_i   (STREAMID[7:0]),
        .cfg_tapg_i       (TAPG),
        .cfg_flags_i      (FLAGS),
        .m_pix_data_o     (m_pix_data),
        .m_pix_valid_o    (m_pix_valid),
        .m_pix_ready_i    (m_pix_ready),
        .m_pix_sof_o      (m_pix_sof),
        .m_pix_sol_o      (m_pix_sol),
        .m_pix_eol_o      (m_pix_eol),
        .m_pix_eof_o      (m_pix_eof),
        .meta_o           (meta)
    );
endmodule

`default_nettype wire
