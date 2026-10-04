//-----------------------------------------------------------------------------
// tb_cxp_tx_arbiter_top
//
// Cocotb wrapper for the transmit scheduler as cxp_interface_top builds it:
// cxp_tx_arbiter over the three long-packet sources (ack, linktest,
// stream, in cxp_pkg::TX_PORT_* order), then cxp_tx_inserter with the
// trigger and I/O-ack sources.  The flat pre-restyle names (`p_<src>_*`,
// `m_*`, no `_i`/`_o` suffix) are kept so the test drives every source
// bundle directly.
//
// `m_data` / `m_kmask` / `idle_seen` are the word the inserter chooses in
// the current cycle (the one whose source sees ready); `wire_data` /
// `wire_kmask` are the registered wire word, one cycle later.  Top-level
// `TESTCASE` register is written by Python so the current test number is
// visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_tx_arbiter_top (
    input  wire  logic        tx_clk,
    input  wire  logic        tx_rst_n,

    input  wire  logic [31:0] p_trig_data,
    input  wire  logic [3:0]  p_trig_kmask,
    input  wire  logic        p_trig_valid,
    input  wire  logic        p_trig_sop,
    input  wire  logic        p_trig_eop,
    output logic              p_trig_ready,

    input  wire  logic [31:0] p_ioack_data,
    input  wire  logic [3:0]  p_ioack_kmask,
    input  wire  logic        p_ioack_valid,
    input  wire  logic        p_ioack_sop,
    input  wire  logic        p_ioack_eop,
    output logic              p_ioack_ready,

    input  wire  logic [31:0] p_ack_data,
    input  wire  logic [3:0]  p_ack_kmask,
    input  wire  logic        p_ack_valid,
    input  wire  logic        p_ack_sop,
    input  wire  logic        p_ack_eop,
    output logic              p_ack_ready,

    input  wire  logic [31:0] p_linktest_data,
    input  wire  logic [3:0]  p_linktest_kmask,
    input  wire  logic        p_linktest_valid,
    input  wire  logic        p_linktest_sop,
    input  wire  logic        p_linktest_eop,
    output logic              p_linktest_ready,

    input  wire  logic [31:0] p_stream_data,
    input  wire  logic [3:0]  p_stream_kmask,
    input  wire  logic        p_stream_valid,
    input  wire  logic        p_stream_sop,
    input  wire  logic        p_stream_eop,
    output logic              p_stream_ready,

    output logic              idle_seen,
    output logic [31:0]       m_data,
    output logic [3:0]        m_kmask,
    output logic [31:0]       wire_data,
    output logic [3:0]        wire_kmask
);
    import cxp_pkg::*;

    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    cxp_txw_t              src [TX_PORTS];
    logic [TX_PORTS-1:0]   ready;
    cxp_txw_t              long_w, trig_w, ioack_w;
    logic                  long_ready;

    assign trig_w              = '{p_trig_data,     p_trig_kmask,     p_trig_valid,     p_trig_sop,     p_trig_eop};
    assign ioack_w             = '{p_ioack_data,    p_ioack_kmask,    p_ioack_valid,    p_ioack_sop,    p_ioack_eop};
    assign src[TX_PORT_ACK]    = '{p_ack_data,      p_ack_kmask,      p_ack_valid,      p_ack_sop,      p_ack_eop};
    assign src[TX_PORT_LT]     = '{p_linktest_data, p_linktest_kmask, p_linktest_valid, p_linktest_sop, p_linktest_eop};
    assign src[TX_PORT_STREAM] = '{p_stream_data,   p_stream_kmask,   p_stream_valid,   p_stream_sop,   p_stream_eop};

    assign p_ack_ready      = ready[TX_PORT_ACK];
    assign p_linktest_ready = ready[TX_PORT_LT];
    assign p_stream_ready   = ready[TX_PORT_STREAM];

    cxp_tx_arbiter cxp_tx_arbiter_i (
        .tx_clk    (tx_clk),
        .tx_rst_n  (tx_rst_n),
        .src_i     (src),
        .ready_o   (ready),
        .m_o       (long_w),
        .m_ready_i (long_ready)
    );

    cxp_tx_inserter cxp_tx_inserter_i (
        .tx_clk        (tx_clk),
        .tx_rst_n      (tx_rst_n),
        .trig_i        (trig_w),
        .trig_ready_o  (p_trig_ready),
        .ioack_i       (ioack_w),
        .ioack_ready_o (p_ioack_ready),
        .long_i        (long_w),
        .long_ready_o  (long_ready),
        .m_data_o      (wire_data),
        .m_kmask_o     (wire_kmask)
    );

    assign m_data    = cxp_tx_inserter_i.word_data;
    assign m_kmask   = cxp_tx_inserter_i.word_kmask;
    assign idle_seen = cxp_tx_inserter_i.word_idle;
endmodule

`default_nettype wire
