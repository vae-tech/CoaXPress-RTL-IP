/*
================================================================================
  cxp_pkg
  CoaXPress 1.1.1 (CXP-001-2015) — shared protocol constants and types.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-28

    Description:
      Single source of truth for the on-wire CoaXPress protocol values that
      were previously redefined as inline `localparam`s in every module:
      8B/10B K-character bytes, the K28.5 10-bit comma symbols, long-packet
      TYPE bytes, image/line stream-marker sub-types, control opcodes,
      acknowledgement/response codes, the CRC-32 setup, trigger-edge codes
      and the GenICam pixel-format codes.

      Numeric values that legitimately recur across different protocol
      fields (e.g. 0x01..0x04 appear as packet TYPEs, marker sub-types,
      opcodes AND ack codes) are intentionally kept as distinct named
      constants — do NOT collapse them by value.

    Versions:
        2026-05-28 - 0.1:   - Init
        2026-09-19 - 0.2:   - KMASK_ALL/NONE, IDLE_WORD, IDLE / link-test /
                              trigger / I/O-ack constants, RX lock defaults,
                              the missing Table 22 ack codes; opcode table cite
        2026-09-19 - 0.3:   - crc_wire(): the one CRC wire-order function;
                              cxp_txw_t and the TX port table; cxp_pix_t,
                              cxp_rxlong_t
        2026-09-19 - 0.4:   - CRC without final XOR, register on the wire
        2026-09-19 - 0.5:   - pixfmt_bits(), dsizel_words(); no DsizeL in
                              cxp_meta_t
        2026-09-19 - 0.6:   - Mono16 = 0x0105; Mono14 packed at 14 bits
        2026-09-19 - 0.7:   - RX_LOSS_WORDS_DEFAULT replaces the 8192-bit
                              sampler loss window
        2026-09-19 - 0.8:   - cxp_rxlong_t.err: packet abort or decode error;
                              RX_MAX_BODY_WORDS
        2026-09-19 - 0.9:   - cxp_ctrl_cmd_t, cxp_ctrl_rsp_t
        2026-09-26 - 0.10:  - cxp_ctrl_cmd_t.err / .wbank, cxp_ctrl_rsp_t.rbank
        2026-09-26 - 0.11:  - Long-packet port table (3 ports); IDLE_SOFT_RUN;
                              TRIG_ACK_TIMEOUT
        2026-09-26 - 0.12:  - RX_BAD_WORDS_DEFAULT
        2026-09-26 - 0.13:  - TRIG_DELAY_MAX / TRIG_UNITS_PER_BIT replace
                              TRIG_DELAY_BASE
        2026-09-27 - 0.14:  - RX_LOCK_IDLES_DEFAULT
        2026-10-04 - 0.15:  - PFNC_* and pfnc_to_pixelf() (§11.2.1.6)

================================================================*/

`timescale 1ns / 1ns

package cxp_pkg;

    //=======================================================================
    // 8B/10B control-character byte values (Kx.y -> y*32 + x), §6.2.1.
    //=======================================================================

    localparam logic [7:0] K27_7 = 8'hFB;   // long-packet SOP byte
    localparam logic [7:0] K28_1 = 8'h3C;   // IDLE word P1/P2
    localparam logic [7:0] K28_2 = 8'h5C;   // falling-edge trigger leader
    localparam logic [7:0] K28_3 = 8'h7C;   // stream image/line marker (§9.4)
    localparam logic [7:0] K28_4 = 8'h9C;   // rising-edge trigger leader
    localparam logic [7:0] K28_5 = 8'hBC;   // IDLE word P0 / comma
    localparam logic [7:0] K28_6 = 8'hDC;   // I/O-ack packet marker
    localparam logic [7:0] K29_7 = 8'hFD;   // long-packet EOP byte
    localparam logic [7:0] D21_5 = 8'hB5;   // IDLE word P3 (data char)

    //=======================================================================
    // K28.5 10-bit symbol, din-layout (LSB-first abcdei.fghj):
    //   RD- : abcdei fghj = 001111 1010 -> din = 10'b0101111100
    //   RD+ : abcdei fghj = 110000 0101 -> din = 10'b1010000011
    //=======================================================================

    localparam logic [9:0] K28_5_NEG = 10'b0101111100;
    localparam logic [9:0] K28_5_POS = 10'b1010000011;

    // Table 15 low-speed trigger leaders, same layout:
    //   K28.2 RD- 001111 0101, RD+ 110000 1010
    //   K28.4 RD- 001111 0010, RD+ 110000 1101
    localparam logic [9:0] K28_2_NEG = 10'b1010111100;
    localparam logic [9:0] K28_2_POS = 10'b0101000011;
    localparam logic [9:0] K28_4_NEG = 10'b0100111100;
    localparam logic [9:0] K28_4_POS = 10'b1011000011;

    //=======================================================================
    // IDLE word layout (§8.2.5): {D21.5, K28.1, K28.1, K28.5} = P3..P0,
    // kmask = K K K D.
    //=======================================================================

    localparam logic [3:0] KMASK_IDLE = 4'b0111;
    localparam logic [31:0] IDLE_WORD = {D21_5, K28_1, K28_1, K28_5};

    // Whole-word kmasks: all four lanes K (SOP / EOP / markers) or data.
    localparam logic [3:0] KMASK_ALL  = 4'b1111;
    localparam logic [3:0] KMASK_NONE = 4'b0000;

    //=======================================================================
    // IDLE insertion limits (§8.2.5.1): at least one IDLE word every N
    // words.  32-bit words; a low-speed word is 40 line bits.
    //=======================================================================

    localparam int IDLE_MAX_INTERVAL = 100;     // high-speed connection
    // Run of words after which cxp_tx_inserter sends the IDLE as soon as
    // no two-word packet is due (<= IDLE_MAX_INTERVAL - 5, so a trigger
    // and an I/O acknowledgment still fit before it).
    localparam int IDLE_SOFT_RUN     = IDLE_MAX_INTERVAL - 5;
    localparam int LS_IDLE_MAX_WORDS = 10000;   // low-speed connection
    localparam int LS_BITS_PER_WORD  = 40;      // 4 x 10b symbols

    //=======================================================================
    // RX lock defaults (sampler / link monitor / rx_top / interface_top).
    // The link is lost after RX_LOSS_WORDS_DEFAULT words without an IDLE:
    // twice the §8.2.5.1 low-speed interval, i.e. 800 000 bits.  The
    // framing is judged wrong after RX_BAD_WORDS_DEFAULT words since the
    // last clean IDLE that do not fit it (cxp_rx_link_mon).
    //=======================================================================

    localparam int RX_LOCK_HITS_DEFAULT  = 2;   // K28.5 hits: sampler lock
    localparam int RX_LOCK_IDLES_DEFAULT = 2;   // clean IDLE words: link up
    localparam int RX_LOSS_WORDS_DEFAULT = 2 * LS_IDLE_MAX_WORDS;
    localparam int RX_BAD_WORDS_DEFAULT  = 32;

    //=======================================================================
    // Long-packet TYPE byte (word after SOP).
    //=======================================================================

    localparam logic [7:0] PKT_TYPE_STREAM = 8'h01;   // stream data
    localparam logic [7:0] PKT_TYPE_CTRL   = 8'h02;   // control command
    localparam logic [7:0] PKT_TYPE_ACK    = 8'h03;   // control acknowledge
    localparam logic [7:0] PKT_TYPE_LT     = 8'h04;   // link test

    //=======================================================================
    // Connection test packet (§8.7.2, Table 23): SOP, TYPE, 1024 counter
    // words, EOP.  The inter-packet gap is this device's choice (§8.7.3
    // only asks the Host for >= 1 IDLE word).
    //=======================================================================

    localparam int LT_DATA_WORDS = 1024;
    localparam int LT_PKT_WORDS  = LT_DATA_WORDS + 3;   // 1027 (Table 23)
    localparam int LT_GAP_WORDS  = 16;

    // Longest uplink packet body the receiver accepts (TYPE word to the
    // word before K29.7): twice a test packet; a longer body is a lost
    // trailer.
    localparam int RX_MAX_BODY_WORDS = 2 * LT_DATA_WORDS;

    //=======================================================================
    // Stream-marker sub-type byte (word after the K28.3 marker, §9.4).
    //=======================================================================

    localparam logic [7:0] HDR_TYPE_REC  = 8'h01;     // image header, rectangle
    localparam logic [7:0] HDR_TYPE_ARB  = 8'h03;     // image header, arbitrary
    localparam logic [7:0] LINE_TYPE_RECT = 8'h02;    // line marker, rectangle
    localparam logic [7:0] LINE_TYPE_ARB  = 8'h04;    // line marker, arbitrary

    //=======================================================================
    // Control-command opcodes (§8.6.2 Table 21).
    //=======================================================================

    localparam logic [7:0] CTRL_OP_READ  = 8'h00;
    localparam logic [7:0] CTRL_OP_WRITE = 8'h01;
    localparam logic [7:0] CTRL_OP_RESET = 8'hFF;

    //=======================================================================
    // Acknowledgement / response codes (§8.6.3 Table 22).
    //=======================================================================

    localparam logic [7:0] ACK_OK           = 8'h00;  // read OK (carries data)
    localparam logic [7:0] ACK_WRITE_OK     = 8'h01;  // write OK (no data)
    localparam logic [7:0] ACK_RESET_DONE   = 8'h03;  // reset done
    localparam logic [7:0] ACK_WAIT         = 8'h04;  // wait (4-byte ms payload)
    localparam logic [7:0] ACK_ERR_BAD_ADDR = 8'h40;  // invalid address (0x40-class)
    localparam logic [7:0] ACK_ERR_BAD_DATA = 8'h41;  // invalid data for the address
    localparam logic [7:0] ACK_ERR_BAD_OP   = 8'h42;  // invalid control operation code
    localparam logic [7:0] ACK_ERR_RO_WRITE = 8'h43;  // write to read-only address
    localparam logic [7:0] ACK_ERR_WO_READ  = 8'h44;  // read from write-only address
    localparam logic [7:0] ACK_ERR_OVERSIZE = 8'h45;  // size field too large
    localparam logic [7:0] ACK_ERR_SIZE_MISMATCH = 8'h46; // size inconsistent with message
    localparam logic [7:0] ACK_ERR_MALFORMED = 8'h47; // malformed packet
    localparam logic [7:0] ACK_ERR_CRC      = 8'h80;  // CRC error

    //=======================================================================
    // CRC-32 setup (§8.2.2.2): the 802.3 polynomial, reflected, seed
    // 0xFFFFFFFF, no final XOR.
    //=======================================================================

    localparam logic [31:0] CRC_SEED   = 32'hFFFF_FFFF;
    localparam logic [31:0] CRC_POLY   = 32'hEDB8_8320;   // reflected 0x04C11DB7

    // The CRC word on the wire: §8.2.2.2 sends the register MSB in P0 bit 0;
    // for the reflected register that is the register itself with its
    // LSByte in P0 (worked example: 56 86 5D 6F).  Every framer and checker
    // goes through this one function.
    function automatic logic [31:0] crc_wire(input logic [31:0] crc);
        crc_wire = crc;
    endfunction

    //=======================================================================
    // Trigger edge encoding: internal to the RX trigger path, not a wire
    // value (the §8.3.2.1 packets carry K28.2 / K28.4 instead).
    //=======================================================================

    localparam logic [1:0] TRIG_EDGE_NONE = 2'b00;
    localparam logic [1:0] TRIG_EDGE_RISE = 2'b01;
    localparam logic [1:0] TRIG_EDGE_FALL = 2'b10;

    // Low-speed trigger Delay (§8.3.2.1, Table 15, Figure 20): 0..239, in
    // units of 1/24 of the low-speed bit interval (one character = 240).
    localparam int TRIG_DELAY_MAX      = 239;
    localparam int TRIG_UNITS_PER_BIT  = 24;

    //=======================================================================
    // I/O acknowledgment (§8.3.3, Table 17).
    //=======================================================================

    localparam logic [7:0] IOACK_CODE_OK = 8'h01;   // trigger packet received OK

    // §8.3.3: tx_clk cycles a device trigger packet waits for the host's
    // I/O acknowledgment before the next one may go.  The acknowledgment
    // comes on the low-speed uplink (two 40-bit words at 20.83 Mbps, after
    // at most one word in flight: about 6 us); 4096 cycles is 26 us at
    // 156.25 MHz and 131 us at 31.25 MHz.
    localparam int TRIG_ACK_TIMEOUT = 4096;

    //=======================================================================
    // Pixel-format codes (PixelF, Table 25).
    //=======================================================================

    localparam logic [15:0] PIXFMT_MONO8  = 16'h0101;
    localparam logic [15:0] PIXFMT_MONO10 = 16'h0102;
    localparam logic [15:0] PIXFMT_MONO12 = 16'h0103;
    localparam logic [15:0] PIXFMT_MONO14 = 16'h0104;
    localparam logic [15:0] PIXFMT_MONO16 = 16'h0105;

    // PixelFormat register values: GenICam PFNC (§11.2.1.6).  The device
    // maps them to the PixelF code it sends.
    localparam logic [31:0] PFNC_MONO8  = 32'h0108_0001;
    localparam logic [31:0] PFNC_MONO10 = 32'h0110_0003;
    localparam logic [31:0] PFNC_MONO12 = 32'h0110_0005;
    localparam logic [31:0] PFNC_MONO14 = 32'h0110_0025;
    localparam logic [31:0] PFNC_MONO16 = 32'h0110_0007;

    // PFNC value -> Table 25 PixelF code; 0 for a value without one (the
    // register refuses those).
    function automatic logic [15:0] pfnc_to_pixelf(input logic [31:0] pfnc);
        unique case (pfnc)
            PFNC_MONO8:  pfnc_to_pixelf = PIXFMT_MONO8;
            PFNC_MONO10: pfnc_to_pixelf = PIXFMT_MONO10;
            PFNC_MONO12: pfnc_to_pixelf = PIXFMT_MONO12;
            PFNC_MONO14: pfnc_to_pixelf = PIXFMT_MONO14;
            PFNC_MONO16: pfnc_to_pixelf = PIXFMT_MONO16;
            default:     pfnc_to_pixelf = 16'h0000;
        endcase
    endfunction

    // Bits per pixel cxp_app_pixel_packer puts on the wire for a PixelF code.
    function automatic logic [4:0] pixfmt_bits(input logic [15:0] pixfmt);
        unique case (pixfmt)
            PIXFMT_MONO10: pixfmt_bits = 5'd10;
            PIXFMT_MONO12: pixfmt_bits = 5'd12;
            PIXFMT_MONO14: pixfmt_bits = 5'd14;
            PIXFMT_MONO16: pixfmt_bits = 5'd16;
            default:       pixfmt_bits = 5'd8;
        endcase
    endfunction

    // DsizeL (Tables 38/41): 32-bit data words per line of xsize pixels.
    function automatic logic [23:0] dsizel_words(input logic [23:0] xsize,
                                                 input logic [15:0] pixfmt);
        logic [31:0] bits;
        bits = 32'(xsize) * 32'(pixfmt_bits(pixfmt));
        dsizel_words = 24'((bits + 32'd31) >> 5);
    endfunction

    //=======================================================================
    // TX word bus: one packet source into cxp_tx_arbiter (long packets) or
    // cxp_tx_inserter (triggers, I/O acknowledgments).  Every source sits
    // behind ready.
    //=======================================================================

    typedef struct packed {
        logic [31:0] data;      // word, P0 in [7:0]
        logic [3:0]  kmask;     // per-byte K-character flag
        logic        valid;     // word valid
        logic        sop;       // first word of a packet
        logic        eop;       // last word of a packet
    } cxp_txw_t;

    // Long-packet ports of cxp_tx_arbiter (Table 13 priority 2); the index
    // is the order between them, 0 first.
    localparam int TX_PORTS       = 3;
    localparam int TX_PORT_ACK    = 0;    // control acknowledge
    localparam int TX_PORT_LT     = 1;    // connection-test packets
    localparam int TX_PORT_STREAM = 2;    // stream data

    //=======================================================================
    // Image / line stream-marker metadata.  DsizeL is not carried: the
    // marker generators derive it from xsize and pixfmt (dsizel_words).
    //=======================================================================

    typedef struct packed {
        logic        arbitrary;
        logic [7:0]  streamid;
        logic [15:0] sourcetag;
        logic [23:0] xsize;
        logic [23:0] ysize;
        logic [23:0] xoffs;
        logic [23:0] yoffs;
        logic [15:0] pixfmt;
        logic [15:0] tapg;
        logic [7:0]  flags;
    } cxp_meta_t;

    //=======================================================================
    // Long-packet words from the uplink receiver (cxp_rx_link) to its
    // consumers (control plane, connection-test checker).
    //=======================================================================

    typedef struct packed {
        logic [31:0] data;      // packet word (TYPE word, body, trailer)
        logic [3:0]  kmask;     // per-byte K-character flag
        logic        valid;     // word valid
        logic        sop;       // TYPE word, first word of the packet
        logic        eop;       // last word: K29.7 trailer, or an abort
        logic        err;       // with eop: packet aborted (framing / link
                                // loss); without: 8B/10B error in this word
        logic [7:0]  ptype;     // packet TYPE (voted)
    } cxp_rxlong_t;

    //=======================================================================
    // Control plane (§8.6): one validated command from the router to the
    // bus master, and one held response per command back to the
    // acknowledgment framer.
    //=======================================================================

    typedef struct packed {
        logic [7:0]  op;        // Table 21 opcode
        logic [23:0] size;      // Size B, bytes
        logic [31:0] addr;      // first byte address
        logic [15:0] nwords;    // N = ceil(B / 4)
        logic [7:0]  err;       // 0 = execute; else the code to answer
        logic        wbank;     // write-buffer bank holding the data
    } cxp_ctrl_cmd_t;

    typedef struct packed {
        logic [7:0]  code;      // Table 22 acknowledgment code
        logic [15:0] nwords;    // read data words in the read buffer
        logic [23:0] size;      // Size B of a 0x00 ack
        logic [31:0] wait_ms;   // reply data of a 0x04 ack
        logic        timeout;   // the access timed out (code is 0x40)
        logic        rbank;     // read-buffer bank holding the data
    } cxp_ctrl_rsp_t;

    //=======================================================================
    // Single-pixel stream between a pixel source (TPG / sensor ingress) and
    // cxp_app_pixel_packer.  Pixel value LSB-justified in data.
    //=======================================================================

    typedef struct packed {
        logic [15:0] data;      // pixel value
        logic        valid;     // pixel valid
        logic        sof;       // first pixel of a frame
        logic        sol;       // first pixel of a line
        logic        eol;       // last pixel of a line
        logic        eof;       // last pixel of a frame
    } cxp_pix_t;

    //=======================================================================
    // Device configuration into cxp_interface_top: quasi-static levels on
    // rx_clk (the register file's clock), and the two subsets that cross
    // into the pixel (app_clk) and transmit (tx_clk) domains.
    //=======================================================================

    typedef struct packed {
        logic        use_tpg;       // 1 = test pattern, 0 = sensor pixels
        logic        run;           // pixel gate armed without an acquisition
        logic [1:0]  acq_mode;      // 0 continuous, 2 acq_frames images
        logic [15:0] acq_frames;    // images per acquisition (mode 2)
        logic        stream_en;     // a stream packet fits in StreamPacketSizeMax
        logic [15:0] xsize;         // TPG width (px), 0 = maximum
        logic [15:0] ysize;         // TPG height (lines), 0 = maximum
        logic [15:0] pixfmt;        // PixelFormat, 0 = TPG default / sensor's
        logic [7:0]  streamid;      // Image1StreamID (TPG images)
        logic [15:0] xoffs;         // OffsetX (TPG header)
        logic [15:0] yoffs;         // OffsetY (TPG header)
        logic [15:0] srctag;        // SourceTag preset (on change)
        logic [15:0] tapg;          // TapGeometry (TPG header)
        logic [7:0]  flags;         // StreamFlags (TPG header)
        logic [1:0]  testpat;       // TPG pattern 0..3
        logic        arbitrary;     // arbitrary-form header and markers
        logic [15:0] dsizeP;        // stream payload words per packet
        logic        trig_polarity; // trigger pin 0 = high, 1 = low active
        logic        test_mode;     // TestMode (§10.3.35)
        logic        ext_link;      // §5.1 strap: an extension connection
    } cxp_cfg_t;

    typedef struct packed {
        logic        use_tpg;
        logic        run;
        logic [1:0]  acq_mode;
        logic [15:0] acq_frames;
        logic        stream_en;
        logic [15:0] xsize;
        logic [15:0] ysize;
        logic [15:0] pixfmt;
        logic [7:0]  streamid;
        logic [15:0] xoffs;
        logic [15:0] yoffs;
        logic [15:0] srctag;
        logic [15:0] tapg;
        logic [7:0]  flags;
        logic [1:0]  testpat;
        logic        arbitrary;
        logic [15:0] dsizeP;
    } cxp_cfg_app_t;

    typedef struct packed {
        logic        test_mode;
        logic        trig_polarity;
        logic        stream_en;
    } cxp_cfg_tx_t;

    //=======================================================================
    // Link status out of cxp_interface_top, on rx_clk.
    //=======================================================================

    typedef struct packed {
        logic        rx_lock;           // sampler symbol lock
        logic        aligned;           // link up (IDLE monitor)
        logic        link_detected;     // §10.1.1 Detected
        logic [31:0] lt_err_count;      // TestErrorCount (§10.3.37)
        logic [63:0] lt_pkt_count_tx;   // TestPacketCountTx (§10.3.38)
        logic [63:0] lt_pkt_count_rx;   // TestPacketCountRx (§10.3.39)
        logic        ctrl_reset_pulse;  // host 0xFF control-channel reset
        logic        ctrl_nack_pulse;   // command answered without an
        logic [7:0]  ctrl_nack_code;    //   access (0x4x, 0x80)
        logic        pkt_err_pulse;     // uplink packet framing error
        logic        code_err_pulse;    // 8B/10B code error
        logic        disp_err_pulse;    // 8B/10B running-disparity error
    } cxp_status_t;

    //=======================================================================
    // Protocol-specific helpers: K28.5 comma and K28.2 / K28.4 trigger
    // leader detect on a 10-bit symbol.
    //=======================================================================

    function automatic logic is_k28_5(input logic [9:0] s);
        is_k28_5 = (s == K28_5_NEG) | (s == K28_5_POS);
    endfunction

    function automatic logic is_k28_2(input logic [9:0] s);
        is_k28_2 = (s == K28_2_NEG) | (s == K28_2_POS);
    endfunction

    function automatic logic is_k28_4(input logic [9:0] s);
        is_k28_4 = (s == K28_4_NEG) | (s == K28_4_POS);
    endfunction

endpackage
