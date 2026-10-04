/*
================================================================================
  cxp_cdc_link
  Both ends of a clock crossing out of reset, as seen from each end.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-25

    Description:
      A crossing primitive (cxp_cdc_pulse / _bus / _req) keeps state on
      both sides.  When one side is reset alone, that state no longer
      agrees and a toggle-based protocol reads the difference as an event.
      This block tells each side whether the pair is usable:

        src_ok_o   (src_clk)  both sides out of reset and settled
        dst_ok_o   (dst_clk)  the same, in the destination domain

      Each goes low asynchronously the moment either reset asserts, and
      comes back only after the other side has seen this side settled:
      the source settles (3 src_clk edges), the destination sees it and
      settles (3 dst_clk edges), then the source sees that (3 src_clk
      edges).  So dst_ok_o rises before src_ok_o, and a primitive that
      starts events only on src_ok_o and absorbs state only while
      dst_ok_o is low never turns a reset into an event.

      Contract: an event the source offers while src_ok_o is low is not
      transferred.  A reset must be held for at least one edge of its own
      clock.

    Versions:
        2026-09-25 - 0.1:   - Init

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_cdc_link (
    input  wire  logic src_clk,                              // source clock
    input  wire  logic src_rst_n,                            // source reset
    input  wire  logic dst_clk,                              // destination clock
    input  wire  logic dst_rst_n,                            // destination reset
    output logic       src_ok_o,                             // pair usable (src_clk)
    output logic       dst_ok_o                              // pair usable (dst_clk)
);

    //=======================================================================
    // Signals
    //=======================================================================

    logic       pair_rst_n;    // either side in reset
    logic [2:0] src_up_q;      // source settled after its own reset
    logic [2:0] dst_up_q;      // destination settled after its own reset
    logic [2:0] src_seen_q;    // src_up in the destination domain
    logic [2:0] dst_seen_q;    // dst_ok in the source domain

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign pair_rst_n = src_rst_n & dst_rst_n;
    assign dst_ok_o   = dst_up_q[2] & src_seen_q[2];
    assign src_ok_o   = src_up_q[2] & dst_seen_q[2];

    //=======================================================================
    // Own settle chains
    //=======================================================================

    always_ff @(posedge src_clk or negedge src_rst_n) begin
        if (!src_rst_n) src_up_q <= '0;
        else            src_up_q <= {src_up_q[1:0], 1'b1};
    end

    always_ff @(posedge dst_clk or negedge dst_rst_n) begin
        if (!dst_rst_n) dst_up_q <= '0;
        else            dst_up_q <= {dst_up_q[1:0], 1'b1};
    end

    //=======================================================================
    // Views of the other side: cleared at once by either reset
    //=======================================================================

    always_ff @(posedge dst_clk or negedge pair_rst_n) begin
        if (!pair_rst_n) src_seen_q <= '0;
        else             src_seen_q <= {src_seen_q[1:0], src_up_q[2]};
    end

    always_ff @(posedge src_clk or negedge pair_rst_n) begin
        if (!pair_rst_n) dst_seen_q <= '0;
        else             dst_seen_q <= {dst_seen_q[1:0], dst_ok_o};
    end

endmodule

`default_nettype wire
