/*
================================================================================
  cxp_cdc_stream_fifo
  CoaXPress 1.1.1 (CXP-001-2015) §8.5.4 — per-stream CDC FIFO (app->tx clock).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-27

    Description:
      Per-stream cross-clock-domain FIFO between the app-clock pixel
      pipeline and the tx-word-clock packet mux.

      One instance per stream.  Producer side (app_clk) accepts {data,
      kmask, sop, eop} beats; consumer side (tx_clk) drains the same
      beats in order.  Sized so that one full StreamPacketDataSize
      packet fits with margin (default p_DEPTH = 4096 entries ->
      16 KiB per stream at p_DATA_W = 32).

      CDC: classical gray-code asynchronous FIFO with two-flop
      synchronisers on the wr/rd pointers in their opposite clock
      domain.  `m_pkt_avail_o` is computed from a gray-coded EOP-
      counter pair so the consumer can wait for a full packet to be
      present before starting transmission — this avoids the line-
      side underrun that would corrupt a CXP stream packet.  With it,
      `m_len_o` / `m_streamid_o` describe the packet whose SOP is at the
      head: the producer takes the StreamID at the SOP, counts the
      packet's words and stores both beside the SOP slot when the EOP is
      written, before the EOP count that publishes the packet crosses to
      tx_clk.  The EOP count takes one more synchroniser stage than the
      write pointer, so a packet is never announced before its words are
      visible on the read side.

      Almost-full backpressure: `s_ready_o` deasserts when fewer than
      `p_ALMOST_FULL_MARGIN` slots remain (>= 4 by default).  Because
      the synchronised read pointer is conservatively stale, the true
      free space is always at least `p_ALMOST_FULL_MARGIN` when
      `s_ready_o=1`, so the producer cannot overflow the FIFO.

      Reset of one side alone (cxp_cdc_link): while the pair is not up,
      the read side discards everything written (its pointers follow the
      write side's) and the write side stalls (`s_ready_o` low); the read
      side then skips to the next SOP, so the rest of a packet whose start
      was discarded never comes out.  So neither an app-only nor a
      tx-only reset replays delivered data, loses the pointers' agreement
      or delivers a fragment.

      Flush (tx_clk): while `flush_i` is high and the consumer is not in
      the middle of a packet (`m_busy_i` low) the read side discards the
      same way.  The request crosses to app_clk as `s_flush_o`; the write
      side drops everything while it is high and resumes at the next SOP.
      `flush_ack_o` rises once the write side has been seen dropping and
      the read side has discarded for a few cycles more, i.e. both sides
      are empty; the requester then releases `flush_i`.

      Memory: combinational read (LUTRAM-style).  For deep instances
      on FPGAs that prefer BRAM, replace with a registered-read
      primitive plus a small skid buffer.

      p_DEPTH must be a power of two and >= 4.

    Versions:
        2026-05-27 - 0.1:   - Restyle to coding-style template
        2026-09-22 - 0.2:   - m_len_o / m_streamid_o: the packet at the head
        2026-09-25 - 0.3:   - One-sided resets and a flush request empty
                              both sides; no replay, no fragment

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_cdc_stream_fifo #(
    parameter int p_DEPTH              = 4096,
    parameter int p_DATA_W             = 32,
    parameter int p_ALMOST_FULL_MARGIN = 4
) (
    // App-side producer clock domain
    input  wire  logic                app_clk,                  // App clock
    input  wire  logic                app_rst_n,                // App reset (async/sync-low)
    // TX-side consumer clock domain
    input  wire  logic                tx_clk,                   // TX clock
    input  wire  logic                tx_rst_n,                 // TX reset (async/sync-low)

    // App-side producer
    input  wire  logic [p_DATA_W-1:0] s_data_i,                 // Producer data word
    input  wire  logic [3:0]          s_kmask_i,                // Producer K-flag per byte
    input  wire  logic                s_valid_i,                // Producer beat valid
    input  wire  logic                s_sop_i,                  // Producer SOP
    input  wire  logic                s_eop_i,                  // Producer EOP
    input  wire  logic [7:0]          s_streamid_i,             // StreamID, taken at the SOP
    output logic                      s_ready_o,                // Almost-full backpressure

    // TX-side consumer
    output logic [p_DATA_W-1:0]       m_data_o,                 // Consumer data word
    output logic [3:0]                m_kmask_o,                // Consumer K-flag per byte
    output logic                      m_valid_o,                // Consumer beat valid
    output logic                      m_sop_o,                  // Consumer SOP
    output logic                      m_eop_o,                  // Consumer EOP
    input  wire  logic                m_ready_i,                // Consumer ready
    output logic                      m_pkt_avail_o,            // >= 1 fully-written packet
    output logic [15:0]               m_len_o,                  // head packet's words (at its SOP)
    output logic [7:0]                m_streamid_o,             // head packet's StreamID
    input  wire  logic                m_busy_i,                 // consumer mid-packet

    // Flush (request and acknowledge on tx_clk, its app_clk view)
    input  wire  logic                flush_i,                  // empty both sides
    output logic                      flush_ack_o,              // both sides empty
    output logic                      s_flush_o                 // app_clk: dropping for a flush
);

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    localparam int ADDR_W = $clog2(p_DEPTH);
    localparam int PTR_W  = ADDR_W + 1;
    // EOP counter — wider than PTR_W is unnecessary (<= 1 EOP per slot).
    localparam int EOP_W  = PTR_W;

    // ------ Types ------

    typedef struct packed {
        logic [p_DATA_W-1:0] data;
        logic [3:0]          kmask;
        logic                sop;
        logic                eop;
    } slot_t;

    // ------ Functions and Tasks ------

    function automatic logic [PTR_W-1:0] bin2gray_ptr(input logic [PTR_W-1:0] b);
        bin2gray_ptr = b ^ (b >> 1);
    endfunction
    function automatic logic [PTR_W-1:0] gray2bin_ptr(input logic [PTR_W-1:0] g);
        logic [PTR_W-1:0] b;
        b = g;
        for (int i = PTR_W - 2; i >= 0; i--) b[i] = b[i+1] ^ g[i];
        gray2bin_ptr = b;
    endfunction
    function automatic logic [EOP_W-1:0] bin2gray_eop(input logic [EOP_W-1:0] b);
        bin2gray_eop = b ^ (b >> 1);
    endfunction
    function automatic logic [EOP_W-1:0] gray2bin_eop(input logic [EOP_W-1:0] g);
        logic [EOP_W-1:0] b;
        b = g;
        for (int i = EOP_W - 2; i >= 0; i--) b[i] = b[i+1] ^ g[i];
        gray2bin_eop = b;
    endfunction

    //=======================================================================
    // Signals
    //=======================================================================

    slot_t              mem [p_DEPTH];      // Storage
    logic [23:0]        desc_mem [p_DEPTH]; // {StreamID, length} at the packet's SOP slot

    // Producer side (app_clk domain)
    logic [PTR_W-1:0]   wr_ptr_q;
    logic [PTR_W-1:0]   wr_ptr_gray_q;
    logic [EOP_W-1:0]   wr_eop_cnt_q;
    logic [EOP_W-1:0]   wr_eop_cnt_gray_q;
    logic [PTR_W-1:0]   rd_ptr_gray_sync_wr_q [2];

    logic [PTR_W-1:0]   rd_ptr_bin_in_wr;
    logic [PTR_W-1:0]   fill_wr;
    logic               almost_full_wr;
    logic               accept_wr;

    logic [PTR_W-1:0]   wr_ptr_n;
    logic [EOP_W-1:0]   wr_eop_cnt_n;
    logic [ADDR_W-1:0]  wr_addr;
    logic [15:0]        wr_len_q;           // words of the open packet so far
    logic [ADDR_W-1:0]  wr_sop_addr_q;      // its SOP slot
    logic [7:0]         wr_sid_q;           // its StreamID
    logic [15:0]        wr_len_n;
    logic               app_ok;             // pair up (app_clk)
    logic               wr_drop;            // drop what is offered (flush)
    logic               wr_want_sop_q;      // after a flush, resume at a SOP
    logic               s_flush_q;          // flush seen on app_clk
    logic               flush_app_s;        // flush_i on app_clk

    // Consumer side (tx_clk domain)
    logic [PTR_W-1:0]   rd_ptr_q;
    logic [PTR_W-1:0]   rd_ptr_gray_q;
    logic [EOP_W-1:0]   rd_eop_cnt_q;
    logic               skip_q;       // skip-until-SOP after a tx-side reset

    logic [PTR_W-1:0]   wr_ptr_gray_sync_rd_q     [2];
    logic [EOP_W-1:0]   wr_eop_cnt_gray_sync_rd_q [3];  // one stage behind the pointer

    logic               empty_rd;
    logic [ADDR_W-1:0]  rd_addr;
    slot_t              head_entry;
    logic               emit_word;   // valid handshake fires (consumer pop)
    logic               skip_word;   // stale-data drop after tx reset
    logic [PTR_W-1:0]   rd_ptr_n;
    logic [EOP_W-1:0]   rd_eop_cnt_n;
    logic               skip_n;

    logic [EOP_W-1:0]   wr_eop_cnt_bin_in_rd;
    logic [EOP_W-1:0]   pkts_in_fifo;
    logic               tx_ok;              // pair up (tx_clk)
    logic [3:0]         tx_ok_q;            // tx_ok delayed: the EOP count settles first
    logic               discard;            // read side follows the write side
    logic               flush_drop_s;       // write side dropping, seen on tx_clk
    logic [2:0]         flush_cnt_q;        // cycles discarded with the writer quiet

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    // Producer
    assign rd_ptr_bin_in_wr = gray2bin_ptr(rd_ptr_gray_sync_wr_q[1]);
    // PTR_W-bit modular subtract: result is in [0..p_DEPTH] because the
    // FIFO is never allowed to exceed p_DEPTH entries.
    assign fill_wr          = wr_ptr_q - rd_ptr_bin_in_wr;
    assign almost_full_wr   = (fill_wr >= PTR_W'(p_DEPTH - p_ALMOST_FULL_MARGIN));
    assign wr_drop          = s_flush_q | (wr_want_sop_q & ~s_sop_i);
    assign s_ready_o        = app_ok & (wr_drop | ~almost_full_wr);
    assign accept_wr        = s_valid_i & s_ready_o & ~wr_drop;
    assign s_flush_o        = s_flush_q;
    assign wr_addr          = wr_ptr_q[ADDR_W-1:0];
    assign wr_len_n         = s_sop_i ? 16'd1 : (wr_len_q + 16'd1);

    // Consumer
    assign empty_rd             = (rd_ptr_gray_q == wr_ptr_gray_sync_rd_q[1]);
    assign rd_addr              = rd_ptr_q[ADDR_W-1:0];
    assign head_entry           = mem[rd_addr];
    assign wr_eop_cnt_bin_in_rd = gray2bin_eop(wr_eop_cnt_gray_sync_rd_q[2]);
    assign pkts_in_fifo         = wr_eop_cnt_bin_in_rd - rd_eop_cnt_q;
    assign m_pkt_avail_o        = (pkts_in_fifo != EOP_W'(0)) & ~discard;
    // The packet being read is never cut: it is whole in the RAM (store
    // and forward), so the reader finishes it first.
    assign discard              = (~tx_ok_q[3] | flush_i) & ~m_busy_i;
    assign flush_ack_o          = flush_i & (flush_cnt_q == 3'd7);
    assign m_len_o              = desc_mem[rd_addr][15:0];
    assign m_streamid_o         = desc_mem[rd_addr][23:16];

    //=======================================================================
    // Elaboration-time parameter checks
    //=======================================================================

    if (p_DEPTH < 4 || (p_DEPTH & (p_DEPTH - 1)) != 0) begin : g_chk_depth
        $error("cxp_cdc_stream_fifo: p_DEPTH (%0d) must be a power of two >= 4", p_DEPTH);
    end
    if (p_ALMOST_FULL_MARGIN < 1 || p_ALMOST_FULL_MARGIN >= p_DEPTH) begin : g_chk_margin
        $error("cxp_cdc_stream_fifo: p_ALMOST_FULL_MARGIN (%0d) out of range",
               p_ALMOST_FULL_MARGIN);
    end

    //=======================================================================
    // Producer next-state (app_clk)
    //=======================================================================

    always_comb begin
        wr_ptr_n     = wr_ptr_q;
        wr_eop_cnt_n = wr_eop_cnt_q;
        if (accept_wr) begin
            wr_ptr_n = wr_ptr_q + PTR_W'(1);
            if (s_eop_i) wr_eop_cnt_n = wr_eop_cnt_q + EOP_W'(1);
        end
    end

    //=======================================================================
    // Producer registers + read-ptr gray sync into app domain
    //=======================================================================

    always_ff @(posedge app_clk or negedge app_rst_n) begin
        if (!app_rst_n) begin
            wr_ptr_q                 <= '0;
            wr_ptr_gray_q            <= '0;
            wr_eop_cnt_q             <= '0;
            wr_eop_cnt_gray_q        <= '0;
            rd_ptr_gray_sync_wr_q[0] <= '0;
            rd_ptr_gray_sync_wr_q[1] <= '0;
            wr_len_q                 <= '0;
            wr_sop_addr_q            <= '0;
            wr_sid_q                 <= '0;
            wr_want_sop_q            <= 1'b0;
        end else begin
            // After a flush the write side resumes on a packet boundary.
            if (s_flush_q)      wr_want_sop_q <= 1'b1;
            else if (accept_wr) wr_want_sop_q <= 1'b0;
            wr_ptr_q                 <= wr_ptr_n;
            wr_ptr_gray_q            <= bin2gray_ptr(wr_ptr_n);
            wr_eop_cnt_q             <= wr_eop_cnt_n;
            wr_eop_cnt_gray_q        <= bin2gray_eop(wr_eop_cnt_n);
            rd_ptr_gray_sync_wr_q[0] <= rd_ptr_gray_q;
            rd_ptr_gray_sync_wr_q[1] <= rd_ptr_gray_sync_wr_q[0];
            if (accept_wr) begin
                wr_len_q <= wr_len_n;
                if (s_sop_i) begin
                    wr_sop_addr_q <= wr_addr;
                    wr_sid_q      <= s_streamid_i;
                end
            end
        end
    end

    //=======================================================================
    // Memory write (no async reset on RAM)
    //=======================================================================

    always_ff @(posedge app_clk) begin
        if (accept_wr) begin
            mem[wr_addr].data  <= s_data_i;
            mem[wr_addr].kmask <= s_kmask_i;
            mem[wr_addr].sop   <= s_sop_i;
            mem[wr_addr].eop   <= s_eop_i;
        end
        // The packet's descriptor goes beside its SOP when its EOP is written.
        if (accept_wr && s_eop_i) begin
            if (s_sop_i) desc_mem[wr_addr]       <= {s_streamid_i, wr_len_n};
            else         desc_mem[wr_sop_addr_q] <= {wr_sid_q, wr_len_n};
        end
    end

    //=======================================================================
    // Consumer: write-side pointer gray sync into tx domain
    //=======================================================================

    always_ff @(posedge tx_clk or negedge tx_rst_n) begin
        if (!tx_rst_n) begin
            wr_ptr_gray_sync_rd_q[0]     <= '0;
            wr_ptr_gray_sync_rd_q[1]     <= '0;
            wr_eop_cnt_gray_sync_rd_q[0] <= '0;
            wr_eop_cnt_gray_sync_rd_q[1] <= '0;
            wr_eop_cnt_gray_sync_rd_q[2] <= '0;
        end else begin
            wr_ptr_gray_sync_rd_q[0]     <= wr_ptr_gray_q;
            wr_ptr_gray_sync_rd_q[1]     <= wr_ptr_gray_sync_rd_q[0];
            wr_eop_cnt_gray_sync_rd_q[0] <= wr_eop_cnt_gray_q;
            wr_eop_cnt_gray_sync_rd_q[1] <= wr_eop_cnt_gray_sync_rd_q[0];
            wr_eop_cnt_gray_sync_rd_q[2] <= wr_eop_cnt_gray_sync_rd_q[1];
        end
    end

    //=======================================================================
    // Consumer combinational outputs + next-state (skip-until-SOP)
    //=======================================================================

    always_comb begin
        m_data_o  = head_entry.data;
        m_kmask_o = head_entry.kmask;
        m_sop_o   = head_entry.sop;
        m_eop_o   = head_entry.eop;

        m_valid_o = 1'b0;
        emit_word = 1'b0;
        skip_word = 1'b0;
        skip_n    = skip_q;

        if (discard) begin
            // Nothing is presented; the pointers follow the writer below.
            skip_n = 1'b1;
        end else if (!empty_rd) begin
            if (skip_q) begin
                if (head_entry.sop) begin
                    // First in-order SOP after the reset — leave skip mode
                    // and present this beat to the consumer.
                    skip_n    = 1'b0;
                    m_valid_o = 1'b1;
                    emit_word = m_ready_i;
                end else begin
                    // Stale fragment from before the reset; drop without
                    // exposing to the consumer.
                    skip_word = 1'b1;
                end
            end else begin
                m_valid_o = 1'b1;
                emit_word = m_ready_i;
            end
        end

        rd_ptr_n     = rd_ptr_q;
        rd_eop_cnt_n = rd_eop_cnt_q;
        if (emit_word | skip_word) begin
            rd_ptr_n = rd_ptr_q + PTR_W'(1);
            if (head_entry.eop) rd_eop_cnt_n = rd_eop_cnt_q + EOP_W'(1);
        end
    end

    //=======================================================================
    // Consumer registers (tx_clk)
    //=======================================================================

    always_ff @(posedge tx_clk or negedge tx_rst_n) begin
        if (!tx_rst_n) begin
            rd_ptr_q      <= '0;
            rd_ptr_gray_q <= '0;
            rd_eop_cnt_q  <= '0;
            // Drop any residue left in the FIFO until we are aligned to
            // the next SOP.  Asserting initially is also safe — the very
            // first transfer after power-on is itself an SOP.
            skip_q        <= 1'b1;
            tx_ok_q       <= '0;
        end else begin
            skip_q        <= skip_n;
            tx_ok_q       <= {tx_ok_q[2:0], tx_ok};
            if (discard) begin
                // Everything written so far is dropped.
                rd_ptr_q      <= gray2bin_ptr(wr_ptr_gray_sync_rd_q[1]);
                rd_ptr_gray_q <= wr_ptr_gray_sync_rd_q[1];
                rd_eop_cnt_q  <= wr_eop_cnt_bin_in_rd;
            end else begin
                rd_ptr_q      <= rd_ptr_n;
                rd_ptr_gray_q <= bin2gray_ptr(rd_ptr_n);
                rd_eop_cnt_q  <= rd_eop_cnt_n;
            end
        end
    end

    //=======================================================================
    // Flush handshake: request to app_clk, the writer's drop back to tx_clk
    //=======================================================================

    always_ff @(posedge app_clk or negedge app_rst_n) begin
        if (!app_rst_n) s_flush_q <= 1'b0;
        else            s_flush_q <= flush_app_s;
    end

    // Acknowledge after the writer has been seen dropping and the reader
    // has discarded for seven more cycles: the write pointer and the EOP
    // count have crossed (three stages) and nothing more is coming.
    always_ff @(posedge tx_clk or negedge tx_rst_n) begin
        if (!tx_rst_n)                                  flush_cnt_q <= '0;
        else if (!(flush_i && discard && flush_drop_s)) flush_cnt_q <= '0;
        else if (flush_cnt_q != 3'd7)                   flush_cnt_q <= flush_cnt_q + 3'd1;
    end

    //=======================================================================
    // Instances
    //=======================================================================

    cxp_cdc_sync #(.p_W(1)) cxp_cdc_sync_flush_i (
        .clk   (app_clk),
        .rst_n (app_rst_n),
        .d_i   (flush_i),
        .q_o   (flush_app_s)
    );

    cxp_cdc_sync #(.p_W(1)) cxp_cdc_sync_flush_drop_i (
        .clk   (tx_clk),
        .rst_n (tx_rst_n),
        .d_i   (s_flush_q),
        .q_o   (flush_drop_s)
    );

    cxp_cdc_link cxp_cdc_link_i (
        .src_clk   (app_clk),
        .src_rst_n (app_rst_n),
        .dst_clk   (tx_clk),
        .dst_rst_n (tx_rst_n),
        .src_ok_o  (app_ok),
        .dst_ok_o  (tx_ok)
    );

endmodule

`default_nettype wire
