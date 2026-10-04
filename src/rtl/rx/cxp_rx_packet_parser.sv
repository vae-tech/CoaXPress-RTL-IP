/*
================================================================================
  cxp_rx_packet_parser
  CoaXPress 1.1.1 (CXP-001-2015) §8.2 / §8.3 / §8.5 — uplink packet demux.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-27

    Description:
      CoaXPress 1.1.1 host->camera (uplink) packet demultiplexer.
      Reference: JIIA CXP-001-2015 §8.2 (K-code map Table 11), §8.3
      (I/O acknowledgment Table 17), §8.5 (stream data packet).  Migrated
      from the v1.0 numbering.

      Input  : a 32-bit / 4-bit-kmask word stream produced by four
               8B/10B decoders, forwarded by cxp_rx_link_mon while the link is up.
               Bit-position convention matches the rest of the IP:
                  d_in_i[ 7: 0] = P0 (first transmitted)
                  d_in_i[15: 8] = P1
                  d_in_i[23:16] = P2
                  d_in_i[31:24] = P3
                  d_kmask_i[i]  = 1 -> lane Pi is a K-character.

      The parser classifies every accepted word into one of:

        * IDLE     — silently discarded.  Pattern: K28.5 K28.1 K28.1
                     D21.5 (kmask == 4'b0111).  We accept any
                     all-K-on-P0..P2 + D-char-on-P3 as IDLE and just
                     drop it (the parser does not flag a malformed
                     IDLE — that is a link-test concern).

        * I/O ACK  — 4xK28.6 (3 of 4 lanes) followed by the data word
                     4xCode (Table 17), the host's acknowledgment of a
                     device trigger (§8.3.3).  It is inserted at a word
                     boundary (§8.2.4), so it may sit between two words of
                     a long packet: both words are taken out and the packet
                     goes on.  ioack_o pulses on the code word when its
                     voted byte is 0x01.  A repeated leader starts it again;
                     a leader followed by any other K-character word is
                     dropped and that word is parsed as usual.

        * LONG PKT — K27.7 SOP (3 of 4 lanes), followed by N+1 data
                     words (the first is always 4xTYPE), terminated by
                     K29.7 EOP (3 of 4 lanes).  The body word stream is
                     exposed verbatim — including the 4xTYPE word — to
                     the downstream consumers, which select on the voted
                     TYPE byte; sop is asserted on the TYPE word, eop on
                     the K29.7 word.

      The low-speed trigger is the six-character Table 15 packet, taken
      out of the character stream by cxp_rx_lspd_sampler before the
      words are formed; the Table 16 word form belongs to the high-speed
      links only.  A K28.2 / K28.4 word that still reaches the parser is
      a damaged leader: in hunt it is discarded like any stray word
      (as is the v1.0 GPIO leader 4xK28.0, removed in CoaXPress 1.1),
      inside a body it is carried as data.

      Long-packet framing (§8.2.2.1, §8.2.5.2): inside a body, an IDLE
      word (low-speed packets are never stretched), a new SOP or a body
      longer than RX_MAX_BODY_WORDS means the trailer was lost.  The
      packet is then aborted: one word with long_eop_o = long_err_o = 1
      ends it for the consumers, `pkt_err_pulse_o` fires, and a new SOP
      starts the next packet at once.  flush_i (link lost) aborts a packet
      in flight the same way.  A K-character in any body lane, or a
      K29.7 in only one or two lanes, is carried as a data word (the
      command CRC or the test-word compare catches it).  A body word
      with an 8B/10B error (d_err_i) is forwarded with long_err_o set and
      long_eop_o clear.

    Versions:
        2026-05-27 - 0.1:   - Restyle to coding-style template
        2026-09-19 - 0.2:   - flush_i from the link monitor
        2026-09-19 - 0.3:   - 3-of-4 SOP / EOP; abort on SOP, IDLE, length or
                              flush inside a body (long_err_o)
        2026-09-19 - 0.4:   - 8B/10B errors marked on body words
        2026-09-26 - 0.5:   - Table 17 I/O acknowledgment taken out of the
                              word stream (ioack_o), also inside a packet
        2026-09-26 - 0.6:   - Table 16 word-form trigger path removed

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_rx_packet_parser (
    // Receive clock domain
    input  wire  logic        rx_clk,                           // RX clock
    input  wire  logic        rx_rst_n,                         // Active-low sync reset

    // Aligned 32-bit / 4-bit-kmask word stream
    input  wire  logic [31:0] d_in_i,                           // Lane-aligned data word
    input  wire  logic [3:0]  d_kmask_i,                        // Per-lane K-flag
    input  wire  logic        d_err_i,                          // 8B/10B error in this word
    input  wire  logic        d_valid_i,                        // Word valid pulse
    input  wire  logic        flush_i,                          // Link lost: back to hunt

    // Table 17 I/O acknowledgment from the host (§8.3.3)
    output logic              ioack_o,                          // code 0x01 received (pulse)

    // Long-packet body output
    output logic [31:0]       long_data_o,                      // Body word
    output logic [3:0]        long_kmask_o,                     // Body kmask
    output logic              long_valid_o,                     // Body valid
    output logic              long_sop_o,                       // Start-of-packet (TYPE word)
    output logic              long_eop_o,                       // End-of-packet (K29.7 or abort)
    output logic              long_err_o,                       // Abort (with eop) / bad word
    output logic [7:0]        long_type_o,                      // Latched at SOP (TYPE)

    // Diagnostics
    output logic              pkt_err_pulse_o                   // Framing error
);

    import cxp_pkg::*;
    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // K-code byte values and the IDLE kmask come from cxp_pkg.

    // ------ Types ------

    typedef enum logic [1:0] {
        ST_IDLE_HUNT,    // discarding IDLE / waiting for a SOP
        ST_LONG_TYPE,    // expect 4xTYPE word inside long packet
        ST_LONG_BODY     // streaming words until K29.7
    } state_t;

    //=======================================================================
    // Signals
    //=======================================================================

    logic [7:0] b0;                   // Lane-0 byte
    logic [7:0] b1;                   // Lane-1 byte
    logic [7:0] b2;                   // Lane-2 byte
    logic [7:0] b3;                   // Lane-3 byte
    logic       k0;                   // Lane-0 K-flag
    logic       k1;                   // Lane-1 K-flag
    logic       k2;                   // Lane-2 K-flag
    logic       k3;                   // Lane-3 K-flag

    logic       is_idle;              // IDLE pattern detected
    logic [2:0] cnt_k27_7;            // Count of K27.7 lanes
    logic [2:0] cnt_k29_7;            // Count of K29.7 lanes

    state_t     state_q;              // FSM state
    state_t     state_n;              // FSM next-state
    logic [7:0] long_type_q;          // Latched TYPE byte
    logic [$clog2(RX_MAX_BODY_WORDS+1)-1:0] body_cnt_q;   // body words so far
    logic       is_sop3;              // K27.7 in at least 3 lanes
    logic       is_eop3;              // K29.7 in at least 3 lanes
    logic       too_long;             // body reached RX_MAX_BODY_WORDS
    logic       abort;                // end the packet in flight with an error
    logic [7:0] type_vote;            // 3-of-4 vote of the TYPE word
    logic       ioack_q;              // I/O-ack leader seen, code word next
    logic       ioack_lead;           // this word is an I/O-ack leader
    logic       ioack_code;           // this word is the I/O-ack code word
    logic       ioack_word;           // this word belongs to an I/O ack

    // ------ Functions and Tasks ------

    // Count the number of lanes holding `kc` (0..4).
    function automatic logic [2:0] cnt_kc(input logic [7:0] kc);
        logic [2:0] c;
        c = '0;
        if (k0 & (b0 == kc)) c = c + 3'd1;
        if (k1 & (b1 == kc)) c = c + 3'd1;
        if (k2 & (b2 == kc)) c = c + 3'd1;
        if (k3 & (b3 == kc)) c = c + 3'd1;
        cnt_kc = c;
    endfunction

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign {b3, b2, b1, b0} = d_in_i;
    // §8.2.2.1: the 4x TYPE word is voted, so one corrupted lane is harmless.
    assign type_vote        = mvote(b0, b1, b2, b3);
    assign {k3, k2, k1, k0} = d_kmask_i;

    //=======================================================================
    // Word classification (combinational on the input word)
    //=======================================================================

    always_comb begin
        // IDLE: P0=K28.5, P1=K28.1, P2=K28.1, P3=D21.5 (D-char).
        is_idle = (d_kmask_i == KMASK_IDLE)
                & (b0 == K28_5) & (b1 == K28_1) & (b2 == K28_1);
        // (D21.5 has byte value 0xB5, but we accept any D-char in P3
        // to tolerate occasional jitter on the unused filler byte.)

        cnt_k27_7      = cnt_kc(K27_7);
        cnt_k29_7      = cnt_kc(K29_7);
        // §8.2.2.1: a replicated K-character survives one bad lane.
        is_sop3        = (cnt_k27_7 >= 3'd3);
        is_eop3        = (cnt_k29_7 >= 3'd3);
        ioack_lead     = (cnt_kc(K28_6) >= 3'd3);          // a repeat restarts it
        ioack_code     = ioack_q && (d_kmask_i == KMASK_NONE);
        // K28_3 marker (image-header / line-marker) only appears inside
        // the long-packet body and is treated as ordinary payload here.
    end

    //=======================================================================
    // Output mux (per-state)
    //=======================================================================

    assign too_long   = (body_cnt_q == $bits(body_cnt_q)'(RX_MAX_BODY_WORDS));
    assign ioack_word = d_valid_i & (ioack_lead | ioack_code);
    assign ioack_o    = d_valid_i & ~flush_i & ioack_code
                      & (mvote(b0, b1, b2, b3) == IOACK_CODE_OK);
    // A packet in flight ends early: link lost, or (in the body) an IDLE,
    // a new SOP or an over-long body.  The words of an inserted I/O ack
    // are not part of it.
    assign abort = (state_q == ST_LONG_BODY) &
                   (flush_i | (d_valid_i & ~ioack_word & ~is_eop3 &
                               (is_idle | is_sop3 | too_long)));

    always_comb begin
        long_data_o       = 32'h0;
        long_kmask_o      = KMASK_NONE;
        long_valid_o      = 1'b0;
        long_sop_o        = 1'b0;
        long_eop_o        = 1'b0;
        long_err_o        = 1'b0;
        long_type_o       = long_type_q;
        pkt_err_pulse_o   = 1'b0;

        if (abort) begin
            // The abort word carries no data; it only closes the packet.
            long_valid_o    = 1'b1;
            long_eop_o      = 1'b1;
            long_err_o      = 1'b1;
            pkt_err_pulse_o = 1'b1;
        end else if (d_valid_i && !flush_i && !ioack_word) begin
            unique case (state_q)

                //===========================================================
                // Hunt: discarding IDLE / waiting for a SOP; outputs default.
                //
                ST_IDLE_HUNT: ;     // outputs default

                //===========================================================
                // Long type: present the 4×TYPE word as SOP of a long packet.
                // A K-character here (IDLE, SOP, trailer) is not a TYPE
                // word: nothing is presented and the hunt starts again.
                //
                ST_LONG_TYPE: begin
                    if (d_kmask_i == KMASK_NONE) begin
                        long_data_o   = d_in_i;
                        long_kmask_o  = d_kmask_i;
                        long_valid_o  = 1'b1;
                        long_sop_o    = 1'b1;
                        long_type_o   = type_vote;
                    end else if (!is_sop3) begin
                        pkt_err_pulse_o = 1'b1;
                    end
                end

                //===========================================================
                // Long body: stream words until the K29.7 trailer (EOP).
                //
                ST_LONG_BODY: begin
                    long_data_o  = d_in_i;
                    long_kmask_o = d_kmask_i;
                    long_valid_o = 1'b1;
                    long_eop_o   = is_eop3;
                    long_err_o   = d_err_i & ~is_eop3;   // a voted trailer is good
                end

                default: ;
            endcase
        end
    end

    //=======================================================================
    // FSM next-state
    //=======================================================================

    always_comb begin
        state_n = state_q;
        if (flush_i) begin
            state_n = ST_IDLE_HUNT;
        end else if (d_valid_i && !ioack_word) begin
            unique case (state_q)
                ST_IDLE_HUNT: begin
                    // Anything but a SOP (IDLE, a damaged trigger leader,
                    // the deleted v1.0 K28.0 GPIO leader) is discarded.
                    if (is_sop3) state_n = ST_LONG_TYPE;
                end

                ST_LONG_TYPE: begin
                    if      (d_kmask_i == KMASK_NONE) state_n = ST_LONG_BODY;
                    else if (!is_sop3)                state_n = ST_IDLE_HUNT;
                end

                ST_LONG_BODY: begin
                    if      (is_eop3)             state_n = ST_IDLE_HUNT;
                    else if (is_sop3)             state_n = ST_LONG_TYPE;   // abort, next packet
                    else if (is_idle || too_long) state_n = ST_IDLE_HUNT;   // abort
                end

                default: state_n = ST_IDLE_HUNT;
            endcase
        end
    end

    //=======================================================================
    // FSM sequential (state + TYPE latch)
    //=======================================================================

    always_ff @(posedge rx_clk or negedge rx_rst_n) begin
        if (!rx_rst_n) begin
            state_q        <= ST_IDLE_HUNT;
            long_type_q    <= 8'h00;
            body_cnt_q     <= '0;
            ioack_q        <= 1'b0;
        end else if (flush_i) begin
            state_q <= ST_IDLE_HUNT;
            ioack_q <= 1'b0;
        end else if (d_valid_i && ioack_word) begin
            // Words of an inserted I/O ack: the packet state holds.
            ioack_q <= ioack_lead;
        end else if (d_valid_i) begin
            state_q <= state_n;
            ioack_q <= 1'b0;

            if (state_q == ST_LONG_TYPE)
                body_cnt_q <= $bits(body_cnt_q)'(1);
            else if (state_q == ST_LONG_BODY && !too_long)
                body_cnt_q <= body_cnt_q + 1'b1;

            // Latch TYPE byte on the cycle we present long_sop_o.
            if (state_q == ST_LONG_TYPE && d_kmask_i == KMASK_NONE)
                long_type_q <= type_vote;
        end
    end

endmodule

`default_nettype wire
