/*
================================================================================
  cxp_ctrl_bootstrap_regs
  CoaXPress 1.1.1 (CXP-001-2015) §10.3 / Table 45 — bootstrap register file.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-27

    Description:
      Register file behind the control-command executor.  One access per
      we_i / re_i strobe; ready_o, rdata_o and err_o follow one cycle
      later.  err_o is the Table 22 code of the access, 0 when it is done:
        0x40  unaligned, or no register at the address (full 32 bits)
        0x43  write to a read-only register, string or the XML ROM
        0x44  read of a write-only feature
        0x41  write of a value the register does not take
      A refused write changes nothing.

      The map is the ROWS table: one row per register or byte range with
      its address, length, kind (kind_t) and power-on value.  The decode
      scans the table and the last row that covers addr_i answers (acc),
      so a window row precedes the words inside it.  The kind decides who
      serves a read, which writes are refused and where an accepted write
      lands; every word register is reg_q[row].  Addresses, power-on
      values and the XML ROM geometry come from cxp_regmap_pkg (generated
      from src/regmap/cxp_regmap.yaml); value_ok() mirrors that map's
      min / max / allowed / multiple entries.
        bootstrap     Table 45 rows at 0x0000 .. 0xFFFF
        device        §10.3.19-27: the 0x3000 slot reads the feature's
                      address; the feature is a register at that address
        manufacturer  words at MFR_BASE + 4*n (n < MFR_WORDS); a word
                      without a row reads 0 and drops a write
        XML ROM       XML_BLOB_BYTES at XML_BLOB_ADDR, read-only

      ConnectionReset (§10.3.28) is owned here.  Writing 1 to 0x4000 (or
      a conn_reset_req_i pulse) loads every register's ConnectionReset
      value at once, pulses the test-counter clears and sets the bit.
      The bit is ctl_connection_reset_active_o: the other clock domains
      apply their part (PacketTag restart, trigger de-asserted,
      TestPacketCountTx) while they see it, and echo it back on
      conn_reset_done_i.  The bit clears once it has been set for
      p_LINK_RESET_CLEAR_CYCLES cycles and the echo is back — the device
      has then activated its discovery configuration — or after
      p_CONN_RESET_TIMEOUT cycles without the echo.  Power-up is a
      connection reset: every ConnectionReset value is also the power-on
      value (src/regmap/gen_regmap.py enforces it).

      Byte order: strings and 8-byte counters are big-endian — the lowest
      address holds the first characters / the most significant word.

    Versions:
        2026-05-27 - 0.1:   - Restyle to coding-style template
        2026-09-23 - 0.2:   - One ROWS table with a kind per row, reg_q[row]
                              storage and value_ok() limits; hand-written
                              again (the map itself stays generated)
        2026-09-25 - 0.3:   - ConnectionReset owned here: active level out,
                              done echo in, local request in; the clr_*
                              inputs are gone
        2026-09-27 - 0.4:   - Word registers honour wstrb_i: a write of B
                              bytes changes only those bytes, and the
                              value check sees the merged word
        2026-09-27 - 0.5:   - ctl_stream_flags_o: StreamFlags drives the TPG
                              header Flags
        2026-09-30 - 0.6:   - Served strings (XmlUrl, identity) come from
                              cxp_regmap_pkg, generated with the XML
        2026-10-04 - 0.7:   - PixelFormat takes GenICam PFNC values
                              (§11.2.1.6)

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_ctrl_bootstrap_regs #(
    parameter int    p_NUM_LINKS               = 1,     // connections (test counters)
    parameter int    p_LINK_RESET_CLEAR_CYCLES = 16,    // ConnectionReset bit, minimum cycles
    parameter int    p_CONN_RESET_TIMEOUT      = 65535, // ... maximum without the done echo
    parameter string p_XML_BLOB_MEM            = cxp_regmap_pkg::XML_BLOB_MEM,  // ROM image
    // Power-on PixelFormat / Image1StreamID (the device top passes its TPG values)
    parameter logic [31:0] p_PIXEL_FORMAT_RESET     = cxp_regmap_pkg::PIXEL_FORMAT_RESET,
    parameter logic [31:0] p_IMAGE1_STREAM_ID_RESET = cxp_regmap_pkg::IMAGE1_STREAM_ID_RESET
) (
    input  wire  logic         sys_clk,                             // register clock
    input  wire  logic         sys_rst_n,                           // async active-low reset

    input  wire  logic [31:0]  addr_i,                              // byte address
    input  wire  logic [31:0]  wdata_i,                             // write data
    input  wire  logic [3:0]   wstrb_i,                             // byte enables, [3] = 31:24
    input  wire  logic         we_i,                                // write strobe
    input  wire  logic         re_i,                                // read strobe
    output logic [31:0]        rdata_o,                             // read data, 1 cycle after re_i
    output logic               ready_o,                             // done, 1 cycle after a strobe
    output logic [7:0]         err_o,                               // Table 22 code with ready_o

    output logic               ctl_connection_reset_pulse_o,        // ConnectionReset pulse
    output logic               ctl_connection_reset_active_o,       // ConnectionReset bit (level)
    output logic               ctl_connection_config_wr_o,          // ConnectionConfig written
    output logic [31:0]        ctl_device_connection_id_o,          // DeviceConnectionID
    output logic [31:0]        ctl_master_host_connection_id_o,     // MasterHostConnectionID
    output logic [31:0]        ctl_stream_pkt_dsize_o,              // StreamPacketSizeMax
    output logic [31:0]        ctl_connection_config_o,             // ConnectionConfig
    output logic               ctl_test_mode_o,                     // TestMode
    output logic               ctl_acquisition_start_wr_o,          // AcquisitionStart written
    output logic               ctl_acquisition_stop_wr_o,           // AcquisitionStop written
    output logic [31:0]        ctl_tpg_width_o,                     // Width
    output logic [31:0]        ctl_tpg_height_o,                    // Height
    output logic [31:0]        ctl_acquisition_mode_o,              // AcquisitionMode
    output logic [31:0]        ctl_acquisition_start_o,             // AcquisitionStart
    output logic [31:0]        ctl_acquisition_stop_o,              // AcquisitionStop
    output logic [31:0]        ctl_pixel_format_o,                  // PixelFormat
    output logic [31:0]        ctl_tap_geometry_o,                  // TapGeometry
    output logic [31:0]        ctl_image1_stream_id_o,              // Image1StreamID
    output logic [31:0]        ctl_frame_count_o,                   // FrameCount
    output logic [31:0]        ctl_test_pattern_o,                  // TestPattern
    output logic [31:0]        ctl_offset_x_o,                      // OffsetX
    output logic [31:0]        ctl_offset_y_o,                      // OffsetY
    output logic [31:0]        ctl_source_tag_o,                    // SourceTag
    output logic [31:0]        ctl_stream_flags_o,                  // StreamFlags
    output logic [31:0]        ctl_tpg_run_o,                       // TpgRun
    output logic               ctl_test_err_count_clr_o,            // clear TestErrorCount
    output logic               ctl_test_pkt_tx_clr_o,               // clear TestPacketCountTx
    output logic               ctl_test_pkt_rx_clr_o,               // clear TestPacketCountRx
    output logic [127:0]       ctl_device_user_id_o,                // DeviceUserID

    input  var   logic [31:0]  test_err_count_i [p_NUM_LINKS],      // TestErrorCount, per link
    input  var   logic [63:0]  test_pkt_count_tx_i [p_NUM_LINKS],   // TestPacketCountTx, per link
    input  var   logic [63:0]  test_pkt_count_rx_i [p_NUM_LINKS],   // TestPacketCountRx, per link
    input  wire  logic [127:0] device_user_id_nv_i,                 // DeviceUserID power-on value

    input  wire  logic         conn_reset_req_i,                    // 1-cycle: as a write of 1
    input  wire  logic         conn_reset_done_i                    // the active level, echoed
);

    import cxp_regmap_pkg::*;
    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    localparam int R_N     = 57;                                   // rows in ROWS
    localparam int LRST_CW = cnt_w(p_LINK_RESET_CLEAR_CYCLES);     // ConnectionReset timer
    localparam int TMO_W   = cnt_w(p_CONN_RESET_TIMEOUT);          // ... and its timeout
    localparam int NL_W    = idx_w(p_NUM_LINKS);                   // link index
    localparam int OFF_W   = idx_w(XML_BLOB_WORDS);                // word offset in a row

    // §10.3.27: Image<n>StreamIDAddress for n > 1 reads 0 up to the CXP group.
    localparam int IMAGE_N_SID_BYTES = int'(CONNECTION_RESET_ADDR)
                                     - int'(IMAGE_N_STREAM_ID_ADDRESS_ADDR);

    // Strings (cxp_regmap_pkg::*_STR), big-endian and NULL-padded;
    // ROWS[].len bounds what is read.
    localparam logic [511:0] XML_URL_TXT                  = str_field(XML_URL_STR);
    localparam logic [511:0] DEVICE_VENDOR_NAME_TXT       = str_field(DEVICE_VENDOR_NAME_STR);
    localparam logic [511:0] DEVICE_MODEL_NAME_TXT        = str_field(DEVICE_MODEL_NAME_STR);
    localparam logic [511:0] DEVICE_MANUFACTURER_INFO_TXT = str_field(DEVICE_MANUFACTURER_INFO_STR);
    localparam logic [511:0] DEVICE_VERSION_TXT           = str_field(DEVICE_VERSION_STR);
    localparam logic [511:0] DEVICE_SERIAL_NUMBER_TXT     = str_field(DEVICE_SERIAL_NUMBER_STR);

    // ------ Types ------

    // What a row of the map is: who serves a read, which writes are refused
    // (Table 22) and where an accepted write lands.
    typedef enum logic [3:0] {
        K_NONE,     // no register: 0x40
        K_RO,       // constant word, ROWS[].rst; a write is 0x43
        K_RW,       // word register reg_q[]; a write value_ok() refuses is 0x41
        K_WO,       // as K_RW, but a read is 0x44
        K_CRST,     // ConnectionReset: bit 0; a write of 1 applies §10.3.28
        K_CNT,      // live test counter, 4 or 8 bytes; a write of 0 clears it
        K_STR,      // read-only string; a write is 0x43
        K_NVSTR,    // DeviceUserID: 16-byte R/W string, power-on from the NV port
        K_ZERO,     // read-only bytes that read 0; a write is 0x43
        K_VOID,     // bytes that read 0 and drop a write
        K_ROM       // the GenICam XML ROM; a write is 0x43
    } kind_t;

    // Row index: the position in ROWS (scan order).
    typedef enum logic [5:0] {
        R_STANDARD,
        R_REVISION,
        R_XML_MANIFEST_SIZE,
        R_XML_MANIFEST_SELECTOR,
        R_XML_VERSION,
        R_XML_SCHEMA_VERSION,
        R_XML_URL_ADDRESS,
        R_IIDC2_ADDRESS,
        R_DEVICE_VENDOR_NAME,
        R_DEVICE_MODEL_NAME,
        R_DEVICE_MANUFACTURER_INFO,
        R_DEVICE_VERSION,
        R_DEVICE_SERIAL_NUMBER,
        R_DEVICE_USER_ID,
        R_WIDTH_SLOT,
        R_HEIGHT_SLOT,
        R_ACQUISITION_MODE_SLOT,
        R_ACQUISITION_START_SLOT,
        R_ACQUISITION_STOP_SLOT,
        R_PIXEL_FORMAT_SLOT,
        R_TAP_GEOMETRY_SLOT,
        R_IMAGE1_STREAM_ID_SLOT,
        R_IMAGE_N_STREAM_ID_ADDRESS,
        R_CONNECTION_RESET,
        R_DEVICE_CONNECTION_ID,
        R_MASTER_HOST_CONNECTION_ID,
        R_CONTROL_PACKET_SIZE_MAX,
        R_STREAM_PACKET_SIZE_MAX,
        R_CONNECTION_CONFIG,
        R_CONNECTION_CONFIG_DEFAULT,
        R_TEST_MODE,
        R_TEST_ERROR_COUNT_SELECTOR,
        R_TEST_ERROR_COUNT,
        R_TEST_PACKET_COUNT_TX,
        R_TEST_PACKET_COUNT_RX,
        R_ELECTRICAL_COMPLIANCE_TEST,
        R_HS_UPCONNECTION,
        R_XML_URL,
        R_MFR_WINDOW,
        R_WIDTH,
        R_HEIGHT,
        R_PIXEL_FORMAT,
        R_MFR_RESERVED1,
        R_ACQUISITION_START,
        R_ACQUISITION_STOP,
        R_FRAME_COUNT,
        R_TEST_PATTERN,
        R_OFFSET_X,
        R_OFFSET_Y,
        R_TAP_GEOMETRY,
        R_IMAGE1_STREAM_ID,
        R_SOURCE_TAG,
        R_STREAM_FLAGS,
        R_TPG_RUN,
        R_MFR_RESERVED2,
        R_ACQUISITION_MODE,
        R_XML_ROM
    } row_t;

    // A row of the map.
    typedef struct packed {
        logic [31:0] addr;      // first byte address
        logic [31:0] len;       // bytes the row answers for
        kind_t       kind;
        logic [31:0] rst;       // K_RO: the value read; registers: power-on value
    } desc_t;

    // The access decoded this cycle.
    typedef struct packed {
        kind_t            kind;     // K_NONE when nothing answers
        row_t             row;      // the row (last match in ROWS)
        logic [OFF_W-1:0] off;      // word offset inside the row
        logic             val_ok;   // the merged write is a value the row takes
    } acc_t;

    // The write accepted last cycle (the *_wr_o / *_clr_o pulses).
    typedef struct packed {
        logic valid;
        row_t row;
        logic zero;                 // an accepted write of 0
    } wr_t;

    // ------ Functions and Tasks ------

    // `s` as a big-endian string field left-justified in 512 bits: first
    // character in bits [511:504], NULL padding below, beyond 64 cut off.
    function automatic logic [511:0] str_field(input string s);
        str_field = '0;
        for (int i = 0; i < s.len() && i < 64; i++)
            str_field[511 - 8*i -: 8] = s[i];
    endfunction

    // Word `widx` of a big-endian string field left-justified in 512 bits.
    function automatic logic [31:0] pick_word(input logic [511:0] field,
                                              input logic [3:0]   widx);
        pick_word = field[511 - 32*widx -: 32];
    endfunction

    // `old` with the bytes of `nw` that `be` enables (be[i] = bits 8i+7:8i).
    function automatic logic [31:0] merge_bytes(input logic [31:0] old,
                                                input logic [31:0] nw,
                                                input logic [3:0]  be);
        for (int i = 0; i < 4; i++)
            merge_bytes[8*i +: 8] = be[i] ? nw[8*i +: 8] : old[8*i +: 8];
    endfunction

    // A write is refused with 0x43 on these kinds.
    function automatic logic read_only(input kind_t k);
        read_only = (k == K_RO) || (k == K_STR) || (k == K_ZERO) || (k == K_ROM);
    endfunction

    // Row `r` takes the value `v` (src/regmap/cxp_regmap.yaml min / max /
    // allowed / multiple; the XML offers the same ranges).
    function automatic logic value_ok(input row_t r, input logic [31:0] v);
        unique case (r)
            R_XML_MANIFEST_SELECTOR:     value_ok = (v == 32'd0);            // one manifest
            R_CONNECTION_RESET:          value_ok = (v <= 32'd1);            // §10.3.28: bit 0
            R_STREAM_PACKET_SIZE_MAX:    value_ok = (v[1:0] == 2'd0);        // §10.3.32: words
            R_CONNECTION_CONFIG:         value_ok = (v == CONNECTION_CONFIG_DEFAULT_VALUE);
            R_TEST_MODE:                 value_ok = (v <= 32'd1);            // §10.3.35
            R_TEST_ERROR_COUNT_SELECTOR: value_ok = (v < 32'(p_NUM_LINKS));  // §10.3.36
            R_WIDTH, R_HEIGHT:           value_ok = (v inside {[32'd1:32'd4096]});
            R_PIXEL_FORMAT:              value_ok = (cxp_pkg::pfnc_to_pixelf(v) != 16'h0); // PFNC
            R_ACQUISITION_MODE:          value_ok = (v == 32'd0);            // Continuous
            R_TAP_GEOMETRY:              value_ok = (v == 32'd0);            // Geometry_1X_1Y
            R_IMAGE1_STREAM_ID:          value_ok = (v <= 32'h00FF);         // Table 38
            R_FRAME_COUNT, R_SOURCE_TAG: value_ok = (v <= 32'hFFFF);
            R_TEST_PATTERN:              value_ok = (v <= 32'd3);
            R_OFFSET_X, R_OFFSET_Y:      value_ok = (v <= 32'd4095);
            R_STREAM_FLAGS:              value_ok = (v <= 32'h00FF);
            R_TPG_RUN:                   value_ok = (v <= 32'd1);
            default:                     value_ok = 1'b1;
        endcase
    endfunction

    // ------ Register map ------

    // One row per register or byte range, in scan order (see row_t).
    localparam desc_t ROWS [R_N] = '{
        //  addr                                 len   kind     rst / value
        // Support group (§10.3.5-12)
        '{32'(STANDARD_ADDR),                    4,    K_RO,    STANDARD_VALUE},
        '{32'(REVISION_ADDR),                    4,    K_RO,    REVISION_VALUE},
        '{32'(XML_MANIFEST_SIZE_ADDR),           4,    K_RO,    XML_MANIFEST_SIZE_VALUE},
        '{32'(XML_MANIFEST_SELECTOR_ADDR),       4,    K_RW,    XML_MANIFEST_SELECTOR_RESET},
        '{32'(XML_VERSION_ADDR),                 4,    K_RO,    XML_VERSION_VALUE},
        '{32'(XML_SCHEMA_VERSION_ADDR),          4,    K_RO,    XML_SCHEMA_VERSION_VALUE},
        '{32'(XML_URL_ADDRESS_ADDR),             4,    K_RO,    XML_URL_ADDRESS_VALUE},
        '{32'(IIDC2_ADDRESS_ADDR),               4,    K_RO,    IIDC2_ADDRESS_VALUE},
        // GenICam strings (§10.3.13-18)
        '{32'(DEVICE_VENDOR_NAME_ADDR),          32,   K_STR,   32'h0},
        '{32'(DEVICE_MODEL_NAME_ADDR),           32,   K_STR,   32'h0},
        '{32'(DEVICE_MANUFACTURER_INFO_ADDR),    48,   K_STR,   32'h0},
        '{32'(DEVICE_VERSION_ADDR),              32,   K_STR,   32'h0},
        '{32'(DEVICE_SERIAL_NUMBER_ADDR),        16,   K_STR,   32'h0},
        '{32'(DEVICE_USER_ID_ADDR),              16,   K_NVSTR, 32'h0},
        // Use-case feature addresses (§10.3.19-27): each slot reads its alias
        '{32'(WIDTH_SLOT),                       4,    K_RO,    WIDTH_ALIAS},
        '{32'(HEIGHT_SLOT),                      4,    K_RO,    HEIGHT_ALIAS},
        '{32'(ACQUISITION_MODE_SLOT),            4,    K_RO,    ACQUISITION_MODE_ALIAS},
        '{32'(ACQUISITION_START_SLOT),           4,    K_RO,    ACQUISITION_START_ALIAS},
        '{32'(ACQUISITION_STOP_SLOT),            4,    K_RO,    ACQUISITION_STOP_ALIAS},
        '{32'(PIXEL_FORMAT_SLOT),                4,    K_RO,    PIXEL_FORMAT_ALIAS},
        '{32'(TAP_GEOMETRY_SLOT),                4,    K_RO,    TAP_GEOMETRY_ALIAS},
        '{32'(IMAGE1_STREAM_ID_SLOT),            4,    K_RO,    IMAGE1_STREAM_ID_ALIAS},
        '{32'(IMAGE_N_STREAM_ID_ADDRESS_ADDR),   IMAGE_N_SID_BYTES, K_ZERO, 32'h0},
        // CXP group (§10.3.28-41)
        '{32'(CONNECTION_RESET_ADDR),            4,    K_CRST,  32'h0},
        '{32'(DEVICE_CONNECTION_ID_ADDR),        4,    K_RO,    DEVICE_CONNECTION_ID_VALUE},
        '{32'(MASTER_HOST_CONNECTION_ID_ADDR),   4,    K_RW,    MASTER_HOST_CONNECTION_ID_RESET},
        '{32'(CONTROL_PACKET_SIZE_MAX_ADDR),     4,    K_RO,    CONTROL_PACKET_SIZE_MAX_VALUE},
        '{32'(STREAM_PACKET_SIZE_MAX_ADDR),      4,    K_RW,    STREAM_PACKET_SIZE_MAX_RESET},
        '{32'(CONNECTION_CONFIG_ADDR),           4,    K_RW,    CONNECTION_CONFIG_RESET},
        '{32'(CONNECTION_CONFIG_DEFAULT_ADDR),   4,    K_RO,    CONNECTION_CONFIG_DEFAULT_VALUE},
        '{32'(TEST_MODE_ADDR),                   4,    K_RW,    TEST_MODE_RESET},
        '{32'(TEST_ERROR_COUNT_SELECTOR_ADDR),   4,    K_RW,    TEST_ERROR_COUNT_SELECTOR_RESET},
        '{32'(TEST_ERROR_COUNT_ADDR),            4,    K_CNT,   32'h0},
        '{32'(TEST_PACKET_COUNT_TX_ADDR),        8,    K_CNT,   32'h0},
        '{32'(TEST_PACKET_COUNT_RX_ADDR),        8,    K_CNT,   32'h0},
        '{32'(ELECTRICAL_COMPLIANCE_TEST_ADDR),  4,    K_RW,    ELECTRICAL_COMPLIANCE_TEST_RESET},
        '{32'(HS_UPCONNECTION_ADDR),             4,    K_RO,    HS_UPCONNECTION_VALUE},
        // XmlUrl string (§10.3.11)
        '{32'(XML_URL_ADDR),                     64,   K_STR,   32'h0},
        // Manufacturer window: the window first, then the words inside it
        '{MFR_BASE,                              4 * MFR_WORDS, K_VOID, 32'h0},
        '{WIDTH_ALIAS,                           4,    K_RW,    WIDTH_RESET},
        '{HEIGHT_ALIAS,                          4,    K_RW,    HEIGHT_RESET},
        '{PIXEL_FORMAT_ALIAS,                    4,    K_RW,    p_PIXEL_FORMAT_RESET},
        '{MFR_RESERVED1_ADDR,                    4,    K_RW,    32'h0},
        '{ACQUISITION_START_ALIAS,               4,    K_WO,    ACQUISITION_START_RESET},
        '{ACQUISITION_STOP_ALIAS,                4,    K_WO,    ACQUISITION_STOP_RESET},
        '{FRAME_COUNT_ADDR,                      4,    K_RW,    32'h0},
        '{TEST_PATTERN_ADDR,                     4,    K_RW,    32'h0},
        '{OFFSET_X_ADDR,                         4,    K_RW,    32'h0},
        '{OFFSET_Y_ADDR,                         4,    K_RW,    32'h0},
        '{TAP_GEOMETRY_ALIAS,                    4,    K_RW,    TAP_GEOMETRY_RESET},
        '{IMAGE1_STREAM_ID_ALIAS,                4,    K_RW,    p_IMAGE1_STREAM_ID_RESET},
        '{SOURCE_TAG_ADDR,                       4,    K_RW,    32'h0},
        '{STREAM_FLAGS_ADDR,                     4,    K_RW,    32'h0},
        '{TPG_RUN_ADDR,                          4,    K_RW,    32'h1},    // free-running
        '{MFR_RESERVED2_ADDR,                    4,    K_RW,    32'h0},
        '{ACQUISITION_MODE_ALIAS,                4,    K_RW,    ACQUISITION_MODE_RESET},
        // GenICam XML ROM
        '{XML_BLOB_ADDR,                         XML_BLOB_BYTES, K_ROM, 32'h0}
    };

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    if (p_CONN_RESET_TIMEOUT <= p_LINK_RESET_CLEAR_CYCLES) begin : g_chk_crst
        $error("cxp_ctrl_bootstrap_regs: p_CONN_RESET_TIMEOUT (=%0d) must exceed %s (=%0d)",
               p_CONN_RESET_TIMEOUT, "p_LINK_RESET_CLEAR_CYCLES", p_LINK_RESET_CLEAR_CYCLES);
    end

    if (p_NUM_LINKS < 1) begin : g_chk_links
        $error("cxp_ctrl_bootstrap_regs: p_NUM_LINKS (=%0d) must be >= 1", p_NUM_LINKS);
    end

    // acc.off serves a 16-word string with its low 4 bits.
    if (XML_BLOB_WORDS < 16) begin : g_chk_rom
        $error("cxp_ctrl_bootstrap_regs: XML_BLOB_WORDS (=%0d) must be >= 16", XML_BLOB_WORDS);
    end

    //=======================================================================
    // Signals
    //=======================================================================

    logic [31:0]        xml_rom [XML_BLOB_WORDS]; // GenICam file

    acc_t               acc;                // the access decoded this cycle
    logic [7:0]         err_c;              // its Table 22 code
    logic               wr_ok;              // the write is accepted
    logic [31:0]        wval;               // the word a write leaves (wstrb_i merged)
    logic               crst_write;         // host writes 1 to ConnectionReset
    logic               crst_apply;         // §10.3.28 takes effect
    logic [NL_W-1:0]    link_sel;           // TestErrorCountSelector link
    logic [63:0]        cnt_sel;            // counter of a K_CNT row, high word first
    logic [511:0]       str_sel;            // text of a K_STR row
    logic [31:0]        rdata_c;            // read data this cycle

    logic [31:0]        reg_q [R_N];        // one word per row; registers live here
    logic [127:0]       user_id_q;          // DeviceUserID
    logic [LRST_CW-1:0] crst_timer_q;       // ConnectionReset bit, minimum lifetime
    logic [TMO_W-1:0]   crst_tmo_q;         // ... cycles left before it clears anyway
    logic               crst_pulse_q;       // host wrote 1 to ConnectionReset
    logic               crst_apply_q;       // §10.3.28 applied last cycle
    wr_t                wr_q;               // the write accepted last cycle
    logic [31:0]        rdata_q;            // rdata_o
    logic               ready_q;            // ready_o
    logic [7:0]         err_q;              // err_o

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign rdata_o = rdata_q;
    assign ready_o = ready_q;
    assign err_o   = err_q;

    assign wr_ok      = we_i && (err_c == 8'h00);
    assign crst_write = wr_ok && (acc.row == R_CONNECTION_RESET) && wval[0];
    assign crst_apply = crst_write || conn_reset_req_i;
    assign link_sel   = reg_q[R_TEST_ERROR_COUNT_SELECTOR][NL_W-1:0];

    // Exported registers and write pulses
    assign ctl_connection_reset_pulse_o    = crst_pulse_q;
    assign ctl_connection_reset_active_o   = reg_q[R_CONNECTION_RESET][0];
    assign ctl_connection_config_wr_o      = wr_q.valid && (wr_q.row == R_CONNECTION_CONFIG);
    assign ctl_acquisition_start_wr_o      = wr_q.valid && (wr_q.row == R_ACQUISITION_START);
    assign ctl_acquisition_stop_wr_o       = wr_q.valid && (wr_q.row == R_ACQUISITION_STOP);
    assign ctl_device_connection_id_o      = DEVICE_CONNECTION_ID_VALUE;
    assign ctl_master_host_connection_id_o = reg_q[R_MASTER_HOST_CONNECTION_ID];
    assign ctl_stream_pkt_dsize_o          = reg_q[R_STREAM_PACKET_SIZE_MAX];
    assign ctl_connection_config_o         = reg_q[R_CONNECTION_CONFIG];
    assign ctl_test_mode_o                 = reg_q[R_TEST_MODE][0];
    assign ctl_tpg_width_o                 = reg_q[R_WIDTH];
    assign ctl_tpg_height_o                = reg_q[R_HEIGHT];
    assign ctl_acquisition_mode_o          = reg_q[R_ACQUISITION_MODE];
    assign ctl_acquisition_start_o         = reg_q[R_ACQUISITION_START];
    assign ctl_acquisition_stop_o          = reg_q[R_ACQUISITION_STOP];
    assign ctl_pixel_format_o              = reg_q[R_PIXEL_FORMAT];
    assign ctl_tap_geometry_o              = reg_q[R_TAP_GEOMETRY];
    assign ctl_image1_stream_id_o          = reg_q[R_IMAGE1_STREAM_ID];
    assign ctl_frame_count_o               = reg_q[R_FRAME_COUNT];
    assign ctl_test_pattern_o              = reg_q[R_TEST_PATTERN];
    assign ctl_offset_x_o                  = reg_q[R_OFFSET_X];
    assign ctl_offset_y_o                  = reg_q[R_OFFSET_Y];
    assign ctl_source_tag_o                = reg_q[R_SOURCE_TAG];
    assign ctl_stream_flags_o              = reg_q[R_STREAM_FLAGS];
    assign ctl_tpg_run_o                   = reg_q[R_TPG_RUN];
    assign ctl_test_err_count_clr_o        = crst_apply_q ||
                                             (wr_q.zero && wr_q.row == R_TEST_ERROR_COUNT);
    assign ctl_test_pkt_tx_clr_o           = crst_apply_q ||
                                             (wr_q.zero && wr_q.row == R_TEST_PACKET_COUNT_TX);
    assign ctl_test_pkt_rx_clr_o           = crst_apply_q ||
                                             (wr_q.zero && wr_q.row == R_TEST_PACKET_COUNT_RX);
    assign ctl_device_user_id_o            = user_id_q;

    //=======================================================================
    // GenICam XML ROM
    //=======================================================================

    initial $readmemh(p_XML_BLOB_MEM, xml_rom);

    //=======================================================================
    // Address decode: the last row of ROWS that covers addr_i answers.
    //=======================================================================

    always_comb begin
        acc = '{kind: K_NONE, row: row_t'(0), off: '0, val_ok: 1'b1};
        for (int i = 0; i < R_N; i++) begin
            if (addr_i >= ROWS[i].addr && addr_i < ROWS[i].addr + ROWS[i].len) begin
                acc.kind = ROWS[i].kind;
                acc.row  = row_t'(i);
                acc.off  = OFF_W'((addr_i - ROWS[i].addr) >> 2);
            end
        end
        acc.val_ok = value_ok(acc.row, merge_bytes(reg_q[acc.row], wdata_i, wstrb_i));
    end

    // A write of B bytes (Table 21) enables only those bytes; the rest of
    // the word keeps the register's value.
    assign wval = merge_bytes(reg_q[acc.row], wdata_i, wstrb_i);

    // Table 22 code of the access; a refused write changes nothing.
    always_comb begin
        err_c = 8'h00;
        if (addr_i[1:0] != 2'b00 || acc.kind == K_NONE) err_c = cxp_pkg::ACK_ERR_BAD_ADDR;
        else if (we_i && read_only(acc.kind))           err_c = cxp_pkg::ACK_ERR_RO_WRITE;
        else if (re_i && acc.kind == K_WO)              err_c = cxp_pkg::ACK_ERR_WO_READ;
        else if (we_i && !acc.val_ok)                   err_c = cxp_pkg::ACK_ERR_BAD_DATA;
    end

    //=======================================================================
    // Row sources: the live counter of a K_CNT row, the text of a K_STR row.
    //=======================================================================

    always_comb begin
        unique case (acc.row)
            R_TEST_ERROR_COUNT:     cnt_sel = {test_err_count_i[link_sel], 32'h0};
            R_TEST_PACKET_COUNT_TX: cnt_sel = test_pkt_count_tx_i[link_sel];
            R_TEST_PACKET_COUNT_RX: cnt_sel = test_pkt_count_rx_i[link_sel];
            default:                cnt_sel = 64'h0;
        endcase
    end

    always_comb begin
        unique case (acc.row)
            R_XML_URL:                  str_sel = XML_URL_TXT;
            R_DEVICE_VENDOR_NAME:       str_sel = DEVICE_VENDOR_NAME_TXT;
            R_DEVICE_MODEL_NAME:        str_sel = DEVICE_MODEL_NAME_TXT;
            R_DEVICE_MANUFACTURER_INFO: str_sel = DEVICE_MANUFACTURER_INFO_TXT;
            R_DEVICE_VERSION:           str_sel = DEVICE_VERSION_TXT;
            R_DEVICE_SERIAL_NUMBER:     str_sel = DEVICE_SERIAL_NUMBER_TXT;
            default:                    str_sel = '0;
        endcase
    end

    //=======================================================================
    // Read data by kind
    //=======================================================================

    always_comb begin
        unique case (acc.kind)
            K_RO:                rdata_c = ROWS[acc.row].rst;
            K_RW, K_WO, K_CRST:  rdata_c = reg_q[acc.row];
            K_CNT:               rdata_c = acc.off[0] ? cnt_sel[31:0] : cnt_sel[63:32];
            K_STR:               rdata_c = pick_word(str_sel, acc.off[3:0]);
            K_NVSTR:             rdata_c = pick_word({user_id_q, 384'h0}, {2'b00, acc.off[1:0]});
            K_ROM:               rdata_c = xml_rom[acc.off];
            default:             rdata_c = 32'h0;
        endcase
    end

    //=======================================================================
    // Sequential: the answer, writes, ConnectionReset
    //=======================================================================

    always_ff @(posedge sys_clk or negedge sys_rst_n) begin
        if (!sys_rst_n) begin
            for (int i = 0; i < R_N; i++) reg_q[i] <= ROWS[i].rst;
            user_id_q    <= device_user_id_nv_i;
            crst_timer_q <= '0;
            crst_tmo_q   <= '0;
            crst_pulse_q <= 1'b0;
            crst_apply_q <= 1'b0;
            wr_q         <= '0;
            rdata_q      <= 32'h0;
            ready_q      <= 1'b0;
            err_q        <= 8'h00;
        end else begin
            //---- The answer, one cycle after the strobe -----------------
            ready_q <= we_i | re_i;
            err_q   <= (we_i | re_i) ? err_c : 8'h00;
            if (re_i) rdata_q <= rdata_c;

            //---- Host write ---------------------------------------------
            wr_q         <= '{valid: wr_ok, row: acc.row, zero: wr_ok && (wval == 32'h0)};
            crst_pulse_q <= crst_write;
            crst_apply_q <= crst_apply;
            if (wr_ok) begin
                unique case (acc.kind)
                    K_RW, K_WO: reg_q[acc.row] <= wval;
                    // DeviceUserID: a write of B bytes changes only those B bytes;
                    // wstrb_i masks the zero padding of the command's last word.
                    K_NVSTR: begin
                        for (int w = 0; w < 4; w++)
                            if (acc.off[1:0] == 2'(w))
                                user_id_q[127 - 32*w -: 32] <=
                                    merge_bytes(user_id_q[127 - 32*w -: 32], wdata_i, wstrb_i);
                    end
                    default: ;
                endcase
            end

            //---- ConnectionReset bit: set by a request, clears once the ---
            //     other domains have seen it (or on the timeout)
            if (crst_apply) begin
                reg_q[R_CONNECTION_RESET] <= 32'h1;
                crst_timer_q              <= LRST_CW'(p_LINK_RESET_CLEAR_CYCLES);
                crst_tmo_q                <= TMO_W'(p_CONN_RESET_TIMEOUT);
            end else if (reg_q[R_CONNECTION_RESET][0]) begin
                if (crst_timer_q != '0) crst_timer_q <= crst_timer_q - 1'b1;
                if (crst_tmo_q != '0)   crst_tmo_q   <= crst_tmo_q - 1'b1;
                if ((crst_timer_q == '0 && conn_reset_done_i) || crst_tmo_q == '0)
                    reg_q[R_CONNECTION_RESET] <= 32'h0;
            end

            //---- §10.3.28 ConnectionReset values ------------------------
            if (crst_apply) begin
                reg_q[R_XML_MANIFEST_SELECTOR]      <= 32'h0;
                reg_q[R_MASTER_HOST_CONNECTION_ID]  <= 32'h0;
                reg_q[R_STREAM_PACKET_SIZE_MAX]     <= 32'h0;
                reg_q[R_CONNECTION_CONFIG]          <= CONNECTION_CONFIG_CONN_RESET;
                reg_q[R_TEST_MODE]                  <= 32'h0;
                reg_q[R_TEST_ERROR_COUNT_SELECTOR]  <= 32'h0;
                reg_q[R_ELECTRICAL_COMPLIANCE_TEST] <= 32'h0;
            end
        end
    end

endmodule

`default_nettype wire
