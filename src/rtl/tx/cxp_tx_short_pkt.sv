/*
================================================================================
  cxp_tx_short_pkt
  CoaXPress 1.1.1 — two-word "leader + code" packet source (§8.3.2, §8.3.3).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-19

    Description:
      The high-speed trigger packet (Table 16) and the I/O acknowledgment
      (Table 17) share one shape:

          Word 0 : 4×leader_i   kmask 1111, m_sop_o   (K28.4 / K28.2 / K28.6)
          Word 1 : 4×code_i     kmask 0000, m_eop_o   (delay / ack code)

      The caller keeps its own event queue and tells this block when a
      packet is wanted:

        avail_i  in ST_IDLE: start a packet next cycle.
        more_i   while the last word is accepted: start the next packet
                 back-to-back instead of returning to ST_IDLE.

      start_o marks the cycle a packet is committed (the caller picks its
      leader then), busy_o a packet in flight, done_o the cycle its last
      word is accepted.  leader_i and code_i must hold for the whole
      packet.  A packet, once started, is always sent whole.

    Versions:
        2026-09-19 - 0.1:   - Init (FSM shared by tx_trigger_hs and
                              tx_io_ack)
        2026-09-26 - 0.2:   - No abort: a started packet always completes

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_tx_short_pkt (
    input  wire  logic        tx_clk,                       // TX clock
    input  wire  logic        tx_rst_n,                     // async active-low reset

    // Packet request
    input  wire  logic        avail_i,                      // work queued (idle start)
    input  wire  logic        more_i,                       // work queued (back-to-back)
    input  wire  logic [7:0]  leader_i,                     // word-0 K-code
    input  wire  logic [7:0]  code_i,                       // word-1 data byte
    output logic              start_o,                      // packet committed this cycle
    output logic              busy_o,                       // a packet is in flight
    output logic              done_o,                       // last word accepted

    // Packet out
    output logic [31:0]       m_data_o,                     // word data
    output logic [3:0]        m_kmask_o,                    // per-byte K-flag
    output logic              m_valid_o,                    // word valid
    output logic              m_sop_o,                      // start of packet
    output logic              m_eop_o,                      // end of packet
    input  wire  logic        m_ready_i                     // inserter ready
);

    import cxp_pkg::*;
    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Types ------

    typedef enum logic [1:0] {
        ST_IDLE,    // nothing in flight
        ST_HDR,     // leader word
        ST_COD      // code word
    } state_t;

    //=======================================================================
    // Signals
    //=======================================================================

    state_t state_q;
    state_t state_n;
    logic   out_fire;                  // m_valid_o & m_ready_i

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign out_fire = m_valid_o & m_ready_i;
    assign start_o  = (state_n == ST_HDR) && (state_q != ST_HDR);
    assign busy_o   = (state_q != ST_IDLE);
    assign done_o   = (state_q == ST_COD) && out_fire;

    //=======================================================================
    // Output mux
    //=======================================================================

    always_comb begin
        m_data_o  = 32'h0000_0000;
        m_kmask_o = KMASK_NONE;
        m_valid_o = 1'b0;
        m_sop_o   = 1'b0;
        m_eop_o   = 1'b0;
        unique case (state_q)

            //===============================================================
            // Idle: output bus quiet.
            //
            ST_IDLE: ;

            //===============================================================
            // Header: 4×leader K-code, SOP set.
            //
            ST_HDR: begin
                m_data_o  = rep4(leader_i);
                m_kmask_o = KMASK_ALL;
                m_valid_o = 1'b1;
                m_sop_o   = 1'b1;
            end

            //===============================================================
            // Code: 4×code data word, EOP set.
            //
            ST_COD: begin
                m_data_o  = rep4(code_i);
                m_valid_o = 1'b1;
                m_eop_o   = 1'b1;
            end

            default: ;
        endcase
    end

    //=======================================================================
    // FSM next-state
    //=======================================================================

    always_comb begin
        state_n = state_q;
        unique case (state_q)
            ST_IDLE: if (avail_i)  state_n = ST_HDR;
            ST_HDR:  if (out_fire) state_n = ST_COD;
            ST_COD:  if (out_fire) state_n = more_i ? ST_HDR : ST_IDLE;
            default: state_n = ST_IDLE;
        endcase
    end

    //=======================================================================
    // FSM sequential
    //=======================================================================

    always_ff @(posedge tx_clk or negedge tx_rst_n) begin
        if (!tx_rst_n) state_q <= ST_IDLE;
        else           state_q <= state_n;
    end

endmodule

`default_nettype wire
