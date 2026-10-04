/*
================================================================================
  cxp_tx_inserter
  CoaXPress 1.1.1 — per-word downlink scheduler (§8.2.4, §8.2.5, Table 13).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-26

    Description:
      Puts one word on the wire per tx_clk.  Every cycle it picks, in this
      order (§8.2.4, Table 13):

        1. the second word of a two-word packet whose first word just went
        2. a trigger packet (priority 0)            while run_q <= 97
        3. an I/O acknowledgment (priority 1)       while run_q <= 95
        4. an IDLE word, once run_q >= p_IDLE_SOFT
        5. the long-packet word cxp_tx_arbiter offers (priority 2)
        6. an IDLE word

      run_q counts the words sent since the last IDLE.  A trigger or I/O
      acknowledgment is inserted at the next word boundary of whatever
      long packet is on the wire, which then resumes (§8.2.4); the long
      packet is stalled by leaving long_ready_o low, and IDLE words fill
      any cycle nothing else is due (§8.2.5.2 allows them inside a
      high-speed packet).

      Two-word packets (Tables 16, 17) are never split: once a leader is
      sent the next word is its Delay / code word, so the trigger waits one
      word behind an I/O acknowledgment's first word.  The limits on the
      run keep the §8.2.5.1 rule (an IDLE at least every 100 words) with
      no split: an I/O acknowledgment starts only while two more
      two-word packets would still fit (run_q <= 95), a trigger while one
      fits (run_q <= 97), and from p_IDLE_SOFT on nothing else starts
      before the IDLE.  The run therefore never passes 99, and a trigger
      finds its word free unless another trigger or an I/O acknowledgment
      has just filled the last words before the IDLE.

      An I/O acknowledgment is on the wire at most three words after its
      leader is offered (a trigger, then the IDLE it made due).

      The wire word is registered: m_data_o / m_kmask_o carry the word
      chosen in the previous cycle.  After reset they carry IDLE.

    Versions:
        2026-09-26 - 0.1:   - Init (triggers, I/O acknowledgments and the
                              IDLE cadence out of cxp_tx_arbiter and
                              cxp_tx_idle_gen)

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_tx_inserter #(
    parameter int p_IDLE_SOFT = cxp_pkg::IDLE_SOFT_RUN          // run at which an IDLE is due
) (
    input  wire  logic              tx_clk,                     // TX clock
    input  wire  logic              tx_rst_n,                   // async active-low reset

    // Two-word packets: trigger (Table 16) and I/O acknowledgment (Table 17)
    input  wire  cxp_pkg::cxp_txw_t trig_i,                     // trigger words
    output logic                    trig_ready_o,               // trigger word sent
    input  wire  cxp_pkg::cxp_txw_t ioack_i,                    // I/O-ack words
    output logic                    ioack_ready_o,              // I/O-ack word sent

    // Long packets from cxp_tx_arbiter
    input  wire  cxp_pkg::cxp_txw_t long_i,                     // offered long word
    output logic                    long_ready_o,               // long word sent

    // Wire word, registered, to the 8B/10B encoder
    output logic [31:0]             m_data_o,                   // word, P0 in [7:0]
    output logic [3:0]              m_kmask_o                   // per-byte K flag
);

    import cxp_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    localparam int MAX_RUN   = IDLE_MAX_INTERVAL - 1;  // non-IDLE words between IDLEs
    localparam int TRIG_LAST = MAX_RUN - 2;            // last run a trigger may start at
    localparam int ACK_LAST  = MAX_RUN - 4;            // ... an I/O acknowledgment

    // ------ Types ------

    typedef enum logic [1:0] {
        SH_NONE,    // no two-word packet half sent
        SH_TRIG,    // trigger leader sent, Delay word next
        SH_IOACK    // I/O-ack leader sent, code word next
    } short_t;

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    if (p_IDLE_SOFT < 1 || p_IDLE_SOFT > ACK_LAST) begin : g_chk_soft
        $error("cxp_tx_inserter: p_IDLE_SOFT (=%0d) must be 1..%0d (§8.2.5.1)",
               p_IDLE_SOFT, ACK_LAST);
    end

    //=======================================================================
    // Signals
    //=======================================================================

    short_t      short_q, short_n;              // two-word packet half sent
    logic [6:0]  run_q;                         // words since the last IDLE
    logic        idle_due;                      // run reached p_IDLE_SOFT
    logic        trig_go;                       // a trigger word goes this cycle
    logic        ioack_go;                      // an I/O-ack word goes this cycle
    logic        long_go;                       // the long word goes this cycle
    logic [31:0] word_data;                     // word chosen this cycle
    logic [3:0]  word_kmask;
    logic        word_idle;                     // the chosen word is IDLE

    //=======================================================================
    // Per-word choice.
    //=======================================================================

    assign idle_due = (run_q >= 7'(p_IDLE_SOFT));

    always_comb begin
        trig_go  = 1'b0;
        ioack_go = 1'b0;
        long_go  = 1'b0;
        unique case (short_q)
            SH_TRIG:  trig_go  = 1'b1;
            SH_IOACK: ioack_go = 1'b1;
            default: begin
                if (trig_i.valid && trig_i.sop && run_q <= 7'(TRIG_LAST))
                    trig_go = 1'b1;
                else if (ioack_i.valid && ioack_i.sop && run_q <= 7'(ACK_LAST))
                    ioack_go = 1'b1;
                else if (!idle_due)
                    long_go = long_i.valid;
            end
        endcase
    end

    assign trig_ready_o  = trig_go;
    assign ioack_ready_o = ioack_go;
    assign long_ready_o  = long_go;

    always_comb begin
        word_data  = IDLE_WORD;
        word_kmask = KMASK_IDLE;
        word_idle  = 1'b0;
        if (trig_go) begin
            word_data  = trig_i.data;
            word_kmask = trig_i.kmask;
        end else if (ioack_go) begin
            word_data  = ioack_i.data;
            word_kmask = ioack_i.kmask;
        end else if (long_go) begin
            word_data  = long_i.data;
            word_kmask = long_i.kmask;
        end else begin
            word_idle  = 1'b1;
        end
    end

    //=======================================================================
    // Next two-word state: a leader sent opens it, its second word closes it.
    //=======================================================================

    always_comb begin
        short_n = SH_NONE;
        if (short_q == SH_NONE) begin
            if (trig_go && !trig_i.eop)   short_n = SH_TRIG;
            if (ioack_go && !ioack_i.eop) short_n = SH_IOACK;
        end
    end

    //=======================================================================
    // Sequential: two-word state, IDLE run, registered wire word.
    //=======================================================================

    always_ff @(posedge tx_clk or negedge tx_rst_n) begin
        if (!tx_rst_n) begin
            short_q   <= SH_NONE;
            run_q     <= '0;
            m_data_o  <= IDLE_WORD;
            m_kmask_o <= KMASK_IDLE;
        end else begin
            short_q   <= short_n;
            run_q     <= word_idle ? 7'd0 : run_q + 7'd1;
            m_data_o  <= word_data;
            m_kmask_o <= word_kmask;
        end
    end

endmodule

`default_nettype wire
