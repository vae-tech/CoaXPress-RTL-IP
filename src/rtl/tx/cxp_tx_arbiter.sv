/*
================================================================================
  cxp_tx_arbiter
  CoaXPress 1.1.1 device-side long-packet arbiter (§8.2.4, Table 13).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2025-05-27

    Description:
      Chooses which long packet goes to the wire next.  The sources are
      the Table 13 priority-2 packets — control acknowledgment, connection
      test, stream — as an array of cxp_txw_t words; the index is the
      priority (0 highest).  Control acknowledgment ranks first because
      the host's control-cycle timer makes its latency matter.

      Between packets the arbiter offers the SOP of the highest-priority
      source that has one; once that SOP is taken the source owns the
      arbiter until its EOP is taken.  Nothing pre-empts a long packet
      here: triggers, I/O acknowledgments and IDLE words are inserted
      between its words by cxp_tx_inserter, which takes the offered word
      (m_ready_i) only when none of them is due.

      A packet, once started, is sent whole.  Every source offers its
      words back to back (the stream from a store-and-forward FIFO, the
      control acknowledgment from a read buffer filled before the request,
      the test generator from a counter), so there is no watchdog and no
      way to drop a packet; cxp_sva checks, in cxp_tx_domain, that the
      owner presents a word in every cycle it holds the arbiter.

      owner_q encoding: 0 = between packets, i+1 = port i owns the arbiter.

    Versions:
        2025-05-27 - 0.1:   - Init
        2026-09-19 - 0.2:   - Port array with policy table; abort_o;
                              watchdog keeps a word granted on expiry
        2026-09-26 - 0.3:   - No watchdog, no abort, no suppression: a
                              started packet always completes
        2026-09-26 - 0.4:   - Long packets only: triggers, I/O
                              acknowledgments and IDLE moved to
                              cxp_tx_inserter; no pre-emption

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_tx_arbiter #(
    parameter int p_PORTS = cxp_pkg::TX_PORTS                   // number of long-packet sources
) (
    input  wire  logic                  tx_clk,                 // TX clock
    input  wire  logic                  tx_rst_n,               // async active-low reset

    // Long-packet sources, index = priority
    input  wire  cxp_pkg::cxp_txw_t     src_i [p_PORTS],        // source words
    output logic [p_PORTS-1:0]          ready_o,                // word taken this cycle

    // The word offered to cxp_tx_inserter
    output cxp_pkg::cxp_txw_t           m_o,                    // offered word
    input  wire  logic                  m_ready_i               // the inserter takes it
);

    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    localparam int SW = cnt_w(p_PORTS);             // owner: 0 none, 1..p_PORTS

    localparam logic [SW-1:0] S_NONE = '0;

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    if (p_PORTS < 1) begin : g_chk_ports
        $error("cxp_tx_arbiter: p_PORTS (=%0d) must be >= 1", p_PORTS);
    end

    //=======================================================================
    // Signals
    //=======================================================================

    logic [SW-1:0] owner_q;                     // port + 1 owning the arbiter, or none
    logic [SW-1:0] sel;                         // port + 1 offered this cycle, or none
    logic          take;                        // the offered word is taken

    //=======================================================================
    // Per-cycle selection: the owner, or between packets the
    // highest-priority SOP.
    //=======================================================================

    always_comb begin
        sel = owner_q;
        if (owner_q == S_NONE) begin
            for (int i = p_PORTS - 1; i >= 0; i--) begin
                if (src_i[i].valid && src_i[i].sop) sel = SW'(i + 1);
            end
        end
    end

    //=======================================================================
    // Output mux.
    //=======================================================================

    always_comb begin
        m_o     = '0;
        ready_o = '0;
        for (int i = 0; i < p_PORTS; i++) begin
            if (sel == SW'(i + 1)) begin
                m_o        = src_i[i];
                ready_o[i] = m_ready_i;
            end
        end
    end

    assign take = m_o.valid && m_ready_i;

    //=======================================================================
    // Sequential: a taken SOP opens the packet, a taken EOP closes it.
    //=======================================================================

    always_ff @(posedge tx_clk or negedge tx_rst_n) begin
        if (!tx_rst_n)             owner_q <= S_NONE;
        else if (take && m_o.eop)  owner_q <= S_NONE;
        else if (take && m_o.sop)  owner_q <= sel;
    end

endmodule

`default_nettype wire
