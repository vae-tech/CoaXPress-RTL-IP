//-----------------------------------------------------------------------------
// tb_cxp_tx_io_ack_top
//
// Cocotb wrapper around `cxp_tx_io_ack`.  The DUT is an all-scalar leaf
// module already; the wrapper exists so the `@cxp_test` decorator can write
// the test index into a top-level `TESTCASE` register and have it appear in
// the waveform.  The DUT instance name `cxp_tx_io_ack_i` is referenced by
// the FSM-coverage `state_path` in the Python TB.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_tx_io_ack_top (
    input  wire  logic        tx_clk,
    input  wire  logic        tx_rst_n,

    input  wire  logic        trig_rcvd,

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

    cxp_tx_io_ack cxp_tx_io_ack_i (
        .tx_clk      (tx_clk),
        .tx_rst_n    (tx_rst_n),
        .trig_rcvd_i (trig_rcvd),
        .m_o         (m_w),
        .m_ready_i   (m_ready)
    );
endmodule

`default_nettype wire
