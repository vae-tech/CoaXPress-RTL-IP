/*
================================================================================
  cxp_cdc_bus
  Quasi-static bus crossing (request / acknowledge).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-19

    Description:
      For register levels and status values.  The source copies the bus
      into a holding register and toggles a request; the destination takes
      the (stable) holding register when the request arrives and toggles
      the acknowledge back.  The destination copy follows the source within
      a few cycles of both clocks; a value that changes again while a
      transfer is in flight is sent next.
      All flops use the asynchronous active-low reset of their own domain.

      Reset of one side alone (cxp_cdc_link): the source sends its current
      value again once the pair is usable, whatever it sent before, so a
      destination reset to p_RESET re-converges; a source reset leaves the
      destination copy at its last value until then.  While the pair is
      not usable the destination acknowledge follows the request, so no
      stale toggle is taken as a transfer.

    Versions:
        2026-09-19 - 0.1:   - Init
        2026-09-25 - 0.2:   - Re-converges after a reset of one side

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_cdc_bus #(
    parameter int           p_W     = 32,                    // bus width
    parameter logic [p_W-1:0] p_RESET = '0                   // value both copies reset to
) (
    input  wire  logic           src_clk,                    // source clock
    input  wire  logic           src_rst_n,                  // source reset
    input  wire  logic [p_W-1:0] d_i,                        // source bus
    input  wire  logic           dst_clk,                    // destination clock
    input  wire  logic           dst_rst_n,                  // destination reset
    output logic [p_W-1:0]       q_o                         // destination copy
);

    //=======================================================================
    // Signals
    //=======================================================================

    logic [p_W-1:0] hold_q;    // source-side copy being transferred
    logic           req_q;     // source request toggle
    logic           resend_q;  // send d_i even if it equals hold_q
    logic           ack_s;     // acknowledge toggle in the source domain
    logic           req_s;     // request toggle in the destination domain
    logic           ack_q;     // destination acknowledge toggle
    logic           send;      // start a transfer this cycle
    logic           src_ok;    // pair usable (src_clk)
    logic           dst_ok;    // pair usable (dst_clk)

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign send = src_ok && (req_q == ack_s) && (resend_q || d_i != hold_q);

    //=======================================================================
    // Source
    //=======================================================================

    always_ff @(posedge src_clk or negedge src_rst_n) begin
        if (!src_rst_n) begin
            hold_q   <= p_RESET;
            req_q    <= 1'b0;
            resend_q <= 1'b1;
        end else if (!src_ok) begin
            resend_q <= 1'b1;
        end else if (send) begin
            hold_q   <= d_i;
            req_q    <= ~req_q;
            resend_q <= 1'b0;
        end
    end

    //=======================================================================
    // Destination
    //=======================================================================

    always_ff @(posedge dst_clk or negedge dst_rst_n) begin
        if (!dst_rst_n) begin
            q_o   <= p_RESET;
            ack_q <= 1'b0;
        end else if (!dst_ok) begin
            ack_q <= req_s;
        end else if (req_s != ack_q) begin
            q_o   <= hold_q;
            ack_q <= req_s;
        end
    end

    //=======================================================================
    // Instances
    //=======================================================================

    cxp_cdc_sync #(.p_W(1)) cxp_cdc_sync_req_i (
        .clk   (dst_clk),
        .rst_n (dst_rst_n),
        .d_i   (req_q),
        .q_o   (req_s)
    );

    cxp_cdc_sync #(.p_W(1)) cxp_cdc_sync_ack_i (
        .clk   (src_clk),
        .rst_n (src_rst_n),
        .d_i   (ack_q),
        .q_o   (ack_s)
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
