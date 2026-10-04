//-----------------------------------------------------------------------------
// tb_cxp_app_pixel_ingress_top
//
// Cocotb wrapper for the single-pixel sensor adapter `cxp_app_pixel_ingress`
// (modules doc §2.1).  Re-exposes the DUT ports without the `_i`/`_o`
// suffixes and forwards `PIX_W` (default 16) to `p_PIX_W`; no glue logic.
// Top-level `TESTCASE` register is written by Python so the current test
// number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_app_pixel_ingress_top #(
    parameter int PIX_W = 16
) (
    input  wire  logic              app_clk,
    input  wire  logic              app_rst_n,

    input  wire  logic [PIX_W-1:0]  s_pix_data,
    input  wire  logic              s_pix_valid,
    input  wire  logic              s_pix_sof,
    input  wire  logic              s_pix_eol,
    input  wire  logic              s_pix_eof,
    output logic                    s_pix_ready,

    input  wire  logic [23:0]       s_pix_xoffs,
    input  wire  logic [23:0]       s_pix_yoffs,
    input  wire  logic [23:0]       s_pix_xsize,
    input  wire  logic [23:0]       s_pix_ysize,
    input  wire  logic [15:0]       s_pix_pixfmt,
    input  wire  logic [15:0]       s_pix_tapg,
    input  wire  logic [7:0]        s_pix_streamid,
    input  wire  logic [15:0]       s_pix_sourcetag,
    input  wire  logic [7:0]        s_pix_flags,

    output logic [PIX_W-1:0]        m_pix_data,
    output logic                    m_pix_valid,
    output logic                    m_pix_sol,
    output logic                    m_pix_eol,
    output logic                    m_pix_sof,
    output logic                    m_pix_eof,
    input  wire  logic              m_pix_ready,

    output logic [23:0]             m_meta_xoffs,
    output logic [23:0]             m_meta_yoffs,
    output logic [23:0]             m_meta_xsize,
    output logic [23:0]             m_meta_ysize,
    output logic [15:0]             m_meta_pixfmt,
    output logic [15:0]             m_meta_tapg,
    output logic [7:0]              m_meta_streamid,
    output logic [15:0]             m_meta_sourcetag,
    output logic [7:0]              m_meta_flags,
    output logic                    m_meta_valid,

    output logic                    spurious_eof,
    output logic                    sof_restart
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    // Metadata in and out of the DUT as cxp_meta_t, flat for the tests.
    cxp_pkg::cxp_meta_t s_meta, m_meta;

    assign s_meta = '{arbitrary: 1'b0,
                      streamid:  s_pix_streamid,
                      sourcetag: s_pix_sourcetag,
                      xsize:     s_pix_xsize,
                      ysize:     s_pix_ysize,
                      xoffs:     s_pix_xoffs,
                      yoffs:     s_pix_yoffs,
                      pixfmt:    s_pix_pixfmt,
                      tapg:      s_pix_tapg,
                      flags:     s_pix_flags};

    assign m_meta_xoffs     = m_meta.xoffs;
    assign m_meta_yoffs     = m_meta.yoffs;
    assign m_meta_xsize     = m_meta.xsize;
    assign m_meta_ysize     = m_meta.ysize;
    assign m_meta_pixfmt    = m_meta.pixfmt;
    assign m_meta_tapg      = m_meta.tapg;
    assign m_meta_streamid  = m_meta.streamid;
    assign m_meta_sourcetag = m_meta.sourcetag;
    assign m_meta_flags     = m_meta.flags;

    cxp_app_pixel_ingress #(
        .p_PIX_W (PIX_W)
    ) cxp_app_pixel_ingress_i (
        .app_clk         (app_clk),
        .app_rst_n       (app_rst_n),

        .s_pix_data_i    (s_pix_data),
        .s_pix_valid_i   (s_pix_valid),
        .s_pix_sof_i     (s_pix_sof),
        .s_pix_eol_i     (s_pix_eol),
        .s_pix_eof_i     (s_pix_eof),
        .s_pix_ready_o   (s_pix_ready),

        .s_meta_i        (s_meta),

        .m_pix_data_o    (m_pix_data),
        .m_pix_valid_o   (m_pix_valid),
        .m_pix_sol_o     (m_pix_sol),
        .m_pix_eol_o     (m_pix_eol),
        .m_pix_sof_o     (m_pix_sof),
        .m_pix_eof_o     (m_pix_eof),
        .m_pix_ready_i   (m_pix_ready),

        .m_meta_o        (m_meta),
        .m_meta_valid_o  (m_meta_valid),

        .spurious_eof_o  (spurious_eof),
        .sof_restart_o   (sof_restart)
    );
endmodule

`default_nettype wire
