/*
================================================================================
  cxp_app_image_header
  CoaXPress 1.1.1 — image header generator (§9.4).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2025-05-27

    Description:
      Emits one image header per frame, kicked off by a 1-cycle pulse on
      `meta_valid_i` (acting as the SOF tick from cxp_app_pixel_ingress).  The
      header is gated by meta_valid: if no pulse arrives, no header is
      produced.  The frame metadata arrives as one cxp_meta_t; this module
      lays it out as a byte vector and cxp_app_marker_seq sends it.

        Rectangular  (meta_i.arbitrary = 0) — 25 words   (Table 38):
          1 : 4×K28.3                                 (stream marker)
          2 : 4×0x01                                  (rect header type)
          3 : 4×StreamID
          4-5 : 4×SourceTag[15:8]/[7:0]
          6-8 : 4×Xsize  (24-bit, MSB first)
          9-11 : 4×Xoffs (24-bit, MSB first)
         12-14 : 4×Ysize (24-bit, MSB first)
         15-17 : 4×Yoffs (24-bit, MSB first)
         18-20 : 4×DsizeL (24-bit, 32-bit words per line, from xsize and
                 pixfmt — dsizel_words() in cxp_pkg)
         21-22 : 4×PixelF[15:8]/[7:0]
         23-24 : 4×TapG[15:8]/[7:0]
         25 : 4×Flags                                 (interlacing, bits 1:0)

        Arbitrary    (meta_i.arbitrary = 1) — 16 words   (§9.4 arbitrary form):
          1 : 4×K28.3
          2 : 4×0x03                                  (arbitrary header type)
          3 : 4×StreamID
          4-5 : 4×SourceTag[15:8]/[7:0]
          6-8 : 4×Ysize
          9-11 : 4×Yoffs
         12-13 : 4×PixelF
         14-15 : 4×TapG
         16 : 4×Flags

      Each field byte is replicated four times across the 32-bit word so that
      a single-bit error at the receiver is correctable by a per-byte
      majority vote.  `m_word_kmask_o` = 4'b1111 for the K28.3 marker word
      and 4'b0000 for every data word.

      Note: StreamID is a single byte in the spec table.  §9.4 of the spec
      text refers to
      "27 words" for the rectangular header; that is an editorial slip —
      Table 38 enumerates exactly 25 words and is treated as authoritative.

    Versions:
        2025-05-27 - 0.1:   - Init
        2026-09-19 - 0.2:   - cxp_meta_t input; words sent by cxp_app_marker_seq
        2026-09-19 - 0.3:   - DsizeL in words, derived from xsize and pixfmt

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_app_image_header (
    input  wire  logic        app_clk,                // application clock
    input  wire  logic        app_rst_n,              // sync-deassert reset

    // Frame metadata; latched on the cycle a meta_valid_i pulse is observed
    // while the generator is idle.
    input  wire  cxp_pkg::cxp_meta_t meta_i,          // frame metadata + header form
    input  wire  logic        meta_valid_i,           // 1-cycle SOF pulse

    // Outgoing 32-bit word stream.  Each emitted byte is replicated 4× across
    // the lanes for single-bit-error tolerance.
    output logic [31:0]       m_word_data_o,          // 32-bit beat
    output logic [3:0]        m_word_kmask_o,         // per-lane K-char mask
    output logic              m_word_valid_o,         // beat valid
    input  wire  logic        m_word_ready_i          // downstream ready
);

    import cxp_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    // K28_3 / HDR_TYPE_REC / HDR_TYPE_ARB come from cxp_pkg.
    localparam int RECT_WORDS = 25;
    localparam int ARB_WORDS  = 16;
    localparam int IDX_W      = $clog2(RECT_WORDS);  // 5

    //=======================================================================
    // Signals
    //=======================================================================

    logic [RECT_WORDS-1:1][7:0] hdr_bytes;   // bytes for words 1..
    logic [IDX_W-1:0]           last_idx;    // last word index for this form
    logic [23:0]                dsizel;      // Table 38 DsizeL, words

    //=======================================================================
    // Header bytes: Table 38 (rectangular) or the §9.4 arbitrary form.
    //=======================================================================

    assign dsizel = dsizel_words(meta_i.xsize, meta_i.pixfmt);

    always_comb begin
        hdr_bytes = '0;
        if (!meta_i.arbitrary) begin
            last_idx = IDX_W'(RECT_WORDS - 1);
            hdr_bytes[24:1] = {
                meta_i.flags,                                           // 24
                meta_i.tapg[7:0],     meta_i.tapg[15:8],                // 23..22
                meta_i.pixfmt[7:0],   meta_i.pixfmt[15:8],              // 21..20
                dsizel[7:0],          dsizel[15:8],       dsizel[23:16],
                meta_i.yoffs[7:0],    meta_i.yoffs[15:8], meta_i.yoffs[23:16],
                meta_i.ysize[7:0],    meta_i.ysize[15:8], meta_i.ysize[23:16],
                meta_i.xoffs[7:0],    meta_i.xoffs[15:8], meta_i.xoffs[23:16],
                meta_i.xsize[7:0],    meta_i.xsize[15:8], meta_i.xsize[23:16],
                meta_i.sourcetag[7:0], meta_i.sourcetag[15:8],          // 4..3
                meta_i.streamid,                                        // 2
                HDR_TYPE_REC                                            // 1
            };
        end else begin
            last_idx = IDX_W'(ARB_WORDS - 1);
            hdr_bytes[15:1] = {
                meta_i.flags,                                           // 15
                meta_i.tapg[7:0],     meta_i.tapg[15:8],                // 14..13
                meta_i.pixfmt[7:0],   meta_i.pixfmt[15:8],              // 12..11
                meta_i.yoffs[7:0],    meta_i.yoffs[15:8], meta_i.yoffs[23:16],
                meta_i.ysize[7:0],    meta_i.ysize[15:8], meta_i.ysize[23:16],
                meta_i.sourcetag[7:0], meta_i.sourcetag[15:8],          // 4..3
                meta_i.streamid,                                        // 2
                HDR_TYPE_ARB                                            // 1
            };
        end
    end

    //=======================================================================
    // Marker sequencer — K28.3, then one replicated byte per word.
    //=======================================================================

    cxp_app_marker_seq #(
        .p_MAX_WORDS (RECT_WORDS)
    ) cxp_app_marker_seq_i (
        .app_clk        (app_clk),
        .app_rst_n      (app_rst_n),
        .start_i        (meta_valid_i),
        .last_i         (last_idx),
        .bytes_i        (hdr_bytes),
        .m_word_data_o  (m_word_data_o),
        .m_word_kmask_o (m_word_kmask_o),
        .m_word_valid_o (m_word_valid_o),
        .m_word_ready_i (m_word_ready_i)
    );

endmodule

`default_nettype wire
