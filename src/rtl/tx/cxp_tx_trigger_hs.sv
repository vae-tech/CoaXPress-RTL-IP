/*
================================================================================
  cxp_tx_trigger_hs
  CoaXPress 1.1.1 (CXP-001-2015) §8.3.2 / §8.3.3 / Table 16 — device trigger.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-27

    Description:
      Sends the device's trigger pin to the host as high-speed trigger
      packets (Table 16):

          Word | P0     P1     P2     P3   | kmask | Notes
          -----+----------------------------+-------+-----------------------
          HDR  | K28.4  K28.4  K28.4  K28.4 | 1111  | asserted, m_sop=1
               |               or
               | K28.2  K28.2  K28.2  K28.2 | 1111  | de-asserted, m_sop=1
          DLY  | 0x00   0x00   0x00   0x00  | 0000  | Delay not used, m_eop=1

      Pin: trigger_in_i may come from any clock; it is synchronised to
      tx_clk here (two flops).  cfg_polarity_i says which pin level is
      asserted (0 = high, 1 = low); the packet carries the logical edge,
      K28.4 for asserted, K28.2 for de-asserted.

      Level tracking (§8.3.3): host_lvl_q is the level the host was last
      sent.  Whenever the pin's logical level differs from it and no
      packet is waiting for its acknowledgment, one packet with the pin's
      level goes out.  After it the source waits for the host's I/O
      acknowledgment (ack_i, the Table 17 packet decoded on the uplink) or
      for p_ACK_TIMEOUT tx_clk cycles, then sends the pin's level again if
      it has changed — "can send a new trigger packet" — and nothing if it
      has not.  Edges faster than the acknowledgments therefore merge: the
      host sees every level the pin settles at long enough, never a stale
      one, and ends at the pin's level.

      Link and resets: nothing is sent while link_up_i is low; the host's
      level is then taken as de-asserted (§8.3.2: both sides de-assert the
      trigger at link discovery).  While mask_i is high (ConnectionReset,
      §10.3.28: the device trigger is set to 0) the level sent is
      de-asserted, so a host left at asserted gets one K28.2.  After a
      reset or a ConnectionReset the pin is followed only once it has been
      seen de-asserted: a pin held asserted across it is not a new edge.

      Delay (= 0): the transmit side has one-word granularity and no
      sub-word phase; Table 16 allows "when not used set to 0".

    Versions:
        2026-05-27 - 0.1:   - Restyle to coding-style template
        2026-09-19 - 0.2:   - Packet words from cxp_tx_short_pkt; abort_i
        2026-09-26 - 0.3:   - Level tracking with the host's acknowledgment
                              and a timeout (§8.3.3); pin synchronised;
                              polarity as pin sense; link gate; the
                              ConnectionReset mask moved in
        2026-09-27 - 0.4:   - Packet words out as one cxp_txw_t (m_o)

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_tx_trigger_hs #(
    parameter int p_ACK_TIMEOUT = cxp_pkg::TRIG_ACK_TIMEOUT      // tx_clk cycles to wait for an ack
) (
    // Transmit clock domain
    input  wire  logic        tx_clk,                           // TX clock
    input  wire  logic        tx_rst_n,                         // Active-low reset

    // Trigger pin (any clock) and its sense
    input  wire  logic        trigger_in_i,                     // device trigger pin
    input  wire  logic        cfg_polarity_i,                   // 0 = active high, 1 = active low

    // Host side (tx_clk)
    input  wire  logic        link_up_i,                        // a host is connected
    input  wire  logic        mask_i,                           // ConnectionReset in progress
    input  wire  logic        ack_i,                            // host I/O acknowledgment (pulse)

    // Packet output (to cxp_tx_inserter, trigger port)
    output cxp_pkg::cxp_txw_t m_o,                              // packet word, P0 in [7:0]
    input  wire  logic        m_ready_i                         // Inserter ready
);

    import cxp_pkg::*;
    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    localparam logic [7:0] DELAY = 8'h00;           // sub-word phase not modelled
    localparam int         TW    = cnt_w(p_ACK_TIMEOUT);

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    if (p_ACK_TIMEOUT < 1) begin : g_chk_timeout
        $error("cxp_tx_trigger_hs: p_ACK_TIMEOUT (=%0d) must be >= 1", p_ACK_TIMEOUT);
    end

    //=======================================================================
    // Signals
    //=======================================================================

    logic          pin;                 // trigger_in_i on tx_clk
    logic [1:0]    pin_ok_q;            // the synchroniser holds the pin, not its reset
    logic          asserted;            // the pin's logical level
    logic          armed_q;             // pin seen de-asserted since the last reset
    logic          level;               // logical level to send
    logic          host_lvl_q;          // level the host was last sent
    logic          wait_q;              // a packet waits for its acknowledgment
    logic [TW-1:0] tmo_q;               // cycles waited
    logic          send;                // a packet is due
    logic          start;               // a packet is committed this cycle
    logic          done;                // its last word is sent
    logic [7:0]    leader_q;            // K28.4 / K28.2 of the packet in flight

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign asserted = pin ^ cfg_polarity_i;
    assign level    = armed_q & asserted & ~mask_i;
    assign send     = link_up_i & ~wait_q & (level != host_lvl_q);

    //=======================================================================
    // Pin synchroniser
    //=======================================================================

    cxp_cdc_sync #(
        .p_W (1)
    ) cxp_cdc_sync_pin_i (
        .clk   (tx_clk),
        .rst_n (tx_rst_n),
        .d_i   (trigger_in_i),
        .q_o   (pin)
    );

    //=======================================================================
    // Host level, acknowledgment wait, re-arm
    //=======================================================================

    always_ff @(posedge tx_clk or negedge tx_rst_n) begin
        if (!tx_rst_n) begin
            pin_ok_q   <= '0;
            armed_q    <= 1'b0;
            host_lvl_q <= 1'b0;
            wait_q     <= 1'b0;
            tmo_q      <= '0;
            leader_q   <= K28_2;
        end else begin
            // Follow the pin only once it has been seen de-asserted (and
            // not merely the synchroniser's reset value).
            pin_ok_q <= {pin_ok_q[0], 1'b1};
            if (mask_i)                        armed_q <= 1'b0;
            else if (pin_ok_q[1] && !asserted) armed_q <= 1'b1;

            if (start) begin
                leader_q   <= level ? K28_4 : K28_2;
                host_lvl_q <= level;
            end else if (!link_up_i) begin
                host_lvl_q <= 1'b0;
            end

            if (!link_up_i) begin
                wait_q <= 1'b0;
            end else if (done) begin
                wait_q <= 1'b1;
                tmo_q  <= '0;
            end else if (wait_q) begin
                if (ack_i || tmo_q == TW'(p_ACK_TIMEOUT - 1)) wait_q <= 1'b0;
                tmo_q <= tmo_q + 1'b1;
            end
        end
    end

    //=======================================================================
    // Packet source — 4×K-code leader, 4×DELAY.  One packet at a time.
    //=======================================================================

    cxp_tx_short_pkt cxp_tx_short_pkt_i (
        .tx_clk    (tx_clk),
        .tx_rst_n  (tx_rst_n),
        .avail_i   (send),
        .more_i    (1'b0),
        .leader_i  (leader_q),
        .code_i    (DELAY),
        .start_o   (start),
        .busy_o    (),
        .done_o    (done),
        .m_data_o  (m_o.data),
        .m_kmask_o (m_o.kmask),
        .m_valid_o (m_o.valid),
        .m_sop_o   (m_o.sop),
        .m_eop_o   (m_o.eop),
        .m_ready_i (m_ready_i)
    );

endmodule

`default_nettype wire
