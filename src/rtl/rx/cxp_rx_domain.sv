/*
================================================================================
  cxp_rx_domain
  CoaXPress 1.1.1 (CXP-001-2015) — everything on rx_clk: the low-speed
  uplink receiver and the control plane.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-27

    Description:
      rx_clk (= the uplink's oversampling clock) runs the whole uplink and
      the control plane:

        rx_serial_i -> cxp_rx_link --> host trigger (trig_o), Table 15 /
                         |            Table 17 events for the tx side,
                         |            TestErrorCount / TestPacketCountRx
                         v
                   long packets -> cxp_ctrl_plane --> register bus (reg_*),
                                   (parser, executor)  user window (apb_*),
                                                      one response per
                                                      command (rsp_*)

      The configuration inputs come from the register file on this clock
      and are used here uncrossed: the trigger sense, the extension-link
      strap (an extension connection takes no host trigger and ignores
      ConnectionReset / MasterHostConnectionID writes) and the
      ConnectionReset level (the host's trigger is de-asserted while it
      lasts).

      Clocks: rx_clk only, except the read port of the control plane's
      two-bank read buffer: the acknowledgment framer on tx_clk reads it,
      so its clock (rbuf_clk) is tx_clk when the clocks are unrelated
      (cxp_cdc_layer picks it); the response that names the bank crosses
      with a request handshake.

      Status (status_o): the uplink's lock and link state, the test
      counters (TestPacketCountTx comes in from the tx side, already
      crossed) and the error pulses, as one cxp_status_t.

    Versions:
        2026-09-27 - 0.1:   - The rx_clk part of cxp_interface_top,
                              unchanged

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_rx_domain #(
    parameter int          p_OS_RATIO       = 16,               // uplink oversampling ratio
    parameter int          p_SAMP_LOCK_HITS = cxp_pkg::RX_LOCK_HITS_DEFAULT, // K28.5 hits: lock
    parameter int          p_RX_LOSS_WORDS  = cxp_pkg::RX_LOSS_WORDS_DEFAULT, // words: link lost
    parameter int          p_CTRL_BUF_DEPTH = 64,               // ctrl rd/wr buffer (dwords)
    parameter logic [31:0] p_USER_BASE      = 32'h0000_0000,    // user window base
    parameter logic [31:0] p_USER_SIZE      = 32'hFFFF_FFFF,    // user window bytes, 0 = none
    parameter int          p_RX_CLK_KHZ     = 20_833 * p_OS_RATIO // rx_clk (kHz), ms timeouts
) (
    input  wire  logic        rx_clk,                           // uplink oversampling clock
    input  wire  logic        rx_rst_n,                         // rx_clk async reset, active-low
    input  wire  logic        rbuf_clk,                         // read-buffer read-port clock

    // Configuration on rx_clk
    input  wire  logic        cfg_trig_polarity_i,              // trigger 0 high, 1 low active
    input  wire  logic        ext_link_i,                       // §5.1 strap: extension link
    input  wire  logic        crst_i,                           // ConnectionReset in progress
    input  wire  logic        clr_lt_err_i,                     // clear TestErrorCount
    input  wire  logic        clr_lt_pkt_i,                     // clear TestPacketCountRx

    // Uplink and the host's trigger
    input  wire  logic        rx_serial_i,                      // LS uplink bit
    output logic              trig_o,                           // host trigger, rebuilt
    output logic              trig_glitch_o,                    // trigger packet rejected
    output logic              trig_rcvd_o,                      // Table 15 packet taken (pulse)
    output logic              ioack_rcvd_o,                     // Table 17 ack of ours (pulse)

    // Status (TestPacketCountTx comes from the tx side)
    input  wire  logic [63:0] lt_pkt_count_tx_i,                // TestPacketCountTx, crossed
    output cxp_pkg::cxp_status_t status_o,                      // link status and counters

    // Register file port (one Table 22 code per access)
    output logic              reg_req_o,                        // access request
    output logic              reg_we_o,                         // write
    output logic [31:0]       reg_addr_o,                       // byte address
    output logic [31:0]       reg_wdata_o,                      // write data
    output logic [3:0]        reg_wstrb_o,                      // byte enables
    input  wire  logic        reg_ack_i,                        // access done
    input  wire  logic [31:0] reg_rdata_i,                      // read data
    input  wire  logic [7:0]  reg_err_i,                        // Table 22 code, 0 = OK

    // APB3 master (+ PSTRB) for the user window
    output logic              apb_psel_o,                       // select
    output logic              apb_penable_o,                    // access phase
    output logic              apb_pwrite_o,                     // 1 = write
    output logic [31:0]       apb_paddr_o,                      // byte address
    output logic [31:0]       apb_pwdata_o,                     // write data
    output logic [3:0]        apb_pstrb_o,                      // write byte enables
    input  wire  logic [31:0] apb_prdata_i,                     // read data
    input  wire  logic        apb_pready_i,                     // slave ready
    input  wire  logic        apb_pslverr_i,                    // slave error

    // Control response and the read buffer it points into
    input  wire  logic [$clog2(p_CTRL_BUF_DEPTH):0] rbuf_addr_i, // read-buffer word (rbuf_clk)
    output logic [31:0]       rbuf_data_o,                      // ... its data, 1 cycle later
    output logic              rsp_valid_o,                      // response held
    output cxp_pkg::cxp_ctrl_rsp_t rsp_o,                       // the response
    input  wire  logic        rsp_ready_i                       // handed to the framer
);

    import cxp_pkg::*;

    //=======================================================================
    // Signals
    //=======================================================================

    cxp_rxlong_t rx_long;                   // uplink long packets -> control plane

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign status_o.lt_pkt_count_tx = lt_pkt_count_tx_i;

    //=======================================================================
    // RX top (serial -> trigger / connection test / long packets)
    //=======================================================================

    cxp_rx_link #(
        .p_OS_RATIO       (p_OS_RATIO),
        .p_SAMP_LOCK_HITS (p_SAMP_LOCK_HITS),
        .p_RX_LOSS_WORDS  (p_RX_LOSS_WORDS)
    ) cxp_rx_link_i (
        .rx_clk                  (rx_clk),
        .rx_rst_n                (rx_rst_n),
        .rx_serial_i             (rx_serial_i),
        .rx_lock_o               (status_o.rx_lock),
        .aligned_o               (status_o.aligned),
        .link_detected_o         (status_o.link_detected),

        .cfg_trig_polarity_i     (cfg_trig_polarity_i),
        .trig_enable_i           (~ext_link_i),
        .trig_deassert_i         (crst_i),
        .trigger_out_app_o       (trig_o),
        .trigger_glitch_pulse_o  (trig_glitch_o),
        .trig_pkt_rcvd_o         (trig_rcvd_o),
        .ioack_rcvd_o            (ioack_rcvd_o),

        .clr_lt_err_i            (clr_lt_err_i),
        .clr_lt_pkt_i            (clr_lt_pkt_i),
        .lt_err_count_o          (status_o.lt_err_count),
        .lt_pkt_count_rx_o       (status_o.lt_pkt_count_rx),

        .long_o                  (rx_long),

        .pkt_err_pulse_o         (status_o.pkt_err_pulse),
        .rx_code_err_pulse_o     (status_o.code_err_pulse),
        .rx_disp_err_pulse_o     (status_o.disp_err_pulse)
    );

    //=======================================================================
    // Control plane (command parser -> executor, APB bridge inside)
    //=======================================================================

    cxp_ctrl_plane #(
        .p_BUF_DEPTH (p_CTRL_BUF_DEPTH),
        .p_USER_BASE (p_USER_BASE),
        .p_USER_SIZE (p_USER_SIZE),
        .p_CLK_KHZ   (p_RX_CLK_KHZ)
    ) cxp_ctrl_plane_i (
        .rx_clk                  (rx_clk),
        .rx_rst_n                (rx_rst_n),
        .long_i                  (rx_long),
        .from_extension_link_i   (ext_link_i),

        .reg_req_o               (reg_req_o),
        .reg_we_o                (reg_we_o),
        .reg_addr_o              (reg_addr_o),
        .reg_wdata_o             (reg_wdata_o),
        .reg_wstrb_o             (reg_wstrb_o),
        .reg_ack_i               (reg_ack_i),
        .reg_rdata_i             (reg_rdata_i),
        .reg_err_i               (reg_err_i),

        .apb_psel_o              (apb_psel_o),
        .apb_penable_o           (apb_penable_o),
        .apb_pwrite_o            (apb_pwrite_o),
        .apb_paddr_o             (apb_paddr_o),
        .apb_pwdata_o            (apb_pwdata_o),
        .apb_pstrb_o             (apb_pstrb_o),
        .apb_prdata_i            (apb_prdata_i),
        .apb_pready_i            (apb_pready_i),
        .apb_pslverr_i           (apb_pslverr_i),

        .rbuf_clk                (rbuf_clk),
        .rbuf_addr_i             (rbuf_addr_i),
        .rbuf_data_o             (rbuf_data_o),
        .rsp_valid_o             (rsp_valid_o),
        .rsp_o                   (rsp_o),
        .rsp_ready_i             (rsp_ready_i),

        .ctrl_reset_pulse_o      (status_o.ctrl_reset_pulse),
        .nack_pulse_o            (status_o.ctrl_nack_pulse),
        .nack_code_o             (status_o.ctrl_nack_code)
    );

endmodule

`default_nettype wire
