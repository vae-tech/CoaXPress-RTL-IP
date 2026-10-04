/*
================================================================================
  cxp_rx_link
  CoaXPress 1.1.1 (CXP-001-2015) — device-side RX framing top.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-27

    Description:
      From the low-speed serial uplink (~20.83 Mbps soft-sampled in
      fabric) to demuxed trigger / connection-test channels and the
      long-packet stream that cxp_ctrl_plane executes.

      Block diagram:
        rx_serial_i (single-bit serial)
           |
           v  cxp_rx_lspd_sampler          oversample, edge-CDR, K28.5
           |                               comma-align, Table 15 trigger
           |                               taken out -> 4x10b parallel,
           |                               K28.5 in lane P0
           v  cxp_rx_8b10b_decoder x4      RD chain, registered output
           v  cxp_rx_link_mon              IDLE monitor: link up / lost
           v  cxp_rx_packet_parser         demux: IDLE / I/O ack / long
           |
           +--> cxp_rx_linktest            type 0x04  -> err_count
           +--> long_o                     every long packet -> cxp_ctrl_plane

      Single clock domain: everything runs on `rx_clk` (= os_clk).

      8B/10B running disparity: the 4 lanes share a single RD register.
      rd_chain[0] is the RD entering lane P0, the decoder rd_out
      signals chain through P1..P3, and rd_chain[4] is registered back
      to rd_chain[0] for the next word.  The host's disparity is unknown
      at lock, so the first word after lock (which always starts with
      the K28.5 the sampler locked on) seeds the chain from the K28.5
      form: 001111 1010 is sent at RD-, 110000 0101 at RD+.

      A Table 15 trigger (six characters at any character boundary) is
      taken out by the sampler, which reports whether they changed the
      running disparity on the lane of the next character (sym_rd_flip);
      that lane's decoder starts from the chain's RD inverted.  The
      packet goes to cxp_rx_trigger_lspd (Delay vote, Figure 20 wait) only
      while the link is detected (§10.1.1): before that the framing is
      not confirmed.  Every packet it accepts strobes trig_pkt_rcvd_o for
      the §8.3.3 I/O acknowledgment.

      A word with a code or disparity error on any lane is marked (err)
      and travels to the parser with that mark; the consumers treat it
      as a bad word (control command: 0x80; test packet: one error).

    Versions:
        2026-05-27 - 0.1:   - Restyle to coding-style template
        2026-09-19 - 0.2:   - Command executor moved to cxp_ctrl_plane;
                              decoder lanes as a generate loop
        2026-09-19 - 0.3:   - cxp_rx_link_mon replaces the lane aligner and
                              owns link detection and loss of lock
        2026-09-19 - 0.4:   - RD seeded from the first K28.5; decode errors
                              marked on the word
        2026-09-22 - 0.5:   - Table 15 trigger from the sampler
        2026-09-26 - 0.6:   - ioack_rcvd_o: the host's Table 17 I/O
                              acknowledgment, decoded at the word seam
        2026-09-26 - 0.7:   - Table 15 only, gated by link_detected;
                              acknowledged when accepted; RD flip lanes;
                              p_RX_BAD_WORDS
        2026-09-27 - 0.8:   - trig_enable_i (Master connection only),
                              trig_deassert_i (ConnectionReset)
        2026-09-27 - 0.9:   - p_LINK_LOCK_IDLES: the link monitor's count
                              of IDLE words, apart from the sampler's
                              K28.5 hits

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_rx_link #(
    parameter int p_OS_RATIO         = 16,
    parameter int p_SAMP_LOCK_HITS   = cxp_pkg::RX_LOCK_HITS_DEFAULT,  // K28.5 hits: sampler lock
    parameter int p_LINK_LOCK_IDLES  = cxp_pkg::RX_LOCK_IDLES_DEFAULT, // clean IDLEs: link up
    parameter int p_RX_LOSS_WORDS    = cxp_pkg::RX_LOSS_WORDS_DEFAULT, // words w/o IDLE: link lost
    parameter int p_RX_BAD_WORDS     = cxp_pkg::RX_BAD_WORDS_DEFAULT,  // misfit words: re-hunt
    parameter bit p_RX_SHORT_LOSS_OK = 1'b0                            // benches only
) (
    // Receive clock domain
    input  wire  logic                       rx_clk,                  // RX/oversample clock
    input  wire  logic                       rx_rst_n,                // Active-low sync reset

    // Serial uplink (from analog slicer)
    input  wire  logic                       rx_serial_i,             // 20.83 Mbps serial

    // Link status
    output logic                             rx_lock_o,               // Sampler symbol lock
    output logic                             aligned_o,               // IDLE monitor: link up
    output logic                             link_detected_o,         // §8.2 Detected

    // Trigger
    input  wire  logic                       cfg_trig_polarity_i,     // 0=rising, 1=falling
    input  wire  logic                       trig_enable_i,           // 0 = extension connection
    input  wire  logic                       trig_deassert_i,         // ConnectionReset in progress
    output logic                             trigger_out_app_o,       // Reconstructed pulse
    output logic                             trigger_glitch_pulse_o,  // Packet rejected
    // §8.3.3: one-cycle strobe for every trigger packet accepted, drives
    // the cxp_tx_io_ack K28.6 ack generator.
    output logic                             trig_pkt_rcvd_o,         // I/O-ack source
    // §8.3.3: one-cycle strobe for every Table 17 I/O acknowledgment the
    // host sends for a device trigger (code 0x01).
    output logic                             ioack_rcvd_o,            // device trigger acked

    // Linktest (connection test, §8.7)
    input  wire  logic                       clr_lt_err_i,            // Clear TestErrorCount
    input  wire  logic                       clr_lt_pkt_i,            // Clear TestPacketCountRx
    output logic [31:0]                      lt_err_count_o,          // §10.3.37
    output logic [63:0]                      lt_pkt_count_rx_o,       // §10.3.39

    // Long packets (to cxp_ctrl_plane)
    output cxp_pkg::cxp_rxlong_t             long_o,                  // packet words + type

    // Side band
    output logic                             pkt_err_pulse_o,         // Parser framing error
    output logic                             rx_code_err_pulse_o,     // 8b10b code error
    output logic                             rx_disp_err_pulse_o      // 8b10b disparity error
);

    import cxp_pkg::*;

    //=======================================================================
    // Signals
    //=======================================================================

    // 0. Sampler output
    logic [39:0] sym_in;
    logic        sym_valid;
    logic [3:0]  sym_rd_flip;       // lane's entering RD inverted (after a trigger)
    logic        rd_lane [4];       // RD entering each lane's decoder

    // 1. Table 15 trigger from the sampler
    logic        t15_valid;
    logic [1:0]  t15_edge;
    logic [29:0] t15_dly;

    logic        resync;            // link monitor -> sampler

    // 2. Decoder outputs
    logic [7:0]  d_dec [4];
    logic        k_dec [4];
    logic        disp_err_w [4];
    logic        code_err_w [4];
    logic        rd_chain [5];
    logic        rd_q;
    logic        rd_known_q;        // RD seeded since lock
    logic        word_err;          // code or disparity error on any lane

    // 3. Decoder output register
    logic [31:0] d_reg_q;
    logic [3:0]  k_reg_q;
    logic [3:0]  disp_err_q;
    logic [3:0]  code_err_q;
    logic        d_valid_q;
    logic        accept_sym;

    // 4. Link monitor outputs
    logic [31:0] mon_data;
    logic [3:0]  mon_kmask;
    logic        mon_err;
    logic        mon_valid;
    logic        mon_flush;         // link left UP: parser ends its packet

    // 5. Packet parser outputs
    logic [31:0] long_data_w;
    logic [3:0]  long_kmask_w;
    logic        long_valid_w;
    logic        long_sop_w;
    logic        long_eop_w;
    logic        long_err_w;
    logic [7:0]  long_type_w;

    // 6. Linktest gate
    logic        lt_gate;

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    // Until seeded, the RD entering P0 is taken from the K28.5 form in P0.
    assign rd_chain[0]      = rd_known_q ? rd_q : (sym_in[9:0] == K28_5_POS);
    assign word_err         = (|code_err_q) | (|disp_err_q);
    assign accept_sym       = sym_valid & rx_lock_o;
    assign rx_code_err_pulse_o = d_valid_q & (|code_err_q);
    assign rx_disp_err_pulse_o = d_valid_q & (|disp_err_q);

    assign lt_gate = long_valid_w & (long_type_w == PKT_TYPE_LT);

    assign long_o = '{data:  long_data_w,
                      kmask: long_kmask_w,
                      valid: long_valid_w,
                      sop:   long_sop_w,
                      eop:   long_eop_w,
                      err:   long_err_w,
                      ptype: long_type_w};

    //=======================================================================
    // Decoder output register and RD chain
    //=======================================================================

    always_ff @(posedge rx_clk or negedge rx_rst_n) begin
        if (!rx_rst_n) begin
            rd_q       <= 1'b0;
            rd_known_q <= 1'b0;
            d_reg_q    <= 32'h0;
            k_reg_q    <= 4'h0;
            disp_err_q <= 4'h0;
            code_err_q <= 4'h0;
            d_valid_q  <= 1'b0;
        end else begin
            d_valid_q <= accept_sym;
            if (!rx_lock_o) rd_known_q <= 1'b0;
            if (accept_sym) begin
                rd_q       <= rd_chain[4];
                if (is_k28_5(sym_in[9:0])) rd_known_q <= 1'b1;
                d_reg_q    <= {d_dec[3],     d_dec[2],     d_dec[1],     d_dec[0]};
                k_reg_q    <= {k_dec[3],     k_dec[2],     k_dec[1],     k_dec[0]};
                disp_err_q <= {disp_err_w[3], disp_err_w[2], disp_err_w[1], disp_err_w[0]};
                code_err_q <= {code_err_w[3], code_err_w[2], code_err_w[1], code_err_w[0]};
            end
        end
    end

    //=======================================================================
    // Soft 20.83 Mbps sampler (serial -> 4x10b parallel word stream)
    //=======================================================================

    cxp_rx_lspd_sampler #(
        .p_OS_RATIO  (p_OS_RATIO),
        .p_LOCK_HITS (p_SAMP_LOCK_HITS)
    ) cxp_rx_lspd_sampler_i (
        .os_clk       (rx_clk),
        .os_rst_n     (rx_rst_n),
        .rx_serial_i  (rx_serial_i),
        .resync_i     (resync),
        .sym_out_o    (sym_in),
        .sym_valid_o   (sym_valid),
        .sym_rd_flip_o (sym_rd_flip),
        .rx_lock_o     (rx_lock_o),
        .trig_valid_o  (t15_valid),
        .trig_edge_o   (t15_edge),
        .trig_dly_o    (t15_dly)
    );

    //=======================================================================
    // 8B/10B decoder x4 with shared running-disparity chain
    //=======================================================================

    for (genvar i = 0; i < 4; i++) begin : g_lane
        assign rd_lane[i] = rd_chain[i] ^ sym_rd_flip[i];

        cxp_rx_8b10b_decoder cxp_rx_8b10b_decoder_i (
            .din_i      (sym_in[10*i +: 10]),
            .rd_in_i    (rd_lane[i]),
            .dout_o     (d_dec[i]),
            .k_out_o    (k_dec[i]),
            .disp_err_o (disp_err_w[i]),
            .code_err_o (code_err_w[i]),
            .rd_out_o   (rd_chain[i+1])
        );
    end

    //=======================================================================
    // IDLE monitor (§8.2.5.1 / §10.2): link up, loss of lock, resync
    //=======================================================================

    cxp_rx_link_mon #(
        .p_LOCK_IDLES    (p_LINK_LOCK_IDLES),
        .p_LOSS_WORDS    (p_RX_LOSS_WORDS),
        .p_BAD_WORDS     (p_RX_BAD_WORDS),
        .p_SHORT_LOSS_OK (p_RX_SHORT_LOSS_OK)
    ) cxp_rx_link_mon_i (
        .rx_clk          (rx_clk),
        .rx_rst_n        (rx_rst_n),
        .rx_lock_i       (rx_lock_o),
        .data_i          (d_reg_q),
        .kmask_i         (k_reg_q),
        .err_i           (word_err),
        .valid_i         (d_valid_q),
        .data_o          (mon_data),
        .kmask_o         (mon_kmask),
        .err_o           (mon_err),
        .valid_o         (mon_valid),
        .up_o            (aligned_o),
        .link_detected_o (link_detected_o),
        .resync_o        (resync),
        .flush_o         (mon_flush)
    );

    //=======================================================================
    // Packet parser (demux IDLE / short / long packets)
    //=======================================================================

    cxp_rx_packet_parser cxp_rx_packet_parser_i (
        .rx_clk            (rx_clk),
        .rx_rst_n          (rx_rst_n),
        .d_in_i            (mon_data),
        .d_kmask_i         (mon_kmask),
        .d_err_i           (mon_err),
        .d_valid_i         (mon_valid),
        .flush_i           (mon_flush),
        .ioack_o           (ioack_rcvd_o),
        .long_data_o       (long_data_w),
        .long_kmask_o      (long_kmask_w),
        .long_valid_o      (long_valid_w),
        .long_sop_o        (long_sop_w),
        .long_eop_o        (long_eop_w),
        .long_err_o        (long_err_w),
        .long_type_o       (long_type_w),
        .pkt_err_pulse_o   (pkt_err_pulse_o)
    );

    //=======================================================================
    // Trigger receiver (Table 15 Delay vote, Figure 20 wait)
    //=======================================================================

    cxp_rx_trigger_lspd #(
        .p_OS_RATIO (p_OS_RATIO)
    ) cxp_rx_trigger_lspd_i (
        .rx_clk                 (rx_clk),
        .rx_rst_n               (rx_rst_n),
        .cfg_polarity_i         (cfg_trig_polarity_i),
        .deassert_i             (trig_deassert_i),
        // The I/O channel is the Master connection's (§8.3): an extension
        // connection's triggers are neither recreated nor acknowledged.
        .trig_valid_i           (t15_valid & link_detected_o & trig_enable_i),
        .trig_edge_i            (t15_edge),
        .trig_dly_i             (t15_dly),
        .trig_ok_o              (trig_pkt_rcvd_o),
        .trigger_out_app_o      (trigger_out_app_o),
        .trigger_glitch_pulse_o (trigger_glitch_pulse_o)
    );

    //=======================================================================
    // Linktest counter (gated on type-0x04 long packets)
    //=======================================================================

    cxp_rx_linktest cxp_rx_linktest_i (
        .rx_clk        (rx_clk),
        .rx_rst_n      (rx_rst_n),
        .gate_i        (lt_gate),
        .long_data_i   (long_data_w),
        .long_kmask_i  (long_kmask_w),
        .long_sop_i    (long_sop_w),
        .long_eop_i    (long_eop_w),
        .long_err_i    (long_err_w),
        .clr_err_i     (clr_lt_err_i),
        .clr_pkt_i     (clr_lt_pkt_i),
        .err_count_o   (lt_err_count_o),
        .pkt_count_o   (lt_pkt_count_rx_o)
    );

endmodule

`default_nettype wire
