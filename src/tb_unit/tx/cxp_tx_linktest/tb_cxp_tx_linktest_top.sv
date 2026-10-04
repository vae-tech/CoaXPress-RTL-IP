//-----------------------------------------------------------------------------
// tb_cxp_tx_linktest_top
//
// Thin SystemVerilog wrapper around cxp_tx_linktest.  Two reasons it exists
// (mirroring the RX-side wrapper convention in src/tb_unit/rx/cxp_rx_linktest/):
//
//   1. Shrinks the production parameters so the TB stays interactive:
//      - DATA_WORDS  : 1024 → 16  (one minimum-spec packet body in 16 cycles)
//      - GAP_WORDS   :   16 →  4  (just long enough to confirm pacing)
//      Per-test overrides are applied via Verilator's `cocotb_sim.mk` flow
//      using `make COMPILE_ARGS+=…` if a TB ever needs the production
//      values; for the included tests the shrunk geometry is enough to
//      cover every observable behaviour.
//
//   2. Surfaces a public_flat TESTCASE register so the cocotb @cxp_test
//      decorator can label which test produced any given slice of the wave.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_tx_linktest_top #(
    parameter int DATA_WORDS = 16,
    parameter int GAP_WORDS  = 4
) (
    input  wire  logic        tx_clk,
    input  wire  logic        tx_rst_n,

    input  wire  logic        cfg_test_mode,
    output logic              suppress_traffic,

    input  wire  logic        clr_pkt_count,
    output logic [63:0]       pkt_count,

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

    cxp_tx_linktest #(
        .DATA_WORDS (DATA_WORDS),
        .GAP_WORDS  (GAP_WORDS)
    ) cxp_tx_linktest_i (
        .tx_clk             (tx_clk),
        .tx_rst_n           (tx_rst_n),
        .cfg_test_mode_i    (cfg_test_mode),
        .suppress_traffic_o (suppress_traffic),
        .clr_pkt_count_i    (clr_pkt_count),
        .pkt_count_o        (pkt_count),
        .m_o                (m_w),
        .m_ready_i          (m_ready)
    );
endmodule

`default_nettype wire
