/*
================================================================================
  cxp_device_top
  CoaXPress 1.1.1 (CXP-001-2015) — complete device IP: link, datapath and
  register file.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-19

    Description:
      The IP boundary an integrator instantiates: serial uplink in,
      32-bit downlink word out, pixels in.  cxp_interface_top plus the
      register file (cxp_ctrl_bootstrap_regs, generated from
      src/regmap/cxp_regmap.yaml) on its register-bus port, and every
      register side-effect wired to the datapath:

        Width / Height / PixelFormat / TestPattern  -> TPG and packer
        OffsetX / OffsetY / SourceTag /
        TapGeometry / StreamFlags                   -> TPG image header
        Image1StreamID                              -> StreamID of TPG
                                                       images (powers up
                                                       at p_IMAGE1_STREAM_ID_
                                                       RESET)
        AcquisitionStart / AcquisitionStop          -> cxp_app_acq_ctrl: which
                                                       TPG images start
        TpgRun / FrameCount                         -> acquisition length
        TestMode                                    -> connection-test TX
        ConnectionReset (0x4000 write)              -> applied in the register
                                                       file; its level
                                                       restarts the
                                                       PacketTags, holds the
                                                       device trigger and
                                                       clears
                                                       TestPacketCountTx
        StreamPacketSizeMax                         -> stream packet size
        TestErrorCount / TestPacketCount*           <- live counters;
                                                       writing 0 clears them

      StreamPacketSizeMax is the size of the whole packet in bytes
      (§8.5.2, §10.3.32): the payload is that / 4 less the 8 framing
      words of Table 19 (at least 1 word; the chopper may send less).
      While it reads 0 (at power-up and after a ConnectionReset) no image
      starts and no stream packet is sent (Table 44).

      Acquisition (§11.2.1.4/5): AcquisitionStart starts test-pattern
      images and AcquisitionStop ends them after the current image; a
      ConnectionReset stops the acquisition as well.  With TpgRun = 1
      images follow until AcquisitionStop; with TpgRun = 0 the
      acquisition ends by itself after FrameCount images (0 counts as 1).
      cfg_run_i keeps the generator running without an acquisition.
      PixelFormat holds a GenICam PFNC value (§11.2.1.6) and powers up at
      p_PIXEL_FORMAT_RESET; the datapath and the image header get the
      Table 25 PixelF code it maps to (cxp_pkg::pfnc_to_pixelf).

      Sensor pixels are p_PIX_W-bit samples, LSB-justified on s_pix_data_i;
      they are MSB-aligned into the PixelFormat in use (§9.4.2, Figure
      32).  s_meta_i.pixfmt is not used: the PixelFormat register always
      selects the format.

      The user window's APB port carries PSTRB (APB4): a write that ends
      inside a word enables only its bytes.  A transfer the control plane
      gives up (0xFF, command timeout) still runs until PREADY.

      XML ROM: the register file loads the GenICam XML image from
      p_XML_BLOB_MEM ($readmemh at elaboration).  The default is the bare
      file name, which resolves against the simulator's run directory;
      builds pass the absolute path of src/rtl/gen/cxp_camera_xml.mem
      (src/regmap/regmap.mk).

      User window: with p_USER_SIZE != 0, control accesses to
      [p_USER_BASE, p_USER_BASE + p_USER_SIZE) go to the APB3 master port
      (m_apb_*, rx_clk) instead of the register file.  A slave that stalls
      gets one Wait acknowledgment after 100 ms and a 0x40 after 900 ms of
      the command, measured in p_RX_CLK_KHZ cycles.  With p_USER_SIZE = 0
      the port is idle and its inputs are ignored.

      Clocking: the register file runs on rx_clk beside the control
      plane.  cxp_interface_top takes its configuration, the
      ConnectionReset level and the counter clears on rx_clk and returns
      the ConnectionReset echo and the counters on rx_clk; with
      p_ASYNC_CLOCKS = 1 it synchronises every crossing (see
      cxp_interface_top).  The cfg_*_i inputs are quasi-static levels
      taken on rx_clk.

      Reset: the three reset inputs are one request (cxp_cdc_reset).
      Any of them asserted resets the whole device; the domains are
      released in the order rx, tx, app, each synchronised to its clock.
      A reset of one domain alone is therefore a reset of the device —
      the crossings never see one side reset without the other.

    Versions:
        2026-09-19 - 0.1:   - Init
        2026-09-22 - 0.2:   - Image1StreamID drives the TPG StreamID;
                              byte enables into the register file;
                              ConnectionConfig write resets the PacketTags
        2026-09-25 - 0.3:   - cxp_cdc_reset: one reset for the three
                              domains; ConnectionReset owned by the
                              register file
        2026-09-26 - 0.4:   - User window: p_USER_BASE / p_USER_SIZE and
                              the APB3 master port
        2026-09-26 - 0.5:   - p_TRIG_ACK_TIMEOUT: the device trigger waits
                              for the host's I/O acknowledgment
        2026-09-27 - 0.6:   - The user window may not overlap the register
                              file (elaboration check)
        2026-09-27 - 0.7:   - s_meta_i passed through whole
        2026-09-27 - 0.8:   - TapGeometry and StreamFlags reach the TPG header;
                              p_TPG_PIXFMT / p_TPG_STREAMID renamed to the
                              power-on values they are; p_TPG_TAPG / p_TPG_FLAGS
                              removed
        2026-09-27 - 0.9:   - cfg_dsizeP_i removed: it was used only while
                              StreamPacketSizeMax read 0, when nothing streams
        2026-09-30 - 1.0:   - p_XML_BLOB_MEM: the XML ROM image path
        2026-10-04 - 1.1:   - PixelFormat holds PFNC values; m_apb_pstrb_o;
                              sensor samples MSB-aligned

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_device_top #(
    // Test-pattern generator: maximum geometry (every header field comes
    // from the registers)
    parameter int          p_TPG_X_SIZE    = 64,                // TPG max line width (px)
    parameter int          p_TPG_Y_SIZE    = 32,                // TPG max frame height (lines)

    // Power-on values of two registers
    parameter logic [31:0] p_PIXEL_FORMAT_RESET     = cxp_pkg::PFNC_MONO8,   // PixelFormat (PFNC)
    parameter logic [7:0]  p_IMAGE1_STREAM_ID_RESET = 8'h01,                 // Image1StreamID

    parameter int p_FIFO_DEPTH     = 1024,                      // stream CDC FIFO depth (words)
    parameter int p_PIX_W          = 16,                        // sensor pixel width
    parameter int p_CTRL_BUF_DEPTH = 64,                        // ctrl rd/wr buffer (dwords)
    parameter int p_OS_RATIO       = 16,                        // LS uplink oversampling ratio
    parameter int p_SAMP_LOCK_HITS = cxp_pkg::RX_LOCK_HITS_DEFAULT, // K28.5 hits to lock
    parameter int p_RX_LOSS_WORDS = cxp_pkg::RX_LOSS_WORDS_DEFAULT, // words w/o IDLE: link lost
    parameter int p_TRIG_ACK_TIMEOUT = cxp_pkg::TRIG_ACK_TIMEOUT, // tx cycles to wait for I/O ack
    parameter int p_LINK_RESET_CLEAR_CYCLES = 8,                // ConnectionReset bit, min cycles
    parameter int p_RX_CLK_KHZ     = 20_833 * p_OS_RATIO,       // rx_clk (kHz), ms timeouts
    parameter logic [31:0] p_USER_BASE = 32'h0002_0000,         // user window base
    parameter logic [31:0] p_USER_SIZE = 32'h0000_0000,         // user window bytes, 0 = off
    parameter string p_XML_BLOB_MEM = cxp_regmap_pkg::XML_BLOB_MEM, // XML ROM image ($readmemh)
    parameter bit p_ASYNC_CLOCKS   = 1'b0                       // unrelated app / tx / rx clocks
) (
    // Clocks / resets
    input  wire  logic        app_clk,                          // application / pixel clock
    input  wire  logic        app_rst_n,                        // app_clk async reset
    input  wire  logic        tx_clk,                           // downlink word clock
    input  wire  logic        tx_rst_n,                         // tx_clk async reset
    input  wire  logic        rx_clk,                           // uplink oversample clock
    input  wire  logic        rx_rst_n,                         // rx_clk async reset

    // Configuration not held in registers
    input  wire  logic        cfg_use_tpg_i,                    // 1 = TPG, 0 = sensor pixels
    input  wire  logic        cfg_run_i,                        // TPG free-run, no acquisition
    input  wire  logic        cfg_arbitrary_i,                  // arbitrary header / markers
    input  wire  logic        cfg_trig_polarity_i,              // trigger 0 high, 1 low active
    input  wire  logic        from_extension_link_i,            // §5.1 strap: block writes

    // Local trigger I/O (§8.3.2)
    input  wire  logic        trig_i,                           // device -> host trigger pin
    output logic              trig_o,                           // host -> device trigger
    output logic              trig_glitch_pulse_o,              // corrupted trigger seen

    // Sensor single-pixel input (cfg_use_tpg_i = 0)
    input  wire  logic [15:0] s_pix_data_i,                     // pixel, LSB-justified
    input  wire  logic        s_pix_valid_i,                    // pixel valid
    input  wire  logic        s_pix_sof_i,                      // start of frame
    input  wire  logic        s_pix_eol_i,                      // end of line
    input  wire  logic        s_pix_eof_i,                      // end of frame
    output logic              s_pix_ready_o,                    // ingress ready
    input  wire  cxp_pkg::cxp_meta_t s_meta_i,                  // sensor frame metadata

    // Serial uplink / parallel downlink
    input  wire  logic        rx_serial_i,                      // LS uplink bit
    output logic [31:0]       cxp_if_data_o,                    // word to 8B/10B (P0 = [7:0])
    output logic [3:0]        cxp_if_kmask_o,                   // per-lane K flag

    // User window: APB3 master + PSTRB (APB4) on rx_clk (idle when p_USER_SIZE = 0)
    output logic              m_apb_psel_o,                     // select
    output logic              m_apb_penable_o,                  // access phase
    output logic              m_apb_pwrite_o,                   // 1 = write
    output logic [31:0]       m_apb_paddr_o,                    // byte address
    output logic [31:0]       m_apb_pwdata_o,                   // write data
    output logic [3:0]        m_apb_pstrb_o,                    // byte enables, [n]: pwdata[8n+:8]
    input  wire  logic [31:0] m_apb_prdata_i,                   // read data
    input  wire  logic        m_apb_pready_i,                   // slave ready
    input  wire  logic        m_apb_pslverr_i,                  // slave error (answers 0x40)

    // Register file
    input  wire  logic [127:0] device_user_id_nv_i,             // DeviceUserID power-on value
    output logic [127:0]      device_user_id_o,                 // DeviceUserID (to NV storage)

    // Status
    output logic              rx_lock_o,                        // sampler symbol lock
    output logic              aligned_o,                        // link up (IDLE monitor)
    output logic              link_detected_o,                  // §8.2 link detected
    output logic              link_reset_active_o,              // ConnectionReset in progress
    output logic              rate_to_discovery_o               // SerDes to discovery rate
);

    import cxp_pkg::*;

    //=======================================================================
    // Signals
    //=======================================================================

    // Register bus between the control plane and the register file
    logic        reg_req, reg_wr;
    logic [31:0] reg_addr, reg_wdata;
    logic [3:0]  reg_wstrb;            // byte enables of a write
    logic        reg_we, reg_re;
    logic [31:0] reg_rdata;
    logic        reg_ready;
    logic [7:0]  reg_err;              // Table 22 code of the access

    // Register values into the datapath
    logic [31:0] reg_width, reg_height, reg_pixfmt, reg_testpat;
    logic [31:0] reg_stream_id;        // Image1StreamID
    logic [31:0] reg_offset_x;         // OffsetX
    logic [31:0] reg_offset_y;         // OffsetY
    logic [31:0] reg_source_tag;       // SourceTag preset
    logic [31:0] reg_tap_geometry;     // TapGeometry
    logic [31:0] reg_stream_flags;     // StreamFlags
    logic [31:0] reg_stream_pkt_size;
    logic        reg_test_mode;
    logic        reg_conn_reset;
    logic        reg_conn_cfg_wr;      // ConnectionConfig written
    logic        reg_clr_test_err;          // TestErrorCount written 0
    logic        reg_clr_test_pkt_tx;       // TestPacketCountTx written 0
    logic        reg_clr_test_pkt_rx;       // TestPacketCountRx written 0
    logic        reg_acq_start;             // AcquisitionStart written
    logic        reg_acq_stop;              // AcquisitionStop written
    logic [31:0] reg_frame_count;           // FrameCount: images when TpgRun = 0
    logic [31:0] reg_tpg_run;               // TpgRun: images until AcquisitionStop

    // Domain resets from cxp_cdc_reset
    logic        rst_req_n;            // any reset input asserted
    logic        rx_rst_s_n;           // rx_clk reset, released first
    logic        tx_rst_s_n;           // tx_clk reset, after rx
    logic        app_rst_s_n;          // app_clk reset, after tx

    // ConnectionReset between the register file and the link
    logic        reg_conn_reset_active;     // ConnectionReset bit
    logic        conn_reset_done;           // echoed by the tx domain

    // Connection-test counters
    logic [31:0] lt_err_count      [1];
    logic [63:0] lt_pkt_count_tx   [1];
    logic [63:0] lt_pkt_count_rx   [1];

    logic [15:0] dsizeP;
    cxp_cfg_t    cfg;                  // configuration into the datapath
    cxp_status_t status;               // link status and counters
    logic [29:0] spsm_words;        // StreamPacketSizeMax / 4: whole packet

    // The smallest stream packet (Table 19): SOP, type, 4 header words,
    // one data word, CRC, EOP.  Below it no packet fits and the stream is
    // held, as at 0 (Table 44).
    localparam logic [31:0] SPSM_MIN = 32'd36;

    // The user window [p_USER_BASE, p_USER_BASE + p_USER_SIZE) overlaps the
    // byte range [lo, lo + len) of the register file.
    function automatic bit user_hits(input logic [31:0] lo, input logic [31:0] len);
        user_hits = (p_USER_SIZE != 32'h0)
                  && (33'(p_USER_BASE) < 33'(lo) + 33'(len))
                  && (33'(lo) < 33'(p_USER_BASE) + 33'(p_USER_SIZE));
    endfunction

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    // The user window may not hide a register the host must reach: the
    // bootstrap block (Table 45), the URL string, the manufacturer window
    // or the XML file; nor may it wrap past the top of the address space.
    if (33'(p_USER_BASE) + 33'(p_USER_SIZE) > 33'h1_0000_0000
        || user_hits(32'h0, cxp_regmap_pkg::BOOTSTRAP_END + 32'd1)
        || user_hits(32'(cxp_regmap_pkg::XML_URL_ADDR), 32'd64)
        || user_hits(cxp_regmap_pkg::MFR_BASE, 32'(4 * cxp_regmap_pkg::MFR_WORDS))
        || user_hits(cxp_regmap_pkg::XML_BLOB_ADDR, cxp_regmap_pkg::XML_BLOB_BYTES))
    begin : g_chk_user_window
        $error("cxp_device_top: user window 0x%08h + 0x%08h overlaps the register file",
               p_USER_BASE, p_USER_SIZE);
    end

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    // Payload words = packet words - SOP - 5 header words - CRC - EOP.
    assign spsm_words = reg_stream_pkt_size[31:2];
    // While StreamPacketSizeMax is 0 nothing streams (Table 44); the
    // chopper then cuts at its maximum (0).
    assign dsizeP = (reg_stream_pkt_size == 32'h0)     ? 16'd0
                  : (spsm_words <= 30'd9)              ? 16'd1
                  : (spsm_words >= 30'd65543)          ? 16'hFFFF
                  : 16'(spsm_words - 30'd8);

    assign cfg = '{use_tpg:       cfg_use_tpg_i,
                   run:           cfg_run_i,
                   acq_mode:      reg_tpg_run[0] ? 2'd0 : 2'd2,
                   acq_frames:    reg_frame_count[15:0],
                   stream_en:     reg_stream_pkt_size >= SPSM_MIN,
                   xsize:         reg_width[15:0],
                   ysize:         reg_height[15:0],
                   pixfmt:        pfnc_to_pixelf(reg_pixfmt),
                   streamid:      reg_stream_id[7:0],
                   xoffs:         reg_offset_x[15:0],
                   yoffs:         reg_offset_y[15:0],
                   srctag:        reg_source_tag[15:0],
                   tapg:          reg_tap_geometry[15:0],
                   flags:         reg_stream_flags[7:0],
                   testpat:       reg_testpat[1:0],
                   arbitrary:     cfg_arbitrary_i,
                   dsizeP:        dsizeP,
                   trig_polarity: cfg_trig_polarity_i,
                   test_mode:     reg_test_mode,
                   ext_link:      from_extension_link_i};

    assign rx_lock_o          = status.rx_lock;
    assign aligned_o          = status.aligned;
    assign link_detected_o    = status.link_detected;
    assign lt_err_count[0]    = status.lt_err_count;
    assign lt_pkt_count_tx[0] = status.lt_pkt_count_tx;
    assign lt_pkt_count_rx[0] = status.lt_pkt_count_rx;

    assign rst_req_n = app_rst_n & tx_rst_n & rx_rst_n;

    assign link_reset_active_o = reg_conn_reset_active;
    assign rate_to_discovery_o = reg_conn_reset_active;

    // One register access per request; the register file answers with
    // ready one cycle later.
    assign reg_we = reg_req &  reg_wr;
    assign reg_re = reg_req & ~reg_wr;

    //=======================================================================
    // Reset: one request, three synchronised releases
    //=======================================================================

    cxp_cdc_reset cxp_cdc_reset_i (
        .rst_n       (rst_req_n),
        .rx_clk      (rx_clk),
        .tx_clk      (tx_clk),
        .app_clk     (app_clk),
        .rx_rst_n_o  (rx_rst_s_n),
        .tx_rst_n_o  (tx_rst_s_n),
        .app_rst_n_o (app_rst_s_n)
    );

    //=======================================================================
    // Link, datapath and control plane
    //=======================================================================

    cxp_interface_top #(
        .p_TPG_X_SIZE     (p_TPG_X_SIZE),
        .p_TPG_Y_SIZE     (p_TPG_Y_SIZE),
        .p_FIFO_DEPTH     (p_FIFO_DEPTH),
        .p_PIX_W          (p_PIX_W),
        .p_CTRL_BUF_DEPTH (p_CTRL_BUF_DEPTH),
        .p_APB_AW         (32),
        .p_APB_DW         (32),
        .p_USER_BASE      (p_USER_BASE),
        .p_USER_SIZE      (p_USER_SIZE),
        .p_RX_CLK_KHZ     (p_RX_CLK_KHZ),
        .p_OS_RATIO       (p_OS_RATIO),
        .p_SAMP_LOCK_HITS (p_SAMP_LOCK_HITS),
        .p_RX_LOSS_WORDS (p_RX_LOSS_WORDS),
        .p_TRIG_ACK_TIMEOUT (p_TRIG_ACK_TIMEOUT),
        .p_ASYNC_CLOCKS   (p_ASYNC_CLOCKS)
    ) cxp_interface_top_i (
        .app_clk                      (app_clk),
        .app_rst_n                    (app_rst_s_n),
        .tx_clk                       (tx_clk),
        .tx_rst_n                     (tx_rst_s_n),
        .rx_clk                       (rx_clk),
        .rx_rst_n                     (rx_rst_s_n),

        .cfg                          (cfg),
        .acq_start                    (reg_acq_start),
        .acq_stop                     (reg_acq_stop | reg_conn_reset),
        .clr_lt_err                   (reg_clr_test_err),
        .clr_lt_pkt_tx                (reg_clr_test_pkt_tx),
        .clr_lt_pkt_rx                (reg_clr_test_pkt_rx),

        .trig_in                      (trig_i),
        .trig_out                     (trig_o),
        .trig_out_glitch_pulse        (trig_glitch_pulse_o),

        .s_pix_data                   (s_pix_data_i),
        .s_pix_valid                  (s_pix_valid_i),
        .s_pix_sof                    (s_pix_sof_i),
        .s_pix_eol                    (s_pix_eol_i),
        .s_pix_eof                    (s_pix_eof_i),
        .s_pix_ready                  (s_pix_ready_o),

        .s_meta                       (s_meta_i),

        .rx_serial                    (rx_serial_i),
        .cxp_if_data_o                (cxp_if_data_o),
        .cxp_if_kmask_o               (cxp_if_kmask_o),

        .reg_req                      (reg_req),
        .reg_we                       (reg_wr),
        .reg_addr                     (reg_addr),
        .reg_wdata                    (reg_wdata),
        .reg_wstrb                    (reg_wstrb),
        .reg_ack                      (reg_ready),
        .reg_rdata                    (reg_rdata),
        .reg_err                      (reg_err),

        .apb_psel                     (m_apb_psel_o),
        .apb_penable                  (m_apb_penable_o),
        .apb_pwrite                   (m_apb_pwrite_o),
        .apb_paddr                    (m_apb_paddr_o),
        .apb_pwdata                   (m_apb_pwdata_o),
        .apb_pstrb                    (m_apb_pstrb_o),
        .apb_prdata                   (m_apb_prdata_i),
        .apb_pready                   (m_apb_pready_i),
        .apb_pslverr                  (m_apb_pslverr_i),

        .sb_status                    (status),
        .sb_pix_restart_pulse         (),
        .sb_pix_stray_eof_pulse       (),

        .conn_reset_active            (reg_conn_reset_active),
        .conn_reset_done              (conn_reset_done),
        .conn_cfg_wr                  (reg_conn_cfg_wr)
    );

    //=======================================================================
    // Register file
    //=======================================================================

    cxp_ctrl_bootstrap_regs #(
        .p_NUM_LINKS               (1),
        .p_LINK_RESET_CLEAR_CYCLES (p_LINK_RESET_CLEAR_CYCLES),
        .p_PIXEL_FORMAT_RESET      (p_PIXEL_FORMAT_RESET),
        .p_IMAGE1_STREAM_ID_RESET  (32'(p_IMAGE1_STREAM_ID_RESET)),
        .p_XML_BLOB_MEM            (p_XML_BLOB_MEM)
    ) cxp_ctrl_bootstrap_regs_i (
        .sys_clk                         (rx_clk),
        .sys_rst_n                       (rx_rst_s_n),

        .addr_i                          (reg_addr),
        .wdata_i                         (reg_wdata),
        .wstrb_i                         (reg_wstrb),
        .we_i                            (reg_we),
        .re_i                            (reg_re),
        .rdata_o                         (reg_rdata),
        .ready_o                         (reg_ready),
        .err_o                           (reg_err),

        .ctl_connection_reset_pulse_o    (reg_conn_reset),
        .ctl_connection_reset_active_o   (reg_conn_reset_active),
        .ctl_connection_config_wr_o      (reg_conn_cfg_wr),
        .ctl_device_connection_id_o      (),
        .ctl_master_host_connection_id_o (),
        .ctl_stream_pkt_dsize_o          (reg_stream_pkt_size),
        .ctl_connection_config_o         (),
        .ctl_test_mode_o                 (reg_test_mode),
        .ctl_acquisition_start_wr_o      (reg_acq_start),
        .ctl_acquisition_stop_wr_o       (reg_acq_stop),
        .ctl_tpg_width_o                 (reg_width),
        .ctl_tpg_height_o                (reg_height),
        .ctl_acquisition_mode_o          (),
        .ctl_acquisition_start_o         (),
        .ctl_acquisition_stop_o          (),
        .ctl_pixel_format_o              (reg_pixfmt),
        .ctl_tap_geometry_o              (reg_tap_geometry),
        .ctl_image1_stream_id_o          (reg_stream_id),
        .ctl_frame_count_o               (reg_frame_count),
        .ctl_test_pattern_o              (reg_testpat),
        .ctl_offset_x_o                  (reg_offset_x),
        .ctl_offset_y_o                  (reg_offset_y),
        .ctl_source_tag_o                (reg_source_tag),
        .ctl_stream_flags_o              (reg_stream_flags),
        .ctl_tpg_run_o                   (reg_tpg_run),
        .ctl_test_err_count_clr_o        (reg_clr_test_err),
        .ctl_test_pkt_tx_clr_o           (reg_clr_test_pkt_tx),
        .ctl_test_pkt_rx_clr_o           (reg_clr_test_pkt_rx),
        .ctl_device_user_id_o            (device_user_id_o),

        .test_err_count_i                (lt_err_count),
        .test_pkt_count_tx_i             (lt_pkt_count_tx),
        .test_pkt_count_rx_i             (lt_pkt_count_rx),
        .device_user_id_nv_i             (device_user_id_nv_i),

        .conn_reset_req_i                (1'b0),
        .conn_reset_done_i               (conn_reset_done)
    );

endmodule

`default_nettype wire
