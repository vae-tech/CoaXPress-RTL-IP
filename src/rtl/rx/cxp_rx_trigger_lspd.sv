/*
================================================================================
  cxp_rx_trigger_lspd
  CoaXPress 1.1.1 (CXP-001-2015) §8.3.2 / §8.3.2.1 / Table 15 — LS trig RX.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-27

    Description:
      Recreates the host's trigger event from a low-speed trigger packet
      (§8.3.2.1, Table 15, Figure 20).

      cxp_rx_lspd_sampler takes the six characters out of the uplink and
      presents, on the last of them, the edge of the leader and the three
      Delay characters as received (10b).  This module:

        * Decodes each Delay character (a data character, at either
          running disparity) and votes: two characters that decode to the
          same byte give the Delay (§8.2.2, one bad copy is out-voted).
          No two alike, or a Delay above 239, is a glitch:
          `trigger_glitch_pulse_o`, no application pulse, no
          acknowledgment.
        * Pulses `trig_ok_o` for every packet it accepts, whatever the
          edge: the §8.3.3 I/O acknowledgment is owed for each one.
        * Passes the edge selected by `cfg_polarity_i` to the application.
        * Waits Delay units of 1/24 bit interval (2 ns at 20.83 Mbps)
          before `trigger_out_app_o`, as Figure 20 does: the transmitter
          coded 239 minus the units between the event and the packet, so
          event-to-output latency is constant.  One bit is p_OS_RATIO
          rx_clk cycles, so the wait is Delay x p_OS_RATIO / 24 cycles
          (rounded), counted from the sampler's strobe, which comes a
          fixed 60 bit intervals after the first leader character.
        * A new packet while one is still counting down replaces it; a
          packet on the cycle that fires the previous one arms the next.
        * Tracks the host's trigger level (a rising packet asserts it, a
          falling one de-asserts it).  A packet that does not change the
          level is the host re-sending one it had no acknowledgment for
          (§8.3.3 "resend the last trigger packet"): acknowledged, not a
          second event.
        * `deassert_i` (ConnectionReset in progress) de-asserts the level
          as link discovery does (§8.3.2: "the effect of a falling edge
          trigger packet"): if the host was left asserted, the falling
          edge passes to the application like a packet with Delay 0, and
          a countdown still running is dropped.

      Reset: outputs low.

    Versions:
        2026-05-27 - 0.1:   - Restyle to coding-style template
        2026-09-26 - 0.2:   - Table 15 only: three Delay characters decoded
                              and voted; wait Delay x p_OS_RATIO / 24
                              (Figure 20); retrigger overwrites; trig_ok_o
        2026-09-27 - 0.3:   - Host trigger level: a repeated edge is a
                              resend; deassert_i (ConnectionReset)

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_rx_trigger_lspd #(
    // rx_clk cycles per low-speed bit (the sampler's oversampling ratio).
    parameter int p_OS_RATIO = 16
) (
    // Receive clock domain
    input  wire  logic        rx_clk,                           // RX clock
    input  wire  logic        rx_rst_n,                         // Active-low async reset

    input  wire  logic        cfg_polarity_i,                   // 0 = pass rising, 1 = falling
    input  wire  logic        deassert_i,                       // ConnectionReset in progress

    input  wire  logic        trig_valid_i,                     // Packet strobe (sampler)
    input  wire  logic [1:0]  trig_edge_i,                      // 01 rising, 10 falling
    input  wire  logic [29:0] trig_dly_i,                       // 3 Delay chars, first in [9:0]

    output logic              trig_ok_o,                        // Packet accepted (I/O ack)
    output logic              trigger_out_app_o,                // Recreated trigger pulse
    output logic              trigger_glitch_pulse_o            // Packet rejected
);

    import cxp_pkg::*;
    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    // Countdown in 1/24-cycle steps: loaded with Delay x p_OS_RATIO plus
    // half a cycle for rounding, less TRIG_UNITS_PER_BIT per cycle.
    localparam int ACC_MAX = TRIG_DELAY_MAX * p_OS_RATIO + TRIG_UNITS_PER_BIT / 2;
    localparam int AW      = cnt_w(ACC_MAX);

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    if (p_OS_RATIO < 1) begin : g_chk_os_ratio
        $error("cxp_rx_trigger_lspd: p_OS_RATIO (=%0d) must be >= 1", p_OS_RATIO);
    end

    //=======================================================================
    // Signals
    //=======================================================================

    logic [7:0]    dly_b  [3];        // Delay character, decoded at RD- / RD+
    logic [7:0]    dly_bp [3];
    logic          dly_ok [3];        // a data character at one of the two RDs
    logic [7:0]    dly_v  [3];        // its byte

    logic [7:0]    delay;             // voted Delay
    logic          vote_ok;           // two characters agree
    logic          bad;               // packet rejected
    logic          edge_pass;         // selected edge polarity passes
    logic          new_lvl;           // level the packet's edge sets
    logic          lvl_q;             // host trigger level (1 = asserted)
    logic          deassert_d_q;      // deassert_i, one cycle late
    logic          discovery;         // deassert_i rose: de-assert now

    logic [AW-1:0] acc_q;             // countdown, 1/24 cycle steps
    logic          armed_q;           // countdown active
    logic          fire_q;            // output pulse this cycle
    logic          glitch_q;          // glitch pulse this cycle
    logic          ok_q;              // accepted-packet pulse

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign trigger_out_app_o      = fire_q;
    assign trigger_glitch_pulse_o = glitch_q;
    assign trig_ok_o              = ok_q;

    assign bad = ~vote_ok | (delay > 8'(TRIG_DELAY_MAX))
               | ((trig_edge_i != TRIG_EDGE_RISE) & (trig_edge_i != TRIG_EDGE_FALL));

    assign edge_pass = (trig_edge_i == TRIG_EDGE_RISE) ? ~cfg_polarity_i : cfg_polarity_i;
    assign new_lvl   = (trig_edge_i == TRIG_EDGE_RISE);
    assign discovery = deassert_i & ~deassert_d_q;

    //=======================================================================
    // Delay characters: decode and vote
    //=======================================================================

    for (genvar i = 0; i < 3; i++) begin : g_dly
        logic k_m, k_p, cerr_m, cerr_p, derr_m, derr_p;

        cxp_rx_8b10b_decoder cxp_rx_8b10b_decoder_m_i (
            .din_i      (trig_dly_i[10*i +: 10]),
            .rd_in_i    (1'b0),
            .dout_o     (dly_b[i]),
            .k_out_o    (k_m),
            .disp_err_o (derr_m),
            .code_err_o (cerr_m),
            .rd_out_o   ()
        );

        cxp_rx_8b10b_decoder cxp_rx_8b10b_decoder_p_i (
            .din_i      (trig_dly_i[10*i +: 10]),
            .rd_in_i    (1'b1),
            .dout_o     (dly_bp[i]),
            .k_out_o    (k_p),
            .disp_err_o (derr_p),
            .code_err_o (cerr_p),
            .rd_out_o   ()
        );

        // The copies alternate between the two RD forms of the character,
        // so each is checked at the RD it fits.
        assign dly_ok[i] = ~(k_m | cerr_m | derr_m) | ~(k_p | cerr_p | derr_p);
        assign dly_v[i]  = ~(k_m | cerr_m | derr_m) ? dly_b[i] : dly_bp[i];
    end

    always_comb begin
        vote_ok = 1'b1;
        if (dly_ok[0] && dly_ok[1] && dly_v[0] == dly_v[1])      delay = dly_v[0];
        else if (dly_ok[0] && dly_ok[2] && dly_v[0] == dly_v[2]) delay = dly_v[0];
        else if (dly_ok[1] && dly_ok[2] && dly_v[1] == dly_v[2]) delay = dly_v[1];
        else begin
            delay   = 8'h00;
            vote_ok = 1'b0;
        end
    end

    //=======================================================================
    // Delay countdown
    //=======================================================================

    // A load later in this block wins over the decrement, so a packet on
    // any cycle of a countdown, including the one that fires, arms anew.
    always_ff @(posedge rx_clk or negedge rx_rst_n) begin
        if (!rx_rst_n) begin
            acc_q    <= '0;
            armed_q  <= 1'b0;
            fire_q   <= 1'b0;
            glitch_q <= 1'b0;
            ok_q     <= 1'b0;
            lvl_q    <= 1'b0;
            deassert_d_q <= 1'b0;
        end else begin
            fire_q   <= 1'b0;
            glitch_q <= 1'b0;
            ok_q     <= 1'b0;
            deassert_d_q <= deassert_i;

            if (armed_q) begin
                if (acc_q < AW'(TRIG_UNITS_PER_BIT)) begin
                    fire_q  <= 1'b1;
                    armed_q <= 1'b0;
                end else begin
                    acc_q <= acc_q - AW'(TRIG_UNITS_PER_BIT);
                end
            end

            if (trig_valid_i) begin
                glitch_q <= bad;
                ok_q     <= ~bad;
                // A packet that does not change the level is a resend.
                if (!bad && new_lvl != lvl_q) begin
                    lvl_q <= new_lvl;
                    if (edge_pass) begin
                        armed_q <= 1'b1;
                        acc_q   <= AW'(delay) * AW'(p_OS_RATIO) + AW'(TRIG_UNITS_PER_BIT / 2);
                    end
                end
            end

            // Link discovery de-asserts the trigger: a host left asserted
            // gets the falling edge now, and nothing still counting fires.
            if (discovery) begin
                armed_q <= 1'b0;
                lvl_q   <= 1'b0;
                fire_q  <= lvl_q & cfg_polarity_i;
            end
        end
    end

endmodule

`default_nettype wire
