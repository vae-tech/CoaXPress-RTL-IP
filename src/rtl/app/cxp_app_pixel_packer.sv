/*
================================================================================
  cxp_app_pixel_packer
  CoaXPress 1.1.1 — pixel packer (§9.4.2, Figures 27-31).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2025-05-27

    Description:
      Packs a single-pixel-per-cycle input stream into 32-bit packed words
      matching the on-the-wire byte layout the CXP transmitter expects.  The
      first pixel of every line lands in the MSB byte (P0); no cross-line
      packing — at end-of-line the packer flushes the partial word and
      reports the touched-byte mask via `m_word_lane_vld_o`.

      Supported pixel formats (PixelF code, Table 25):
        0x0101 Mono8   — 8 bpp,  4 px / 32-bit word                 (Figure 27)
        0x0102 Mono10  — 10 bpp, 4 px in 5 bytes                    (Figure 28)
        0x0103 Mono12  — 12 bpp, 2 px in 3 bytes                    (Figure 29)
        0x0104 Mono14  — 14 bpp, 4 px in 7 bytes                    (Figure 30)
        0x0105 Mono16  — 16 bpp, 2 px / 32-bit word                 (Figure 31)
      Any other code is packed as Mono8.

      Byte ordering on the 32-bit word:
        bits [31:24] = byte 0 = P0      (MSB / lane 0)
        bits [23:16] = byte 1 = P1      (lane 1)
        bits [15: 8] = byte 2 = P2      (lane 2)
        bits [ 7: 0] = byte 3 = P3      (LSB / lane 3)

      A 64-bit shift accumulator holds pending bits MSB-first.  `fill_q`
      tracks how many valid bits sit at the top of the accumulator.  Each
      accepted pixel inserts `cbits` bits and bumps fill_q.  When fill_q
      reaches 32 the top 32 bits are emitted as a full word; at EOL with
      non-zero residual fill a partial flush word is emitted instead.

      Cycle-budget corner case ("case B"): one pixel can leave both a
      full-word emit AND a partial residual to flush in the same cycle
      (Mono10 with EOL on the 4th pixel — 40 bits → 1 full word + 8
      leftover).  The leftover is queued in `flush_pend_q` and drained the
      following cycle; `s_pix_ready_o` deasserts so the input stalls for
      one cycle.

      `cfg_pixfmt_i` is sampled on the SOF pixel of every frame and latched
      for the duration of that frame.  Within a frame, changing the
      pixfmt has no effect.  `m_pixfmt_o` is that latched format, for the
      image header of the frame.

      A SOF pixel starts on an empty accumulator: bits of a line that was
      cut off without its EOL (a truncated frame) are dropped, so the new
      frame's first word holds only its own pixels (§9.4.2).

      Pixel value and format width (`s_pix_w_i`, latched with the SOF
      pixel like the format):
        0      the pixel is LSB-justified at the format's width and taken
               as is (the test pattern's values);
        1..16  the pixel is a sensor sample of that many bits,
               LSB-justified.  It is MSB-aligned into the format (§9.4.2,
               Figure 32): a format wider than the sample gets zero LSBs,
               a narrower one keeps the sample's most significant bits
               (Mono8 from a 12-bit sensor sends bits [11:4]).

    Versions:
        2025-05-27 - 0.1:   - Init
        2026-09-19 - 0.2:   - Mono14 packed densely; Mono16 = 0x0105
        2026-09-25 - 0.3:   - SOF clears the accumulator; m_pixfmt_o
        2026-10-04 - 0.4:   - s_pix_w_i: sensor samples MSB-aligned into
                              the format (Figure 32)

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_app_pixel_packer (
    input  wire  logic        app_clk,                 // application clock
    input  wire  logic        app_rst_n,               // sync-deassert reset

    // Configuration — sampled on each SOF pixel and held for the frame.
    input  wire  logic [15:0] cfg_pixfmt_i,            // GenICam pixel format

    // Pixel input — single pixel per cycle, LSB-justified.
    input  wire  logic [15:0] s_pix_data_i,            // pixel value
    input  wire  logic [4:0]  s_pix_w_i,               // sample bits, 0 = format's (see above)
    input  wire  logic        s_pix_valid_i,           // pixel beat valid
    input  wire  logic        s_pix_sol_i,             // start of line
    input  wire  logic        s_pix_eol_i,             // end of line
    input  wire  logic        s_pix_sof_i,             // start of frame
    input  wire  logic        s_pix_eof_i,             // end of frame
    output logic              s_pix_ready_o,           // packer accepts pixel

    // Packed 32-bit word output.
    output logic [31:0]       m_word_data_o,           // packed 32-bit word
    output logic [3:0]        m_word_lane_vld_o,       // [0]=P0..[3]=P3 valid
    output logic              m_word_valid_o,          // word beat valid
    output logic              m_word_sol_o,            // start of line
    output logic              m_word_eol_o,            // end of line
    output logic              m_word_sof_o,            // start of frame
    output logic              m_word_eof_o,            // end of frame
    input  wire  logic        m_word_ready_i,          // downstream accepts word
    output logic [15:0]       m_pixfmt_o,              // format of the frame being packed

    // Image metadata: taken with the SOF pixel, held for the frame being
    // packed (the stream path reads it with the frame's first word).
    input  wire  cxp_pkg::cxp_meta_t s_meta_i,          // metadata at the SOF pixel
    output cxp_pkg::cxp_meta_t       m_meta_o           // metadata of the frame being packed
);

    import cxp_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    // ACC_W=64 covers worst-case pre-emit fill of 31 (residual) + 16 (max
    // cbits) = 47 bits.
    localparam int ACC_W = 64;

    // ------ Types ------

    typedef enum logic [2:0] {
        PF_MONO8  = 3'd0,
        PF_MONO10 = 3'd1,
        PF_MONO12 = 3'd2,
        PF_MONO14 = 3'd3,
        PF_MONO16 = 3'd4
    } pixfmt_e;

    //=======================================================================
    // Signals
    //=======================================================================

    // Pixfmt latch — captured on SOF pixel, used for the rest of the frame.
    // SOF pixel itself uses live cfg_pixfmt_i (combinational override).
    logic [15:0]      pixfmt_latched_q;
    logic [4:0]       pix_w_latched_q;   // s_pix_w_i of the frame
    logic [4:0]       pix_w_eff;
    logic [15:0]      sample_msb;        // sensor sample MSB-aligned in 16 bits
    logic [15:0]      keep_mask;         // the format's cbits at the top
    cxp_meta_t        meta_latched_q;    // taken with the SOF pixel
    logic [15:0]      pixfmt_eff;
    pixfmt_e          fmt;
    logic [5:0]       cbits;             // container bits in packed stream
    logic [15:0]      container_msb;     // pixel bits MSB-aligned in 16-bit

    // Shift accumulator.
    logic [ACC_W-1:0] acc_q;
    logic [5:0]       fill_q;
    logic [ACC_W-1:0] acc_base;        // accumulator the pixel lands in (empty at SOF)
    logic [5:0]       fill_base;

    // Output skid register (1-deep, registered).
    logic [31:0]      m_word_data_q;
    logic [3:0]       m_word_lane_vld_q;
    logic             m_word_valid_q;
    logic             m_word_sol_q;
    logic             m_word_eol_q;
    logic             m_word_sof_q;
    logic             m_word_eof_q;

    // Pending-flush register (case B).
    logic             flush_pend_q;
    logic [31:0]      flush_data_q;
    logic [3:0]       flush_lane_vld_q;
    logic             flush_eol_q;
    logic             flush_eof_q;

    // SOL / SOF carry — strobe latched if no emit fires this cycle.
    logic             sol_pending_q;
    logic             sof_pending_q;

    // Handshake helpers.
    logic             output_consumed;
    logic             output_slot_free;
    logic             accept_pix;

    // Combinational pre-emit calculations.
    logic [ACC_W-1:0] pix_shifted;
    logic [ACC_W-1:0] acc_post;
    logic [5:0]       fill_post;
    logic [ACC_W-1:0] acc_after_full;
    logic [5:0]       fill_after_full;
    logic [31:0]      emit_full_data;
    logic [31:0]      partial_data;
    logic [5:0]       partial_fill;
    logic [3:0]       partial_lvld;
    logic             do_emit_full;
    logic             do_residual;
    logic             do_emit_partial;
    logic             eff_sol;
    logic             eff_sof;

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign pixfmt_eff       = (s_pix_valid_i & s_pix_sof_i) ? cfg_pixfmt_i : pixfmt_latched_q;
    assign pix_w_eff        = (s_pix_valid_i & s_pix_sof_i) ? s_pix_w_i    : pix_w_latched_q;

    assign m_pixfmt_o        = pixfmt_latched_q;
    assign m_meta_o          = meta_latched_q;
    assign m_word_data_o     = m_word_data_q;
    assign m_word_lane_vld_o = m_word_lane_vld_q;
    assign m_word_valid_o    = m_word_valid_q;
    assign m_word_sol_o      = m_word_sol_q;
    assign m_word_eol_o      = m_word_eol_q;
    assign m_word_sof_o      = m_word_sof_q;
    assign m_word_eof_o      = m_word_eof_q;

    assign output_consumed  = m_word_valid_q & m_word_ready_i;
    assign output_slot_free = ~m_word_valid_q | output_consumed;
    assign s_pix_ready_o    =  output_slot_free & ~flush_pend_q;
    assign accept_pix       =  s_pix_valid_i & s_pix_ready_o;

    //=======================================================================
    // Pixel format decode → cbits and MSB-aligned container.
    //=======================================================================

    always_comb begin
        unique case (pixfmt_eff)
            PIXFMT_MONO8:  fmt = PF_MONO8;
            PIXFMT_MONO10: fmt = PF_MONO10;
            PIXFMT_MONO12: fmt = PF_MONO12;
            PIXFMT_MONO14: fmt = PF_MONO14;
            PIXFMT_MONO16: fmt = PF_MONO16;
            default:  fmt = PF_MONO8;
        endcase

        unique case (fmt)
            PF_MONO8:  cbits = 6'd8;
            PF_MONO10: cbits = 6'd10;
            PF_MONO12: cbits = 6'd12;
            PF_MONO14: cbits = 6'd14;
            PF_MONO16: cbits = 6'd16;
            default:   cbits = 6'd8;
        endcase

        // A sensor sample, MSB-aligned; the format keeps its top cbits.
        sample_msb = (pix_w_eff == 5'd0 || pix_w_eff > 5'd16) ? s_pix_data_i
                   : s_pix_data_i << (5'd16 - pix_w_eff);
        keep_mask  = ~(16'hFFFF >> cbits);

        if (pix_w_eff != 5'd0) begin
            container_msb = sample_msb & keep_mask;
        end else begin
            unique case (fmt)
                PF_MONO8:  container_msb = {s_pix_data_i[ 7:0],  8'h00};
                PF_MONO10: container_msb = {s_pix_data_i[ 9:0],  6'b00_0000};
                PF_MONO12: container_msb = {s_pix_data_i[11:0],  4'h0};
                PF_MONO14: container_msb = {s_pix_data_i[13:0],  2'b00};
                PF_MONO16: container_msb =  s_pix_data_i;
                default:   container_msb = {s_pix_data_i[ 7:0],  8'h00};
            endcase
        end
    end

    //=======================================================================
    // Pre-emit combinational packing.
    //=======================================================================

    always_comb begin
        // Place the new pixel's container bits at position [63-fill_q -: 16].
        acc_base    = s_pix_sof_i ? '0   : acc_q;
        fill_base   = s_pix_sof_i ? 6'd0 : fill_q;
        pix_shifted = {{(ACC_W-16){1'b0}}, container_msb} << (6'(ACC_W - 16) - fill_base);
        acc_post    = acc_base | pix_shifted;
        fill_post   = fill_base + cbits;

        // Full-word emit (top 32 bits of acc_post).
        emit_full_data  = acc_post[ACC_W-1 -: 32];
        acc_after_full  = acc_post << 32;
        fill_after_full = fill_post - 6'd32;

        // Partial-word emit:
        //   - case D: emit acc_post's top 32 bits, mask = bytes(fill_post)
        //   - case B: after the full-word emit, emit the leftover top 32 bits
        //             of acc_after_full, mask = bytes(fill_after_full)
        if (fill_post >= 6'd32) begin
            partial_data = acc_after_full[ACC_W-1 -: 32];
            partial_fill = fill_after_full;
        end else begin
            partial_data = acc_post[ACC_W-1 -: 32];
            partial_fill = fill_post;
        end

        partial_lvld[0] = (partial_fill >  6'd0);
        partial_lvld[1] = (partial_fill >  6'd8);
        partial_lvld[2] = (partial_fill > 6'd16);
        partial_lvld[3] = (partial_fill > 6'd24);

        // Case classification (only valid when accept_pix=1):
        //   - do_emit_full     = produces a full word from this pixel
        //   - do_residual      = case B: full word + queued flush
        //   - do_emit_partial  = case D: partial flush only (no full word)
        do_emit_full    = (fill_post >= 6'd32);
        do_residual     = do_emit_full     & s_pix_eol_i & (fill_after_full != 6'd0);
        do_emit_partial = ~do_emit_full    & s_pix_eol_i & (fill_post       != 6'd0);

        eff_sol = sol_pending_q | s_pix_sol_i;
        eff_sof = sof_pending_q | s_pix_sof_i;
    end

    //=======================================================================
    // Sequential.
    //=======================================================================

    always_ff @(posedge app_clk or negedge app_rst_n) begin
        if (!app_rst_n) begin
            acc_q             <= '0;
            fill_q            <= 6'd0;
            pixfmt_latched_q  <= PIXFMT_MONO8;
            pix_w_latched_q   <= 5'd0;
            m_word_data_q     <= 32'h0000_0000;
            m_word_lane_vld_q <= 4'b0000;
            m_word_valid_q    <= 1'b0;
            m_word_sol_q      <= 1'b0;
            m_word_eol_q      <= 1'b0;
            m_word_sof_q      <= 1'b0;
            m_word_eof_q      <= 1'b0;
            flush_pend_q      <= 1'b0;
            flush_data_q      <= 32'h0000_0000;
            flush_lane_vld_q  <= 4'b0000;
            flush_eol_q       <= 1'b0;
            flush_eof_q       <= 1'b0;
            sol_pending_q     <= 1'b0;
            sof_pending_q     <= 1'b0;
        end else begin
            // Drain the output register first (may be overridden below).
            if (output_consumed) m_word_valid_q <= 1'b0;

            if (output_slot_free) begin
                if (flush_pend_q) begin
                    // Drain the queued flush word.
                    m_word_data_q     <= flush_data_q;
                    m_word_lane_vld_q <= flush_lane_vld_q;
                    m_word_valid_q    <= 1'b1;
                    m_word_sol_q      <= 1'b0;   // sol/sof rode on the prior emit
                    m_word_eol_q      <= flush_eol_q;
                    m_word_sof_q      <= 1'b0;
                    m_word_eof_q      <= flush_eof_q;
                    flush_pend_q      <= 1'b0;
                end else if (accept_pix) begin
                    // Latch cfg_pixfmt_i on the SOF pixel.
                    if (s_pix_sof_i) pixfmt_latched_q <= cfg_pixfmt_i;
                    if (s_pix_sof_i) pix_w_latched_q  <= s_pix_w_i;
                    if (s_pix_sof_i) meta_latched_q   <= s_meta_i;

                    if (do_emit_full) begin
                        // Case A / B / C : emit a full 32-bit word.
                        m_word_data_q     <= emit_full_data;
                        m_word_lane_vld_q <= 4'b1111;
                        m_word_valid_q    <= 1'b1;
                        m_word_sol_q      <= eff_sol;
                        m_word_sof_q      <= eff_sof;
                        sol_pending_q     <= 1'b0;
                        sof_pending_q     <= 1'b0;

                        if (do_residual) begin
                            // Case B: queue the residual partial word.
                            m_word_eol_q     <= 1'b0;
                            m_word_eof_q     <= 1'b0;
                            flush_pend_q     <= 1'b1;
                            flush_data_q     <= partial_data;
                            flush_lane_vld_q <= partial_lvld;
                            flush_eol_q      <= 1'b1;
                            flush_eof_q      <= s_pix_eof_i;
                            acc_q            <= '0;
                            fill_q           <= 6'd0;
                        end else if (s_pix_eol_i) begin
                            // Case A: full emit covers the line exactly.
                            m_word_eol_q     <= 1'b1;
                            m_word_eof_q     <= s_pix_eof_i;
                            acc_q            <= '0;
                            fill_q           <= 6'd0;
                        end else begin
                            // Case C: keep residual for next pixels.
                            m_word_eol_q     <= 1'b0;
                            m_word_eof_q     <= 1'b0;
                            acc_q            <= acc_after_full;
                            fill_q           <= fill_after_full;
                        end
                    end else if (do_emit_partial) begin
                        // Case D : partial flush at EOL, no full word.
                        m_word_data_q     <= partial_data;
                        m_word_lane_vld_q <= partial_lvld;
                        m_word_valid_q    <= 1'b1;
                        m_word_sol_q      <= eff_sol;
                        m_word_sof_q      <= eff_sof;
                        m_word_eol_q      <= 1'b1;
                        m_word_eof_q      <= s_pix_eof_i;
                        sol_pending_q     <= 1'b0;
                        sof_pending_q     <= 1'b0;
                        acc_q             <= '0;
                        fill_q            <= 6'd0;
                    end else begin
                        // Case E : accumulate, no emit.
                        acc_q  <= acc_post;
                        fill_q <= fill_post;
                        if (s_pix_sol_i) sol_pending_q <= 1'b1;
                        if (s_pix_sof_i) sof_pending_q <= 1'b1;
                    end
                end
            end
        end
    end

endmodule

`default_nettype wire
