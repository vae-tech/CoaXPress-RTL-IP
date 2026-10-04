/*
================================================================================
  cxp_cdc_req
  Valid / ready request crossing with payload.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-19

    Description:
      The source raises src_valid_i and holds it and src_data_i until
      src_ready_o pulses.  The destination sees dst_valid_o with the payload
      (read straight across: it is stable for the whole handshake) and
      raises dst_ready_i when it has consumed the request; src_ready_o
      pulses once that has crossed back.
      All flops use the asynchronous active-low reset of their own domain.

      Reset of one side alone (cxp_cdc_link): a request in flight when the
      destination resets is dropped — src_ready_o pulses at once, so the
      source neither waits for ever nor sends it again; when the source
      resets, the destination acknowledge follows the request without
      raising dst_valid_o, so the source's reset toggle is not a request.
      A request is started only while the pair is usable.

    Versions:
        2026-09-19 - 0.1:   - Init
        2026-09-25 - 0.2:   - No phantom or repeated request from a reset
                              of one side

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_cdc_req #(
    parameter int p_W = 32                                   // payload width
) (
    input  wire  logic           src_clk,                    // source clock
    input  wire  logic           src_rst_n,                  // source reset
    input  wire  logic           src_valid_i,                // request (held until ready)
    input  wire  logic [p_W-1:0] src_data_i,                 // payload (held with valid)
    output logic                 src_ready_o,                // 1-cycle: consumed or dropped
    input  wire  logic           dst_clk,                    // destination clock
    input  wire  logic           dst_rst_n,                  // destination reset
    output logic                 dst_valid_o,                // request pending
    output logic [p_W-1:0]       dst_data_o,                 // payload
    input  wire  logic           dst_ready_i                 // 1-cycle: consume it
);

    //=======================================================================
    // Signals
    //=======================================================================

    logic req_q;       // source request toggle
    logic ack_s;       // acknowledge toggle in the source domain
    logic ack_s_d_q;   // previous ack_s (edge detect)
    logic busy_q;      // a request is in flight
    logic req_s;       // request toggle in the destination domain
    logic ack_q;       // destination acknowledge toggle
    logic src_ok;      // pair usable (src_clk)
    logic src_ok_q;    // src_ok at the last edge (a whole-cycle drop pulse)
    logic dst_ok;      // pair usable (dst_clk)

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign src_ready_o = busy_q && (!src_ok_q || ack_s != ack_s_d_q);

    // The payload is stable from the toggle until the acknowledge
    // returns, so it is read straight across.
    assign dst_valid_o = dst_ok && (req_s != ack_q);
    assign dst_data_o  = src_data_i;

    //=======================================================================
    // Source: toggle once per request, report the consume edge
    //=======================================================================

    always_ff @(posedge src_clk or negedge src_rst_n) begin
        if (!src_rst_n) begin
            req_q     <= 1'b0;
            busy_q    <= 1'b0;
            ack_s_d_q <= 1'b0;
            src_ok_q  <= 1'b0;
        end else begin
            ack_s_d_q <= ack_s;
            src_ok_q  <= src_ok;
            if (src_ready_o) begin
                busy_q <= 1'b0;
            end else if (!busy_q && src_valid_i && src_ok && src_ok_q) begin
                req_q  <= ~req_q;
                busy_q <= 1'b1;
            end
        end
    end

    //=======================================================================
    // Destination: acknowledge a consumed request
    //=======================================================================

    always_ff @(posedge dst_clk or negedge dst_rst_n) begin
        if (!dst_rst_n)                      ack_q <= 1'b0;
        else if (!dst_ok)                    ack_q <= req_s;
        else if (dst_valid_o && dst_ready_i) ack_q <= req_s;
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
