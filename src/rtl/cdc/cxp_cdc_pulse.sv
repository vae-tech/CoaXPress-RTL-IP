/*
================================================================================
  cxp_cdc_pulse
  One-cycle pulse crossing (toggle synchroniser).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-19

    Description:
      Each source pulse flips a toggle; the destination sees a one-cycle
      pulse per flip.  Space pulses at least three destination cycles
      apart: two closer than that cancel (the toggle returns before the
      destination sees it), three give one.
      All flops use the asynchronous active-low reset of their own domain.

      Reset of one side alone (cxp_cdc_link): while either side is in
      reset or settling, the destination follows the toggle without
      pulsing, so a toggle returned to 0 by a source reset is not a pulse;
      a source pulse in that window is dropped.

    Versions:
        2026-09-19 - 0.1:   - Init
        2026-09-25 - 0.2:   - No pulse from a reset of one side

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_cdc_pulse (
    input  wire  logic src_clk,                              // source clock
    input  wire  logic src_rst_n,                            // source reset
    input  wire  logic pulse_i,                              // source pulse
    input  wire  logic dst_clk,                              // destination clock
    input  wire  logic dst_rst_n,                            // destination reset
    output logic       pulse_o                               // destination pulse
);

    //=======================================================================
    // Signals
    //=======================================================================

    logic tog_q;       // source toggle
    logic tog_s;       // toggle in the destination domain
    logic tog_d_q;     // previous synchronised toggle
    logic src_ok;      // pair usable (src_clk)
    logic dst_ok;      // pair usable (dst_clk)

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign pulse_o = dst_ok & (tog_s ^ tog_d_q);

    //=======================================================================
    // Source toggle / destination edge
    //=======================================================================

    always_ff @(posedge src_clk or negedge src_rst_n) begin
        if (!src_rst_n)             tog_q <= 1'b0;
        else if (pulse_i && src_ok) tog_q <= ~tog_q;
    end

    always_ff @(posedge dst_clk or negedge dst_rst_n) begin
        if (!dst_rst_n) tog_d_q <= 1'b0;
        else            tog_d_q <= tog_s;
    end

    //=======================================================================
    // Instances
    //=======================================================================

    cxp_cdc_sync #(.p_W(1)) cxp_cdc_sync_i (
        .clk   (dst_clk),
        .rst_n (dst_rst_n),
        .d_i   (tog_q),
        .q_o   (tog_s)
    );

    cxp_cdc_link cxp_cdc_link_i (
        .src_clk   (src_clk),
        .src_rst_n (src_rst_n),
        .dst_clk   (dst_clk),
        .dst_rst_n (dst_rst_n),
        .src_ok_o  (src_ok),
        .dst_ok_o  (dst_ok)
    );

endmodule

`default_nettype wire
