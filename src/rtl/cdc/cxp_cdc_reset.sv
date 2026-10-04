/*
================================================================================
  cxp_cdc_reset
  One reset request in, one synchronised reset per clock domain out.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-25

    Description:
      The device keeps state on both sides of every clock crossing, so a
      reset of one domain alone leaves the crossings disagreeing.  Here
      one active-low request resets all three domains at once (assertion
      is asynchronous) and releases them in a fixed order, each through a
      two-flop synchroniser in its own clock:

        rx_rst_n_o   first: the register file and the uplink
        tx_rst_n_o   once rx is out of reset
        app_rst_n_o  once tx is out of reset

      The integration ANDs its reset sources into rst_n; a reset of any
      one domain is a reset of the device.

    Versions:
        2026-09-25 - 0.1:   - Init

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_cdc_reset (
    input  wire  logic rst_n,                                // reset request, async active-low
    input  wire  logic rx_clk,                               // uplink / register clock
    input  wire  logic tx_clk,                               // downlink clock
    input  wire  logic app_clk,                              // pixel clock
    output logic       rx_rst_n_o,                           // rx_clk reset, released first
    output logic       tx_rst_n_o,                           // tx_clk reset, after rx
    output logic       app_rst_n_o                           // app_clk reset, after tx
);

    //=======================================================================
    // Signals
    //=======================================================================

    logic [1:0] rx_q;       // rx release synchroniser
    logic [1:0] tx_q;       // tx release synchroniser, fed by the rx release
    logic [1:0] app_q;      // app release synchroniser, fed by the tx release

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign rx_rst_n_o  = rx_q[1];
    assign tx_rst_n_o  = tx_q[1];
    assign app_rst_n_o = app_q[1];

    //=======================================================================
    // Release chain
    //=======================================================================

    always_ff @(posedge rx_clk or negedge rst_n) begin
        if (!rst_n) rx_q <= '0;
        else        rx_q <= {rx_q[0], 1'b1};
    end

    always_ff @(posedge tx_clk or negedge rst_n) begin
        if (!rst_n) tx_q <= '0;
        else        tx_q <= {tx_q[0], rx_q[1]};
    end

    always_ff @(posedge app_clk or negedge rst_n) begin
        if (!rst_n) app_q <= '0;
        else        app_q <= {app_q[0], tx_q[1]};
    end

endmodule

`default_nettype wire
