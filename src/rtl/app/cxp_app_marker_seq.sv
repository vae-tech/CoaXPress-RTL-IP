/*
================================================================================
  cxp_app_marker_seq
  CoaXPress 1.1.1 — stream marker word sequencer (§9.4).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-19

    Description:
      Image headers and line markers (§9.4, Tables 38-41) are one shape:

          Word 0      : 4×K28.3        kmask 1111
          Word 1..N   : 4×bytes_i[w]   kmask 0000, one byte per word

      On a start_i pulse while idle the byte vector and the last word index
      are latched, so the caller may change them for the next marker while
      this one drains.  A start_i pulse while a marker is in flight is
      dropped.  cxp_app_image_header and cxp_app_line_marker only decide
      which bytes go in the vector.

      AXI-Stream-like handshake: m_word_valid_o is high while a marker is
      in flight, m_word_ready_i advances it one word.

    Versions:
        2026-09-19 - 0.1:   - Init (sequencer shared by image_header_gen
                              and line_marker_gen)

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_app_marker_seq #(
    parameter int p_MAX_WORDS = 25                          // marker word + data words
) (
    input  wire  logic        app_clk,                      // application clock
    input  wire  logic        app_rst_n,                    // async active-low reset

    input  wire  logic        start_i,                      // 1-cycle start pulse
    input  wire  logic [cxp_util_pkg::idx_w(p_MAX_WORDS)-1:0] last_i, // last word index
    input  wire  logic [p_MAX_WORDS-1:1][7:0] bytes_i,      // byte for words 1..

    output logic [31:0]       m_word_data_o,                // 32-bit beat
    output logic [3:0]        m_word_kmask_o,               // per-lane K-char mask
    output logic              m_word_valid_o,               // beat valid
    input  wire  logic        m_word_ready_i                // downstream ready
);

    import cxp_pkg::*;
    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    localparam int IDX_W = idx_w(p_MAX_WORDS);

    //=======================================================================
    // Signals
    //=======================================================================

    logic [p_MAX_WORDS-1:0][7:0] bytes_q;   // K28.3, then the latched bytes
    logic [IDX_W-1:0]            last_q;    // latched last word index
    logic [IDX_W-1:0]            word_idx_q;
    logic                        active_q;  // marker in flight
    logic                        is_marker; // current word is the K28.3 word
    logic [7:0]                  rep_byte;  // byte replicated this beat

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign is_marker = (word_idx_q == '0);
    assign rep_byte  = bytes_q[word_idx_q];

    assign m_word_valid_o = active_q;
    assign m_word_data_o  = active_q ? rep4(rep_byte) : 32'h0000_0000;
    assign m_word_kmask_o = (active_q && is_marker) ? KMASK_ALL : KMASK_NONE;

    //=======================================================================
    // Sequential — latch the marker, walk the word index.
    //=======================================================================

    always_ff @(posedge app_clk or negedge app_rst_n) begin
        if (!app_rst_n) begin
            bytes_q    <= '0;
            last_q     <= '0;
            word_idx_q <= '0;
            active_q   <= 1'b0;
        end else begin
            if (!active_q && start_i) begin
                bytes_q    <= {bytes_i, K28_3};
                last_q     <= last_i;
                word_idx_q <= '0;
                active_q   <= 1'b1;
            end else if (active_q && m_word_ready_i) begin
                if (word_idx_q == last_q) begin
                    active_q   <= 1'b0;
                    word_idx_q <= '0;
                end else begin
                    word_idx_q <= word_idx_q + 1'b1;
                end
            end
        end
    end

endmodule

`default_nettype wire
