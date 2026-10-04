//-----------------------------------------------------------------------------
// tb_cxp_tx_trigger_hs_top
//
// Thin cocotb wrapper around the device trigger source.  Flattens the ports
// to un-suffixed names and adds a top-level `TESTCASE` register, written by
// Python so the current test number is visible in waves.  `ACK_TIMEOUT_P`
// (default 64 tx_clk cycles) is the acknowledgment timeout.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_tx_trigger_hs_top #(
    parameter int ACK_TIMEOUT_P = 64
) (
    // --- system ---
    input  wire  logic        tx_clk,
    input  wire  logic        tx_rst_n,

    // --- trigger pin / config ---
    input  wire  logic        trigger_in,
    input  wire  logic        cfg_polarity,

    // --- host side ---
    input  wire  logic        link_up,
    input  wire  logic        mask,
    input  wire  logic        ack,

    // --- packet out (inserter trigger port) ---
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

    cxp_tx_trigger_hs #(
        .p_ACK_TIMEOUT (ACK_TIMEOUT_P)
    ) cxp_tx_trigger_hs_i (
        .tx_clk         (tx_clk),
        .tx_rst_n       (tx_rst_n),
        .trigger_in_i   (trigger_in),
        .cfg_polarity_i (cfg_polarity),
        .link_up_i      (link_up),
        .mask_i         (mask),
        .ack_i          (ack),
        .m_o            (m_w),
        .m_ready_i      (m_ready)
    );
endmodule

`default_nettype wire
