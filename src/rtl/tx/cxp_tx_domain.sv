/*
================================================================================
  cxp_tx_domain
  CoaXPress 1.1.1 (CXP-001-2015) — everything on tx_clk: the packet
  sources, the transmit scheduler and the stream flush request.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-27

    Description:
      One clock, tx_clk.  Every input that comes from another domain has
      already been crossed by cxp_cdc_layer.

        stream FIFO read side -> cxp_tx_stream_pkt  --,
        control response      -> cxp_tx_ctrl_ack    --+-> cxp_tx_arbiter
        TestMode              -> cxp_tx_linktest    --'   (long packets)
                                                              |
        trigger pin           -> cxp_tx_trigger_hs  --> cxp_tx_inserter
        host trigger received -> cxp_tx_io_ack      -->   (+ IDLE)
                                                              |
                                                     m_data_o / m_kmask_o
                                                     (registered wire word)

      Long packets (Table 13 priority 2) go through a non-pre-emptive
      arbiter, control acknowledgment > connection test > stream; the
      inserter puts the two-word trigger and I/O-acknowledgment packets
      and the IDLE words (§8.2.5) between or inside them.

      ConnectionReset (crst_i, for as long as it lasts): restarts the
      PacketTags, holds the device trigger at its de-asserted level, clears
      TestPacketCountTx and flushes the stream.  crst_done_o echoes it once
      the flush is done.

      Stream flush request (flush_o): ConnectionReset and TestMode for as
      long as they last, a ConnectionConfig write until the FIFO
      acknowledges it (or 65535 cycles — far beyond the longest packet in
      flight — if the pixel clock is not running).  A ConnectionConfig
      write (and ConnectionReset) restarts the PacketTags (§8.5.3).

    Versions:
        2026-09-27 - 0.1:   - The tx_clk part of cxp_interface_top, unchanged

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_tx_domain #(
    parameter int p_CTRL_BUF_DEPTH   = 64,                      // ctrl read buffer (dwords)
    parameter int p_TRIG_ACK_TIMEOUT = cxp_pkg::TRIG_ACK_TIMEOUT // cycles to wait for an I/O ack
) (
    input  wire  logic        tx_clk,                           // TX word clock
    input  wire  logic        tx_rst_n,                         // tx_clk async reset, active-low

    // Configuration and events, on tx_clk
    input  wire  cxp_pkg::cxp_cfg_tx_t cfg_i,                   // TestMode, polarity, stream on
    input  wire  logic        crst_i,                           // ConnectionReset in progress
    output logic              crst_done_o,                      // ... applied, stream flushed
    input  wire  logic        conn_cfg_wr_i,                    // ConnectionConfig written
    input  wire  logic        clr_lt_pkt_i,                     // host clear of TestPacketCountTx
    output logic [63:0]       lt_pkt_count_o,                   // TestPacketCountTx
    input  wire  logic        link_i,                           // uplink detected

    // Triggers
    input  wire  logic        trig_pin_i,                       // device trigger pin (any clock)
    input  wire  logic        trig_rcvd_i,                      // host trigger received (pulse)
    input  wire  logic        ioack_rcvd_i,                     // host acked a device trigger

    // Control response and the read buffer it points into
    input  wire  logic        rsp_valid_i,                      // response pending
    input  wire  cxp_pkg::cxp_ctrl_rsp_t rsp_i,                 // the response
    output logic              ack_busy_o,                       // acknowledgment in flight
    output logic [$clog2(p_CTRL_BUF_DEPTH):0] rbuf_addr_o,      // read-buffer word
    input  wire  logic [31:0] rbuf_data_i,                      // ... its data, 1 cycle later

    // Stream FIFO read side
    input  wire  cxp_pkg::cxp_txw_t fifo_i,                     // head word
    input  wire  logic        fifo_pkt_avail_i,                 // a whole packet is in
    input  wire  logic [15:0] fifo_len_i,                       // head packet's data words
    input  wire  logic [7:0]  fifo_streamid_i,                  // head packet's StreamID
    output logic              fifo_ready_o,                     // head word taken
    output logic              fifo_busy_o,                      // a packet is being read
    output logic              flush_o,                          // empty the stream path
    input  wire  logic        flush_ack_i,                      // ... done

    // Wire word to the 8B/10B encoder
    output logic [31:0]       m_data_o,                         // word, P0 in [7:0]
    output logic [3:0]        m_kmask_o                         // per-byte K flag
);

    import cxp_pkg::*;

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    // §8.7.4: at least 16 word intervals between connection-test packets.
    if (LT_GAP_WORDS < 16) begin : g_chk_lt_gap
        $error("cxp_tx_domain: LT_GAP_WORDS (=%0d) below the 16 of §8.7.4", LT_GAP_WORDS);
    end

    //=======================================================================
    // Signals
    //=======================================================================

    // Long-packet sources -> arbiter (index = cxp_pkg::TX_PORT_*) ->
    // inserter <- trigger, I/O acknowledgment
    cxp_txw_t                    tx_src [TX_PORTS];
    logic [TX_PORTS-1:0]         tx_ready;
    cxp_txw_t                    tx_long;
    logic                        tx_long_ready;
    cxp_txw_t                    tx_trig;
    logic                        tx_trig_ready;
    cxp_txw_t                    tx_ioack;
    logic                        tx_ioack_ready;

    logic        linktest_suppress_traffic;     // TestMode: no new stream packet
    logic        cfgwr_hold_q;                  // a ConnectionConfig write's flush pending
    logic [15:0] cfgwr_tmo_q;                   // ... cycles left before it is given up

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign flush_o     = crst_i | cfgwr_hold_q | cfg_i.test_mode;
    assign crst_done_o = crst_i & flush_ack_i;

    //=======================================================================
    // A ConnectionConfig write's flush, held until acknowledged
    //=======================================================================

    always_ff @(posedge tx_clk or negedge tx_rst_n) begin
        if (!tx_rst_n) begin
            cfgwr_hold_q <= 1'b0;
            cfgwr_tmo_q  <= 16'd0;
        end else if (conn_cfg_wr_i) begin
            cfgwr_hold_q <= 1'b1;
            cfgwr_tmo_q  <= 16'hFFFF;
        end else if (cfgwr_hold_q) begin
            cfgwr_tmo_q <= cfgwr_tmo_q - 16'd1;
            if (flush_ack_i || cfgwr_tmo_q == 16'd0) cfgwr_hold_q <= 1'b0;
        end
    end

    //=======================================================================
    // Stream packet framer (type 0x01, Table 19)
    //=======================================================================

    cxp_tx_stream_pkt cxp_tx_stream_pkt_i (
        .tx_clk              (tx_clk),
        .tx_rst_n            (tx_rst_n),
        // §8.5.3: ConnectionReset and every ConnectionConfig write (same
        // value too) restart the PacketTags.
        .stream_ctrl_reset_i (crst_i | conn_cfg_wr_i),
        .stream_en_i         (cfg_i.stream_en),
        .suppress_stream_i   (linktest_suppress_traffic),

        .s_data_i            (fifo_i.data),
        .s_kmask_i           (fifo_i.kmask),
        .s_valid_i           (fifo_i.valid),
        .s_sop_i             (fifo_i.sop),
        .s_eop_i             (fifo_i.eop),
        .s_streamid_i        (fifo_streamid_i),
        .s_len_i             (fifo_len_i),
        .s_pkt_avail_i       (fifo_pkt_avail_i),
        .s_ready_o           (fifo_ready_o),

        .m_o                 (tx_src[TX_PORT_STREAM]),
        .m_ready_i           (tx_ready[TX_PORT_STREAM]),
        .busy_o              (fifo_busy_o)
    );

    //=======================================================================
    // Control acknowledge TX (type 0x03)
    //=======================================================================

    cxp_tx_ctrl_ack #(
        .p_BUF_DEPTH (p_CTRL_BUF_DEPTH)
    ) cxp_tx_ctrl_ack_i (
        .tx_clk             (tx_clk),
        .tx_rst_n           (tx_rst_n),
        .ack_req_i          (rsp_valid_i),
        .ack_code_i         (rsp_i.code),
        .ack_size_i         (rsp_i.size),
        .ack_wait_ms_i      (rsp_i.wait_ms),
        .ack_rbank_i        (rsp_i.rbank),
        .ack_busy_o         (ack_busy_o),
        .rbuf_addr_o        (rbuf_addr_o),
        .rbuf_data_i        (rbuf_data_i),
        .m_o                (tx_src[TX_PORT_ACK]),
        .m_ready_i          (tx_ready[TX_PORT_ACK])
    );

    //=======================================================================
    // Link-test packet generator (type 0x04, §8.7 / Table 23)
    //=======================================================================

    // NOTE: cxp_tx_linktest deliberately keeps DATA_WORDS/GAP_WORDS
    // *without* p_ prefix (Python TB introspects them).
    cxp_tx_linktest cxp_tx_linktest_i (
        .tx_clk             (tx_clk),
        .tx_rst_n           (tx_rst_n),
        .cfg_test_mode_i    (cfg_i.test_mode),
        .suppress_traffic_o (linktest_suppress_traffic),
        .clr_pkt_count_i    (clr_lt_pkt_i | crst_i),
        .pkt_count_o        (lt_pkt_count_o),
        .m_o                (tx_src[TX_PORT_LT]),
        .m_ready_i          (tx_ready[TX_PORT_LT])
    );

    //=======================================================================
    // HS trigger TX (§8.3.2 / Table 16)
    //=======================================================================

    cxp_tx_trigger_hs #(
        .p_ACK_TIMEOUT (p_TRIG_ACK_TIMEOUT)
    ) cxp_tx_trigger_hs_i (
        .tx_clk         (tx_clk),
        .tx_rst_n       (tx_rst_n),
        .trigger_in_i   (trig_pin_i),
        .cfg_polarity_i (cfg_i.trig_polarity),
        .link_up_i      (link_i),
        .mask_i         (crst_i),
        .ack_i          (ioack_rcvd_i),
        .m_o            (tx_trig),
        .m_ready_i      (tx_trig_ready)
    );

    //=======================================================================
    // I/O-acknowledgment TX (§8.3.3 / Table 17)
    //=======================================================================

    cxp_tx_io_ack cxp_tx_io_ack_i (
        .tx_clk      (tx_clk),
        .tx_rst_n    (tx_rst_n),
        .trig_rcvd_i (trig_rcvd_i),
        .m_o         (tx_ioack),
        .m_ready_i   (tx_ioack_ready)
    );

    //=======================================================================
    // Long-packet arbiter (control acknowledgment > test > stream)
    //=======================================================================

    cxp_tx_arbiter cxp_tx_arbiter_i (
        .tx_clk    (tx_clk),
        .tx_rst_n  (tx_rst_n),
        .src_i     (tx_src),
        .ready_o   (tx_ready),
        .m_o       (tx_long),
        .m_ready_i (tx_long_ready)
    );

    //=======================================================================
    // Inserter: trigger > I/O acknowledgment > IDLE due > long word,
    // registered to the 8B/10B encoder
    //=======================================================================

    cxp_tx_inserter cxp_tx_inserter_i (
        .tx_clk        (tx_clk),
        .tx_rst_n      (tx_rst_n),
        .trig_i        (tx_trig),
        .trig_ready_o  (tx_trig_ready),
        .ioack_i       (tx_ioack),
        .ioack_ready_o (tx_ioack_ready),
        .long_i        (tx_long),
        .long_ready_o  (tx_long_ready),
        .m_data_o      (m_data_o),
        .m_kmask_o     (m_kmask_o)
    );

endmodule

`default_nettype wire
