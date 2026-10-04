//-----------------------------------------------------------------------------
// tb_cxp_tx_stream_pkt_top
//
// Thin cocotb wrapper around `cxp_tx_stream_pkt`.  Re-exposes the DUT ports
// without the `_i`/`_o` suffixes (pre-restyle names used by the Python
// test); no glue logic.  Top-level `TESTCASE` register is written by Python
// so the current test number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_tx_stream_pkt_top (
    input  wire  logic        tx_clk,
    input  wire  logic        tx_rst_n,

    input  wire  logic        stream_ctrl_reset,
    input  wire  logic        stream_en,
    input  wire  logic        suppress_stream,

    input  wire  logic [31:0] s_data,
    input  wire  logic [3:0]  s_kmask,
    input  wire  logic        s_valid,
    input  wire  logic        s_sop,
    input  wire  logic        s_eop,
    input  wire  logic [7:0]  s_streamid,
    input  wire  logic [15:0] s_len,
    input  wire  logic        s_pkt_avail,
    output logic              s_ready,

    output logic [31:0]       m_data,
    output logic [3:0]        m_kmask,
    output logic              m_valid,
    output logic              m_sop,
    output logic              m_eop,
    input  wire  logic        m_ready
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    // The DUT's packet word, unpacked to the flat ports the tests read.
    cxp_pkg::cxp_txw_t m_w;

    assign m_data  = m_w.data;
    assign m_kmask = m_w.kmask;
    assign m_valid = m_w.valid;
    assign m_sop   = m_w.sop;
    assign m_eop   = m_w.eop;

    cxp_tx_stream_pkt cxp_tx_stream_pkt_i (
        .tx_clk              (tx_clk),
        .tx_rst_n            (tx_rst_n),
        .stream_ctrl_reset_i (stream_ctrl_reset),
        .stream_en_i         (stream_en),
        .suppress_stream_i   (suppress_stream),
        .s_data_i            (s_data),
        .s_kmask_i           (s_kmask),
        .s_valid_i           (s_valid),
        .s_sop_i             (s_sop),
        .s_eop_i             (s_eop),
        .s_streamid_i        (s_streamid),
        .s_len_i             (s_len),
        .s_pkt_avail_i       (s_pkt_avail),
        .s_ready_o           (s_ready),
        .m_o                 (m_w),
        .m_ready_i           (m_ready)
    );
endmodule

`default_nettype wire
