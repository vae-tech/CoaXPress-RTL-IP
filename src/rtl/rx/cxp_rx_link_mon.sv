/*
================================================================================
  cxp_rx_link_mon
  CoaXPress 1.1.1 (CXP-001-2015) §8.2.5.1 / §10.1.1 / §10.2 — uplink IDLE
  monitor: link detection and loss of lock.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-19

    Description:
      Watches the decoded uplink word stream for the Table 14 IDLE word
      (K28.5 K28.1 K28.1 D21.5, kmask 0111) and owns the link state:

        DOWN  After reset, or while the sampler has no symbol lock.  Words
              are not forwarded.  p_LOCK_IDLES error-free IDLE words with
              the sampler locked bring the link UP (§10.1.1 Detected).
        UP    Words are forwarded to the packet parser.  A counter runs on
              every word and clears on each IDLE.  §8.2.5.1 lets a
              low-speed transmitter send 10 000 words between IDLEs, so
              the link is declared lost only after p_LOSS_WORDS words
              without one (default twice the spec interval, 800 000
              bits).

      The monitor also decides, with the sampler locked and in both
      states, that the sampler's character or word framing is wrong
      (§10.2: "character and word alignment re-established"):
        * p_BAD_WORDS words with a code or disparity error since the last
          clean IDLE (a lock on the wrong bit phase, a bit slip or a line
          that went quiet), or
        * one error-free word with K28.5 in P1..P3 — Table 14 sends K28.5
          only in P0, so the lanes turned by whole characters.
      Error-free words that are not IDLE do not count: §8.2.5.1 lets a
      low-speed host send one IDLE in 10 000 words and §8.7.3 only one
      between 1027-word test packets, so a link coming up in the middle
      of such traffic sees long runs of clean data between IDLEs.
      Either, or the IDLE loss, sends the sampler back to hunt (resync_o)
      and the link goes DOWN; the sampler re-anchors on the next comma.

      flush_o pulses for one cycle whenever the link leaves UP (IDLE loss,
      wrong framing or the sampler losing lock) and with every resync_o;
      the packet parser uses it to end a packet in flight.

      link_detected_o = sampler lock AND UP.

    Versions:
        2026-09-19 - 0.1:   - Init (replaces the lane aligner, whose offset
                              the sampler already fixes at 0)
        2026-09-26 - 0.2:   - Wrong framing detected while DOWN and UP
                              (p_BAD_WORDS, K28.5 outside P0)
        2026-10-04 - 0.3:   - DOWN counts errored words only, as UP, and
                              checks K28.5 outside P0: the link comes up
                              under sparse IDLE (§8.2.5.1, §8.7.3)

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_rx_link_mon #(
    // Error-free IDLE words, with the sampler locked, to bring the link up.
    parameter int p_LOCK_IDLES   = cxp_pkg::RX_LOCK_IDLES_DEFAULT,
    // Words without an IDLE before the link is declared lost.
    parameter int p_LOSS_WORDS   = cxp_pkg::RX_LOSS_WORDS_DEFAULT,
    // Words since the last clean IDLE that do not fit the framing (see
    // above) before the sampler is sent back to hunt.
    parameter int p_BAD_WORDS    = cxp_pkg::RX_BAD_WORDS_DEFAULT,
    // Benches only: allow p_LOSS_WORDS below the §8.2.5.1 interval.
    parameter bit p_SHORT_LOSS_OK = 1'b0
) (
    input  wire  logic        rx_clk,                           // RX clock
    input  wire  logic        rx_rst_n,                         // async reset, active-low

    input  wire  logic        rx_lock_i,                        // sampler symbol lock

    // Decoded words from the 8B/10B decoders
    input  wire  logic [31:0] data_i,                           // word, P0 in [7:0]
    input  wire  logic [3:0]  kmask_i,                          // per-lane K flag
    input  wire  logic        err_i,                            // code or disparity error
    input  wire  logic        valid_i,                          // word valid

    // Words forwarded while the link is up
    output logic [31:0]       data_o,
    output logic [3:0]        kmask_o,
    output logic              err_o,
    output logic              valid_o,

    output logic              up_o,                             // link up (IDLE-aligned)
    output logic              link_detected_o,                  // §10.1.1 Detected
    output logic              resync_o,                         // 1-cycle: sampler re-hunts
    output logic              flush_o                           // 1-cycle: link left UP
);

    import cxp_pkg::*;
    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    localparam int HW = cnt_w(p_LOCK_IDLES);
    localparam int LW = cnt_w(p_LOSS_WORDS);
    localparam int BW = cnt_w(p_BAD_WORDS);

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    if (p_LOCK_IDLES < 1) begin : g_chk_lock_idles
        $error("cxp_rx_link_mon: p_LOCK_IDLES (=%0d) must be >= 1", p_LOCK_IDLES);
    end

    if (p_BAD_WORDS < 1) begin : g_chk_bad_words
        $error("cxp_rx_link_mon: p_BAD_WORDS (=%0d) must be >= 1", p_BAD_WORDS);
    end

    if (!p_SHORT_LOSS_OK && p_LOSS_WORDS < LS_IDLE_MAX_WORDS) begin : g_chk_loss_words
        $error("cxp_rx_link_mon: p_LOSS_WORDS (=%0d) < %0d, the low-speed IDLE interval",
               p_LOSS_WORDS, LS_IDLE_MAX_WORDS);
    end

    //=======================================================================
    // Signals
    //=======================================================================

    logic          up_q;          // link up
    logic [HW-1:0] hits_q;        // IDLEs seen while going up
    logic [LW-1:0] since_q;       // words since the last IDLE
    logic [BW-1:0] bad_q;         // errored words since the last clean IDLE
    logic          is_idle;       // this word is an error-free IDLE
    logic          k28_5_off;     // error-free word, K28.5 in P1..P3
    logic          bad_word;      // this word has a code or disparity error
    logic          misframed;     // framing judged wrong this cycle
    logic          loss;          // no IDLE for p_LOSS_WORDS words
    logic          drop;          // the link leaves UP this cycle

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign is_idle   = valid_i & ~err_i & (kmask_i == KMASK_IDLE) & (data_i == IDLE_WORD);
    assign k28_5_off = valid_i & ~err_i
                     & ((kmask_i[1] & (data_i[15:8]  == K28_5))
                      | (kmask_i[2] & (data_i[23:16] == K28_5))
                      | (kmask_i[3] & (data_i[31:24] == K28_5)));
    assign bad_word  = valid_i & err_i;
    assign misframed = rx_lock_i & ((bad_word & (bad_q == BW'(p_BAD_WORDS - 1))) | k28_5_off);
    assign loss      = up_q & valid_i & ~is_idle & (since_q == LW'(p_LOSS_WORDS - 1));
    assign drop      = up_q & (~rx_lock_i | loss | misframed);

    assign data_o          = data_i;
    assign kmask_o         = kmask_i;
    assign err_o           = err_i;
    assign valid_o         = valid_i & up_q & ~drop;
    assign up_o            = up_q;
    assign link_detected_o = up_q & rx_lock_i;

    //=======================================================================
    // Link state
    //=======================================================================

    always_ff @(posedge rx_clk or negedge rx_rst_n) begin
        if (!rx_rst_n) begin
            up_q     <= 1'b0;
            hits_q   <= '0;
            since_q  <= '0;
            bad_q    <= '0;
            resync_o <= 1'b0;
            flush_o  <= 1'b0;
        end else begin
            resync_o <= loss | misframed;
            flush_o  <= drop | misframed;

            if (!rx_lock_i || misframed || is_idle) bad_q <= '0;
            else if (bad_word)                      bad_q <= bad_q + 1'b1;

            if (!up_q) begin
                // DOWN: count IDLEs while the sampler is locked; a code
                // error, loss of symbol lock or a re-hunt starts over.
                if (!rx_lock_i || misframed || (valid_i && err_i)) begin
                    hits_q <= '0;
                end else if (is_idle) begin
                    if (hits_q == HW'(p_LOCK_IDLES - 1)) begin
                        up_q    <= 1'b1;
                        hits_q  <= '0;
                        since_q <= '0;
                    end else begin
                        hits_q <= hits_q + 1'b1;
                    end
                end
            end else if (drop) begin
                up_q    <= 1'b0;
                since_q <= '0;
            end else if (is_idle) begin
                since_q <= '0;
            end else if (valid_i) begin
                since_q <= since_q + 1'b1;
            end
        end
    end

endmodule

`default_nettype wire
