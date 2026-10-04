/*
================================================================================
  cxp_tx_ctrl_ack
  CoaXPress 1.1.1 — control acknowledge (device → host) packet framer.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2025-05-27

    Description:
      Reference: JIIA CXP-001-2015 §8.6.3 "Acknowledgment Message Format",
      codes Table 22.  Migrated from the v1.0 numbering (old §6.6.3 Table 21).

      v1.0 -> v1.1.1 acknowledgment-code delta (Table 22 / Annex C.2):
        * The v1.0 "tentative" code 0x02 is REPLACED by 0x04 "Wait": the time
          for the Host to wait is sent as a 4-byte ms integer in the reply
          data field, range 100 ms .. 10 s, framed exactly like a 0x00 read
          acknowledgment (§8.6.3).  This module gains a 0x04 payload path.
        * New logical-error codes 0x45 (size field too large), 0x46
          (inconsistent size), 0x47 (malformed packet).  Framer transmits
          whatever 8-bit code the router / APB master selects.
        * 0x00 (read OK) and 0x04 (Wait) carry Size+Data+CRC; for every other
          code the framer emits the §8.6.3 immediate ack (SOP|TYPE|CODE|EOP,
          no Size, no CRC) and the Host treats it as a final ack.
        * Control-channel-reset ack 0x03 is STILL VALID in v1.1.1 — unchanged.

      Consumes the response handshake produced by cxp_rx_ctrl_apb_master (or
      by the connection-reset / CRC-error side bands) and emits a fully
      framed type-0x03 packet over the AXI-Stream-style bundle that
      cxp_tx_arbiter (long-packet port 0) hands to cxp_tx_inserter.

      Wire format (§8.6.3 defines TWO acknowledgment shapes):
        (a) Immediate ack — write-OK (0x01), reset-done (0x03) and every
            0x40-class / 0x80 error code.  No Size, no data, no CRC:
              4×K27.7   (SOP)
              4×0x03    (ack type)
              4×AckCode
              4×K29.7   (EOP)
        (b) Data ack — read-OK (0x00) and Wait (0x04), Table 22:
              4×K27.7   (SOP)
              4×0x03    (ack type)
              Word 0      : 4×AckCode
              Word 1      : Size = B, big-endian (one raw word)
              Word 2..N+1 : N data words, big-endian (§8.2.1); the
                            4N − B pad bytes of the last word are 0
              Word N+2    : CRC32 (the register, LSByte in P0)
              4×K29.7   (EOP)
            N + 6 words.  B is the Size of the command (ack_size_i);
            N = ceil(B / 4).  A Wait always carries B = 4.

      CRC32 (§8.2.2.2) — data acks only.  Polynomial 0xEDB88320, seed
      0xFFFFFFFF, no final XOR, register LSByte in P0.  Coverage: words
      0..N+1 (AckCode .. last data word); TYPE word, CRC word and K-char
      framing are excluded.

      Framing and the CRC come from cxp_tx_pkt_framer; this module builds the
      header words (replicated code, raw Size), decides the shape
      (immediate or data ack) and where the data words come from.

    Versions:
        2025-05-27 - 0.1:   - Init
        2026-09-19 - 0.2:   - Built on cxp_tx_pkt_framer; abort_i from the arbiter
        2026-09-19 - 0.3:   - Table 22 Size word (B, one word), zero pad bytes
        2026-09-26 - 0.4:   - Read-buffer bank latched with the request
        2026-09-26 - 0.5:   - No abort: a started acknowledgment always
                              completes
        2026-09-27 - 0.6:   - Packet words out as one cxp_txw_t (m_o)

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_tx_ctrl_ack #(
    parameter int p_BUF_DEPTH = 64
) (
    // Transmit clock domain
    input  wire  logic        tx_clk,                           // packet TX clock
    input  wire  logic        tx_rst_n,                         // sync-deassert active-low reset

    // Request from the register router / link-reset
    input  wire  logic        ack_req_i,                        // 1-cycle pulse, kicks the FSM
    input  wire  logic [7:0]  ack_code_i,                       // §8.6.3 ack code
    input  wire  logic [23:0] ack_size_i,                       // Size B in bytes (0 for non-read)
    input  wire  logic [31:0] ack_wait_ms_i,                    // 0x04 Wait reply data, ms
    input  wire  logic        ack_rbank_i,                      // read-buffer bank of the data
    output logic              ack_busy_o,                       // 1 while a packet is in flight

    // Read-data buffer port {bank, word} (lives inside cxp_ctrl_bus_master)
    output logic [$clog2(p_BUF_DEPTH):0] rbuf_addr_o,           // 1-cycle-ahead read pointer
    input  wire  logic [31:0] rbuf_data_i,                      // read data, 1-cycle latency

    // Packet output (to cxp_tx_arbiter, long-packet port 0)
    output cxp_pkg::cxp_txw_t m_o,                              // packet word, P0 in [7:0]
    input  wire  logic        m_ready_i                         // arbiter accepts the beat
);

    import cxp_pkg::*;
    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    localparam int BUF_AW = idx_w(p_BUF_DEPTH);

    if (p_BUF_DEPTH < 2 || (p_BUF_DEPTH & (p_BUF_DEPTH - 1)) != 0) begin : g_chk_buf_depth
        $error("cxp_tx_ctrl_ack: p_BUF_DEPTH (=%0d) must be a power of two >= 2 (banked)",
               p_BUF_DEPTH);
    end

    //=======================================================================
    // Signals
    //=======================================================================

    logic [ 7:0] code_q;             // latched ack code
    logic [15:0] nwords_q;           // latched N = ceil(B / 4)
    logic [23:0] size_bytes_q;       // latched Size B in bytes
    logic [31:0] wait_word_q;        // latched 0x04 Wait reply data word
    logic        rbank_q;            // latched read-buffer bank
    logic        is_wait;            // current code is 0x04 Wait
    logic        is_bare;            // immediate (no Size/Data/CRC) ack
    logic [31:0] data_word;          // native data word (rbuf or wait)
    logic [31:0] wire_word;          // data word on the wire, pad bytes 0
    logic [15:0] n_words;            // ceil(ack_size_i / 4)

    logic        start;              // new ack accepted this cycle
    logic        data_phase;         // framer is emitting data words
    logic [15:0] data_idx;           // data words already emitted
    logic [3:1][31:0] hdr;           // TYPE, CODE, Size

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign is_wait = (code_q == ACK_WAIT);
    // §8.6.3: only read-OK (0x00) and Wait (0x04) carry Size+Data+CRC.
    // Every other code is an immediate ack (SOP|TYPE|CODE|EOP).
    assign is_bare = (code_q != ACK_OK) && (code_q != ACK_WAIT);

    // §8.2.1: read / Wait reply data is transmitted big-endian; rbuf and
    // wait_word hold native; swap out on the wire.
    assign data_word = is_wait ? wait_word_q : rbuf_data_i;

    // Table 22: when B is not a multiple of 4, the 4N − B bytes after the
    // last data byte are 0.  On the big-endian wire the data bytes of the
    // last word are P0..P(B%4 − 1).
    always_comb begin
        wire_word = bswap32(data_word);
        if (data_idx == nwords_q - 16'd1) begin
            unique case (size_bytes_q[1:0])
                2'd1:    wire_word[31:8]  = 24'h0;
                2'd2:    wire_word[31:16] = 16'h0;
                2'd3:    wire_word[31:24] = 8'h0;
                default: ;
            endcase
        end
    end

    assign n_words = 16'((ack_size_i + 24'd3) >> 2);

    assign start = !ack_busy_o && ack_req_i;

    assign hdr = {bswap32({8'h00, size_bytes_q}), rep4(code_q), rep4(PKT_TYPE_ACK)};

    //=======================================================================
    // rbuf_addr_o drive.  Prefetched one cycle ahead: word 0 is addressed
    // before the first data beat, and the pointer advances only when the
    // arbiter accepts the current word.
    //=======================================================================

    always_comb begin
        rbuf_addr_o = {rbank_q, BUF_AW'(0)};
        if (data_phase) begin
            rbuf_addr_o[BUF_AW-1:0] = m_ready_i ? (data_idx[BUF_AW-1:0] + BUF_AW'(1))
                                                :  data_idx[BUF_AW-1:0];
        end
    end

    //=======================================================================
    // Sequential — latch the ack parameters when a request is accepted.
    //=======================================================================

    always_ff @(posedge tx_clk or negedge tx_rst_n) begin
        if (!tx_rst_n) begin
            code_q       <= 8'h00;
            nwords_q     <= 16'h0;
            size_bytes_q <= 24'h0;
            wait_word_q  <= 32'h0;
            rbank_q      <= 1'b0;
        end else if (start) begin
            code_q  <= ack_code_i;
            rbank_q <= ack_rbank_i;
            // §8.6.3: a Wait (0x04) always carries exactly one reply word
            // (the 4-byte ms timeout); any other code uses the command's
            // Size B (non-zero only for a 0x00 read ack).
            if (ack_code_i == ACK_WAIT) begin
                nwords_q     <= 16'd1;
                size_bytes_q <= 24'd4;
            end else begin
                nwords_q     <= n_words;
                size_bytes_q <= ack_size_i;
            end
            wait_word_q <= ack_wait_ms_i;
        end
    end

    //=======================================================================
    // Packet framer — SOP, header, data, CRC, EOP.
    //=======================================================================

    cxp_tx_pkt_framer #(
        .p_HDR_WORDS (4),
        .p_HAS_CRC   (1'b1),
        .p_CRC_FROM  (2)
    ) cxp_tx_pkt_framer_i (
        .tx_clk       (tx_clk),
        .tx_rst_n     (tx_rst_n),
        .start_i      (ack_req_i),
        .hdr_last_i   (is_bare ? 3'd2 : 3'd3),
        .hdr_i        (hdr),
        .has_body_i   (!is_bare),
        .skip_empty_i (1'b1),
        .n_words_i    (nwords_q),
        .pl_data_i    (wire_word),
        .pl_kmask_i   (KMASK_NONE),
        .pl_valid_i   (1'b1),
        .pl_eop_i     (1'b0),
        .pl_ready_o   (),
        .busy_o       (ack_busy_o),
        .data_phase_o (data_phase),
        .data_idx_o   (data_idx),
        .m_data_o     (m_o.data),
        .m_kmask_o    (m_o.kmask),
        .m_valid_o    (m_o.valid),
        .m_sop_o      (m_o.sop),
        .m_eop_o      (m_o.eop),
        .m_ready_i    (m_ready_i),
        .done_o       ()
    );

endmodule

`default_nettype wire
