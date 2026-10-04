//-----------------------------------------------------------------------------
// tb_cxp_cdc_stream_fifo_top
//
// Cocotb wrapper around `cxp_cdc_stream_fifo`.  Reduces DEPTH to 64 so the
// almost-full / fill / wrap behaviour can be exercised in a few hundred
// simulator cycles.  All other parameters track the RTL defaults.  Both
// clocks and resets are driven from Python.  Top-level `TESTCASE` register
// is written by Python so the current test number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_cdc_stream_fifo_top #(
    parameter int DEPTH              = 64,
    parameter int DATA_W             = 32,
    parameter int ALMOST_FULL_MARGIN = 4
) (
    input  wire  logic              app_clk,
    input  wire  logic              app_rst_n,
    input  wire  logic              tx_clk,
    input  wire  logic              tx_rst_n,

    input  wire  logic [DATA_W-1:0] s_data,
    input  wire  logic [3:0]        s_kmask,
    input  wire  logic              s_valid,
    input  wire  logic              s_sop,
    input  wire  logic              s_eop,
    output logic                    s_ready,

    output logic [DATA_W-1:0]       m_data,
    output logic [3:0]              m_kmask,
    output logic                    m_valid,
    output logic                    m_sop,
    output logic                    m_eop,
    input  wire  logic              m_ready,
    output logic                    m_pkt_avail,
    output logic [15:0]             m_len,
    input  wire  logic [7:0]        s_streamid,
    output logic [7:0]              m_streamid,
    input  wire  logic              m_busy,
    input  wire  logic              flush,
    output logic                    flush_ack,
    output logic                    s_flush
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    cxp_cdc_stream_fifo #(
        .p_DEPTH              (DEPTH),
        .p_DATA_W             (DATA_W),
        .p_ALMOST_FULL_MARGIN (ALMOST_FULL_MARGIN)
    ) cxp_cdc_stream_fifo_i (
        .app_clk       (app_clk),
        .app_rst_n     (app_rst_n),
        .tx_clk        (tx_clk),
        .tx_rst_n      (tx_rst_n),
        .s_data_i      (s_data),
        .s_kmask_i     (s_kmask),
        .s_valid_i     (s_valid),
        .s_sop_i       (s_sop),
        .s_eop_i       (s_eop),
        .s_ready_o     (s_ready),
        .m_data_o      (m_data),
        .m_kmask_o     (m_kmask),
        .m_valid_o     (m_valid),
        .m_sop_o       (m_sop),
        .m_eop_o       (m_eop),
        .m_ready_i     (m_ready),
        .m_pkt_avail_o (m_pkt_avail),
        .m_len_o       (m_len),
        .s_streamid_i  (s_streamid),
        .m_streamid_o  (m_streamid),
        .m_busy_i      (m_busy),
        .flush_i       (flush),
        .flush_ack_o   (flush_ack),
        .s_flush_o     (s_flush)
    );
endmodule

`default_nettype wire
