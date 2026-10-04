/*
================================================================================
  cxp_ctrl_cmd_parser
  CoaXPress 1.1.1 (CXP-001-2015) §8.6.2 / Table 21 — control-cmd parser (RX).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-27

    Description:
      Control command (host -> camera) packet parser.

      Wire format consumed (from cxp_rx_packet_parser, gated by
        long_type == 8'h02):

          TYPE    : 4x0x02   (already vetted, sop asserted here)
          Word 0  : Cmd in P0 (0x00 read, 0x01 write, 0xFF control
                    reset), Size[23:16] in P1, [15:8] in P2, [7:0] in P3
          Word 1  : Addr, big-endian (Addr[31:24] in P0)
          Word 2..N+1 : N data words (writes only; reads have N=0).
                    Big-endian on the wire (§8.2.1) — byte-swapped into
                    native register order before buffering.
                    N = ceil(Size / 4) — Size is in bytes
          Word N+2: 32-bit CRC (register LSByte in P0)
          4xK29.7 : (already detected, eop asserted here)

      This is Table 21: N+6 words from K27.7 to K29.7.  The header words
      are not replicated, so a single-bit error in them is caught by the
      CRC rather than voted away.

      CRC32 covers words 0..N+1 (Cmd/Size through the last data word),
      seed 0xFFFFFFFF, polynomial 0xEDB88320 (reflected 0x04C11DB7), no
      final XOR — same setup as the TX framers (§8.2.2.2).  The CRC word
      and the K29.7 trailer are not in the CRC.

      A word the receiver marks with an 8B/10B error (long_err_i without
      long_eop_i) fails the command like a CRC error (0x80, §8.2.2.2),
      even if the CRC happens to match.

      Nothing is executed before the trailer: the fields and the write
      data are buffered, and every packet ends in one command on cmd_o
      (cmd_valid_o, one cycle after the trailer or the abort).  cmd_o.err
      is 0 for a command to execute, otherwise the code to answer it with
      and nothing is executed (§8.6.1.1; §8.6.3: a failed CRC must not
      write).  Checked at the trailer, first match wins:
          packet aborted by the parser (lost trailer, link loss)   0x47
          trailer before the command word                          0x47
          opcode other than 0x00 / 0x01 / 0xFF                     0x42
          trailer early or late for the declared Size              0x46
          CRC mismatch, or a word with an 8B/10B error             0x80
          read / write with Size 0 (Table 21: B >= 1)              0x46
          N words over the ControlPacketSizeMax limit (§8.6.4)     0x45
          write received on an extension link (§5.1)               0x43
      except that on an extension link a write of ConnectionReset or
      MasterHostConnectionID (by its start address) is answered 0x01 and
      not executed (§10.3.28 / §10.3.30 notes).  N is computed in 26 bits,
      so a Size of 0xFFFFFD .. 0xFFFFFF is oversize, not wrapped to 0.
      An undefined opcode has no known length, so its packet is not
      length- or CRC-checked.  A control channel reset (0xFF) takes no
      data; its Size and Addr are not checked, so a reset always gets
      through.

      Write buffer: two banks of p_BUF_DEPTH words.  An executed write
      carries the bank of its data (cmd_o.wbank); the next write goes to
      the other bank, so a write can arrive while the previous one is
      still executing.  cmd_full_i (sampled at the start of a packet)
      says the executor has no room for another command: that packet's
      data are not stored and, unless it is a 0xFF, no command comes out
      of it.

      The size limit comes from the packet size the device advertises:
      p_PKT_SIZE_MAX bytes (ControlPacketSizeMax, default from the
      generated register map) hold Table 21's six framing words plus N
      data words.  A read is limited the same way, since its Table 22
      acknowledgment is also N + 6 words.  p_BUF_DEPTH must hold that N.

      Output (consumed by cxp_ctrl_bus_master): cmd_o = {op, size, addr,
      nwords (N, saturated at 0xFFFF), err, wbank}; the write buffer is
      read through wbuf_addr_i = {bank, word} / wbuf_data_o (1-cycle
      latency).

    Versions:
        2026-05-27 - 0.1:   - Restyle to coding-style template
        2026-09-19 - 0.2:   - Table 21 header: Cmd+Size word, Addr word
        2026-09-19 - 0.3:   - Aborted packet (long_err_i) acked 0x47; a word
                              with a decode error fails the CRC check
        2026-09-19 - 0.4:   - Opcode, Size and length checks (0x42, 0x46);
                              size limit from ControlPacketSizeMax
        2026-09-26 - 0.5:   - One command record per packet with its code;
                              N in 26 bits; extension-link write check;
                              two write-buffer banks

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_ctrl_cmd_parser #(
    parameter int p_BUF_DEPTH    = 64,                          // write buffer (words)
    parameter int p_PKT_SIZE_MAX = int'(cxp_regmap_pkg::CONTROL_PACKET_SIZE_MAX_VALUE) // bytes
) (
    // Receive clock domain
    input  wire  logic        rx_clk,                           // RX clock
    input  wire  logic        rx_rst_n,                         // Active-low sync reset

    // Long-packet body stream from cxp_rx_packet_parser
    input  wire  logic [31:0] long_data_i,                      // Body word
    input  wire  logic [3:0]  long_kmask_i,                     // Body K-flag (unused)
    input  wire  logic        long_valid_i,                     // Body valid
    input  wire  logic        long_sop_i,                       // Start-of-packet
    input  wire  logic        long_eop_i,                       // End-of-packet
    input  wire  logic        long_err_i,                       // Abort (with eop) / bad word
    input  wire  logic [7:0]  long_type_i,                      // Latched TYPE byte

    input  wire  logic        from_extension_link_i,            // §5.1: writes refused

    // One command per packet, err = 0: execute
    output logic              cmd_valid_o,                      // 1-cycle
    output cxp_pkg::cxp_ctrl_cmd_t cmd_o,
    input  wire  logic        cmd_full_i,                       // executor has no room

    // Write-data buffer read port {bank, word} (used by the executor)
    input  wire  logic [$clog2(p_BUF_DEPTH):0] wbuf_addr_i,     // Buf rd addr
    output logic [31:0]       wbuf_data_o                       // Buf rd data
);

    import cxp_pkg::*;
    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    // Address-bits constant for the buffer port.
    localparam int            BUF_AW = idx_w(p_BUF_DEPTH);
    // Largest N a packet of p_PKT_SIZE_MAX bytes carries (Table 21: N + 6
    // words from K27.7 to K29.7).
    localparam int            MAX_N_I = p_PKT_SIZE_MAX / 4 - 6;
    localparam logic [15:0]   MAX_N   = 16'(MAX_N_I);

    // ------ Types ------

    typedef enum logic [2:0] {
        ST_IDLE,        // waiting for a TYPE-0x02 SOP
        ST_CMDSZ,       // word 0 = Cmd (P0), Size (P1..P3)
        ST_ADDR,        // word 1 = Addr, big-endian
        ST_DATA,        // words 2..N+1 (only for writes)
        ST_CRC,         // word N+2
        ST_EOP          // trailer K29.7
    } state_t;

    //=======================================================================
    // Signals
    //=======================================================================

    logic        gate;             // long_valid & long_type == 0x02

    state_t      state_q;
    state_t      state_n;

    // Latched fields
    logic [7:0]  op_q;
    logic [23:0] size_q;
    logic [31:0] addr_q;
    logic [15:0] n_words_q;        // computed at the time we know Size
    logic [15:0] data_idx_q;       // current word index 0..N-1
    logic        op_bad_q;         // opcode not 0x00 / 0x01 / 0xFF
    logic        size_zero_q;      // read / write with Size 0
    logic        oversize_q;       // N over the packet size limit
    logic        long_q;           // a word arrived after the CRC word
    logic [7:0]  rej_code;         // 0x4x code for this trailer, 0 = none
    logic [7:0]  code;             // the command's answer, 0 = execute
    logic        full_q;           // the executor was full at the SOP
    logic        wbank_q;          // bank of the next write
    logic        ext_write;        // a write on an extension link
    logic        ext_ignored;      // ... to a register ignored there

    // Word 0 fields
    logic [7:0]  op_w;             // Cmd, P0
    logic [23:0] size_w;           // Size, P1 = [23:16]
    logic [25:0] n_w;              // ceil(Size / 4), no wrap
    logic        op_known_w;

    // CRC engine wires
    logic        crc_init;
    logic        crc_din_valid;
    logic [31:0] crc_final;
    logic        crc_pass_q;

    // Write-data buffer, two banks (BRAM-friendly).
    logic [31:0] wbuf [2 * p_BUF_DEPTH];

    // Combinational decode helpers
    logic        word_fire;        // body word landed
    logic        body_eop;         // long_eop_i fired
    logic        pkt_abort;        // the parser aborted the packet
    logic        pkt_end;          // trailer of a packet in progress
    logic        bad_word_q;       // a body word had an 8B/10B error

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign gate              = long_valid_i & (long_type_i == PKT_TYPE_CTRL);
    assign op_w              = long_data_i[7:0];
    assign size_w            = {long_data_i[15:8], long_data_i[23:16], long_data_i[31:24]};
    assign n_w               = (26'(size_w) + 26'd3) >> 2;
    assign op_known_w        = (op_w == CTRL_OP_READ) | (op_w == CTRL_OP_WRITE)
                             | (op_w == CTRL_OP_RESET);

    assign word_fire = gate & ~long_eop_i;
    assign body_eop  = gate & long_eop_i & ~long_err_i;
    assign pkt_abort = gate & long_eop_i &  long_err_i & (state_q != ST_IDLE);
    assign pkt_end   = body_eop & (state_q != ST_IDLE);

    // Rejection at the trailer (description table; the CRC case, 0x80, is
    // added below and sits between 0x46 and the Size checks).
    always_comb begin
        rej_code = 8'h00;
        if (state_q == ST_CMDSZ)                    rej_code = ACK_ERR_MALFORMED;
        else if (op_bad_q)                          rej_code = ACK_ERR_BAD_OP;
        else if (state_q != ST_EOP || long_q)       rej_code = ACK_ERR_SIZE_MISMATCH;
        else if (!crc_pass_q)                       rej_code = 8'h00;
        else if (size_zero_q)                       rej_code = ACK_ERR_SIZE_MISMATCH;
        else if (oversize_q)                        rej_code = ACK_ERR_OVERSIZE;
    end

    // §5.1: an extension link may only read; §10.3.28 / §10.3.30: two
    // registers ignore a write there.
    assign ext_write   = from_extension_link_i & (op_q == CTRL_OP_WRITE);
    assign ext_ignored = (addr_q == 32'(cxp_regmap_pkg::CONNECTION_RESET_ADDR)) |
                         (addr_q == 32'(cxp_regmap_pkg::MASTER_HOST_CONNECTION_ID_ADDR));

    always_comb begin
        code = rej_code;
        if (rej_code == 8'h00) begin
            if (!crc_pass_q)     code = ACK_ERR_CRC;
            else if (ext_write)  code = ext_ignored ? ACK_WRITE_OK : ACK_ERR_RO_WRITE;
        end
    end

    //=======================================================================
    // CRC engine drive
    //=======================================================================

    // CRC engine drive.  Reseeds when a TYPE-0x02 SOP arrives in
    // ST_IDLE, then folds long_data_i for every CRC-covered body
    // word: ST_CMDSZ, ST_ADDR and ST_DATA.  The CRC word itself (state
    // ST_CRC) and the K29.7 trailer are NOT folded.
    always_comb begin
        crc_init      = (state_q == ST_IDLE) && gate && long_sop_i;
        crc_din_valid = word_fire &&
                        ((state_q == ST_CMDSZ) ||
                         (state_q == ST_ADDR)  ||
                         (state_q == ST_DATA));
    end

    //=======================================================================
    // FSM next-state
    //=======================================================================

    always_comb begin
        state_n = state_q;
        unique case (state_q)
            ST_IDLE:    if (gate & long_sop_i) state_n = ST_CMDSZ;
            ST_CMDSZ:   if (word_fire)         state_n = ST_ADDR;
            ST_ADDR:    if (word_fire) begin
                // Data follow for a write with N > 0; otherwise the CRC.
                if (op_q == CTRL_OP_WRITE && n_words_q != 16'd0) state_n = ST_DATA;
                else                                             state_n = ST_CRC;
            end
            ST_DATA:    if (word_fire) begin
                if (data_idx_q == n_words_q - 16'd1) state_n = ST_CRC;
            end
            ST_CRC:     if (word_fire)         state_n = ST_EOP;
            ST_EOP:     if (body_eop)          state_n = ST_IDLE;
            default:                            state_n = ST_IDLE;
        endcase

        // Any other trailer ends the packet: early (the body is shorter
        // than its Size), or aborted by the parser.  Words after the CRC
        // word wait in ST_EOP for the trailer (flagged by long_q).
        if (gate & long_eop_i && (state_q != ST_EOP || long_err_i)) begin
            state_n = ST_IDLE;
        end
    end

    //=======================================================================
    // Write buffer (no reset; a word is read only after it was written)
    //=======================================================================

    always_ff @(posedge rx_clk) begin
        if (state_q == ST_DATA && word_fire && !full_q && data_idx_q < MAX_N)
            wbuf[{wbank_q, data_idx_q[BUF_AW-1:0]}] <= bswap32(long_data_i);
        wbuf_data_o <= wbuf[wbuf_addr_i];
    end

    //=======================================================================
    // FSM sequential (field capture, CRC check, command out)
    //=======================================================================

    always_ff @(posedge rx_clk or negedge rx_rst_n) begin
        if (!rx_rst_n) begin
            state_q                 <= ST_IDLE;
            op_q                    <= 8'h00;
            size_q                  <= 24'h0;
            addr_q                  <= 32'h0;
            n_words_q               <= 16'h0;
            data_idx_q              <= 16'h0;
            op_bad_q                <= 1'b0;
            size_zero_q             <= 1'b0;
            oversize_q              <= 1'b0;
            long_q                  <= 1'b0;
            crc_pass_q              <= 1'b0;
            bad_word_q              <= 1'b0;
            full_q                  <= 1'b0;
            wbank_q                 <= 1'b0;
            cmd_valid_o             <= 1'b0;
            cmd_o                   <= '0;
        end else begin
            cmd_valid_o <= 1'b0;

            state_q <= state_n;

            //---- Field capture ----------------------------------------
            if (state_q == ST_IDLE && gate & long_sop_i) begin
                // Start-of-packet — reset captured fields.  CRC is
                // reseeded on the same cycle by crc_init.
                size_q      <= 24'h0;
                addr_q      <= 32'h0;
                op_bad_q    <= 1'b0;
                size_zero_q <= 1'b0;
                oversize_q  <= 1'b0;
                long_q      <= 1'b0;
                data_idx_q  <= 16'h0;
                crc_pass_q <= 1'b0;
                bad_word_q <= long_err_i;
                full_q     <= cmd_full_i;
            end

            // Word 0: Cmd in P0, Size in P1..P3 (most significant first).
            // A reset takes no data whatever its Size says.
            if (state_q == ST_CMDSZ && word_fire) begin
                op_q        <= op_w;
                size_q      <= size_w;
                n_words_q   <= (op_w == CTRL_OP_RESET) ? 16'd0
                             : (n_w > 26'hFFFF)        ? 16'hFFFF
                             :                           n_w[15:0];
                op_bad_q    <= ~op_known_w;
                size_zero_q <= (op_w != CTRL_OP_RESET) & (size_w == 24'd0);
                oversize_q  <= (op_w != CTRL_OP_RESET) & (n_w > 26'(MAX_N));
            end
            // Word 1: 32-bit address, big-endian on the wire.
            if (state_q == ST_ADDR && word_fire) begin
                addr_q <= bswap32(long_data_i);
            end

            //---- Data words (writes only) -----------------------------
            // §8.3: control write data arrives big-endian on the wire.
            // Byte-swap into the device's native register
            // representation before buffering; the CRC fold above
            // still runs over the on-wire byte order (long_data_i),
            // so CRC coverage is unchanged.
            if (state_q == ST_DATA && word_fire) begin
                data_idx_q <= data_idx_q + 16'd1;
            end

            //---- 8B/10B error in any word: fails like a CRC error ------
            if (word_fire && long_err_i) bad_word_q <= 1'b1;

            //---- A word after the CRC word: longer than its Size ------
            if (state_q == ST_EOP && word_fire) long_q <= 1'b1;

            //---- CRC validation ---------------------------------------
            if (state_q == ST_CRC && word_fire) begin
                // Compared in the device's wire order (crc_wire, cxp_pkg).
                crc_pass_q <= (long_data_i == crc_wire(crc_final))
                            & ~bad_word_q & ~long_err_i;
            end

            //---- One command per packet ------------------------------
            // An aborted packet (§8.6.1.1: an invalid command is
            // acknowledged at once) is 0x47; a trailer gives the code
            // above.  Nothing leaves while the executor is full, except a
            // valid 0xFF, which it always takes.
            if (pkt_abort || pkt_end) begin
                cmd_o.op     <= op_q;
                cmd_o.size   <= size_q;
                cmd_o.addr   <= addr_q;
                cmd_o.nwords <= n_words_q;
                cmd_o.err    <= pkt_abort ? ACK_ERR_MALFORMED : code;
                cmd_o.wbank  <= wbank_q;
                cmd_valid_o  <= ~full_q |
                                (pkt_end & (code == 8'h00) & (op_q == CTRL_OP_RESET));
                if (pkt_end && !full_q && code == 8'h00 && op_q == CTRL_OP_WRITE)
                    wbank_q <= ~wbank_q;
            end
        end
    end

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    if (p_PKT_SIZE_MAX % 4 != 0 || MAX_N_I < 1) begin : g_chk_pkt_size
        $error("cxp_ctrl_cmd_parser: p_PKT_SIZE_MAX (=%0d) must be a multiple of 4 above 24",
               p_PKT_SIZE_MAX);
    end

    if (p_BUF_DEPTH < 2 || (p_BUF_DEPTH & (p_BUF_DEPTH - 1)) != 0) begin : g_chk_buf_pow2
        $error("cxp_ctrl_cmd_parser: p_BUF_DEPTH (=%0d) must be a power of two >= 2 (banked)",
               p_BUF_DEPTH);
    end

    if (p_BUF_DEPTH < MAX_N_I) begin : g_chk_buf_depth
        $error("cxp_ctrl_cmd_parser: p_BUF_DEPTH (=%0d) < %0d words a p_PKT_SIZE_MAX pkt carries",
               p_BUF_DEPTH, MAX_N_I);
    end

    //=======================================================================
    // CRC32 accumulator (shared cxp_lib_crc32 instance)
    //=======================================================================

    cxp_lib_crc32 #(
        .p_IN_W (32)
    ) cxp_lib_crc32_i (
        .clk        (rx_clk),
        .rst_n      (rx_rst_n),
        .init_i     (crc_init),
        .din_i      (long_data_i),
        .din_be_i   (4'b1111),
        .din_valid_i(crc_din_valid),
        .crc_o      (crc_final)
    );

endmodule

`default_nettype wire
