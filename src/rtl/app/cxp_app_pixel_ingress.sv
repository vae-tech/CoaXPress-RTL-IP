/*
================================================================================
  cxp_app_pixel_ingress
  Sensor / application-side single-pixel adapter (§9.4, modules doc §2.1).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2025-05-27

    Description:
      Adapts the sensor / application-side AXI4-Stream-style single-pixel bus
      into the IP and forwards it to `cxp_app_pixel_packer`.  One pixel per cycle
      in, one pixel per cycle out, through a one-pixel look-ahead stage
      and a 1-deep output register, so the downstream packer can
      back-pressure without dropping pixels.

      SOL derivation
        Sensor bus carries `s_pix_sof_i` / `s_pix_eol_i` / `s_pix_eof_i` but
        no explicit start-of-line.  `m_pix_sol_o` is asserted on the first
        pixel of the frame (SOF) and on the first pixel following an EOL
        that was not also an EOF.

      Frame gating / spurious-EOF guard
        Pixels are only forwarded once a SOF has opened a frame.  Stray
        pixels (including a spurious `s_pix_eof_i`) arriving before the
        first SOF are consumed and dropped, and pulsed on `spurious_eof_o`.
        A SOF inside an open frame starts a new frame (the old one is cut
        off) and pulses `sof_restart_o`.

      Cut frames end with EOF
        Every pixel but an EOF waits in the look-ahead stage until the
        sensor's next pixel arrives.  When that pixel is a SOF inside the
        open frame, the waiting pixel leaves with EOL and EOF set: the cut
        frame ends like any other, so the acquisition gate counts it, the
        packer flushes its last word and the stream closes its packet
        (§8.5.2) even when the next frame is not admitted.  An EOF pixel
        does not wait.

      Metadata latch
        The frame's metadata (geometry, format, tap geometry, StreamID,
        SourceTag, flags) is captured on the accepted SOF pixel, travels
        with it and is held for the whole frame; `m_meta_valid_o` strobes
        for one cycle with the SOF pixel's `m_pix_sof_o`.  Mid-frame
        changes take effect only on the next SOF.  The sensor may present
        the next frame's metadata as soon as this frame's last pixel is
        taken; the latched copy is the one the image's header takes
        (through cxp_app_pixel_packer).

    Versions:
        2025-05-27 - 0.1:   - Init
        2026-09-25 - 0.2:   - sof_restart_o: a SOF inside an open frame
        2026-09-27 - 0.3:   - StreamID, SourceTag and flags latched with
                              the geometry
        2026-09-27 - 0.4:   - Metadata in and out as cxp_meta_t
        2026-10-04 - 0.5:   - One-pixel look-ahead: a frame cut by a SOF
                              ends with EOL / EOF on its last pixel

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_app_pixel_ingress #(
    parameter int p_PIX_W = 16
) (
    input  wire  logic              app_clk,                  // application clock
    input  wire  logic              app_rst_n,                // sync-deassert reset

    // ---- Sensor side (AXI4-Stream-like, single pixel / cycle) ----
    input  wire  logic [p_PIX_W-1:0] s_pix_data_i,            // pixel value
    input  wire  logic              s_pix_valid_i,            // pixel valid
    input  wire  logic              s_pix_sof_i,              // start-of-frame
    input  wire  logic              s_pix_eol_i,              // end-of-line
    input  wire  logic              s_pix_eof_i,              // end-of-frame
    output logic                    s_pix_ready_o,            // can accept a pixel

    // ---- Frame metadata (sampled on the SOF pixel) ----
    input  wire  cxp_pkg::cxp_meta_t s_meta_i,                // sensor's frame metadata

    // ---- Packer side (single pixel / cycle) ----
    output logic [p_PIX_W-1:0]      m_pix_data_o,             // pixel value
    output logic                    m_pix_valid_o,            // pixel valid
    output logic                    m_pix_sol_o,              // start-of-line (derived)
    output logic                    m_pix_eol_o,              // end-of-line
    output logic                    m_pix_sof_o,              // start-of-frame
    output logic                    m_pix_eof_o,              // end-of-frame
    input  wire  logic              m_pix_ready_i,            // downstream ready

    // ---- Latched metadata (the image header takes it via the packer) ----
    output cxp_pkg::cxp_meta_t      m_meta_o,                 // metadata of the open frame
    output logic                    m_meta_valid_o,           // 1-cycle capture pulse

    // ---- Status ----
    output logic                    spurious_eof_o,           // EOF dropped pre-frame
    output logic                    sof_restart_o             // SOF inside an open frame
);

    //=======================================================================
    // Signals
    //=======================================================================

    // Frame gating + SOL derivation, evaluated at sensor-accept time.
    logic in_frame_q;        // a SOF has opened a frame, no EOF yet
    logic pending_sol_q;     // previous accepted pixel ended a line

    logic accept_in;         // a real pixel is taken from the sensor bus
    logic drop_in;           // stray pixel before any SOF — consume & discard
    logic pix_is_real;       // belongs to an open frame
    logic sol_in;            // SOL flag of the inbound pixel

    // Look-ahead stage: the last accepted pixel, until its successor
    // arrives (an EOF pixel leaves at once).
    logic [p_PIX_W-1:0] la_data_q;
    logic               la_valid_q;
    logic               la_sol_q;
    logic               la_eol_q;
    logic               la_sof_q;
    logic               la_eof_q;
    logic               la_move;       // the look-ahead pixel goes to the output
    logic               la_cut;        // ... as the last pixel of a cut frame
    cxp_pkg::cxp_meta_t la_meta_q;     // metadata taken with a SOF pixel, until it leaves

    // 1-deep output register of the augmented pixel.
    logic [p_PIX_W-1:0] buf_data_q;
    logic               buf_valid_q;
    logic               buf_sol_q;
    logic               buf_eol_q;
    logic               buf_sof_q;
    logic               buf_eof_q;

    logic               skid_free;     // skid has room for a new pixel
    logic               meta_capture;  // accepted SOF pixel — latch metadata

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign pix_is_real   = in_frame_q | (s_pix_valid_i & s_pix_sof_i);
    assign sol_in        = s_pix_sof_i | pending_sol_q;

    assign skid_free     = ~buf_valid_q | m_pix_ready_i;
    assign s_pix_ready_o = skid_free;

    // In an open frame the look-ahead stage always holds the frame's last
    // accepted pixel (one that is not an EOF).
    assign la_move       = la_valid_q & skid_free & (la_eof_q | accept_in);
    assign la_cut        = accept_in & s_pix_sof_i & in_frame_q;

    assign accept_in     = s_pix_valid_i & s_pix_ready_o &  pix_is_real;
    assign drop_in       = s_pix_valid_i & s_pix_ready_o & ~pix_is_real;
    assign spurious_eof_o = drop_in & s_pix_eof_i;
    assign sof_restart_o  = accept_in & s_pix_sof_i & in_frame_q;

    assign m_pix_data_o  = buf_data_q;
    assign m_pix_valid_o = buf_valid_q;
    assign m_pix_sol_o   = buf_sol_q;
    assign m_pix_eol_o   = buf_eol_q;
    assign m_pix_sof_o   = buf_sof_q;
    assign m_pix_eof_o   = buf_eof_q;

    assign meta_capture  = accept_in & s_pix_sof_i;

    //=======================================================================
    // Sequential — frame gating, look-ahead stage, output register.
    //=======================================================================

    always_ff @(posedge app_clk or negedge app_rst_n) begin
        if (!app_rst_n) begin
            in_frame_q    <= 1'b0;
            pending_sol_q <= 1'b0;
            la_valid_q    <= 1'b0;
            la_data_q     <= '0;
            la_sol_q      <= 1'b0;
            la_eol_q      <= 1'b0;
            la_sof_q      <= 1'b0;
            la_eof_q      <= 1'b0;
            buf_valid_q   <= 1'b0;
            buf_data_q    <= '0;
            buf_sol_q     <= 1'b0;
            buf_eol_q     <= 1'b0;
            buf_sof_q     <= 1'b0;
            buf_eof_q     <= 1'b0;
        end else begin
            if (buf_valid_q & m_pix_ready_i) buf_valid_q <= 1'b0;

            // Look-ahead -> output register.
            if (la_move) begin
                buf_data_q  <= la_data_q;
                buf_valid_q <= 1'b1;
                buf_sol_q   <= la_sol_q;
                buf_eol_q   <= la_eol_q | la_cut;
                buf_sof_q   <= la_sof_q;
                buf_eof_q   <= la_eof_q | la_cut;
                la_valid_q  <= 1'b0;
            end

            // Sensor -> look-ahead.
            if (accept_in) begin
                la_data_q  <= s_pix_data_i;
                la_valid_q <= 1'b1;
                la_sol_q   <= sol_in;
                la_eol_q   <= s_pix_eol_i;
                la_sof_q   <= s_pix_sof_i;
                la_eof_q   <= s_pix_eof_i;

                in_frame_q    <= s_pix_eof_i ? 1'b0 : 1'b1;
                pending_sol_q <= s_pix_eol_i & ~s_pix_eof_i;
            end else if (drop_in) begin
                pending_sol_q <= 1'b0;
            end
        end
    end

    //=======================================================================
    // Sequential — metadata latch.  Taken from s_meta_i with the accepted
    // SOF pixel and carried with it through the look-ahead stage, so
    // m_meta_valid_o rises on the same edge as the SOF pixel's
    // m_pix_sof_o (a one-pixel frame cut by the next SOF keeps its own).
    //=======================================================================

    always_ff @(posedge app_clk or negedge app_rst_n) begin
        if (!app_rst_n) begin
            la_meta_q      <= '0;
            m_meta_o       <= '0;
            m_meta_valid_o <= 1'b0;
        end else begin
            m_meta_valid_o <= la_move & la_sof_q;
            if (la_move && la_sof_q) m_meta_o  <= la_meta_q;
            if (meta_capture)        la_meta_q <= s_meta_i;
        end
    end

endmodule

`default_nettype wire
