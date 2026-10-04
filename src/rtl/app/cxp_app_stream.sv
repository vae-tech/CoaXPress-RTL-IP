/*
================================================================================
  cxp_app_stream
  CoaXPress 1.1.1 (CXP-001-2015) — stream path, application-clock side:
  image header, line markers, pixel words, packet boundaries.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-27

    Description:
      Takes a pixel-word stream from the pixel source (test-pattern
      generator or sensor ingress, through cxp_app_pixel_packer) and writes
      the words of the stream packets into the stream FIFO, the packet
      boundaries marked:

         pix_word_*        frame_start -- cxp_image_  --,
             |                            header_gen    |
             |             line_start  -- cxp_line_  ---|--,
             v                            marker_gen    |  |
          skid register                                 v  v
             |      priority merger (hdr > line > pix) + DsizeP chopper
             v
          m_*_o  -> cxp_cdc_stream_fifo (app_clk write side)

      The framing (SOP, header words, CRC, EOP) is added on tx_clk by
      cxp_tx_stream_pkt at the FIFO's read side; this block runs on
      app_clk only.

      The image-header / line-marker generators need one cycle to latch
      their pulse before their words are valid.  The pixel path therefore
      goes through a 1-stage skid register (pix_d_*_q), so the first pixel
      word of a frame / line is held back one cycle while the header /
      marker generator comes up; the merger then sees the header or marker
      first.  With no pulse in flight the skid register adds no latency.

      Packet boundaries (§8.5.2): the chopper splits the merged word flow
      into packets of cfg_dsizeP_i words irrespective of where image
      headers, line markers or pixels fall within them, and closes a
      packet early on the last pixel word of an image (pix_word_eof_i), so
      an image's last packet is as short as the image leaves it.  A packet
      is at most the FIFO's size less a margin (PKT_MAX; cfg_dsizeP_i = 0
      means that size): the framer starts a packet only once all of it is
      in the FIFO.  The StreamID written beside a packet's first word is
      the one of the image the packet opens in.

      meta_i is sampled on pix_frame_start_i and held for the image: the
      image header reads it in that cycle, every later line marker reads
      the held copy.

      Flush (flush_i, the FIFO's app-side view of a flush request): from
      the moment it is seen until the next image starts after it is
      released, everything is dropped — pixel words, image headers, line
      markers — so the first packet afterwards opens a whole image.

    Versions:
        2026-09-27 - 0.1:   - The app_clk half of cxp_stream_top (skid
                              register, merger, chopper, marker
                              generators), unchanged

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_app_stream #(
    parameter int p_FIFO_DEPTH = 1024                           // stream FIFO depth (words)
) (
    input  wire  logic        app_clk,                          // App clock
    input  wire  logic        app_rst_n,                        // App reset (active-low)

    input  wire  logic [15:0] cfg_dsizeP_i,                     // Stream-pkt payload size

    // Pixel-word source
    input  wire  logic [31:0] pix_word_data_i,                  // Pixel word data
    input  wire  logic        pix_word_valid_i,                 // Pixel word valid
    input  wire  logic        pix_word_eof_i,                   // last pixel word of the image
    output logic              pix_word_ready_o,                 // Pixel word ready

    // 1-cycle pulses with the accept of the first pixel word of a frame /
    // line (or one cycle before it)
    input  wire  logic        pix_frame_start_i,                // Frame-start pulse
    input  wire  logic        pix_line_start_i,                 // Line-start pulse

    // Frame metadata and marker form (sampled on pix_frame_start_i)
    input  wire  cxp_pkg::cxp_meta_t meta_i,                    // Frame metadata

    // Stream FIFO write side
    output logic [31:0]       m_data_o,                         // packet word
    output logic [3:0]        m_kmask_o,                        // per-byte K flag
    output logic              m_valid_o,                        // word valid
    output logic              m_sop_o,                          // first word of a packet
    output logic              m_eop_o,                          // last word of a packet
    output logic [7:0]        m_streamid_o,                     // StreamID of the packet
    input  wire  logic        m_ready_i,                        // the FIFO takes the word
    input  wire  logic        flush_i                           // the FIFO is flushing
);

    import cxp_pkg::*;

    //=======================================================================
    // Local Parameters
    //=======================================================================

    // Longest packet the FIFO can hold whole (store and forward); the
    // margin covers the FIFO's almost-full slack and the stale read
    // pointer seen from app_clk.
    localparam logic [15:0] PKT_MAX = 16'(p_FIFO_DEPTH - 8);

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    // A packet must fit in the FIFO whole before it may leave: with fewer
    // than 16 words PKT_MAX is 0, no packet is ever closed by its count
    // and the first image stops the stream.
    if (p_FIFO_DEPTH < 16) begin : g_chk_fifo_depth
        $error("cxp_app_stream: p_FIFO_DEPTH (=%0d) must be >= 16", p_FIFO_DEPTH);
    end

    //=======================================================================
    // Signals
    //=======================================================================

    // Image-header generator output
    logic [31:0] hdr_word_data;
    logic [3:0]  hdr_word_kmask;
    logic        hdr_word_valid;
    logic        hdr_word_ready;

    // Line-marker generator output
    logic [31:0] line_word_data;
    logic [3:0]  line_word_kmask;
    logic        line_word_valid;
    logic        line_word_ready;

    // Pixel-path skid register
    logic [31:0] pix_d_data_q;
    logic        pix_d_valid_q;
    logic        pix_d_eof_q;       // the held word is the image's last
    logic        pix_d_take;
    logic        pix_d_fire;

    // Priority merger output
    logic [31:0] merge_data;
    logic [3:0]  merge_kmask;
    logic        merge_valid;
    logic        merge_ready;
    logic        merge_eof;         // merged word is the image's last pixel word

    // DsizeP chopper
    logic [15:0] word_cnt_q;
    logic [15:0] word_cnt_n;
    logic [15:0] pkt_words;         // packet length the chopper counts to
    logic        chop_sop;
    logic        chop_eop;
    logic        chop_fire;

    // The image's metadata, latched at its frame-start pulse, and the one
    // the line markers read (live in the frame-start cycle, held after)
    cxp_pkg::cxp_meta_t frame_meta_q;
    cxp_pkg::cxp_meta_t line_meta;

    // Flush
    logic        drop_q;            // dropping until the next image starts
    logic        drop;              // drop this cycle

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    // Dropping: from the flush request until an image starts after it.
    // The image-start cycle itself still drops (the skid register may
    // hold a word of the dropped image); its header comes a cycle later.
    assign drop         = flush_i | drop_q;
    assign merge_ready  = drop | m_ready_i;

    assign pix_d_fire   = pix_d_valid_q & merge_ready
                          & ~hdr_word_valid & ~line_word_valid;
    assign pix_d_take   = pix_word_valid_i & (~pix_d_valid_q | pix_d_fire);

    assign pkt_words    = (cfg_dsizeP_i == 16'd0 || cfg_dsizeP_i > PKT_MAX) ? PKT_MAX
                                                                          : cfg_dsizeP_i;
    assign chop_sop     = (word_cnt_q == 16'h0000);
    // >=: a smaller size written mid-packet closes the packet at once.
    assign chop_eop     = (word_cnt_q >= pkt_words - 16'd1) | merge_eof;
    assign chop_fire    = merge_valid & merge_ready;

    // FIFO write side
    assign m_data_o     = merge_data;
    assign m_kmask_o    = merge_kmask;
    assign m_valid_o    = merge_valid & ~drop;
    assign m_sop_o      = chop_sop;
    assign m_eop_o      = chop_eop;
    assign m_streamid_o = frame_meta_q.streamid;

    //=======================================================================
    // Pixel-path skid register
    //=======================================================================

    // See header for the 1-cycle stand-off rationale; bypasses fully
    // in steady state.
    always_ff @(posedge app_clk or negedge app_rst_n) begin
        if (!app_rst_n) begin
            pix_d_valid_q <= 1'b0;
            pix_d_data_q  <= 32'h0000_0000;
            pix_d_eof_q   <= 1'b0;
        end else if (pix_d_take) begin
            pix_d_valid_q <= 1'b1;
            pix_d_data_q  <= pix_word_data_i;
            pix_d_eof_q   <= pix_word_eof_i;
        end else if (pix_d_fire) begin
            pix_d_valid_q <= 1'b0;
        end
    end

    // The image's metadata, held from its frame-start pulse (one cycle
    // ahead of its header) until the next image's.
    always_ff @(posedge app_clk or negedge app_rst_n) begin
        if (!app_rst_n)             frame_meta_q <= '0;
        else if (pix_frame_start_i) frame_meta_q <= meta_i;
    end

    assign line_meta = pix_frame_start_i ? meta_i : frame_meta_q;

    //=======================================================================
    // Priority merger (hdr > line-marker > skid-buffered pixel)
    //=======================================================================

    always_comb begin
        merge_data       = pix_d_data_q;
        merge_kmask      = KMASK_NONE;         // pixel words are pure data
        merge_valid      = pix_d_valid_q;
        merge_eof        = pix_d_eof_q;
        hdr_word_ready   = 1'b0;
        line_word_ready  = 1'b0;
        pix_word_ready_o = pix_d_take;

        if (hdr_word_valid) begin
            merge_data      = hdr_word_data;
            merge_kmask     = hdr_word_kmask;
            merge_valid     = 1'b1;
            merge_eof       = 1'b0;
            hdr_word_ready  = merge_ready;
            line_word_ready = 1'b0;
        end else if (line_word_valid) begin
            merge_data      = line_word_data;
            merge_kmask     = line_word_kmask;
            merge_valid     = 1'b1;
            merge_eof       = 1'b0;
            hdr_word_ready  = 1'b0;
            line_word_ready = merge_ready;
        end
    end

    //=======================================================================
    // DsizeP chopper
    //=======================================================================

    always_comb begin
        word_cnt_n = word_cnt_q;
        if (chop_fire) begin
            word_cnt_n = chop_eop ? 16'h0000 : (word_cnt_q + 16'd1);
        end
    end

    always_ff @(posedge app_clk or negedge app_rst_n) begin
        if (!app_rst_n) word_cnt_q <= 16'h0000;
        else if (drop)  word_cnt_q <= 16'h0000;       // next word kept opens a packet
        else            word_cnt_q <= word_cnt_n;
    end

    always_ff @(posedge app_clk or negedge app_rst_n) begin
        if (!app_rst_n) drop_q <= 1'b0;
        else            drop_q <= flush_i | (drop_q & ~pix_frame_start_i);
    end

    //=======================================================================
    // Image-header generator (fires on pix_frame_start_i)
    //=======================================================================

    cxp_app_image_header cxp_app_image_header_i (
        .app_clk          (app_clk),
        .app_rst_n        (app_rst_n),
        .meta_i           (meta_i),
        .meta_valid_i     (pix_frame_start_i),

        .m_word_data_o    (hdr_word_data),
        .m_word_kmask_o   (hdr_word_kmask),
        .m_word_valid_o   (hdr_word_valid),
        .m_word_ready_i   (hdr_word_ready)
    );

    //=======================================================================
    // Line-marker generator (fires on pix_line_start_i)
    //=======================================================================

    cxp_app_line_marker cxp_app_line_marker_i (
        .app_clk         (app_clk),
        .app_rst_n       (app_rst_n),
        .meta_i          (line_meta),
        .line_start_i    (pix_line_start_i),

        .m_word_data_o   (line_word_data),
        .m_word_kmask_o  (line_word_kmask),
        .m_word_valid_o  (line_word_valid),
        .m_word_ready_i  (line_word_ready)
    );

endmodule

`default_nettype wire
