/*
================================================================================
  cxp_cdc_sync
  N-bit two-flop synchroniser.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-19

    Description:
      Two flops in the destination domain.  For single bits, or for buses
      that are gray-coded or quasi-static (a bus that changes needs
      cxp_cdc_bus instead).  Add the vendor's ASYNC_REG / false-path
      constraints in the integration.
      All flops use the asynchronous active-low reset of their own domain.

    Versions:
        2026-09-19 - 0.1:   - Init

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_cdc_sync #(
    parameter int          p_W     = 1,                      // bits
    parameter logic [63:0] p_RESET = '0                      // reset value
) (
    input  wire  logic           clk,                        // destination clock
    input  wire  logic           rst_n,                      // destination reset
    input  wire  logic [p_W-1:0] d_i,                        // asynchronous input
    output logic [p_W-1:0]       q_o                         // synchronised output
);

    logic [p_W-1:0] s1_q;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s1_q <= p_RESET[p_W-1:0];
            q_o  <= p_RESET[p_W-1:0];
        end else begin
            s1_q <= d_i;
            q_o  <= s1_q;
        end
    end

endmodule

`default_nettype wire
