/*
================================================================================
  cxp_cdc_layer
  CoaXPress 1.1.1 (CXP-001-2015) — every crossing between the three clock
  domains of cxp_interface_top, and the stream FIFO.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-27

    Description:
      The only block of cxp_interface_top that runs on more than one
      clock.  The register file and the control plane are on rx_clk, the
      pixel path on app_clk, the downlink on tx_clk:

        rx -> app   configuration (cxp_cfg_app_t, bus), AcquisitionStart /
                    AcquisitionStop (pulses)
        rx -> tx    configuration (cxp_cfg_tx_t, bus), ConnectionReset
                    (level), ConnectionConfig written, TestPacketCountTx
                    clear, host trigger received, I/O acknowledgment of a
                    device trigger (pulses), uplink detected (level), the
                    control response (request / acknowledge, payload held
                    by the executor until the framer takes it)
        tx -> rx    ConnectionReset applied (level), TestPacketCountTx (bus)
        app -> tx   the stream words (cxp_cdc_stream_fifo: store-and-forward,
                    gray pointers; its flush request / acknowledgment)

      p_ASYNC_CLOCKS = 0: the three clocks are one clock (or phase-
      locked); every crossing but the stream FIFO is a plain wire.
      p_ASYNC_CLOCKS = 1: each goes through its cxp_cdc_* primitive, and
      the control plane's read buffer is read on tx_clk (rbuf_clk_o).
      Resets must be released synchronously to their clock
      (cxp_cdc_reset in cxp_device_top).

      The ConnectionReset echo returns to rx_clk as the tx side's
      "applied and flushed" level (tx_crst_done_i); the register file
      clears ConnectionReset only once it has seen it.

    Versions:
        2026-09-27 - 0.1:   - The crossings of cxp_interface_top and the
                              stream FIFO, unchanged

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_cdc_layer #(
    parameter int p_FIFO_DEPTH   = 1024,                        // stream FIFO depth (words)
    parameter bit p_ASYNC_CLOCKS = 1'b0                         // 1 = synchronise every crossing
) (
    input  wire  logic        app_clk,                          // application / pixel clock
    input  wire  logic        app_rst_n,                        // app_clk async reset, active-low
    input  wire  logic        tx_clk,                           // downlink word clock
    input  wire  logic        tx_rst_n,                         // tx_clk async reset, active-low
    input  wire  logic        rx_clk,                           // uplink / register-file clock
    input  wire  logic        rx_rst_n,                         // rx_clk async reset, active-low

    // rx_clk side
    input  wire  cxp_pkg::cxp_cfg_t cfg_i,                      // device configuration
    input  wire  logic        acq_start_i,                      // AcquisitionStart (pulse)
    input  wire  logic        acq_stop_i,                       // AcquisitionStop (pulse)
    input  wire  logic        crst_i,                           // ConnectionReset in progress
    output logic              crst_done_o,                      // ... applied on tx_clk
    input  wire  logic        conn_cfg_wr_i,                    // ConnectionConfig written
    input  wire  logic        clr_lt_pkt_tx_i,                  // clear TestPacketCountTx
    input  wire  logic        trig_rcvd_i,                      // host trigger received
    input  wire  logic        ioack_rcvd_i,                     // host acked a device trigger
    input  wire  logic        link_i,                           // uplink detected
    input  wire  logic        rsp_valid_i,                      // control response held
    input  wire  cxp_pkg::cxp_ctrl_rsp_t rsp_i,                 // ... the response
    output logic              rsp_ready_o,                      // ... handed over
    output logic [63:0]       lt_pkt_count_o,                   // TestPacketCountTx
    output logic              rbuf_clk_o,                       // read-buffer read-port clock

    // app_clk side
    output cxp_pkg::cxp_cfg_app_t app_cfg_o,                    // pixel-path configuration
    output logic              app_acq_start_o,                  // AcquisitionStart
    output logic              app_acq_stop_o,                   // AcquisitionStop
    input  wire  logic [31:0] app_data_i,                       // stream word
    input  wire  logic [3:0]  app_kmask_i,                      // per-byte K flag
    input  wire  logic        app_valid_i,                      // word valid
    input  wire  logic        app_sop_i,                        // first word of a packet
    input  wire  logic        app_eop_i,                        // last word of a packet
    input  wire  logic [7:0]  app_streamid_i,                   // StreamID of the packet
    output logic              app_ready_o,                      // the FIFO takes the word
    output logic              app_flush_o,                      // the FIFO is flushing

    // tx_clk side
    output cxp_pkg::cxp_cfg_tx_t tx_cfg_o,                      // TestMode, polarity, stream on
    output logic              tx_crst_o,                        // ConnectionReset in progress
    input  wire  logic        tx_crst_done_i,                   // ... applied, stream flushed
    output logic              tx_conn_cfg_wr_o,                 // ConnectionConfig written
    output logic              tx_clr_lt_pkt_o,                  // clear TestPacketCountTx
    output logic              tx_trig_rcvd_o,                   // host trigger received
    output logic              tx_ioack_rcvd_o,                  // host acked a device trigger
    output logic              tx_link_o,                        // uplink detected
    output logic              tx_rsp_valid_o,                   // control response pending
    output cxp_pkg::cxp_ctrl_rsp_t tx_rsp_o,                    // ... the response
    input  wire  logic        tx_ack_busy_i,                    // acknowledgment in flight
    input  wire  logic [63:0] tx_lt_pkt_count_i,                // TestPacketCountTx
    output cxp_pkg::cxp_txw_t tx_fifo_o,                        // stream FIFO head word
    output logic              tx_fifo_pkt_avail_o,              // a whole packet is in
    output logic [15:0]       tx_fifo_len_o,                    // head packet's data words
    output logic [7:0]        tx_fifo_streamid_o,               // head packet's StreamID
    input  wire  logic        tx_fifo_ready_i,                  // head word taken
    input  wire  logic        tx_fifo_busy_i,                   // a packet is being read
    input  wire  logic        tx_flush_i,                       // empty the stream path
    output logic              tx_flush_ack_o                    // ... done
);

    import cxp_pkg::*;

    //=======================================================================
    // Signals
    //=======================================================================

    cxp_cfg_app_t cfg_rx_app;               // rx_clk source of app_cfg_o
    cxp_cfg_tx_t  cfg_rx_tx;                // rx_clk source of tx_cfg_o

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign cfg_rx_app = '{use_tpg:    cfg_i.use_tpg,
                          run:        cfg_i.run,
                          acq_mode:   cfg_i.acq_mode,
                          acq_frames: cfg_i.acq_frames,
                          stream_en:  cfg_i.stream_en,
                          xsize:      cfg_i.xsize,
                          ysize:      cfg_i.ysize,
                          pixfmt:     cfg_i.pixfmt,
                          streamid:   cfg_i.streamid,
                          xoffs:      cfg_i.xoffs,
                          yoffs:      cfg_i.yoffs,
                          srctag:     cfg_i.srctag,
                          tapg:       cfg_i.tapg,
                          flags:      cfg_i.flags,
                          testpat:    cfg_i.testpat,
                          arbitrary:  cfg_i.arbitrary,
                          dsizeP:     cfg_i.dsizeP};

    assign cfg_rx_tx  = '{test_mode:     cfg_i.test_mode,
                          trig_polarity: cfg_i.trig_polarity,
                          stream_en:     cfg_i.stream_en};

    //=======================================================================
    // Level, event and request crossings
    //=======================================================================

    if (p_ASYNC_CLOCKS) begin : g_cdc

        // ---- rx -> app / tx: configuration levels ------------------------
        cxp_cdc_bus #(
            .p_W ($bits(cxp_cfg_app_t))
        ) cxp_cdc_bus_cfg_app_i (
            .src_clk (rx_clk),  .src_rst_n (rx_rst_n),  .d_i (cfg_rx_app),
            .dst_clk (app_clk), .dst_rst_n (app_rst_n), .q_o (app_cfg_o)
        );

        cxp_cdc_bus #(
            .p_W ($bits(cxp_cfg_tx_t))
        ) cxp_cdc_bus_cfg_tx_i (
            .src_clk (rx_clk), .src_rst_n (rx_rst_n), .d_i (cfg_rx_tx),
            .dst_clk (tx_clk), .dst_rst_n (tx_rst_n), .q_o (tx_cfg_o)
        );

        // ---- rx -> app: acquisition start / stop -------------------------
        cxp_cdc_pulse cxp_cdc_pulse_acq_start_i (
            .src_clk (rx_clk),  .src_rst_n (rx_rst_n),  .pulse_i (acq_start_i),
            .dst_clk (app_clk), .dst_rst_n (app_rst_n), .pulse_o (app_acq_start_o)
        );

        cxp_cdc_pulse cxp_cdc_pulse_acq_stop_i (
            .src_clk (rx_clk),  .src_rst_n (rx_rst_n),  .pulse_i (acq_stop_i),
            .dst_clk (app_clk), .dst_rst_n (app_rst_n), .pulse_o (app_acq_stop_o)
        );

        // ---- rx <-> tx: ConnectionReset level and its echo --------------
        cxp_cdc_sync #(
            .p_W (1)
        ) cxp_cdc_sync_crst_i (
            .clk   (tx_clk),
            .rst_n (tx_rst_n),
            .d_i   (crst_i),
            .q_o   (tx_crst_o)
        );

        cxp_cdc_sync #(
            .p_W (1)
        ) cxp_cdc_sync_crst_done_i (
            .clk   (rx_clk),
            .rst_n (rx_rst_n),
            .d_i   (tx_crst_done_i),
            .q_o   (crst_done_o)
        );

        // ---- rx -> tx: events --------------------------------------------
        cxp_cdc_pulse cxp_cdc_pulse_conn_cfg_wr_i (
            .src_clk (rx_clk), .src_rst_n (rx_rst_n), .pulse_i (conn_cfg_wr_i),
            .dst_clk (tx_clk), .dst_rst_n (tx_rst_n), .pulse_o (tx_conn_cfg_wr_o)
        );

        cxp_cdc_pulse cxp_cdc_pulse_clr_lt_i (
            .src_clk (rx_clk), .src_rst_n (rx_rst_n), .pulse_i (clr_lt_pkt_tx_i),
            .dst_clk (tx_clk), .dst_rst_n (tx_rst_n), .pulse_o (tx_clr_lt_pkt_o)
        );

        cxp_cdc_pulse cxp_cdc_pulse_trig_rcvd_i (
            .src_clk (rx_clk), .src_rst_n (rx_rst_n), .pulse_i (trig_rcvd_i),
            .dst_clk (tx_clk), .dst_rst_n (tx_rst_n), .pulse_o (tx_trig_rcvd_o)
        );

        cxp_cdc_pulse cxp_cdc_pulse_ioack_rcvd_i (
            .src_clk (rx_clk), .src_rst_n (rx_rst_n), .pulse_i (ioack_rcvd_i),
            .dst_clk (tx_clk), .dst_rst_n (tx_rst_n), .pulse_o (tx_ioack_rcvd_o)
        );

        // ---- rx -> tx: uplink detected -----------------------------------
        cxp_cdc_sync #(
            .p_W (1)
        ) cxp_cdc_sync_link_i (
            .clk   (tx_clk),
            .rst_n (tx_rst_n),
            .d_i   (link_i),
            .q_o   (tx_link_o)
        );

        // ---- rx -> tx: control response, held by the executor until the
        //      ack framer starts it; the read buffer is then read on tx_clk
        cxp_cdc_req #(
            .p_W ($bits(cxp_ctrl_rsp_t))
        ) cxp_cdc_req_rsp_i (
            .src_clk     (rx_clk),
            .src_rst_n   (rx_rst_n),
            .src_valid_i (rsp_valid_i),
            .src_data_i  (rsp_i),
            .src_ready_o (rsp_ready_o),
            .dst_clk     (tx_clk),
            .dst_rst_n   (tx_rst_n),
            .dst_valid_o (tx_rsp_valid_o),
            .dst_data_o  (tx_rsp_o),
            .dst_ready_i (~tx_ack_busy_i)
        );

        assign rbuf_clk_o = tx_clk;

        // ---- tx -> rx: the TX packet count ---------------------------------
        cxp_cdc_bus #(
            .p_W (64)
        ) cxp_cdc_bus_lt_pkt_tx_i (
            .src_clk (tx_clk), .src_rst_n (tx_rst_n), .d_i (tx_lt_pkt_count_i),
            .dst_clk (rx_clk), .dst_rst_n (rx_rst_n), .q_o (lt_pkt_count_o)
        );

    end else begin : g_tied

        assign app_cfg_o         = cfg_rx_app;
        assign tx_cfg_o          = cfg_rx_tx;
        assign app_acq_start_o   = acq_start_i;
        assign app_acq_stop_o    = acq_stop_i;
        assign tx_crst_o         = crst_i;
        assign crst_done_o       = tx_crst_done_i;
        assign tx_conn_cfg_wr_o  = conn_cfg_wr_i;
        assign tx_clr_lt_pkt_o   = clr_lt_pkt_tx_i;
        assign tx_trig_rcvd_o    = trig_rcvd_i;
        assign tx_ioack_rcvd_o   = ioack_rcvd_i;
        assign tx_link_o         = link_i;
        assign tx_rsp_valid_o    = rsp_valid_i;
        assign tx_rsp_o          = rsp_i;
        assign rsp_ready_o       = ~tx_ack_busy_i;
        assign rbuf_clk_o        = rx_clk;
        assign lt_pkt_count_o    = tx_lt_pkt_count_i;

    end

    //=======================================================================
    // Stream FIFO (app_clk producer -> tx_clk consumer)
    //=======================================================================

    cxp_cdc_stream_fifo #(
        .p_DEPTH (p_FIFO_DEPTH)
    ) cxp_cdc_stream_fifo_i (
        .app_clk       (app_clk),
        .app_rst_n     (app_rst_n),
        .tx_clk        (tx_clk),
        .tx_rst_n      (tx_rst_n),

        .s_data_i      (app_data_i),
        .s_kmask_i     (app_kmask_i),
        .s_valid_i     (app_valid_i),
        .s_sop_i       (app_sop_i),
        .s_eop_i       (app_eop_i),
        .s_streamid_i  (app_streamid_i),
        .s_ready_o     (app_ready_o),

        .m_data_o      (tx_fifo_o.data),
        .m_kmask_o     (tx_fifo_o.kmask),
        .m_valid_o     (tx_fifo_o.valid),
        .m_sop_o       (tx_fifo_o.sop),
        .m_eop_o       (tx_fifo_o.eop),
        .m_ready_i     (tx_fifo_ready_i),
        .m_pkt_avail_o (tx_fifo_pkt_avail_o),
        .m_len_o       (tx_fifo_len_o),
        .m_streamid_o  (tx_fifo_streamid_o),
        .m_busy_i      (tx_fifo_busy_i),
        .flush_i       (tx_flush_i),
        .flush_ack_o   (tx_flush_ack_o),
        .s_flush_o     (app_flush_o)
    );

endmodule

`default_nettype wire
