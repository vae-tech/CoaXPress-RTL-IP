/*
================================================================================
  cxp_tx_stream_pkt
  CoaXPress 1.1.1 — stream data packet (type 0x01) framer (§8.5, Table 19).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2025-05-27

    Description:
      Wraps a flat stream payload from cxp_cdc_stream_fifo into an on-the-wire
      data packet:

          Word  0       : 4×K27.7     kmask = 4'b1111  (SOP)
          Word  1       : 4×0x01                       (stream data type)
          Word  2       : 4×StreamID
          Word  3       : 4×PacketTag                  (per-StreamID counter)
          Word  4       : 4×DsizeP[15:8]
          Word  5       : 4×DsizeP[ 7:0]
          Word  6..N+5  : N data words, passthrough from upstream
          Word  N+6     : CRC32 (the register, LSByte in P0)
          Word  N+7     : 4×K29.7     kmask = 4'b1111  (EOP)

      All header bytes are replicated four times across the lanes
      (§8.2.2.1 single-bit-error tolerance).  The CRC word is a unique
      32-bit value, the register with its LSByte in P0 (§8.2.2.2).

      CRC32 (§8.2.2.2)
        Shared cxp_lib_crc32 utility.  Reseeds at ST_IDLE → ST_HDR (s_sop_i).
        Coverage: the N data words only (Table 19: "stream data 4 to
        (N+3)"); StreamID, PacketTag and DsizeP are not covered.

      PacketTag (§8.5.3): per-StreamID counter (256 entries: a RAM without
      reset holding each stream's next tag, and one valid bit per stream;
      a clear empties the valid bits, so a stream without one starts at 0).
      Cleared on
      `stream_ctrl_reset_i` (ConnectionReset / ConnectionConfig write,
      §8.5.3 / §10.3.33) and on power-up reset.  NOT cleared on
      AcquisitionStart/Stop, frame edges, or size change.  A packet in
      flight when the tags are cleared does not bump its tag, so the next
      packet carries 0.

      Stream enable (Table 44): while `stream_en_i` is low
      (StreamPacketSizeMax = 0 "not initialized", or below one packet) no
      stream packet starts.  A packet already in flight completes; the
      packets behind it wait in the FIFO and go out once the stream is
      enabled again, so an image already begun on the wire is completed,
      not torn.  What a ConnectionReset, a ConnectionConfig write or
      TestMode must not send later is emptied by the FIFO flush.  A
      disabled stream does not reset PacketTag; only connection control
      does.

      DsizeP (§8.5.2): the packet's own length, s_len_i.
      cxp_cdc_stream_fifo gives the length of the packet at its head and
      s_pkt_avail_i once all of it is in, and a packet starts only then,
      so the header's DsizeP is the payload that follows and the payload
      never pauses (store and forward): once started, a packet offers a
      word every cycle until its trailer.  The last packet of an image is
      as short as the image leaves it.  An s_eop_i before s_len_i words
      closes the packet early.

      TestMode (`suppress_stream_i`, §8.7.4): no packet starts while it
      is high.  A packet already started completes; the FIFO flush that
      comes with TestMode (cxp_tx_domain) empties the rest.

      Framing, header replication and the CRC come from cxp_tx_pkt_framer;
      this module owns the header fields and the tags.

    Versions:
        2025-05-27 - 0.1:   - Init
        2026-09-19 - 0.2:   - Built on cxp_tx_pkt_framer; abort_i from the arbiter
        2026-09-22 - 0.3:   - DsizeP from the packet length; start on a whole packet
        2026-09-22 - 0.4:   - No packets while stream_en_i is low; tag 0 after a
                              clear in flight
        2026-09-26 - 0.5:   - TestMode holds new packets only; no abort, no
                              truncation, DsizeP always from s_len_i
        2026-09-26 - 0.6:   - A tag clear in the cycle a packet starts
                              restarts the next packet at 0
        2026-09-27 - 0.7:   - Packet words out as one cxp_txw_t (m_o)
        2026-09-27 - 0.8:   - PacketTag table as a RAM and a valid bit per
                              stream instead of 2048 reset flops
        2026-10-04 - 0.9:   - Stream disabled: packets wait instead of
                              being dropped (an image begun is not torn)

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_tx_stream_pkt (
    input  wire  logic        tx_clk,                   // TX clock
    input  wire  logic        tx_rst_n,                 // sync-deassert reset

    input  wire  logic        stream_ctrl_reset_i,      // ConnectionReset / Config
    input  wire  logic        stream_en_i,              // StreamPacketSizeMax != 0
    input  wire  logic        suppress_stream_i,        // §8.7.4 TestMode: no new packet

    // Payload in (from cxp_cdc_stream_fifo).
    input  wire  logic [31:0] s_data_i,                 // payload data word
    input  wire  logic [3:0]  s_kmask_i,                // forwarded for §9.4 K28.3
    input  wire  logic        s_valid_i,                // payload beat valid
    input  wire  logic        s_sop_i,                  // start of packet
    input  wire  logic        s_eop_i,                  // end of packet
    input  wire  logic [7:0]  s_streamid_i,             // packet StreamID
    input  wire  logic [15:0] s_len_i,                  // head packet's data words
    input  wire  logic        s_pkt_avail_i,            // the head packet is all in
    output logic              s_ready_o,                // framer accepts payload

    // Framed packet out.
    output cxp_pkg::cxp_txw_t m_o,                      // packet word, P0 in [7:0]
    input  wire  logic        m_ready_i,                // downstream accepts word
    output logic              busy_o                    // a packet is being read from the FIFO
);

    import cxp_pkg::*;
    import cxp_util_pkg::*;

    //=======================================================================
    // Signals
    //=======================================================================

    // Per-StreamID PacketTag table (§8.5.3, Table 19): the next tag of each
    // stream, valid while its bit in tag_vld_q is set (0 otherwise).
    logic [7:0]   tag_mem [256];
    logic [255:0] tag_vld_q;

    // Per-packet latched state.
    logic [7:0]  curr_streamid_q;
    logic [15:0] curr_dsizeP_q;
    logic [7:0]  curr_tag_q;

    logic        busy;              // framer has a packet in flight
    logic        start;             // SOP at the head, framer idle
    logic        done;              // trailer accepted
    logic        pl_ready;          // framer accepts a payload word
    logic        tag_rst_q;         // tags cleared while this packet was in flight
    logic [5:1][31:0] hdr;          // TYPE, StreamID, Tag, DsizeP H/L
    logic        sop_ready;         // SOP at the head with its whole packet behind it

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign sop_ready = s_valid_i && s_sop_i && s_pkt_avail_i && stream_en_i
                     && !suppress_stream_i;
    assign start     = !busy && sop_ready;
    assign busy_o    = busy;

    assign hdr = {rep4(curr_dsizeP_q[7:0]), rep4(curr_dsizeP_q[15:8]),
                  rep4(curr_tag_q), rep4(curr_streamid_q), rep4(PKT_TYPE_STREAM)};

    assign s_ready_o   = pl_ready;

    //=======================================================================
    // Sequential — per-packet header fields and the PacketTag table.
    //=======================================================================

    always_ff @(posedge tx_clk or negedge tx_rst_n) begin
        if (!tx_rst_n) begin
            curr_streamid_q <= 8'h00;
            curr_dsizeP_q   <= 16'h0000;
            curr_tag_q      <= 8'h00;
            tag_rst_q       <= 1'b0;
            tag_vld_q       <= '0;
        end else begin
            // Latch per-packet metadata on the cycle we leave idle.
            if (start) begin
                curr_streamid_q <= s_streamid_i;
                curr_dsizeP_q   <= s_len_i;
                curr_tag_q      <= tag_vld_q[s_streamid_i] ? tag_mem[s_streamid_i] : 8'h00;
                tag_rst_q       <= 1'b0;
            end

            // Trailer fired — bump the per-StreamID PacketTag, unless
            // the tags were cleared while the packet was in flight.
            if (done && !tag_rst_q) begin
                tag_vld_q[curr_streamid_q] <= 1'b1;
            end

            // §8.5.3 / §10.3.33 ConnectionReset / ConnectionConfig write —
            // PacketTag restarts at 0 for every StreamID.
            // The packet in flight — or starting in this cycle, whose header
            // has just read the old tag — keeps the tag its header carries.
            if (stream_ctrl_reset_i) begin
                tag_vld_q <= '0;
                tag_rst_q <= busy | start;
            end
        end
    end

    // The tag RAM (no reset: an entry is read only while its valid bit is
    // set, and the bit is set together with the write).
    always_ff @(posedge tx_clk) begin
        if (done && !tag_rst_q) tag_mem[curr_streamid_q] <= curr_tag_q + 8'd1;
    end

    //=======================================================================
    // Packet framer — SOP, header, data, CRC, EOP.
    //=======================================================================

    cxp_tx_pkt_framer #(
        .p_HDR_WORDS (6),
        .p_HAS_CRC   (1'b1),
        .p_CRC_FROM  (6)                // Table 19: data words only
    ) cxp_tx_pkt_framer_i (
        .tx_clk       (tx_clk),
        .tx_rst_n     (tx_rst_n),
        .start_i      (sop_ready),
        .hdr_last_i   (3'd5),
        .hdr_i        (hdr),
        .has_body_i   (1'b1),
        .skip_empty_i (1'b0),
        .n_words_i    (curr_dsizeP_q),
        .pl_data_i    (s_data_i),
        .pl_kmask_i   (s_kmask_i),     // passthrough K28.3 markers
        .pl_valid_i   (s_valid_i),
        .pl_eop_i     (s_eop_i),
        .pl_ready_o   (pl_ready),
        .busy_o       (busy),
        .data_phase_o (),
        .data_idx_o   (),
        .m_data_o     (m_o.data),
        .m_kmask_o    (m_o.kmask),
        .m_valid_o    (m_o.valid),
        .m_sop_o      (m_o.sop),
        .m_eop_o      (m_o.eop),
        .m_ready_i    (m_ready_i),
        .done_o       (done)
    );

endmodule

`default_nettype wire
