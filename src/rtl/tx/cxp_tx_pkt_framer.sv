/*
================================================================================
  cxp_tx_pkt_framer
  CoaXPress 1.1.1 — long-packet framer shared by every TX packet source.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-19

    Description:
      Emits one long packet (§8.2.2) on an AXI-Stream-style word bus:

          Word 0            : 4×K27.7            kmask 1111, m_sop_o
          Word 1..hdr_last  : hdr_i[w]           header words from the caller
          Data              : payload words      from the pl_* port
          CRC               : crc_wire(CRC-32)   when p_HAS_CRC
          Last              : 4×K29.7            kmask 1111, m_eop_o

      The caller owns what goes into the packet — header words, word count
      and payload — and this block owns the order of the words and the
      CRC.  A header word is either a byte replicated into all four lanes
      (§8.2.2.1, rep4()) or a raw 32-bit value (the Table 22 Size word);
      the caller builds it.  cxp_tx_ctrl_ack, cxp_tx_stream_pkt and
      cxp_tx_linktest are built on it.

      Sequence control
        start_i      In ST_IDLE: begin a packet next cycle.  The caller
                     latches its header fields on the same edge.
        hdr_last_i   Index of the last header word (1..p_HDR_WORDS-1).
        has_body_i   0 = header then EOP, nothing else (immediate ack).
        skip_empty_i With n_words_i == 0: header straight to the CRC word.
        n_words_i    Payload words, sampled when the header completes.
                     0 without skip_empty_i means "until pl_eop_i".

      A packet, once started, runs to its trailer: nothing outside can
      drop it.  Every word is offered until it is taken.

      Payload port
        pl_ready_o is m_ready_i in ST_DATA.  The n_words_i-th word closes
        the payload; pl_eop_i before it closes the packet early.

      CRC (§8.2.2.2)
        Seeded when the packet starts.  Covers header words
        p_CRC_FROM..hdr_last and every payload word, as transmitted.  Only
        packets with a body carry a CRC.  Tables 21/22 cover everything
        after the TYPE word (p_CRC_FROM = 2); Table 19 covers the stream
        data only (p_CRC_FROM = p_HDR_WORDS).

    Versions:
        2026-09-19 - 0.1:   - Init (framing lifted out of ctrl_ack_tx,
                              stream_pkt_tx and tx_linktest)
        2026-09-19 - 0.2:   - 32-bit header words; CRC start index
        2026-09-26 - 0.3:   - No abort, no truncation: a started packet
                              always completes

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_tx_pkt_framer #(
    parameter int p_HDR_WORDS = 6,                          // SOP + up to 7 header words
    parameter bit p_HAS_CRC   = 1'b1,                       // append a CRC word to the body
    parameter int p_CRC_FROM  = 2                           // first header word in the CRC
) (
    input  wire  logic        tx_clk,                       // TX clock
    input  wire  logic        tx_rst_n,                     // async active-low reset

    // Sequence control
    input  wire  logic        start_i,                      // begin a packet (ST_IDLE only)
    input  wire  logic [2:0]  hdr_last_i,                   // index of the last header word
    input  wire  logic [p_HDR_WORDS-1:1][31:0] hdr_i,       // header words 1..
    input  wire  logic        has_body_i,                   // payload + CRC follow the header
    input  wire  logic        skip_empty_i,                 // n_words_i == 0 skips ST_DATA
    input  wire  logic [15:0] n_words_i,                    // payload word count

    // Payload in
    input  wire  logic [31:0] pl_data_i,                    // payload word
    input  wire  logic [3:0]  pl_kmask_i,                   // payload K-char mask
    input  wire  logic        pl_valid_i,                   // payload word valid
    input  wire  logic        pl_eop_i,                     // last payload word
    output logic              pl_ready_o,                   // payload word accepted
    output logic              busy_o,                       // a packet is in flight
    output logic              data_phase_o,                 // framer is in ST_DATA
    output logic [15:0]       data_idx_o,                   // payload words sent so far

    // Packet out
    output logic [31:0]       m_data_o,                     // packet word
    output logic [3:0]        m_kmask_o,                    // K-char mask
    output logic              m_valid_o,                    // word valid
    output logic              m_sop_o,                      // start of packet
    output logic              m_eop_o,                      // end of packet
    input  wire  logic        m_ready_i,                    // downstream accepts the word
    output logic              done_o                        // EOP word accepted this cycle
);

    import cxp_pkg::*;
    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Types ------

    typedef enum logic [2:0] {
        ST_IDLE,    // waiting for start_i
        ST_HDR,     // SOP and header words
        ST_DATA,    // payload words
        ST_CRC,     // CRC word
        ST_EOP      // K29.7 trailer
    } state_t;

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    if (p_HDR_WORDS < 2 || p_HDR_WORDS > 8) begin : g_chk_hdr
        $error("cxp_tx_pkt_framer: p_HDR_WORDS (=%0d) must be 2..8", p_HDR_WORDS);
    end

    if (p_CRC_FROM < 2 || p_CRC_FROM > p_HDR_WORDS) begin : g_chk_crc_from
        $error("cxp_tx_pkt_framer: p_CRC_FROM (=%0d) must be 2..p_HDR_WORDS",
               p_CRC_FROM);
    end

    //=======================================================================
    // Signals
    //=======================================================================

    state_t      state_q, state_n;
    state_t      body_end;          // state after the last payload word

    logic [2:0]  hdr_idx_q;         // header word index
    logic [15:0] data_left_q;       // payload words still to send
    logic [15:0] data_idx_q;        // payload words sent

    logic [7:0][31:0] hdr_words;    // word 0 = 4×K27.7, then hdr_i
    logic [31:0] hdr_word;          // header word being sent
    logic        out_fire;          // m_valid_o & m_ready_i
    logic        hdr_done;          // last header word accepted
    logic        data_fire;         // payload word accepted in ST_DATA
    logic        last_data_word;    // n_words_i-th payload word

    logic [31:0] crc_final;         // CRC register

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    always_comb begin
        hdr_words                  = '0;
        hdr_words[0]               = rep4(K27_7);
        hdr_words[p_HDR_WORDS-1:1] = hdr_i;
    end

    assign hdr_word       = hdr_words[hdr_idx_q];
    assign out_fire       = m_valid_o & m_ready_i;
    assign hdr_done       = (state_q == ST_HDR) & out_fire & (hdr_idx_q == hdr_last_i);
    assign data_fire      = (state_q == ST_DATA) & pl_valid_i & m_ready_i;
    assign last_data_word = (data_left_q == 16'd1);
    assign body_end       = p_HAS_CRC ? ST_CRC : ST_EOP;

    assign busy_o       = (state_q != ST_IDLE);
    assign data_phase_o = (state_q == ST_DATA);
    assign data_idx_o   = data_idx_q;
    assign done_o       = (state_q == ST_EOP) & out_fire;

    //=======================================================================
    // Output mux.
    //=======================================================================

    always_comb begin
        m_data_o   = 32'h0000_0000;
        m_kmask_o  = KMASK_NONE;
        m_valid_o  = 1'b0;
        m_sop_o    = 1'b0;
        m_eop_o    = 1'b0;
        pl_ready_o = 1'b0;

        unique case (state_q)

            //===============================================================
            // Idle: bus quiet; the payload head waits unconsumed.
            //
            ST_IDLE: ;

            //===============================================================
            // Header: 4×K27.7, then the caller's header words.
            //
            ST_HDR: begin
                m_data_o  = hdr_word;
                m_kmask_o = (hdr_idx_q == 3'd0) ? KMASK_ALL : KMASK_NONE;
                m_valid_o = 1'b1;
                m_sop_o   = (hdr_idx_q == 3'd0);
            end

            //===============================================================
            // Data: payload passes straight through.
            //
            ST_DATA: begin
                m_data_o   = pl_data_i;
                m_kmask_o  = pl_kmask_i;
                m_valid_o  = pl_valid_i;
                pl_ready_o = m_ready_i;
            end

            //===============================================================
            // CRC: the register in the device's wire byte order.
            //
            ST_CRC: begin
                m_data_o  = crc_wire(crc_final);
                m_valid_o = 1'b1;
            end

            //===============================================================
            // EOP: 4×K29.7.
            //
            ST_EOP: begin
                m_data_o  = rep4(K29_7);
                m_kmask_o = KMASK_ALL;
                m_valid_o = 1'b1;
                m_eop_o   = 1'b1;
            end

            default: ;
        endcase
    end

    //=======================================================================
    // Next-state.
    //=======================================================================

    always_comb begin
        state_n = state_q;
        unique case (state_q)

            //===============================================================
            // Idle: start on request.
            //
            ST_IDLE: begin
                if (start_i) state_n = ST_HDR;
            end

            //===============================================================
            // Header: after the last word, body, CRC or trailer.
            //
            ST_HDR: begin
                if (hdr_done) begin
                    if (!has_body_i)                            state_n = ST_EOP;
                    else if (skip_empty_i && n_words_i == 16'd0) state_n = body_end;
                    else                                        state_n = ST_DATA;
                end
            end

            //===============================================================
            // Data: close on the counted last word or an early pl_eop_i.
            //
            ST_DATA: begin
                if (data_fire && (last_data_word || pl_eop_i)) state_n = body_end;
            end

            //===============================================================
            // CRC: one word.
            //
            ST_CRC: begin
                if (out_fire) state_n = ST_EOP;
            end

            //===============================================================
            // EOP: one word, then idle.
            //
            ST_EOP: begin
                if (out_fire) state_n = ST_IDLE;
            end

            default: state_n = ST_IDLE;
        endcase
    end

    //=======================================================================
    // Sequential.
    //=======================================================================

    always_ff @(posedge tx_clk or negedge tx_rst_n) begin
        if (!tx_rst_n) begin
            state_q     <= ST_IDLE;
            hdr_idx_q   <= 3'd0;
            data_left_q <= 16'h0;
            data_idx_q  <= 16'h0;
        end else begin
            state_q <= state_n;

            if (state_q == ST_IDLE && start_i) begin
                hdr_idx_q  <= 3'd0;
                data_idx_q <= 16'h0;
            end

            if (state_q == ST_HDR && out_fire) begin
                hdr_idx_q <= hdr_idx_q + 3'd1;
            end

            if (hdr_done) begin
                data_left_q <= n_words_i;
            end

            if (data_fire) begin
                if (data_left_q != 16'h0) data_left_q <= data_left_q - 16'd1;
                data_idx_q <= data_idx_q + 16'd1;
            end
        end
    end

    //=======================================================================
    // CRC32 — header words p_CRC_FROM.. and payload, as transmitted.
    //=======================================================================

    if (p_HAS_CRC) begin : g_crc

        logic crc_init;         // reseed
        logic crc_din_valid;    // fold m_data_o

        assign crc_init      = (state_q == ST_IDLE) && start_i;
        assign crc_din_valid = has_body_i &&
                               ((state_q == ST_HDR && out_fire &&
                                 32'(hdr_idx_q) >= p_CRC_FROM) ||
                                data_fire);

        cxp_lib_crc32 #(
            .p_IN_W (32)
        ) cxp_lib_crc32_i (
            .clk         (tx_clk),
            .rst_n       (tx_rst_n),
            .init_i      (crc_init),
            .din_i       (m_data_o),
            .din_be_i    (4'b1111),
            .din_valid_i (crc_din_valid),
            .crc_o       (crc_final)
        );

    end else begin : g_no_crc

        assign crc_final = 32'h0;

    end

endmodule

`default_nettype wire
