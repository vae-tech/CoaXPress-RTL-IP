/*
================================================================================
  cxp_ctrl_apb_bridge
  Register-bus to APB bridge for user registers (APB3 + PSTRB).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-19

    Description:
      Optional bridge for the device top's user register port: one
      register-bus access (req / we / addr / wdata / wstrb) becomes one
      APB transfer (SETUP, then ACCESS until PREADY); the response comes
      back as ack with rdata, and PSLVERR as the Table 22 code
      p_SLVERR_CODE (APB carries one error bit, so the slave cannot choose
      the code).

      PSTRB (APB4) carries the byte enables of a write, so a write of B
      bytes that ends inside a word changes only those bytes; PSTRB[n]
      covers PWDATA[8n+7:8n].  It is 0 during a read.  An APB3 slave
      without PSTRB writes the whole word.

      abort_i gives up the transfer in progress: no ack follows.  The
      transfer itself is never cut short — PSEL, PENABLE, PADDR, PWRITE,
      PWDATA and PSTRB stay as they are until PREADY, as APB requires —
      and only its completion is discarded.  The master uses it for a
      timed-out access or a control-channel reset and does not start
      another user-window access until the bridge is idle (busy_o).

    Versions:
        2026-09-19 - 0.1:   - Init
        2026-09-26 - 0.2:   - abort_i
        2026-10-04 - 0.3:   - abort_i no longer withdraws PSEL / PENABLE
                              without PREADY; PSTRB; busy_o

================================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_ctrl_apb_bridge #(
    parameter logic [7:0] p_SLVERR_CODE = 8'h40              // ack code for PSLVERR
) (
    input  wire  logic        clk,                           // bus clock
    input  wire  logic        rst_n,                         // async reset, active-low
    input  wire  logic        abort_i,                       // give up the transfer

    // Register bus (slave side)
    input  wire  logic        req_i,
    input  wire  logic        we_i,
    input  wire  logic [31:0] addr_i,
    input  wire  logic [31:0] wdata_i,
    input  wire  logic [3:0]  wstrb_i,                       // byte enables of a write
    output logic              ack_o,
    output logic [31:0]       rdata_o,
    output logic [7:0]        err_o,

    // APB3 master
    output logic              psel_o,
    output logic              penable_o,
    output logic              pwrite_o,
    output logic [31:0]       paddr_o,
    output logic [31:0]       pwdata_o,
    output logic [3:0]        pstrb_o,
    input  wire  logic [31:0] prdata_i,
    input  wire  logic        pready_i,
    input  wire  logic        pslverr_i,

    output logic              busy_o                         // a transfer is open
);

    logic abandon_q;                                         // no ack for this transfer
    logic give_up;                                           // abandoned, as of this cycle

    assign give_up = abandon_q | abort_i;
    assign busy_o  = psel_o;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            psel_o    <= 1'b0;
            penable_o <= 1'b0;
            pwrite_o  <= 1'b0;
            paddr_o   <= 32'h0;
            pwdata_o  <= 32'h0;
            pstrb_o   <= 4'h0;
            ack_o     <= 1'b0;
            rdata_o   <= 32'h0;
            err_o     <= 8'h00;
            abandon_q <= 1'b0;
        end else begin
            ack_o <= 1'b0;
            if (psel_o && give_up) abandon_q <= 1'b1;
            if (req_i && !psel_o) begin
                psel_o   <= 1'b1;             // SETUP
                pwrite_o <= we_i;
                paddr_o  <= addr_i;
                pwdata_o <= wdata_i;
                pstrb_o  <= we_i ? wstrb_i : 4'h0;
            end else if (psel_o && !penable_o) begin
                penable_o <= 1'b1;            // ACCESS
            end else if (psel_o && pready_i) begin
                // Only PREADY ends a transfer; one given up completes
                // without an ack.
                psel_o    <= 1'b0;
                penable_o <= 1'b0;
                abandon_q <= 1'b0;
                ack_o     <= ~give_up;
                rdata_o   <= prdata_i;
                err_o     <= pslverr_i ? p_SLVERR_CODE : 8'h00;
            end
        end
    end

endmodule

`default_nettype wire
