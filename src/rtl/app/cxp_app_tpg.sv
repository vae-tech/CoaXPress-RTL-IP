/*
================================================================================
  cxp_app_tpg
  Synthesisable test-pattern generator for the CoaXPress device-side IP.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2025-05-27

    Description:
      Single-pixel source feeding cxp_app_pixel_packer on an AXI4-Stream-like bus
      (`m_pix_*`).  Keeps the test-pattern path exercising the packer for
      every supported pixel format instead of hard-coding the legacy Mono8
      4-px/word layout.

      Pattern (`cfg_testpat_i`, latched at frame start)
        0  TPG_GRADIENT — (x+y) & 0xFF; gradient is intentionally mismatched
           against the Python reference's `x & 0xFF` (see memory entry).
        1  TPG_BARS     — exactly eight equal-width vertical bars (each
           xsize/8 wide) alternating 0x00 / 0xFF.
        2  TPG_FLAT     — uniform field stepping (frame_count*16) & 0xFF.
        3  TPG_GREYBARS — eight bands stepping through a graduated grey ramp.

      Frame size: `p_X_SIZE` / `p_Y_SIZE` are the *maximum* dimensions; active
      geometry is the run-time `cfg_xsize_i` / `cfg_ysize_i` pair, latched at
      each frame start so a host write takes effect on the next frame.

      Run control: `cfg_run_i` is a level signal; deassert to stop on EOF.

      Frame metadata: `meta_o` (cxp_meta_t, rectangular form) carries,
      latched at each frame start, the geometry, pixel format, offsets and
      SourceTag, and live from the registers the StreamID
      (Image1StreamID), TapG (TapGeometry) and Flags (StreamFlags); the
      packer takes the whole bundle with the image's first pixel.

      SourceTag (Table 38): incremented for each image, wrapping at
      0xFFFF; the first image after reset carries 0.  A change of
      `cfg_srctag_i` (the host SourceTag register) presets the tag of the
      next image to the new value.

    Versions:
        2025-05-27 - 0.1:   - Init
        2026-09-19 - 0.2:   - Metadata as one cxp_meta_t output
        2026-09-22 - 0.3:   - Run-time offsets; SourceTag counts images
        2026-09-27 - 0.4:   - StreamID, TapG and Flags from the registers
                              (inputs), not parameters

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_app_tpg #(
    parameter int p_X_SIZE   = 64,                      // MAX pixels per line
    parameter int p_Y_SIZE   = 32,                      // MAX lines per frame
    parameter logic [15:0] p_PIXFMT    = cxp_pkg::PIXFMT_MONO8  // format while cfg_pixfmt_i = 0
) (
    input  wire  logic        app_clk,                  // application clock
    input  wire  logic        app_rst_n,                // sync-deassert reset

    input  wire  logic        cfg_run_i,                // 1 = free-run, 0 = stop after EOF

    input  wire  logic [15:0] cfg_xsize_i,              // active line width (0 → max)
    input  wire  logic [15:0] cfg_ysize_i,              // active line count (0 → max)
    input  wire  logic [15:0] cfg_pixfmt_i,             // run-time pixel format (0 → param)
    input  wire  logic [1:0]  cfg_testpat_i,            // 0=grad 1=bars 2=flat 3=grey
    input  wire  logic [15:0] cfg_xoffs_i,              // image X offset (OffsetX)
    input  wire  logic [15:0] cfg_yoffs_i,              // image Y offset (OffsetY)
    input  wire  logic [15:0] cfg_srctag_i,             // SourceTag preset (on change)
    input  wire  logic [7:0]  cfg_streamid_i,           // StreamID (Image1StreamID)
    input  wire  logic [15:0] cfg_tapg_i,               // TapG (TapGeometry)
    input  wire  logic [7:0]  cfg_flags_i,              // Flags (StreamFlags)

    // Single-pixel stream out (to cxp_app_pixel_packer).
    output logic [15:0]       m_pix_data_o,             // pixel value (zero-ext 16-bit)
    output logic              m_pix_valid_o,            // pixel beat valid
    input  wire  logic        m_pix_ready_i,            // downstream accepts pixel
    output logic              m_pix_sof_o,              // start of frame
    output logic              m_pix_sol_o,              // start of line
    output logic              m_pix_eol_o,              // end of line
    output logic              m_pix_eof_o,              // end of frame

    // Frame metadata (latched geometry, live StreamID / TapG / Flags).
    output cxp_pkg::cxp_meta_t meta_o                   // rectangular-form metadata
);

    import cxp_pkg::*;
    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    localparam int X_IDX_W = idx_w(p_X_SIZE);
    localparam int Y_IDX_W = idx_w(p_Y_SIZE);

    // TestPattern selector codes — the host TestPattern register (0x1001C)
    // and the GenICam `TestPattern` enum in cxp_camera.xml.
    localparam logic [1:0] TPG_GRADIENT = 2'd0;
    localparam logic [1:0] TPG_BARS     = 2'd1;
    localparam logic [1:0] TPG_FLAT     = 2'd2;
    localparam logic [1:0] TPG_GREYBARS = 2'd3;

    // ------ Types ------

    // FSM
    //   ST_IDLE — waiting for cfg_run_i to start a new frame.
    //   ST_EMIT — streaming pixels with sof/sol/eol/eof flags.
    typedef enum logic {
        ST_IDLE,
        ST_EMIT
    } state_t;

    //=======================================================================
    // Signals
    //=======================================================================

    logic [15:0]         pixfmt_eff;       // run-time or compile-time pixfmt
    logic [15:0]         pixfmt_q;         // latched format for current frame
    logic [15:0]         xsize_eff;        // clamped run-time X size
    logic [15:0]         ysize_eff;        // clamped run-time Y size
    logic [15:0]         xsize_q, ysize_q; // latched geometry for current frame
    logic [1:0]          testpat_q;        // latched pattern select
    logic [7:0]          frame_cnt_q;      // completed-frame counter (FLAT)
    logic [15:0]         xoffs_q, yoffs_q; // latched offsets
    logic [15:0]         srctag_q;         // SourceTag of the current image
    logic [15:0]         srctag_next_q;    // SourceTag of the next image
    logic [15:0]         srctag_cfg_q;     // cfg_srctag_i one cycle ago
    logic                srctag_set;       // cfg_srctag_i changed

    state_t              state_q, state_n;
    logic [X_IDX_W-1:0]  x_q, x_n;
    logic [Y_IDX_W-1:0]  y_q, y_n;

    logic                last_pix_in_line;
    logic                last_pix_in_frame;
    logic                load_size;        // latch geometry on frame start
    logic                frame_done;       // EOF handshake pulse

    logic [2:0]          band_idx;         // 0..7 column band
    logic [7:0]          greybar_val;      // grey level for current band
    logic [15:0]         pix_val;          // pattern pixel

    //=======================================================================
    // Compile-time parameter checks.
    //=======================================================================

    if (p_X_SIZE <= 0) begin : g_chk_x_size
        $error("cxp_app_tpg: p_X_SIZE (%0d) must be positive", p_X_SIZE);
    end
    if (p_Y_SIZE <= 0) begin : g_chk_y_size
        $error("cxp_app_tpg: p_Y_SIZE (%0d) must be positive", p_Y_SIZE);
    end

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    // Run-time format with fallback to compile-time default.
    assign pixfmt_eff = (cfg_pixfmt_i != 16'h0) ? cfg_pixfmt_i : p_PIXFMT;

    assign last_pix_in_line  = (x_q == X_IDX_W'(xsize_q - 16'd1));
    assign last_pix_in_frame = last_pix_in_line &&
                               (y_q == Y_IDX_W'(ysize_q - 16'd1));

    // Latch the effective geometry on every frame start: leaving ST_IDLE
    // with cfg_run, or rolling straight into the next free-run frame.
    assign load_size = ((state_q == ST_IDLE) && cfg_run_i)
                     || ((state_q == ST_EMIT) && m_pix_ready_i
                         && last_pix_in_frame && cfg_run_i);

    // One pulse per completed frame (the EOF handshake).
    assign frame_done = (state_q == ST_EMIT) && m_pix_ready_i
                      && last_pix_in_frame;

    // Frame metadata.  StreamID is one byte on the wire (Table 38).
    assign meta_o.arbitrary = 1'b0;
    assign meta_o.xsize     = 24'(xsize_q);
    assign meta_o.ysize     = 24'(ysize_q);
    assign meta_o.xoffs     = 24'(xoffs_q);
    assign meta_o.yoffs     = 24'(yoffs_q);
    assign meta_o.pixfmt    = pixfmt_q;
    assign meta_o.tapg      = cfg_tapg_i;
    assign meta_o.streamid  = cfg_streamid_i;
    assign meta_o.sourcetag = srctag_q;
    assign meta_o.flags     = cfg_flags_i;

    assign srctag_set = (cfg_srctag_i != srctag_cfg_q);

    //=======================================================================
    // Effective (clamped) run-time geometry.  0 or above the compiled max
    // falls back to the maximum so the counters never overflow.
    //=======================================================================

    always_comb begin
        xsize_eff = ((cfg_xsize_i == 16'd0) || (32'(cfg_xsize_i) > p_X_SIZE))
                  ? 16'(p_X_SIZE) : cfg_xsize_i;
        ysize_eff = ((cfg_ysize_i == 16'd0) || (32'(cfg_ysize_i) > p_Y_SIZE))
                  ? 16'(p_Y_SIZE) : cfg_ysize_i;
    end

    //=======================================================================
    // Column band index 0..7 — active line split into 8 equal-width bands
    // (band width = xsize / 8).  Shared by BARS and GREYBARS so both always
    // show 8 bands whose width scales with the run-time resolution.
    //=======================================================================

    always_comb begin
        automatic int unsigned bw = int'(xsize_q) >> 3;   // xsize / 8
        automatic int unsigned px = int'(x_q);
        if      (px < bw)     band_idx = 3'd0;
        else if (px < bw * 2) band_idx = 3'd1;
        else if (px < bw * 3) band_idx = 3'd2;
        else if (px < bw * 4) band_idx = 3'd3;
        else if (px < bw * 5) band_idx = 3'd4;
        else if (px < bw * 6) band_idx = 3'd5;
        else if (px < bw * 7) band_idx = 3'd6;
        else                  band_idx = 3'd7;
    end

    //=======================================================================
    // GREYBARS graduated grey level per band.
    //=======================================================================

    always_comb begin
        unique case (band_idx)
            3'd0:    greybar_val = 8'h10;
            3'd1:    greybar_val = 8'h30;
            3'd2:    greybar_val = 8'h50;
            3'd3:    greybar_val = 8'h70;
            3'd4:    greybar_val = 8'h90;
            3'd5:    greybar_val = 8'hC0;
            3'd6:    greybar_val = 8'hE0;
            default: greybar_val = 8'hFF;
        endcase
    end

    //=======================================================================
    // Pixel pattern select (latched testpat_q).  8-bit value zero-extended
    // onto the 16-bit pixel bus.
    //=======================================================================

    always_comb begin
        automatic int unsigned x_val = int'(x_q);
        automatic int unsigned y_val = int'(y_q);
        unique case (testpat_q)
            TPG_BARS:                                       // 8 vertical bars
                pix_val = band_idx[0] ? 16'h00FF : 16'h0000;
            TPG_FLAT:                                       // flat field
                pix_val = {8'h00, frame_cnt_q[3:0], 4'h0};
            TPG_GREYBARS:                                   // 8 grey bands
                pix_val = {8'h00, greybar_val};
            default:                                        // TPG_GRADIENT
                pix_val = 16'( (x_val + y_val) & 32'hFF);
        endcase
    end

    //=======================================================================
    // Output mux.
    //=======================================================================

    always_comb begin
        m_pix_data_o  = 16'h0000;
        m_pix_valid_o = 1'b0;
        m_pix_sof_o   = 1'b0;
        m_pix_sol_o   = 1'b0;
        m_pix_eol_o   = 1'b0;
        m_pix_eof_o   = 1'b0;

        if (state_q == ST_EMIT) begin
            m_pix_valid_o = 1'b1;
            m_pix_data_o  = pix_val;
            m_pix_sol_o   = (x_q == '0);
            m_pix_sof_o   = (x_q == '0) && (y_q == '0);
            m_pix_eol_o   = last_pix_in_line;
            m_pix_eof_o   = last_pix_in_frame;
        end
    end

    //=======================================================================
    // Next-state / counter logic.
    //=======================================================================

    always_comb begin
        state_n = state_q;
        x_n     = x_q;
        y_n     = y_q;

        unique case (state_q)
            ST_IDLE: begin
                if (cfg_run_i) begin
                    state_n = ST_EMIT;
                    x_n     = '0;
                    y_n     = '0;
                end
            end

            ST_EMIT: begin
                if (m_pix_ready_i) begin
                    if (last_pix_in_frame) begin
                        x_n     = '0;
                        y_n     = '0;
                        state_n = cfg_run_i ? ST_EMIT : ST_IDLE;
                    end else if (last_pix_in_line) begin
                        x_n = '0;
                        y_n = y_q + Y_IDX_W'(1);
                    end else begin
                        x_n = x_q + X_IDX_W'(1);
                    end
                end
            end

            default: state_n = ST_IDLE;
        endcase
    end

    //=======================================================================
    // Sequential.
    //=======================================================================

    always_ff @(posedge app_clk or negedge app_rst_n) begin
        if (!app_rst_n) begin
            state_q     <= ST_IDLE;
            x_q         <= '0;
            y_q         <= '0;
            xsize_q     <= 16'(p_X_SIZE);
            ysize_q     <= 16'(p_Y_SIZE);
            pixfmt_q    <= p_PIXFMT;
            testpat_q   <= TPG_GRADIENT;
            frame_cnt_q <= 8'h00;
            xoffs_q       <= 16'h0;
            yoffs_q       <= 16'h0;
            srctag_q      <= 16'h0;
            srctag_next_q <= 16'h0;
            srctag_cfg_q  <= 16'h0;
        end else begin
            state_q <= state_n;
            x_q     <= x_n;
            y_q     <= y_n;
            if (load_size) begin
                xsize_q   <= xsize_eff;
                ysize_q   <= ysize_eff;
                pixfmt_q  <= pixfmt_eff;
                testpat_q <= cfg_testpat_i;
                xoffs_q   <= cfg_xoffs_i;
                yoffs_q   <= cfg_yoffs_i;
                srctag_q  <= srctag_set ? cfg_srctag_i : srctag_next_q;
            end
            // SourceTag: a preset names the next image; every image start
            // moves the counter on by one.
            srctag_cfg_q <= cfg_srctag_i;
            if (srctag_set)
                srctag_next_q <= cfg_srctag_i + (load_size ? 16'd1 : 16'd0);
            else if (load_size)
                srctag_next_q <= srctag_next_q + 16'd1;
            // FLAT level counter — persists across cfg_run stop/start.
            if (frame_done)
                frame_cnt_q <= frame_cnt_q + 8'd1;
        end
    end

endmodule

`default_nettype wire
