/*
================================================================================
  cxp_app_domain
  CoaXPress 1.1.1 (CXP-001-2015) — everything on app_clk: the pixel
  sources, the acquisition gate, the packer and the stream words.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-27

    Description:
      One clock, app_clk.  The configuration and the acquisition events
      arrive already crossed by cxp_cdc_layer; the stream words leave into
      the stream FIFO's write side.

        cxp_app_tpg --,
                               +-> mux (cfg_i.use_tpg) -> cxp_app_acq_ctrl
        s_pix_* / s_meta_i  ---'   (pixel + metadata)     (image gate)
          cxp_app_pixel_ingress                                   |
                                                              v
                                                    cxp_app_pixel_packer
                                                              |  byte swap
                                                              v
                                                     cxp_app_stream -> m_*_o

      Pixel datapath (§2 pipeline): both pixel sources present a single-
      pixel-per-cycle stream; cfg_i.use_tpg selects the internal test
      pattern or the sensor bus (via cxp_app_pixel_ingress).  The packer packs
      it into 32-bit P0..P3 words for the image's pixel format (Mono8 ..
      Mono16); its word is byte-reversed so the first-transmitted pixel
      sits in word[7:0].

      Acquisition (§11.2.1.4/5): cxp_app_acq_ctrl arms on acq_start_i and
      disarms on acq_stop_i (cfg_i.run holds it armed).  It is the one gate
      every pixel of either source passes, a whole image at a time: an
      image enters only if it starts while the device is armed and a
      stream packet fits (cfg_i.stream_en), and an image that entered
      always completes.  The test pattern also stops itself while no image
      would enter.

      Metadata: the sensor's is the copy cxp_app_pixel_ingress took with the
      image's first pixel; the test pattern's is latched at its image
      start, with StreamID / TapG / Flags from the registers.  PixelFormat
      (cfg_i.pixfmt) overrides the sensor's format when non-zero.  The
      packer latches the selected bundle with the SOF pixel and the image
      header carries that copy with the format the packer packs.

      Sensor pixels are p_PIX_W-bit samples, LSB-justified on the 16-bit
      port; the packer MSB-aligns them into the format (§9.4.2, Figure
      32): a wider format zero-fills the LSBs, a narrower one keeps the
      sample's MSBs.  The test pattern's values are taken as they are.
      s_meta_i.pixfmt is overridden whenever the PixelFormat register is
      non-zero, which cxp_device_top always makes it.

    Versions:
        2026-09-27 - 0.1:   - The app_clk part of cxp_interface_top,
                              unchanged
        2026-10-04 - 0.2:   - Sensor samples MSB-aligned into the format

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_app_domain #(
    parameter int p_TPG_X_SIZE = 64,                            // TPG max line width (px)
    parameter int p_TPG_Y_SIZE = 32,                            // TPG max frame height (lines)
    parameter int p_FIFO_DEPTH = 1024,                          // stream FIFO depth (words)
    parameter int p_PIX_W      = 16                             // sensor pixel width
) (
    input  wire  logic        app_clk,                          // application / pixel clock
    input  wire  logic        app_rst_n,                        // app_clk async reset, active-low

    // Configuration and acquisition events, on app_clk
    input  wire  cxp_pkg::cxp_cfg_app_t cfg_i,                  // pixel-path configuration
    input  wire  logic        acq_start_i,                      // AcquisitionStart (pulse)
    input  wire  logic        acq_stop_i,                       // AcquisitionStop (pulse)

    // Sensor single-pixel input (cfg_i.use_tpg = 0)
    input  wire  logic [15:0] s_pix_data_i,                     // pixel, LSB-justified
    input  wire  logic        s_pix_valid_i,                    // pixel valid
    input  wire  logic        s_pix_sof_i,                      // start of frame
    input  wire  logic        s_pix_eol_i,                      // end of line
    input  wire  logic        s_pix_eof_i,                      // end of frame
    output logic              s_pix_ready_o,                    // ingress ready
    input  wire  cxp_pkg::cxp_meta_t s_meta_i,                  // sensor frame metadata
    output logic              pix_restart_o,                    // sof inside an image
    output logic              pix_stray_eof_o,                  // eof outside an image

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
    import cxp_util_pkg::*;

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    // The sensor port is 16 bits wide.
    if (p_PIX_W < 1 || p_PIX_W > 16) begin : g_chk_pix_w
        $error("cxp_app_domain: p_PIX_W (=%0d) must be 1..16", p_PIX_W);
    end

    //=======================================================================
    // Signals
    //=======================================================================

    // Single-pixel streams: TPG, sensor ingress, and the one selected
    cxp_pix_t           tpg_pix,   ing_pix,   sel_pix;
    logic               tpg_pix_ready, ing_pix_ready, sel_pix_ready;
    logic [p_PIX_W-1:0] ing_pix_data;

    // Frame metadata: TPG, sensor (latched by the ingress at the image's
    // first pixel), and the one selected
    cxp_meta_t          tpg_meta,  ing_meta,  sel_meta;
    cxp_meta_t          pk_meta;            // metadata of the image the packer packs
    logic               ing_meta_valid;     // unconsumed (waived in cxp_ip.vlt)
    // Pixels leaving the acquisition gate for the packer
    cxp_pix_t           gate_pix;
    logic               gate_pix_ready;
    logic [15:0]        pk_pixfmt;          // format the packer packs the image with
    cxp_meta_t          img_meta;           // metadata the image header carries
    logic               acq_active;         // a new TPG image may start

    // Packer output
    logic [31:0] pk_word_data;
    logic [3:0]  pk_word_lane_vld;
    logic        pk_word_valid;
    logic        pk_word_sol;
    logic        pk_word_eol;
    logic        pk_word_sof;
    logic        pk_word_eof;
    logic        pk_word_ready;

    logic [31:0] sel_word_data;
    logic        sel_frame_start;
    logic        sel_line_start;

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    // Byte-reverse so the first-transmitted pixel lands in word[7:0]
    // (the endianness the downstream pipeline / CRC assume).
    assign sel_word_data    = bswap32(pk_word_data);
    assign sel_frame_start  = pk_word_valid & pk_word_sof & pk_word_ready;
    assign sel_line_start   = pk_word_valid & pk_word_sol & pk_word_ready;

    assign ing_pix.data = 16'(ing_pix_data);

    //=======================================================================
    // TPG / external pixel mux
    //=======================================================================

    // cfg_i.use_tpg picks the single-pixel source and the matching
    // metadata bundle.
    always_comb begin
        sel_pix       = cfg_i.use_tpg ? tpg_pix  : ing_pix;
        sel_meta      = cfg_i.use_tpg ? tpg_meta : ing_meta;
        tpg_pix_ready = cfg_i.use_tpg & sel_pix_ready;
        ing_pix_ready = ~cfg_i.use_tpg & sel_pix_ready;

        sel_meta.arbitrary = cfg_i.arbitrary;
        // Host PixelFormat register overrides the sensor's format when
        // non-zero; the test pattern latched it at its own image start
        // (and the packer latches whatever it gets at the first pixel).
        if (!cfg_i.use_tpg && cfg_i.pixfmt != 16'h0)
            sel_meta.pixfmt = cfg_i.pixfmt;
    end

    // The image header carries the metadata and the format the packer
    // latched with the image's first pixel, whatever the source's ports or
    // the registers do meanwhile.
    always_comb begin
        img_meta        = pk_meta;
        img_meta.pixfmt = pk_pixfmt;
    end

    //=======================================================================
    // Acquisition control: which images enter
    //=======================================================================

    cxp_app_acq_ctrl cxp_app_acq_ctrl_i (
        .clk           (app_clk),
        .rst_n         (app_rst_n),
        .acq_start_i   (acq_start_i),
        .acq_stop_i    (acq_stop_i),
        .acq_mode_i    (cfg_i.acq_mode),
        .frame_count_i (cfg_i.acq_frames),
        .run_i         (cfg_i.run),
        .stream_en_i   (cfg_i.stream_en),
        .src_gated_i   (cfg_i.use_tpg),
        .active_o      (acq_active),
        .s_pix_i       (sel_pix),
        .s_pix_ready_o (sel_pix_ready),
        .m_pix_o       (gate_pix),
        .m_pix_ready_i (gate_pix_ready)
    );

    //=======================================================================
    // Internal test-pattern generator
    //=======================================================================

    cxp_app_tpg #(
        .p_X_SIZE    (p_TPG_X_SIZE),
        .p_Y_SIZE    (p_TPG_Y_SIZE)
    ) cxp_app_tpg_i (
        .app_clk          (app_clk),
        .app_rst_n        (app_rst_n),
        .cfg_run_i        (acq_active & cfg_i.use_tpg),
        .cfg_xsize_i      (cfg_i.xsize),
        .cfg_ysize_i      (cfg_i.ysize),
        .cfg_pixfmt_i     (cfg_i.pixfmt),
        .cfg_testpat_i    (cfg_i.testpat),
        .cfg_xoffs_i      (cfg_i.xoffs),
        .cfg_yoffs_i      (cfg_i.yoffs),
        .cfg_srctag_i     (cfg_i.srctag),
        .cfg_streamid_i   (cfg_i.streamid),
        .cfg_tapg_i       (cfg_i.tapg),
        .cfg_flags_i      (cfg_i.flags),

        .m_pix_data_o     (tpg_pix.data),
        .m_pix_valid_o    (tpg_pix.valid),
        .m_pix_ready_i    (tpg_pix_ready),
        .m_pix_sof_o      (tpg_pix.sof),
        .m_pix_sol_o      (tpg_pix.sol),
        .m_pix_eol_o      (tpg_pix.eol),
        .m_pix_eof_o      (tpg_pix.eof),

        .meta_o           (tpg_meta)
    );

    //=======================================================================
    // External sensor ingress adapter
    //=======================================================================

    cxp_app_pixel_ingress #(
        .p_PIX_W (p_PIX_W)
    ) cxp_app_pixel_ingress_i (
        .app_clk         (app_clk),
        .app_rst_n       (app_rst_n),

        .s_pix_data_i    (s_pix_data_i[p_PIX_W-1:0]),
        .s_pix_valid_i   (s_pix_valid_i & ~cfg_i.use_tpg),
        .s_pix_sof_i     (s_pix_sof_i),
        .s_pix_eol_i     (s_pix_eol_i),
        .s_pix_eof_i     (s_pix_eof_i),
        .s_pix_ready_o   (s_pix_ready_o),
        .s_meta_i        (s_meta_i),

        .m_pix_data_o    (ing_pix_data),
        .m_pix_valid_o   (ing_pix.valid),
        .m_pix_sol_o     (ing_pix.sol),
        .m_pix_eol_o     (ing_pix.eol),
        .m_pix_sof_o     (ing_pix.sof),
        .m_pix_eof_o     (ing_pix.eof),
        .m_pix_ready_i   (ing_pix_ready),
        .m_meta_o        (ing_meta),
        .m_meta_valid_o  (ing_meta_valid),

        .spurious_eof_o  (pix_stray_eof_o),
        .sof_restart_o   (pix_restart_o)
    );

    //=======================================================================
    // Pixel packer
    //=======================================================================

    cxp_app_pixel_packer cxp_app_pixel_packer_i (
        .app_clk           (app_clk),
        .app_rst_n         (app_rst_n),
        .cfg_pixfmt_i      (sel_meta.pixfmt),
        .s_pix_w_i         (cfg_i.use_tpg ? 5'd0 : 5'(p_PIX_W)),

        .s_pix_data_i      (gate_pix.data),
        .s_pix_valid_i     (gate_pix.valid),
        .s_pix_sol_i       (gate_pix.sol),
        .s_pix_eol_i       (gate_pix.eol),
        .s_pix_sof_i       (gate_pix.sof),
        .s_pix_eof_i       (gate_pix.eof),
        .s_pix_ready_o     (gate_pix_ready),

        .m_word_data_o     (pk_word_data),
        .m_word_lane_vld_o (pk_word_lane_vld),
        .m_word_valid_o    (pk_word_valid),
        .m_word_sol_o      (pk_word_sol),
        .m_word_eol_o      (pk_word_eol),
        .m_word_sof_o      (pk_word_sof),
        .m_word_eof_o      (pk_word_eof),
        .m_word_ready_i    (pk_word_ready),
        .m_pixfmt_o        (pk_pixfmt),
        .s_meta_i          (sel_meta),
        .m_meta_o          (pk_meta)
    );

    //=======================================================================
    // Stream words: image header, line markers, pixel words, packet cuts
    //=======================================================================

    cxp_app_stream #(
        .p_FIFO_DEPTH (p_FIFO_DEPTH)
    ) cxp_app_stream_i (
        .app_clk           (app_clk),
        .app_rst_n         (app_rst_n),
        .cfg_dsizeP_i      (cfg_i.dsizeP),

        .pix_word_data_i   (sel_word_data),
        .pix_word_valid_i  (pk_word_valid),
        .pix_word_eof_i    (pk_word_eof),
        .pix_word_ready_o  (pk_word_ready),
        .pix_frame_start_i (sel_frame_start),
        .pix_line_start_i  (sel_line_start),

        .meta_i            (img_meta),

        .m_data_o          (m_data_o),
        .m_kmask_o         (m_kmask_o),
        .m_valid_o         (m_valid_o),
        .m_sop_o           (m_sop_o),
        .m_eop_o           (m_eop_o),
        .m_streamid_o      (m_streamid_o),
        .m_ready_i         (m_ready_i),
        .flush_i           (flush_i)
    );

endmodule

`default_nettype wire
