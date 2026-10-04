//-----------------------------------------------------------------------------
// tb_cxp_tx_ctrl_ack_top
//
// Thin cocotb wrapper around `cxp_tx_ctrl_ack`.  Pins p_BUF_DEPTH = 16 (the
// RTL default is 64) so the rbuf address bus is a tidy 4 bits in the
// waveform.  Re-exposes the DUT ports without the `_i`/`_o` suffixes so the
// test keeps the pre-restyle names.  Top-level `TESTCASE` register is
// written by Python so the current test number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_tx_ctrl_ack_top #(
    parameter int BUF_DEPTH = 16
) (
    input  wire  logic        tx_clk,
    input  wire  logic        tx_rst_n,

    input  wire  logic        ack_req,
    input  wire  logic [7:0]  ack_code,
    input  wire  logic [23:0] ack_size,
    input  wire  logic [31:0] ack_wait_ms,
    input  wire  logic        ack_rbank,
    output logic              rbuf_bank,
    output logic              ack_busy,

    output logic [$clog2(BUF_DEPTH)-1:0] rbuf_addr,
    input  wire  logic [31:0] rbuf_data,

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

    cxp_tx_ctrl_ack #(
        .p_BUF_DEPTH (BUF_DEPTH)
    ) cxp_tx_ctrl_ack_i (
        .tx_clk           (tx_clk),
        .tx_rst_n         (tx_rst_n),
        .ack_req_i        (ack_req),
        .ack_code_i       (ack_code),
        .ack_size_i       (ack_size),
        .ack_wait_ms_i    (ack_wait_ms),
        .ack_rbank_i      (ack_rbank),
        .ack_busy_o       (ack_busy),
        .rbuf_addr_o      ({rbuf_bank, rbuf_addr}),
        .rbuf_data_i      (rbuf_data),
        .m_o              (m_w),
        .m_ready_i        (m_ready)
    );
endmodule

`default_nettype wire
