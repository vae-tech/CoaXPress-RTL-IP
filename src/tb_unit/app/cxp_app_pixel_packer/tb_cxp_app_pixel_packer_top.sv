//-----------------------------------------------------------------------------
// tb_cxp_app_pixel_packer_top
//
// Thin cocotb wrapper around `cxp_app_pixel_packer`.  Re-exposes the DUT ports
// without the `_i`/`_o` suffixes so the test keeps the pre-restyle names.
// Top-level `TESTCASE` register is written by Python so the current test
// number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_app_pixel_packer_top (
    input  wire  logic        app_clk,
    input  wire  logic        app_rst_n,

    input  wire  logic [15:0] cfg_pixfmt,

    input  wire  logic [15:0] s_pix_data,
    input  wire  logic [4:0]  s_pix_w,          // 0 = TPG-style values (the default)
    input  wire  logic        s_pix_valid,
    input  wire  logic        s_pix_sol,
    input  wire  logic        s_pix_eol,
    input  wire  logic        s_pix_sof,
    input  wire  logic        s_pix_eof,
    output logic              s_pix_ready,

    output logic [31:0]       m_word_data,
    output logic [3:0]        m_word_lane_vld,
    output logic              m_word_valid,
    output logic              m_word_sol,
    output logic              m_word_eol,
    output logic              m_word_sof,
    output logic              m_word_eof,
    input  wire  logic        m_word_ready
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    cxp_app_pixel_packer cxp_app_pixel_packer_i (
        .app_clk           (app_clk),
        .app_rst_n         (app_rst_n),
        .cfg_pixfmt_i      (cfg_pixfmt),
        .s_pix_data_i      (s_pix_data),
        .s_pix_w_i         (s_pix_w),
        .s_pix_valid_i     (s_pix_valid),
        .s_pix_sol_i       (s_pix_sol),
        .s_pix_eol_i       (s_pix_eol),
        .s_pix_sof_i       (s_pix_sof),
        .s_pix_eof_i       (s_pix_eof),
        .s_pix_ready_o     (s_pix_ready),
        .m_word_data_o     (m_word_data),
        .m_word_lane_vld_o (m_word_lane_vld),
        .m_word_valid_o    (m_word_valid),
        .m_word_sol_o      (m_word_sol),
        .m_word_eol_o      (m_word_eol),
        .m_word_sof_o      (m_word_sof),
        .m_word_eof_o      (m_word_eof),
        .m_word_ready_i    (m_word_ready),
        .m_pixfmt_o        (),
        .s_meta_i          ('0),
        .m_meta_o          ()
    );
endmodule

`default_nettype wire
