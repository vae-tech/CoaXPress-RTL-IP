/*
================================================================================
  cxp_rx_linktest
  CoaXPress 1.1.1 (CXP-001-2015) §8.7 / §10.3.37 / §10.3.39 — Test Receiver.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-27

    Description:
      Reference: JIIA CXP-001-2015 §8.7 "Connection Test", §10.3.37
      TestErrorCount, §10.3.39 TestPacketCountRx.  Migrated from the
      v1.0 numbering (old §6.7.3).

      The host sends type-0x04 connection-test packets that contain 1024
      32-bit words of a known counting sequence (0x00..0xFF repeated 16
      times per word, byte-by-byte).  This module receives the body of
      such packets from cxp_rx_packet_parser and:
        * counts each 32-bit word that does NOT match the expected
          pattern (TestErrorCount, §10.3.37) — saturates at 0xFFFFFFFF;
        * counts each received test packet (TestPacketCountRx, §10.3.39)
          — a v1.1 addition; 8-byte (64-bit) per-connection register.
      Per §8.7 the Test Receiver "shall run at all times" — this module
      is gated only by (long_type==0x04), NEVER by TestMode (TestMode
      only gates the TX generator).  Each counter has its own clear
      (§10.3.37 / §10.3.39: a host write of 0 to that register);
      ConnectionReset (§10.3.28) pulses both.

      Connection:
        The parser exposes a single long-packet stream.  This module is
        gated externally by `(long_type_q == 8'h04)` so that we only
        accumulate over linktest payloads.  The wrapper passes
        long_data / long_kmask / long_valid / long_sop / long_eop with
        a `gate_i` input.

      Pattern (§8.7.2, Table 23):
        Each 4096-byte payload is the byte sequence 0x00 0x01 ... 0xFF
        repeated 16 times, P0 first: word i carries 4i, 4i+1, 4i+2, 4i+3
        (mod 256) in P0..P3, so the first word is 0x03_02_01_00 and the
        pattern repeats every 64 words.  There is no CRC: word 1023 is
        followed directly by the K29.7 trailer.

      Counting (§8.7.1, one count per word that differs):
        * The 4xTYPE word (long_sop_i) starts a packet at word index 0.
        * Every body word before the trailer is compared with the
          expected word for its index; a data mismatch, a K-character in
          any lane, or a word beyond index 1023 each count one error.  The
          index advances on every body word, so one corrupted or
          K-character word costs exactly one error.  A word flagged
          long_err_i (decode error) differs by definition.
        * At the trailer, every expected word that never arrived
          (index < 1024) counts one error, and the packet is counted.
          A packet the parser aborts (long_eop_i with long_err_i: lost
          trailer, link loss) ends the same way, so its missing words
          count too.

    Versions:
        2026-05-27 - 0.1:   - Restyle to coding-style template
        2026-09-19 - 0.2:   - All 1024 words compared (Table 23 has no CRC);
                              one error per differing word
        2026-09-19 - 0.3:   - Separate clears for the two counters

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_rx_linktest (
    // Receive clock domain
    input  wire  logic        rx_clk,                           // RX clock
    input  wire  logic        rx_rst_n,                         // Active-low sync reset

    // Gated long-packet stream from cxp_rx_packet_parser, qualified by
    // (long_type == 8'h04).
    input  wire  logic        gate_i,                           // long_type==4 && long_valid
    input  wire  logic [31:0] long_data_i,                      // 32-bit long-pkt word
    input  wire  logic [3:0]  long_kmask_i,                     // Per-byte K-flag
    input  wire  logic        long_sop_i,                       // Start-of-packet
    input  wire  logic        long_eop_i,                       // End-of-packet
    input  wire  logic        long_err_i,                       // With eop: packet aborted

    // Counter clears (host write 0 to the register / ConnectionReset)
    input  wire  logic        clr_err_i,                        // Clear TestErrorCount
    input  wire  logic        clr_pkt_i,                        // Clear TestPacketCountRx

    output logic [31:0]       err_count_o,                      // §10.3.37 TestErrorCount
    output logic [63:0]       pkt_count_o                       // §10.3.39 TestPacketCountRx
);

    import cxp_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    localparam logic [10:0] N_WORDS = 11'(LT_DATA_WORDS);   // 1024

    // ------ Functions and Tasks ------

    // Table 23 word i: bytes 4i .. 4i+3 (mod 256), P0 in [7:0].
    function automatic logic [31:0] lt_word(input logic [5:0] i);
        logic [7:0] b;
        b = {i, 2'b00};
        lt_word = {b + 8'd3, b + 8'd2, b + 8'd1, b};
    endfunction

    // Saturating add of an error count.
    function automatic logic [31:0] sat_add(input logic [31:0] a,
                                            input logic [31:0] n);
        logic [32:0] sum;
        sum     = {1'b0, a} + {1'b0, n};
        sat_add = sum[32] ? 32'hFFFF_FFFF : sum[31:0];
    endfunction

    //=======================================================================
    // Signals
    //=======================================================================

    logic        in_pkt_q;            // between the TYPE word and the trailer
    logic [10:0] idx_q;               // body words received (saturates at 2047)
    logic [31:0] err_count_q;         // §10.3.37 TestErrorCount accumulator
    logic [63:0] pkt_count_q;         // §10.3.39 received-test-packet count

    logic        body_word;           // body word of a test packet
    logic        trailer;             // K29.7 trailer of a test packet
    logic        word_bad;            // this body word differs
    logic [31:0] missing;             // words that never arrived

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign body_word = gate_i & in_pkt_q & ~long_sop_i & ~long_eop_i;
    assign trailer   = gate_i & in_pkt_q & long_eop_i;   // K29.7 or abort
    assign word_bad  = long_err_i
                     | (long_kmask_i != KMASK_NONE)
                     | (idx_q >= N_WORDS)
                     | (long_data_i != lt_word(idx_q[5:0]));
    assign missing   = (idx_q < N_WORDS) ? 32'(N_WORDS - idx_q) : 32'h0;

    assign err_count_o = err_count_q;
    assign pkt_count_o = pkt_count_q;

    //=======================================================================
    // Error / packet counters
    //=======================================================================

    always_ff @(posedge rx_clk or negedge rx_rst_n) begin
        if (!rx_rst_n) begin
            in_pkt_q    <= 1'b0;
            idx_q       <= '0;
            err_count_q <= 32'h0000_0000;
            pkt_count_q <= 64'h0;
        end else begin
            if (gate_i & long_sop_i) begin
                in_pkt_q <= 1'b1;
                idx_q    <= '0;
            end else if (trailer) begin
                in_pkt_q <= 1'b0;
            end else if (body_word && idx_q != 11'h7FF) begin
                idx_q <= idx_q + 11'd1;
            end

            // A clear has priority over a count in the same cycle (host
            // write 0 / ConnectionReset, §10.3.37 / §10.3.39).
            if (clr_err_i)
                err_count_q <= 32'h0000_0000;
            else if (trailer)
                err_count_q <= sat_add(err_count_q, missing);
            else if (body_word && word_bad)
                err_count_q <= sat_add(err_count_q, 32'd1);

            if (clr_pkt_i)
                pkt_count_q <= 64'h0;
            else if (trailer)
                pkt_count_q <= pkt_count_q + 64'd1;
        end
    end

endmodule

`default_nettype wire
