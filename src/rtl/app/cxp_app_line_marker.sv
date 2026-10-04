/*
================================================================================
  cxp_app_line_marker
  CoaXPress 1.1.1 — line marker generator (§9.4).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2025-05-27

    Description:
      Emits a short marker word stream at every Start-Of-Line:
        * Rectangular  (meta_i.arbitrary = 0) — 2 words   (Table 39):
                        word 1 : 4×K28.3
                        word 2 : 4×0x02
        * Arbitrary    (meta_i.arbitrary = 1) — 11 words  (§9.4 arbitrary form):
                        word  1 : 4×K28.3
                        word  2 : 4×0x04
                        words 3-5  : 4×Xsize  (24-bit, MSB first)
                        words 6-8  : 4×Xoffs  (24-bit, MSB first)
                        words 9-11 : 4×DsizeL (24-bit, MSB first; 32-bit
                                     words per line, dsizel_words() in cxp_pkg)

      Each field byte is replicated four times across the 32-bit word for
      single-bit-error tolerance (§9.4).

      AXI-Stream-like handshake: `m_word_valid_o` asserts as soon as the
      marker is being emitted, `m_word_ready_i` throttles forward progress.
      `m_word_kmask_o` is 4'b1111 only for the K28.3 marker word and 4'b0000
      for the data words.

    Versions:
        2025-05-27 - 0.1:   - Init
        2026-09-19 - 0.2:   - cxp_meta_t input; words sent by cxp_app_marker_seq
        2026-09-19 - 0.3:   - DsizeL in words, derived from xsize and pixfmt

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_app_line_marker (
    input  wire  logic        app_clk,             // application clock
    input  wire  logic        app_rst_n,           // sync-deassert reset
    input  wire  cxp_pkg::cxp_meta_t meta_i,       // form, Xsize, Xoffs, PixelF
    input  wire  logic        line_start_i,        // 1-cycle SOL pulse

    output logic [31:0]       m_word_data_o,       // 32-bit beat
    output logic [3:0]        m_word_kmask_o,      // per-byte K-char mask
    output logic              m_word_valid_o,      // beat valid
    input  wire  logic        m_word_ready_i       // downstream ready
);

    import cxp_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    // K28_3 / LINE_TYPE_RECT / LINE_TYPE_ARB come from cxp_pkg.
    localparam int RECT_WORDS = 2;
    localparam int ARB_WORDS  = 11;
    localparam int IDX_W      = $clog2(ARB_WORDS); // 4

    //=======================================================================
    // Signals
    //=======================================================================

    logic [ARB_WORDS-1:1][7:0] line_bytes;   // bytes for words 1..
    logic [IDX_W-1:0]          last_idx;     // last word index for this form
    logic [23:0]               dsizel;       // Table 41 DsizeL, words

    //=======================================================================
    // Marker bytes: rectangular (type only) or the §9.4 arbitrary form.
    //=======================================================================

    assign dsizel = dsizel_words(meta_i.xsize, meta_i.pixfmt);

    always_comb begin
        line_bytes = '0;
        if (!meta_i.arbitrary) begin
            last_idx      = IDX_W'(RECT_WORDS - 1);
            line_bytes[1] = LINE_TYPE_RECT;
        end else begin
            last_idx         = IDX_W'(ARB_WORDS - 1);
            line_bytes[10:1] = {
                dsizel[7:0],        dsizel[15:8],       dsizel[23:16],
                meta_i.xoffs[7:0],  meta_i.xoffs[15:8], meta_i.xoffs[23:16],
                meta_i.xsize[7:0],  meta_i.xsize[15:8], meta_i.xsize[23:16],
                LINE_TYPE_ARB                                           // 1
            };
        end
    end

    //=======================================================================
    // Marker sequencer — K28.3, then one replicated byte per word.
    //=======================================================================

    cxp_app_marker_seq #(
        .p_MAX_WORDS (ARB_WORDS)
    ) cxp_app_marker_seq_i (
        .app_clk        (app_clk),
        .app_rst_n      (app_rst_n),
        .start_i        (line_start_i),
        .last_i         (last_idx),
        .bytes_i        (line_bytes),
        .m_word_data_o  (m_word_data_o),
        .m_word_kmask_o (m_word_kmask_o),
        .m_word_valid_o (m_word_valid_o),
        .m_word_ready_i (m_word_ready_i)
    );

endmodule

`default_nettype wire
