/*
================================================================================
  cxp_lib_crc32
  CoaXPress 1.1.1 (CXP-001-2015) — shared CRC-32 accumulator utility (§8.2.2.2).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2025-05-27

    Description:
      Implements the IEEE-802.3 / Ethernet CRC-32 used by the stream-data,
      control-command and control-ack packet types.  The same engine is
      re-used wherever CRC32 coverage is required so the polynomial setup
      cannot drift between TX framers and RX checkers.

      Polynomial setup
        Polynomial : 0x04C11DB7  (reflected constant 0xEDB8_8320)
        Seed (init): 0xFFFF_FFFF
        Final XOR  : none (§8.2.2.2 — this is not the 802.3 CRC)
        Bit order  : reflected — bit 0 (LSB) of each byte absorbed first
                     (matches §8.2.1 transmit order).

      Byte ordering on a 32-bit word (§8.2.1):
        din[ 7: 0] = P0 (transmitted first)
        din[15: 8] = P1
        din[23:16] = P2
        din[31:24] = P3
      The internal byte loop walks lanes 0..IN_W/8-1 in that order so a
      32-bit input is folded P0,P1,P2,P3.

      Interface
        p_IN_W parameter selects 8-bit (bytewise short packets) or 32-bit
        (wordwise stream packets) operation; `din_be_i` has one bit per
        input byte and gates per-byte folding so a single instance can
        also handle a partial last word.

        `init_i` resynchronises the accumulator to 0xFFFF_FFFF at the start
        of every CRC-covered region.  `init_i` and `din_valid_i` may be
        co-asserted: the seed is loaded first, then the data on that cycle
        is folded in on top.  Lets a caller drive {init,valid,first-word}
        in one clock and sample `crc_o` on the next cycle.

        `crc_o` is the register itself, valid every cycle (= the seed
        0xFFFF_FFFF right after `init_i` when no data has been absorbed).
        Sent as is, LSByte in P0 (crc_wire() in cxp_pkg); folding that word
        after the data leaves the register at 0.

      Synthesis notes
        * Pure combinational byte-fold + a single 32-bit register — no FSM.
        * Maps to ~32 LUT-XORs per enabled byte slot.
        * Lint-friendly: `default_nettype none`, no inferred sensitivity.

    Versions:
        2025-05-27 - 0.1:   - Init
        2026-09-19 - 0.2:   - No final XOR (§8.2.2.2)

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_lib_crc32 #(
    // Width of the data input in bits.  Must be a positive multiple of 8.
    parameter int p_IN_W = 32
) (
    input  wire  logic                  clk,           // accumulator clock
    input  wire  logic                  rst_n,         // sync-deassert active-low reset

    input  wire  logic                  init_i,        // synchronous re-seed pulse
    input  wire  logic [p_IN_W-1:0]     din_i,         // data input
    input  wire  logic [p_IN_W/8-1:0]   din_be_i,      // per-byte enable mask
    input  wire  logic                  din_valid_i,   // fold this beat into the CRC

    output logic [31:0]                 crc_o          // CRC register
);

    import cxp_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // CRC_SEED / CRC_POLY come from cxp_pkg.

    // ------ Functions and Tasks ------

    // Per-byte CRC step — reflected polynomial, bit 0 (LSB) absorbed first.
    function automatic logic [31:0] crc32_byte(input logic [31:0] c_in,
                                                input logic [7:0]  b);
        logic [31:0] c;
        logic        fb;
        c = c_in;
        for (int i = 0; i < 8; i++) begin
            fb = c[0] ^ b[i];
            c  = (c >> 1) ^ ({32{fb}} & CRC_POLY);
        end
        return c;
    endfunction

    //=======================================================================
    // Signals
    //=======================================================================

    logic [31:0] crc_q;   // accumulator
    logic [31:0] crc_n;   // next-state accumulator

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    if ((p_IN_W <= 0) || (p_IN_W % 8 != 0)) begin : g_chk_in_w
        $error("cxp_lib_crc32: p_IN_W (=%0d) must be a positive multiple of 8",
               p_IN_W);
    end

    //=======================================================================
    // Next-state: apply the synchronous seed first, then fold enabled byte
    // slots in ascending byte order so a co-asserted init+valid beat folds
    // its data on a fresh seed within the same cycle.
    //=======================================================================

    always_comb begin
        logic [31:0] c;
        c = init_i ? CRC_SEED : crc_q;

        if (din_valid_i) begin
            for (int b = 0; b < p_IN_W/8; b++) begin
                if (din_be_i[b]) begin
                    c = crc32_byte(c, din_i[b*8 +: 8]);
                end
            end
        end
        crc_n = c;
    end

    //=======================================================================
    // Sequential.
    //=======================================================================

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            crc_q <= CRC_SEED;
        end else begin
            crc_q <= crc_n;
        end
    end

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign crc_o = crc_q;

endmodule

`default_nettype wire
