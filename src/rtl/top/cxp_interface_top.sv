/*
================================================================================
  cxp_interface_top
  CoaXPress 1.1.1 (CXP-001-2015) — device-side interface integration top.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-27

    Description:
      Everything from the serial uplink to the parallel 32-bit downlink
      word, and the pixel path, as one block per clock and one crossing
      layer.  8B/10B line coding lives OUTSIDE this top: the integration
      feeds cxp_if_data_o / cxp_if_kmask_o (32 data + 4 K flags) into the
      transceiver's 8B/10B encoder.  The register file is outside too
      (cxp_device_top), on the reg_* port or behind the APB user window.

                       rx_clk                     tx_clk
        rx_serial -> cxp_rx_domain            cxp_tx_domain -> cxp_if_data_o
                     (uplink, control   ...   (packet sources, arbiter,
                      plane)            ...    inserter, stream framer)
                            \                   /
                             cxp_cdc_layer (every crossing,
                             the stream FIFO)
                            /
        s_pix_* ->   cxp_app_domain (app_clk: TPG / ingress, acquisition
                     gate, packer, image header / line markers)

      cxp_app_domain     pixel sources, acquisition gate (§11.2.1.4/5),
                         packer, stream words and packet boundaries
      cxp_tx_domain      stream framer (Table 19), control acknowledgment
                         (Table 20), connection test (Table 23), device
                         trigger (Table 16) and I/O acknowledgment (Table
                         17), long-packet arbiter, inserter + IDLE (§8.2.5)
      cxp_rx_domain      low-speed uplink receiver, host trigger (Table
                         15), control plane (command parser, executor,
                         APB3 user window)
      cxp_cdc_layer      configuration, events, ConnectionReset and its
                         echo, the control response, TestPacketCountTx,
                         and the stream FIFO

      Stream enable (Table 44): while cfg.stream_en is low
      (StreamPacketSizeMax is 0, or below the 36 bytes of the smallest
      stream packet) no image enters and no stream packet is sent.

      Stream flush: a ConnectionReset, a ConnectionConfig write and
      TestMode empty the stream path: the FIFO drops what it holds after
      the packet in flight, the app side drops until the next image, so
      nothing from before goes out afterwards.

      ConnectionReset (§10.3.28): the register file owns it and drives
      conn_reset_active (rx_clk) for as long as the reset takes.  It
      crosses to tx_clk once; there it restarts the PacketTags, flushes the
      stream, holds the device trigger at its de-asserted level and clears
      TestPacketCountTx.  Once the flush is done the tx side's copy is
      echoed back as conn_reset_done, and the register file clears the
      ConnectionReset bit only once it has seen the echo.

      Device trigger (§8.3.2, §8.3.3): sent while the uplink is up
      (sb_status.link_detected, crossed to tx_clk), paced by the host's I/O
      acknowledgment, which the uplink parser takes out of the word stream
      and which crosses rx -> tx as a pulse; p_TRIG_ACK_TIMEOUT bounds the
      wait.

      Clocking: the configuration (cfg), conn_reset_active, the clr_lt_*
      clears, the register bus and the APB port belong to rx_clk (the
      register file's clock); conn_reset_done and sb_status are returned
      on rx_clk; the sensor port and its two framing pulses are on app_clk.
        p_ASYNC_CLOCKS = 0  the three clocks are one clock (or
                            phase-locked); crossings are plain wires.
        p_ASYNC_CLOCKS = 1  every crossing goes through a cxp_cdc_*
                            primitive and the control-ack read-buffer port
                            runs on tx_clk.  Resets must be released
                            synchronously to their clock.

    Versions:
        2026-05-27 - 0.1:   - Restyle to coding-style template
        2026-09-19 - 0.2:   - TX sources as a cxp_txw_t port array with abort;
                              pixel / metadata selection on structs;
                              control plane beside cxp_rx_link;
                              p_ASYNC_CLOCKS crossing layer
        2026-09-22 - 0.3:   - Acquisition start / stop through cxp_app_acq_ctrl;
                              stream enable (Table 44)
        2026-09-22 - 0.4:   - reg_wstrb: byte enables on the register port;
                              cfg_streamid: TPG images carry Image1StreamID
                            - conn_cfg_wr: a ConnectionConfig write resets
                              the PacketTags
                            - cfg_xoffs / cfg_yoffs / cfg_srctag: TPG image
                              offsets and SourceTag preset from registers
        2026-09-25 - 0.5:   - ConnectionReset from the register file: one
                              level in, its echo out; cxp_link_reset_ctrl
                              and its clears removed; trigger re-armed on
                              the de-asserted pin
        2026-09-25 - 0.6:   - cxp_app_acq_ctrl gates both pixel sources; stream
                              flush on ConnectionReset, ConnectionConfig
                              and TestMode; image header PixelF from the
                              packer; sensor restart / stray EOF pulses
        2026-09-26 - 0.7:   - Control plane in two blocks: the APB bridge
                              inside it; sb_ctrl_nack_* replaces the
                              parser and router status outputs; the
                              read-buffer bank travels with the response
        2026-09-26 - 0.8:   - Transmit path: long-packet arbiter, then the
                              inserter (triggers, I/O acknowledgments,
                              IDLE); registered wire word
        2026-09-26 - 0.9:   - Device trigger gated by the host's I/O
                              acknowledgment (rx -> tx pulse) and by the
                              link; the pin is synchronised in
                              cxp_tx_trigger_hs, which owns the reset mask
        2026-09-27 - 0.10:  - p_RX_CLK_KHZ follows p_OS_RATIO; p_PIX_W and
                              the connection-test gap checked
        2026-09-27 - 0.11:  - Sensor metadata in as one cxp_meta_t (s_meta)
        2026-09-27 - 0.12:  - cfg_tapg / cfg_flags; the TPG metadata parameters
                              removed
        2026-09-27 - 0.13:  - One cxp_cfg_t in (cfg), one cxp_status_t out
                              (sb_status)
        2026-09-27 - 0.14:  - One block per clock (cxp_app_domain,
                              cxp_tx_domain, cxp_rx_domain) and the crossing
                              layer (cxp_cdc_layer); cxp_stream_top split
                              into cxp_app_stream and the FIFO / framer

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_interface_top #(
    // Test-pattern generator geometry (only used when cfg.use_tpg = 1).
    // p_TPG_X_SIZE / p_TPG_Y_SIZE are the compiled *maximum* line/frame
    // dimensions; the active size is the run-time cfg.xsize / cfg.ysize.
    // Every other header field of a TPG image comes from cfg.
    parameter int p_TPG_X_SIZE  = 64,                           // TPG max line width (px)
    parameter int p_TPG_Y_SIZE  = 32,                           // TPG max frame height (lines)

    // Stream pipeline
    parameter int p_FIFO_DEPTH = 1024,                          // Per-stream CDC FIFO depth (words)

    // Sensor single-pixel data width into cxp_app_pixel_ingress / the packer.
    parameter int p_PIX_W = 16,                                 // Sensor pixel width (Mono8..16)

    // Control plane.  Register accesses in [p_USER_BASE, +p_USER_SIZE)
    // go to the APB port, all others to the reg_* port (register file).
    // The default sends everything to APB.
    parameter int p_CTRL_BUF_DEPTH = 64,                        // Ctrl rd/wr buffer depth (dwords)
    parameter int p_APB_AW         = 32,                        // APB address width (32)
    parameter int p_APB_DW         = 32,                        // APB data width (32)
    parameter logic [31:0] p_USER_BASE = 32'h0000_0000,         // APB window base
    parameter logic [31:0] p_USER_SIZE = 32'hFFFF_FFFF,         // APB window bytes, 0 = none

    // RX sampler / link monitor (forwarded to cxp_rx_link)
    parameter int p_OS_RATIO       = 16,                        // LS uplink oversampling ratio
    parameter int p_RX_CLK_KHZ     = 20_833 * p_OS_RATIO,       // rx_clk (kHz), for ms timeouts
    parameter int p_SAMP_LOCK_HITS = cxp_pkg::RX_LOCK_HITS_DEFAULT, // K28.5 hits to declare lock
    parameter int p_RX_LOSS_WORDS = cxp_pkg::RX_LOSS_WORDS_DEFAULT, // Words w/o IDLE: link lost

    // Device trigger (§8.3.3): tx_clk cycles to wait for the host's I/O ack
    parameter int p_TRIG_ACK_TIMEOUT = cxp_pkg::TRIG_ACK_TIMEOUT,

    // Clocking
    parameter bit p_ASYNC_CLOCKS   = 1'b0                       // 1 = synchronise every crossing
) (
    // Clocks / resets
    input  wire  logic        app_clk,                          // Application/pixel clock
    input  wire  logic        app_rst_n,                        // app_clk async reset, active-low
    input  wire  logic        tx_clk,                           // TX word clock (downlink)
    input  wire  logic        tx_rst_n,                         // tx_clk async reset, active-low
    input  wire  logic        rx_clk,                           // RX clock (= os_clk inside rx_top)
    input  wire  logic        rx_rst_n,                         // rx_clk async reset, active-low

    // Configuration (quasi-static levels) and events, on rx_clk
    input  wire  cxp_pkg::cxp_cfg_t cfg,                        // device configuration
    input  wire  logic        acq_start,                        // AcquisitionStart (1-cycle)
    input  wire  logic        acq_stop,                         // AcquisitionStop (1-cycle)
    input  wire  logic        clr_lt_err,                       // Clear TestErrorCount
    input  wire  logic        clr_lt_pkt_tx,                    // Clear TestPacketCountTx
    input  wire  logic        clr_lt_pkt_rx,                    // Clear TestPacketCountRx

    // Local trigger I/O (device <-> host, §8.3.2)
    input  wire  logic        trig_in,                          // device trigger pin (any clock)
    output logic              trig_out,                         // Reconstructed RX trigger to app
    output logic              trig_out_glitch_pulse,            // Glitched/invalid trigger event

    // External sensor single-pixel input (cfg.use_tpg = 0)
    input  wire  logic [15:0] s_pix_data,                       // Pixel sample, LSB-justified
    input  wire  logic        s_pix_valid,                      // Pixel valid
    input  wire  logic        s_pix_sof,                        // Start-of-frame
    input  wire  logic        s_pix_eol,                        // End-of-line
    input  wire  logic        s_pix_eof,                        // End-of-frame
    output logic              s_pix_ready,                      // Ingress ready (backpressure)

    // External frame metadata (only used when cfg.use_tpg = 0), taken with
    // the image's first pixel
    input  wire  cxp_pkg::cxp_meta_t s_meta,                    // sensor frame metadata

    // Serial uplink (host -> device, soft-sampled 20.83 Mbps)
    input  wire  logic        rx_serial,                        // LS uplink serial bit

    // Parallel TX word out (device -> host, pre-line-coding)
    output logic [31:0]       cxp_if_data_o,                    // Word to 8B10B (P0=[7:0])
    output logic [3:0]        cxp_if_kmask_o,                   // Per-lane K-character flag

    // Register file port (register bus, one Table 22 code per access)
    output logic              reg_req,                          // access request
    output logic              reg_we,                           // write
    output logic [31:0]       reg_addr,                         // byte address
    output logic [31:0]       reg_wdata,                        // write data
    output logic [3:0]        reg_wstrb,                        // byte enables, [3] = [31:24]
    input  wire  logic        reg_ack,                          // access done
    input  wire  logic [31:0] reg_rdata,                        // read data
    input  wire  logic [7:0]  reg_err,                          // Table 22 code, 0 = OK

    // APB3 master + PSTRB for the user window (inside the control plane)
    output logic              apb_psel,                         // APB select
    output logic              apb_penable,                      // APB enable (access phase)
    output logic              apb_pwrite,                       // APB write (1) / read (0)
    output logic [p_APB_AW-1:0] apb_paddr,                      // APB byte address
    output logic [p_APB_DW-1:0] apb_pwdata,                     // APB write data
    output logic [p_APB_DW/8-1:0] apb_pstrb,                    // APB write byte enables
    input  wire  logic [p_APB_DW-1:0] apb_prdata,               // APB read data
    input  wire  logic        apb_pready,                       // APB slave ready
    input  wire  logic        apb_pslverr,                      // APB slave error

    // Status on rx_clk, and the sensor's framing errors on app_clk
    output cxp_pkg::cxp_status_t sb_status,                     // link, counters, error pulses
    output logic              sb_pix_restart_pulse,              // app_clk: sof inside an image
    output logic              sb_pix_stray_eof_pulse,            // app_clk: eof outside an image

    // ConnectionReset (§10.3.28) and ConnectionConfig, from the register file
    input  wire  logic        conn_reset_active,                 // ConnectionReset in progress
    output logic              conn_reset_done,                   // tx domain has applied it
    input  wire  logic        conn_cfg_wr                        // ConnectionConfig written
);

    import cxp_pkg::*;

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    if (p_APB_AW != 32 || p_APB_DW != 32) begin : g_chk_apb
        $error("cxp_interface_top: the APB port is 32-bit (p_APB_AW=%0d, p_APB_DW=%0d)",
               p_APB_AW, p_APB_DW);
    end

    //=======================================================================
    // Signals
    //=======================================================================

    // cxp_cdc_layer <-> cxp_app_domain (app_clk)
    cxp_cfg_app_t cfg_app;                  // pixel-path configuration
    logic        acq_start_app;             // AcquisitionStart
    logic        acq_stop_app;              // AcquisitionStop
    logic [31:0] fifo_s_data;               // stream words into the FIFO
    logic [3:0]  fifo_s_kmask;
    logic        fifo_s_valid;
    logic        fifo_s_sop;
    logic        fifo_s_eop;
    logic [7:0]  fifo_s_streamid;
    logic        fifo_s_ready;
    logic        fifo_s_flush;              // the FIFO's write side is flushing

    // cxp_cdc_layer <-> cxp_tx_domain (tx_clk)
    cxp_cfg_tx_t cfg_tx;                    // TestMode, polarity, stream on
    logic        crst_tx;                   // ConnectionReset in progress
    logic        crst_done_tx;              // ... applied, stream flushed
    logic        conn_cfg_wr_tx;            // ConnectionConfig written
    logic        clr_lt_tx;                 // host clear of TestPacketCountTx
    logic        trig_pkt_rcvd_tx;          // I/O-ack request
    logic        ioack_rcvd_tx;             // host acknowledged a device trigger
    logic        link_tx;                   // uplink detected
    logic          rsp_valid_tx;            // control response pending
    cxp_ctrl_rsp_t rsp_tx;
    logic        ack_busy;                  // control acknowledgment in flight
    logic [63:0] lt_pkt_count_tx;           // TestPacketCountTx (tx_clk)
    cxp_txw_t    fifo_m;                    // stream FIFO head word
    logic        fifo_m_pkt_avail;
    logic [15:0] fifo_m_len;
    logic [7:0]  fifo_m_streamid;
    logic        fifo_m_ready;
    logic        fifo_m_busy;
    logic        flush_req;                 // empty the stream path
    logic        flush_ack;                 // ... done

    // cxp_rx_domain <-> cxp_cdc_layer (rx_clk), and the read buffer
    logic        trig_pkt_rcvd_w;           // host trigger received
    logic        ioack_rcvd_w;              // host acknowledged a device trigger
    logic [63:0] lt_pkt_count_rx_dom;       // TestPacketCountTx, crossed
    logic          rsp_valid_w;             // control response held
    cxp_ctrl_rsp_t rsp_w;
    logic          rsp_ready_w;
    logic                              rbuf_clk;    // read-buffer read-port clock
    logic [$clog2(p_CTRL_BUF_DEPTH):0] rbuf_addr_w; // ... word, from the ack framer
    logic [31:0]                       rbuf_data_w; // ... its data

    //=======================================================================
    // Crossings and the stream FIFO
    //=======================================================================

    cxp_cdc_layer #(
        .p_FIFO_DEPTH   (p_FIFO_DEPTH),
        .p_ASYNC_CLOCKS (p_ASYNC_CLOCKS)
    ) cxp_cdc_layer_i (
        .app_clk             (app_clk),
        .app_rst_n           (app_rst_n),
        .tx_clk              (tx_clk),
        .tx_rst_n            (tx_rst_n),
        .rx_clk              (rx_clk),
        .rx_rst_n            (rx_rst_n),

        .cfg_i               (cfg),
        .acq_start_i         (acq_start),
        .acq_stop_i          (acq_stop),
        .crst_i              (conn_reset_active),
        .crst_done_o         (conn_reset_done),
        .conn_cfg_wr_i       (conn_cfg_wr),
        .clr_lt_pkt_tx_i     (clr_lt_pkt_tx),
        .trig_rcvd_i         (trig_pkt_rcvd_w),
        .ioack_rcvd_i        (ioack_rcvd_w),
        .link_i              (sb_status.link_detected),
        .rsp_valid_i         (rsp_valid_w),
        .rsp_i               (rsp_w),
        .rsp_ready_o         (rsp_ready_w),
        .lt_pkt_count_o      (lt_pkt_count_rx_dom),
        .rbuf_clk_o          (rbuf_clk),

        .app_cfg_o           (cfg_app),
        .app_acq_start_o     (acq_start_app),
        .app_acq_stop_o      (acq_stop_app),
        .app_data_i          (fifo_s_data),
        .app_kmask_i         (fifo_s_kmask),
        .app_valid_i         (fifo_s_valid),
        .app_sop_i           (fifo_s_sop),
        .app_eop_i           (fifo_s_eop),
        .app_streamid_i      (fifo_s_streamid),
        .app_ready_o         (fifo_s_ready),
        .app_flush_o         (fifo_s_flush),

        .tx_cfg_o            (cfg_tx),
        .tx_crst_o           (crst_tx),
        .tx_crst_done_i      (crst_done_tx),
        .tx_conn_cfg_wr_o    (conn_cfg_wr_tx),
        .tx_clr_lt_pkt_o     (clr_lt_tx),
        .tx_trig_rcvd_o      (trig_pkt_rcvd_tx),
        .tx_ioack_rcvd_o     (ioack_rcvd_tx),
        .tx_link_o           (link_tx),
        .tx_rsp_valid_o      (rsp_valid_tx),
        .tx_rsp_o            (rsp_tx),
        .tx_ack_busy_i       (ack_busy),
        .tx_lt_pkt_count_i   (lt_pkt_count_tx),
        .tx_fifo_o           (fifo_m),
        .tx_fifo_pkt_avail_o (fifo_m_pkt_avail),
        .tx_fifo_len_o       (fifo_m_len),
        .tx_fifo_streamid_o  (fifo_m_streamid),
        .tx_fifo_ready_i     (fifo_m_ready),
        .tx_fifo_busy_i      (fifo_m_busy),
        .tx_flush_i          (flush_req),
        .tx_flush_ack_o      (flush_ack)
    );

    //=======================================================================
    // Pixel path (app_clk)
    //=======================================================================

    cxp_app_domain #(
        .p_TPG_X_SIZE (p_TPG_X_SIZE),
        .p_TPG_Y_SIZE (p_TPG_Y_SIZE),
        .p_FIFO_DEPTH (p_FIFO_DEPTH),
        .p_PIX_W      (p_PIX_W)
    ) cxp_app_domain_i (
        .app_clk         (app_clk),
        .app_rst_n       (app_rst_n),
        .cfg_i           (cfg_app),
        .acq_start_i     (acq_start_app),
        .acq_stop_i      (acq_stop_app),

        .s_pix_data_i    (s_pix_data),
        .s_pix_valid_i   (s_pix_valid),
        .s_pix_sof_i     (s_pix_sof),
        .s_pix_eol_i     (s_pix_eol),
        .s_pix_eof_i     (s_pix_eof),
        .s_pix_ready_o   (s_pix_ready),
        .s_meta_i        (s_meta),
        .pix_restart_o   (sb_pix_restart_pulse),
        .pix_stray_eof_o (sb_pix_stray_eof_pulse),

        .m_data_o        (fifo_s_data),
        .m_kmask_o       (fifo_s_kmask),
        .m_valid_o       (fifo_s_valid),
        .m_sop_o         (fifo_s_sop),
        .m_eop_o         (fifo_s_eop),
        .m_streamid_o    (fifo_s_streamid),
        .m_ready_i       (fifo_s_ready),
        .flush_i         (fifo_s_flush)
    );

    //=======================================================================
    // Downlink (tx_clk)
    //=======================================================================

    cxp_tx_domain #(
        .p_CTRL_BUF_DEPTH   (p_CTRL_BUF_DEPTH),
        .p_TRIG_ACK_TIMEOUT (p_TRIG_ACK_TIMEOUT)
    ) cxp_tx_domain_i (
        .tx_clk           (tx_clk),
        .tx_rst_n         (tx_rst_n),
        .cfg_i            (cfg_tx),
        .crst_i           (crst_tx),
        .crst_done_o      (crst_done_tx),
        .conn_cfg_wr_i    (conn_cfg_wr_tx),
        .clr_lt_pkt_i     (clr_lt_tx),
        .lt_pkt_count_o   (lt_pkt_count_tx),
        .link_i           (link_tx),

        .trig_pin_i       (trig_in),
        .trig_rcvd_i      (trig_pkt_rcvd_tx),
        .ioack_rcvd_i     (ioack_rcvd_tx),

        .rsp_valid_i      (rsp_valid_tx),
        .rsp_i            (rsp_tx),
        .ack_busy_o       (ack_busy),
        .rbuf_addr_o      (rbuf_addr_w),
        .rbuf_data_i      (rbuf_data_w),

        .fifo_i           (fifo_m),
        .fifo_pkt_avail_i (fifo_m_pkt_avail),
        .fifo_len_i       (fifo_m_len),
        .fifo_streamid_i  (fifo_m_streamid),
        .fifo_ready_o     (fifo_m_ready),
        .fifo_busy_o      (fifo_m_busy),
        .flush_o          (flush_req),
        .flush_ack_i      (flush_ack),

        .m_data_o         (cxp_if_data_o),
        .m_kmask_o        (cxp_if_kmask_o)
    );

    //=======================================================================
    // Uplink and control plane (rx_clk)
    //=======================================================================

    cxp_rx_domain #(
        .p_OS_RATIO       (p_OS_RATIO),
        .p_SAMP_LOCK_HITS (p_SAMP_LOCK_HITS),
        .p_RX_LOSS_WORDS  (p_RX_LOSS_WORDS),
        .p_CTRL_BUF_DEPTH (p_CTRL_BUF_DEPTH),
        .p_USER_BASE      (p_USER_BASE),
        .p_USER_SIZE      (p_USER_SIZE),
        .p_RX_CLK_KHZ     (p_RX_CLK_KHZ)
    ) cxp_rx_domain_i (
        .rx_clk              (rx_clk),
        .rx_rst_n            (rx_rst_n),
        .rbuf_clk            (rbuf_clk),

        .cfg_trig_polarity_i (cfg.trig_polarity),
        .ext_link_i          (cfg.ext_link),
        .crst_i              (conn_reset_active),
        .clr_lt_err_i        (clr_lt_err),
        .clr_lt_pkt_i        (clr_lt_pkt_rx),

        .rx_serial_i         (rx_serial),
        .trig_o              (trig_out),
        .trig_glitch_o       (trig_out_glitch_pulse),
        .trig_rcvd_o         (trig_pkt_rcvd_w),
        .ioack_rcvd_o        (ioack_rcvd_w),

        .lt_pkt_count_tx_i   (lt_pkt_count_rx_dom),
        .status_o            (sb_status),

        .reg_req_o           (reg_req),
        .reg_we_o            (reg_we),
        .reg_addr_o          (reg_addr),
        .reg_wdata_o         (reg_wdata),
        .reg_wstrb_o         (reg_wstrb),
        .reg_ack_i           (reg_ack),
        .reg_rdata_i         (reg_rdata),
        .reg_err_i           (reg_err),

        .apb_psel_o          (apb_psel),
        .apb_penable_o       (apb_penable),
        .apb_pwrite_o        (apb_pwrite),
        .apb_paddr_o         (apb_paddr),
        .apb_pwdata_o        (apb_pwdata),
        .apb_pstrb_o         (apb_pstrb),
        .apb_prdata_i        (apb_prdata),
        .apb_pready_i        (apb_pready),
        .apb_pslverr_i       (apb_pslverr),

        .rbuf_addr_i         (rbuf_addr_w),
        .rbuf_data_o         (rbuf_data_w),
        .rsp_valid_o         (rsp_valid_w),
        .rsp_o               (rsp_w),
        .rsp_ready_i         (rsp_ready_w)
    );

endmodule

`default_nettype wire
