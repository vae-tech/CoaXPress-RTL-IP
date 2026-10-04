/*
================================================================================
  cxp_app_acq_ctrl
  Acquisition control and the pixel gate: which images enter the device
  (GenICam SFNC AcquisitionStart / AcquisitionStop / AcquisitionMode /
  AcquisitionFrameCount, Table 44).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-19

    Description:
      AcquisitionStart arms the device; AcquisitionStop disarms it.  run_i
      holds it armed without an acquisition (a free-running test pattern).
        mode 0  Continuous   images until AcquisitionStop
        mode 1  SingleFrame  one image, then disarm
        mode 2  MultiFrame   frame_count_i images (0 counts as 1)
      No image may enter while stream_en_i is low: StreamPacketSizeMax is
      0 ("not initialized", Table 44) or smaller than one stream packet,
      and the device sends only IDLE words.

      Every pixel of either source (test pattern or sensor) passes the
      gate here, one whole image at a time.  At an image's first pixel
      (sof) the gate decides: the image enters if the acquisition is
      armed (or run_i) and stream_en_i is high, otherwise every pixel of
      it is taken and dropped.  An image that has entered always
      completes, so AcquisitionStop, a ConnectionReset or a
      StreamPacketSizeMax of 0 ends the stream on an image boundary.
      Pixels outside an image (after a reset in mid-image, or a sensor
      that runs before any sof) are dropped too.

      active_o tells an image source that can stop itself (the test
      pattern) whether a new image would enter; it drops in the cycle the
      last image of a SingleFrame / MultiFrame burst ends.  Such a source
      decides a few cycles before its first pixel reaches the gate, so
      with src_gated_i set the gate takes its images as they come: an
      image it began because active_o was high is not dropped because the
      acquisition stopped in between.

    Versions:
        2026-09-19 - 0.1:   - Init
        2026-09-25 - 0.2:   - The pixel gate for both sources; run_i;
                              images counted as they leave the gate

================================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_app_acq_ctrl (
    input  wire  logic        clk,                           // pixel clock
    input  wire  logic        rst_n,                         // async reset, active-low

    input  wire  logic        acq_start_i,                   // 1-cycle AcquisitionStart
    input  wire  logic        acq_stop_i,                    // 1-cycle AcquisitionStop
    input  wire  logic [1:0]  acq_mode_i,                    // 0 cont, 1 single, 2 multi
    input  wire  logic [15:0] frame_count_i,                 // MultiFrame image count
    input  wire  logic        run_i,                         // armed without an acquisition
    input  wire  logic        stream_en_i,                   // a stream packet fits
    input  wire  logic        src_gated_i,                   // the source obeys active_o itself
    output logic              active_o,                      // a new image would enter

    // Pixels in (the selected source) and out (to the packer)
    input  wire  cxp_pkg::cxp_pix_t s_pix_i,                 // pixel
    output logic              s_pix_ready_o,                 // pixel taken (passed or dropped)
    output cxp_pkg::cxp_pix_t m_pix_o,                       // pixel of an image that entered
    input  wire  logic        m_pix_ready_i                  // packer takes it
);

    //=======================================================================
    // Signals
    //=======================================================================

    logic        armed_q;      // started and not yet stopped / done
    logic [15:0] done_q;       // images ended since AcquisitionStart
    logic [15:0] limit;        // images this acquisition, 0 = unlimited
    logic        last;         // the image now ending is the last one
    logic        admit;        // an image starting now would enter
    logic        in_img_q;     // inside an image
    logic        pass_q;       // ... that entered
    logic        in_img;       // this pixel belongs to an image
    logic        pass;         // ... that enters / entered
    logic        fire;         // a pixel is taken
    logic        img_end;      // the last pixel of an entered image is taken

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    always_comb begin
        unique case (acq_mode_i)
            2'd1:    limit = 16'd1;
            2'd2:    limit = (frame_count_i == 16'd0) ? 16'd1 : frame_count_i;
            default: limit = 16'd0;
        endcase
    end

    assign admit   = (armed_q | run_i) & stream_en_i;
    assign in_img  = s_pix_i.sof | in_img_q;
    assign pass    = s_pix_i.sof ? (admit | src_gated_i) : pass_q;
    assign fire    = s_pix_i.valid & s_pix_ready_o;
    assign img_end = fire & s_pix_i.eof & in_img & pass;

    assign last     = (limit != 16'd0) && (done_q + 16'd1 >= limit);
    // Low already in the cycle the last image ends, so a source deciding
    // at its last pixel whether to roll into another image sees it.
    assign active_o = admit & ~(img_end & last & ~run_i);

    // An entered image's pixels go on to the packer; any other pixel is
    // taken here and dropped.
    always_comb begin
        m_pix_o       = s_pix_i;
        m_pix_o.valid = s_pix_i.valid & in_img & pass;
        s_pix_ready_o = (in_img & pass) ? m_pix_ready_i : 1'b1;
    end

    //=======================================================================
    // Sequential
    //=======================================================================

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            armed_q  <= 1'b0;
            done_q   <= 16'd0;
            in_img_q <= 1'b0;
            pass_q   <= 1'b0;
        end else begin
            if (fire) begin
                if (s_pix_i.sof) pass_q <= admit | src_gated_i;
                if (in_img)      in_img_q <= ~s_pix_i.eof;
            end
            if (img_end && armed_q) begin
                done_q <= done_q + 16'd1;
                if (last) armed_q <= 1'b0;
            end
            if (acq_stop_i) armed_q <= 1'b0;
            if (acq_start_i) begin
                armed_q <= 1'b1;
                done_q  <= 16'd0;
            end
        end
    end

endmodule

`default_nettype wire
