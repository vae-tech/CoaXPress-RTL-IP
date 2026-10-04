/*
================================================================================
  cxp_tx_linktest
  CoaXPress 1.1.1 (CXP-001-2015) §8.7 / Table 23 / §10.3.35 / §10.3.38 — TX.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-27

    Description:
      Device -> host connection-test packet generator (Test Generator).
      Reference: JIIA CXP-001-2015 §8.7 "Connection Test", packet
      format Table 23, §10.3.35 TestMode, §10.3.38 TestPacketCountTx.
      Migrated from the v1.0 numbering (old §6.7.4 / Table 22).

      When the host writes 1 to the TestMode bootstrap register
      (`cfg_test_mode_i`, §10.3.35) the device emits a continuous
      stream of type-0x04 connection-test packets, separated by >= 16
      word intervals so control acknowledges still have room (§8.7
      spacing requirement).  Each transmitted test packet increments
      the v1.1 TestPacketCountTx register (§10.3.38), exported here
      as an 8-byte (64-bit) count that clears on `clr_pkt_count_i`
      (host write of 0 as a pulse, ConnectionReset as a level).  Only a
      packet that started after the last clear is counted, so a packet
      in flight across a ConnectionReset does not leave the count at 1.

      Packet format (1027 words total — note: NO CRC field; table 22
      stops at the K29.7 trailer).  Bytes are laid out P0..P3 in
      m_o.data[7:0]..[31:24]:

          Word    | P0     P1     P2     P3   | kmask | Notes
          --------+----------------------------+-------+-----------------------
          K27.7   | K27.7  K27.7  K27.7  K27.7 | 1111  | SOP framing (m_sop=1)
          TYPE    | 0x04   0x04   0x04   0x04  | 0000  | linktest indication
          D[0]    | 0x00   0x01   0x02   0x03  | 0000  |
          D[1]    | 0x04   0x05   0x06   0x07  | 0000  |
          ...     | ...                         |       | 1024 data words total
          D[1023] | 0xFC   0xFD   0xFE   0xFF  | 0000  |
          K29.7   | K29.7  K29.7  K29.7  K29.7 | 1111  | EOP (m_eop=1)

      Why no CRC?  Spec table 22 deliberately omits a CRC word from
      the linktest packet — the receiver does its own per-word
      compare against a locally-regenerated counter sequence
      (cxp_rx_linktest), so a packet CRC would be redundant.  Total
      packet size stays at the table's 1027 words: 1 SOP + 1 TYPE +
      1024 data + 1 EOP.

      Sequence generation:
        The 4096-byte payload is the byte sequence 0x00 0x01 ... 0xFF
        repeated 16 times (4096 = 16 x 256).  Packed into 32-bit
        words with P0 in the LSB lane the i-th data word is:

            D[i] = { (4i+3)&0xFF, (4i+2)&0xFF, (4i+1)&0xFF, (4i+0)&0xFF }

        We track an 8-bit `seq_q` (the P0 byte for the next word),
        advance by 4 each accepted data word, and form the word
        combinationally as {seq+3, seq+2, seq+1, seq}.  Natural
        rollover at 256 produces the 16x repetition without any
        extra outer counter.

      Mode exit (§6.7.4 / modules.md test #4):
        `cfg_test_mode_i` is sampled only between packets (in ST_IDLE
        and at the end of ST_GAP).  A 1->0 transition observed
        mid-packet completes the in-flight packet first, then drains
        the gap, then returns to idle — "linktest stops after current
        packet, stream resumes within 1 packet boundary".

      suppress_traffic_o:
        Asserted whenever this module is anywhere except ST_IDLE, OR
        while `cfg_test_mode_i` is high.  No new stream packet starts
        while it is high (cxp_tx_stream_pkt); one already on the wire
        completes first.  Control acknowledgments, triggers and I/O
        acknowledgments are not affected (§8.7.4 restricts data packets).

      Spacing:
        Spec requires "at least 16 word intervals" between
        consecutive linktest packets so control packets can be
        interleaved.  After EOP fires we sit in ST_GAP for
        `GAP_WORDS` cycles with m_o.valid=0; the arbiter gives the
        wire to a control acknowledgment if one waits, otherwise
        cxp_tx_inserter sends IDLE.

    Versions:
        2026-05-27 - 0.1:   - Restyle to coding-style template
        2026-09-19 - 0.2:   - Packet words from cxp_tx_pkt_framer; abort_i
        2026-09-25 - 0.3:   - A packet started before a clear is not counted
        2026-09-26 - 0.4:   - No abort: a started test packet always
                              completes
        2026-09-27 - 0.5:   - GAP_WORDS >= 1 checked
        2026-09-27 - 0.6:   - Packet words out as one cxp_txw_t (m_o)

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_tx_linktest #(
    parameter int DATA_WORDS = cxp_pkg::LT_DATA_WORDS,  // Table 23: 1024 data words per packet
    parameter int GAP_WORDS  = cxp_pkg::LT_GAP_WORDS    // idle words between packets
) (
    // Transmit clock domain
    input  wire  logic        tx_clk,                           // TX clock
    input  wire  logic        tx_rst_n,                         // Active-low sync reset

    // Control
    input  wire  logic        cfg_test_mode_i,                  // §10.3.35 enable
    output logic              suppress_traffic_o,               // no new stream packet

    // §10.3.38 TestPacketCountTx — transmitted-test-packet count,
    // 8-byte per-connection register.  Clears on host write of 0 or
    // ConnectionReset (§10.3.28).
    input  wire  logic        clr_pkt_count_i,                  // Clear pulse
    output logic [63:0]       pkt_count_o,                      // §10.3.38 count

    // Packet output (to cxp_tx_arbiter, long-packet port 1)
    output cxp_pkg::cxp_txw_t m_o,                              // packet word, P0 in [7:0]
    input  wire  logic        m_ready_i                         // Arbiter ready
);

    import cxp_pkg::*;
    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    // Inter-packet gap counter width (0..GAP_WORDS).
    localparam int GW = cnt_w(GAP_WORDS);

    // ------ Types ------

    typedef enum logic [1:0] {
        ST_IDLE,    // test mode off
        ST_PKT,     // framer sending a test packet
        ST_GAP      // idle words between packets
    } state_t;

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    if (DATA_WORDS < 1 || DATA_WORDS > 65535) begin : g_chk_data_words
        $error("cxp_tx_linktest: DATA_WORDS (=%0d) must be 1..65535", DATA_WORDS);
    end

    // The gap counter counts 0 .. GAP_WORDS - 1; 0 would give a two-word
    // gap.  The device's instance uses LT_GAP_WORDS, which its top checks
    // against the 16 word intervals of §8.7.4.
    if (GAP_WORDS < 1) begin : g_chk_gap_words
        $error("cxp_tx_linktest: GAP_WORDS (=%0d) must be >= 1", GAP_WORDS);
    end

    //=======================================================================
    // Signals
    //=======================================================================

    state_t        state_q;
    state_t        state_n;

    logic [7:0]    seq_q;              // Current P0 sequence byte
    logic [GW-1:0] gap_q;              // 0..GAP_WORDS-1
    logic [63:0]   pkt_count_q;        // §10.3.38 accumulator
    logic          counted_q;          // the packet in flight started after the last clear

    logic [31:0]   data_word;          // Current data word (comb from seq_q)
    logic          start;              // framer starts a packet this cycle
    logic          pl_ready;           // framer takes a data word
    logic          pkt_done;           // EOP word accepted
    logic          gap_done;           // Gap counter reached terminal value

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign pkt_count_o = pkt_count_q;

    // Current data word — combinational from seq_q.
    assign data_word = { (seq_q + 8'd3),
                         (seq_q + 8'd2),
                         (seq_q + 8'd1),
                          seq_q };

    // Suppress stream traffic anytime we are armed (test mode on) or
    // still draining a packet/gap from a previous test-mode session.
    assign suppress_traffic_o = cfg_test_mode_i | (state_q != ST_IDLE);

    assign gap_done = (GW'(gap_q) == GW'(GAP_WORDS - 1));
    assign start    = (state_n == ST_PKT) && (state_q != ST_PKT);

    //=======================================================================
    // FSM next-state
    //=======================================================================

    always_comb begin
        state_n = state_q;
        unique case (state_q)

            //===============================================================
            // Idle: start the first packet when test mode turns on.
            //
            ST_IDLE: begin
                if (cfg_test_mode_i) state_n = ST_PKT;
            end

            //===============================================================
            // Packet: wait for its EOP word.
            //
            ST_PKT: begin
                if (pkt_done) state_n = ST_GAP;
            end

            //===============================================================
            // Gap: next packet, or stop once test mode is off.
            //
            ST_GAP: begin
                if (gap_done) state_n = cfg_test_mode_i ? ST_PKT : ST_IDLE;
            end

            default: state_n = ST_IDLE;
        endcase
    end

    //=======================================================================
    // FSM sequential + TestPacketCountTx + counters
    //=======================================================================

    always_ff @(posedge tx_clk or negedge tx_rst_n) begin
        if (!tx_rst_n) begin
            state_q     <= ST_IDLE;
            seq_q       <= 8'h00;
            gap_q       <= '0;
            pkt_count_q <= 64'h0;
            counted_q   <= 1'b0;
        end else begin
            state_q <= state_n;

            // §10.3.38 TestPacketCountTx: one increment per
            // transmitted test packet, counted as its EOP word is
            // accepted.  Clear (host write 0 / ConnectionReset)
            // takes precedence.
            if (clr_pkt_count_i)
                pkt_count_q <= 64'h0;
            else if (pkt_done && counted_q)
                pkt_count_q <= pkt_count_q + 64'd1;

            if (clr_pkt_count_i) counted_q <= 1'b0;
            else if (start)      counted_q <= 1'b1;

            // Restart the sequence for every packet.
            if (start)
                seq_q <= 8'h00;
            else if (pl_ready)
                seq_q <= seq_q + 8'd4;

            // Gap counter — held at 0 outside ST_GAP, increments inside it.
            if (state_q == ST_GAP) begin
                if (!gap_done) gap_q <= gap_q + GW'(1);
            end else begin
                gap_q <= '0;
            end
        end
    end

    //=======================================================================
    // Packet framer — SOP, TYPE, DATA_WORDS counter words, EOP (no CRC).
    //=======================================================================

    cxp_tx_pkt_framer #(
        .p_HDR_WORDS (2),
        .p_HAS_CRC   (1'b0)
    ) cxp_tx_pkt_framer_i (
        .tx_clk       (tx_clk),
        .tx_rst_n     (tx_rst_n),
        .start_i      (start),
        .hdr_last_i   (3'd1),
        .hdr_i        (rep4(PKT_TYPE_LT)),
        .has_body_i   (1'b1),
        .skip_empty_i (1'b0),
        .n_words_i    (16'(DATA_WORDS)),
        .pl_data_i    (data_word),
        .pl_kmask_i   (KMASK_NONE),
        .pl_valid_i   (1'b1),
        .pl_eop_i     (1'b0),
        .pl_ready_o   (pl_ready),
        .busy_o       (),
        .data_phase_o (),
        .data_idx_o   (),
        .m_data_o     (m_o.data),
        .m_kmask_o    (m_o.kmask),
        .m_valid_o    (m_o.valid),
        .m_sop_o      (m_o.sop),
        .m_eop_o      (m_o.eop),
        .m_ready_i    (m_ready_i),
        .done_o       (pkt_done)
    );

endmodule

`default_nettype wire
