/*
================================================================================
  cxp_tx_io_ack
  CoaXPress 1.1.1 (CXP-001-2015) §8.3.3 / Table 17 — I/O ack packet source.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-27

    Description:
      I/O acknowledgment packet source.  NEW in v1.1 (the v1.0 link had
      no acknowledgment of trigger packets).

      Why this module exists:
        §8.3.2: "Trigger packets shall be acknowledged (see section
        8.3.3)."  §8.3.3: "Trigger packets shall be acknowledged with
        an I/O acknowledgment packet."  A v1.1.1 camera device that
        accepts host triggers on the (low-speed) downconnection
        therefore *must* emit a K28.6 I/O-ack packet back to the host
        on every received trigger packet — otherwise the host's §8.3.3
        transmission-timeout fires and the device is non-compliant.
        v1.0 had no such packet (K28.6 was unused in the Table 11
        K-code map; v1.1 assigns it to the I/O ack).

      Packet format (Table 17):
        Word | P0     P1     P2     P3   | kmask | Notes
        -----+----------------------------+-------+-----------------------
        HDR  | K28.6  K28.6  K28.6  K28.6 | 1111  | I/O-ack ind, m_sop=1
        COD  | Code   Code   Code   Code  | 0000  | 4xack code, m_eop=1

        Acknowledgment code 0x01 = "Trigger packet received OK"
        (Table 17).  Packet size: 2 words (8 bytes).  The code is
        parameterised (`p_ACK_CODE`) so a future revision that defines
        additional codes can instantiate per-condition variants without
        touching the FSM; the only code the standard defines today is
        0x01.

      Trigger-received input (`trig_rcvd_i`):
        Asserted by the integration for one (or more) tx_clk cycles
        whenever a trigger packet has been received and accepted by
        the RX path (in this build: cxp_rx_packet_parser `trig_valid`
        qualified with `~trig_glitch`, so corrupted leaders are not
        acknowledged — they are recovered by the host's resend-on-
        timeout rule, §8.3.3).  We rising-edge-detect it internally,
        so a level-held or multi-cycle assertion still produces
        exactly one ack packet.

      Clock domain / CDC:
        The trigger packet is received in the RX (`rx_clk`/`os_clk`)
        domain while this generator runs in `tx_clk`; cxp_cdc_layer
        crosses the strobe with a cxp_cdc_pulse when the clocks are
        unrelated.  The edge detect here makes the module tolerant of a
        strobe longer than one cycle.

      Pending queue:
        Once a packet is in flight cxp_tx_inserter may hold this port
        for a few words (a trigger, or an IDLE that is due; Table 13
        puts the I/O ack at priority 1).  Further `trig_rcvd_i`
        events arriving while a packet drains are counted in a small
        saturating `pend_q` so each received trigger still gets its
        own ack.  Under a spec-compliant sender the outstanding count
        is tiny: §8.3.3 forbids the trigger sender from issuing a new
        trigger packet until the previous one is acknowledged (or its
        timeout expires), so p_PEND_W=2 (<=3 queued) is ample
        headroom; a count beyond that saturates (silently dropped —
        it cannot occur with a compliant peer and is recovered by the
        peer's resend-on-timeout otherwise).

      Priority:
        Table 13: priority 0 = Trigger, 1 = I/O acknowledgment, 2 =
        all other packets.  This block only produces a well-framed
        2-word packet with sop/eop/kmask; cxp_tx_inserter sends it at
        the next word boundary of whatever long packet is on the wire
        (below a trigger, above everything else) and takes both words
        back to back through the valid/ready handshake.

    Versions:
        2026-05-27 - 0.1:   - Restyle to coding-style template
        2026-09-19 - 0.2:   - Packet words from cxp_tx_short_pkt; abort_i
        2026-09-26 - 0.3:   - No abort_i; sent by cxp_tx_inserter
        2026-09-27 - 0.4:   - Packet words out as one cxp_txw_t (m_o)

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_tx_io_ack #(
    // Acknowledgment code emitted in word 1 (Table 17).  0x01 =
    // "Trigger packet received OK" — the only code v1.1.1 defines.
    parameter logic [7:0] p_ACK_CODE = cxp_pkg::IOACK_CODE_OK,
    // Pending-ack saturating-counter width.  <= (2**p_PEND_W - 1) acks
    // may be queued behind an in-flight / back-pressured packet.
    parameter int         p_PEND_W   = 2
) (
    // Transmit clock domain
    input  wire  logic        tx_clk,                           // TX clock
    input  wire  logic        tx_rst_n,                         // Active-low sync reset

    // Trigger-packet-received strobe (tx_clk domain — see header for
    // the CDC contract).  Rising-edge detected: each 0->1 transition
    // queues exactly one I/O-ack packet.
    input  wire  logic        trig_rcvd_i,                      // Trigger event in

    // Packet output (to cxp_tx_inserter, I/O-ack port)
    output cxp_pkg::cxp_txw_t m_o,                              // packet word, P0 in [7:0]
    input  wire  logic        m_ready_i                         // Inserter ready
);

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    // K28_6 (I/O-ack marker byte) comes from cxp_pkg.
    // Width-clean constant 1 for pend_q +/- 1 (lint-friendly).
    localparam logic [p_PEND_W-1:0]   PEND_ONE = {{(p_PEND_W-1){1'b0}}, 1'b1};

    //=======================================================================
    // Signals
    //=======================================================================

    logic                  trig_rcvd_q;     // For rising-edge detect
    logic                  rcvd_evt;        // Detected rising edge

    logic [p_PEND_W-1:0]   pend_q;          // Saturating pending-ack counter
    logic                  pend_full;       // pend_q == all-ones

    logic                  ack_done;        // EOP handshake (ack leaves queue)
    logic                  work_avail;      // Queue non-empty or fresh event
    logic                  work_more;       // More queued behind this ack

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign rcvd_evt   = trig_rcvd_i & ~trig_rcvd_q;
    assign pend_full  = (pend_q == {p_PEND_W{1'b1}});
    assign work_avail = (pend_q != '0) | rcvd_evt;
    // pend_q still counts the packet completing now, so a residual
    // count > 1 (or a fresh event this cycle) means more work remains.
    assign work_more  = (pend_q > PEND_ONE) | rcvd_evt;

    //=======================================================================
    // Pending-ack counter
    //=======================================================================

    // pend_q accounting:
    //   +1  when a new trig_rcvd_i edge is observed (saturate at all-ones)
    //   -1  when the in-flight packet's EOP word handshakes (ack_done)
    // Both can happen in the same cycle; the net delta is applied once.
    always_ff @(posedge tx_clk or negedge tx_rst_n) begin
        if (!tx_rst_n) begin
            trig_rcvd_q <= 1'b0;
            pend_q      <= '0;
        end else begin
            trig_rcvd_q <= trig_rcvd_i;

            unique case ({rcvd_evt, ack_done})
                2'b10: if (!pend_full)   pend_q <= pend_q + PEND_ONE; // enqueue
                2'b01: if (pend_q != '0) pend_q <= pend_q - PEND_ONE; // consume
                // 2'b11: enqueue+consume cancel -> pend_q unchanged,
                //        unless the queue was saturated (the enqueue was
                //        dropped) so only the consume applies -> net -1.
                2'b11: if (pend_full)    pend_q <= pend_q - PEND_ONE;
                default: ; // 2'b00 — no change
            endcase
        end
    end

    //=======================================================================
    // Packet source — 4×K28.6, 4×ack code.
    //=======================================================================

    cxp_tx_short_pkt cxp_tx_short_pkt_i (
        .tx_clk    (tx_clk),
        .tx_rst_n  (tx_rst_n),
        .avail_i   (work_avail),
        .more_i    (work_more),
        .leader_i  (cxp_pkg::K28_6),
        .code_i    (p_ACK_CODE),
        .start_o   (),
        .busy_o    (),
        .done_o    (ack_done),
        .m_data_o  (m_o.data),
        .m_kmask_o (m_o.kmask),
        .m_valid_o (m_o.valid),
        .m_sop_o   (m_o.sop),
        .m_eop_o   (m_o.eop),
        .m_ready_i (m_ready_i)
    );

endmodule

`default_nettype wire
