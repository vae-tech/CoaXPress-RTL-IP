//-----------------------------------------------------------------------------
// tb_cxp_interface_top
//
// Cocotb wrapper for cxp_interface_top.  Connects the bootstrap register
// file via an inline APB3-slave → generic-regfile bridge so the TB sees
// the device's APB master flowing into a real cxp_ctrl_bootstrap_regs slave.
// The register file owns ConnectionReset: `link_reset_req` is its local
// request, its active level goes to the DUT and the DUT's echo comes back.
//
// Geometry pinned tiny (8 × 4 px) so a TPG frame fits in a few thousand
// cycles.  All clocks tied together — rx_clk and tx_clk run at the same
// rate, matching the single-PLL MVP assumption documented at the top of
// cxp_interface_top.sv.
//
// The DUT emits a raw 32-bit + kmask TX word (`cxp_if_data_o` /
// `cxp_if_kmask_o`) — 8B/10B line coding lives outside the IP.  The
// cocotb monitor reads those signals directly, no decoder companion
// needed.  Top-level `TESTCASE` register is written by Python so the
// current test number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_interface_top #(
    parameter string p_XML_BLOB_MEM = cxp_regmap_pkg::XML_BLOB_MEM  // src/regmap/regmap.mk
) (
    input  wire  logic        clk,           // common clock for all domains
    input  wire  logic        rst_n,

    input  wire  logic        cfg_use_tpg,
    input  wire  logic        cfg_run,
    input  wire  logic        cfg_arbitrary,
    input  wire  logic [15:0] cfg_dsizeP,
    input  wire  logic        cfg_trig_polarity,
    input  wire  logic        clr_lt_err,
    input  wire  logic        clr_lt_pkt_tx,
    input  wire  logic        clr_lt_pkt_rx,

    // External sensor single-pixel bus — unused when cfg_use_tpg=1; tied
    // 0 on the TB side and held that way.
    input  wire  logic [15:0] s_pix_data,
    input  wire  logic        s_pix_valid,
    input  wire  logic        s_pix_sof,
    input  wire  logic        s_pix_eol,
    input  wire  logic        s_pix_eof,
    output logic              s_pix_ready,

    input  wire  logic [23:0] ext_meta_xsize,
    input  wire  logic [23:0] ext_meta_ysize,
    input  wire  logic [23:0] ext_meta_xoffs,
    input  wire  logic [23:0] ext_meta_yoffs,
    input  wire  logic [15:0] ext_meta_pixfmt,
    input  wire  logic [15:0] ext_meta_tapg,
    input  wire  logic [15:0] ext_meta_streamid,
    input  wire  logic [15:0] ext_meta_sourcetag,
    input  wire  logic [7:0]  ext_meta_flags,

    input  wire  logic        rx_serial,
    input  wire  logic        link_reset_req,
    // Added to the regfile's ConnectionConfig write strobe: the uplink is
    // not driven here, so the tests raise the strobe themselves.
    input  wire  logic        conn_cfg_wr_inject,

    // 1-bit local HS trigger input (device → host, §6.3.2.2).  Tied to
    // 1'b0 by default; cocotb tests drive it to exercise the
    // cxp_tx_trigger_hs packet path through the arbiter.
    input  wire  logic        trigger_in_app,

    // Raw 32-bit TX word + kmask from the DUT (1 word per tx_clk).
    output logic [31:0]       cxp_if_data_o,
    output logic [3:0]        cxp_if_kmask_o,

    output logic              rx_lock,
    output logic              aligned,
    output logic              link_detected,
    output logic              ctrl_reset_pulse,

    // Snoop of the bootstrap-regs writable outputs so the Python tests
    // can confirm host writes actually landed in the regfile.
    output logic [31:0]       bs_link_config,
    output logic              bs_test_mode,
    output logic [31:0]       bs_stream_pkt_dsize,
    output logic [31:0]       bs_tpg_width,
    output logic [31:0]       bs_tpg_height,
    output logic [31:0]       bs_pixel_format,
    output logic [31:0]       bs_master_host_link_id,

    // §10.3.28 ConnectionReset observability.
    output logic              link_reset_active,    // the ConnectionReset bit
    output logic              link_reset_done,      // 1-cycle: it cleared
    output logic              rate_to_discovery
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    // Rows of cxp_ctrl_bootstrap_regs.reg_q[] the tests backdoor-poke, found by
    // address in the register file's ROWS table so a table change cannot
    // silently retarget a poke.  (A hierarchical reference to the row enum
    // itself crashes Verilator 5.046; the localparam table is fine.)
    function automatic logic [7:0] bs_row(input logic [31:0] addr);
        bs_row = 8'hFF;
        for (int i = 0; i < cxp_ctrl_bootstrap_regs_i.R_N; i++)
            if (cxp_ctrl_bootstrap_regs_i.ROWS[i].addr == addr) bs_row = 8'(i);
    endfunction
    logic [7:0] row_test_mode;
    logic [7:0] row_master_host_conn_id;
    logic [7:0] row_stream_pkt_dsize;
    logic [7:0] row_test_err_cnt_sel;
    assign row_test_mode           = bs_row(32'(cxp_regmap_pkg::TEST_MODE_ADDR));
    assign row_master_host_conn_id = bs_row(32'(cxp_regmap_pkg::MASTER_HOST_CONNECTION_ID_ADDR));
    assign row_stream_pkt_dsize    = bs_row(32'(cxp_regmap_pkg::STREAM_PACKET_SIZE_MAX_ADDR));
    assign row_test_err_cnt_sel    = bs_row(32'(cxp_regmap_pkg::TEST_ERROR_COUNT_SELECTOR_ADDR));

    // ------------------------------------------------------------------------
    // APB wires between cxp_interface_top (master) and cxp_ctrl_bootstrap_regs
    // (slave, via the inline bridge below).
    // ------------------------------------------------------------------------
    logic        psel;
    logic        penable;
    logic        pwrite;
    logic [31:0] paddr;
    logic [31:0] pwdata;
    logic [31:0] prdata;
    logic        pready;
    logic        pslverr;

    // ConnectionReset: the register file's level and the DUT's echo.
    logic crst_active;
    logic crst_done;
    logic crst_active_d_q;

    assign link_reset_active = crst_active;
    assign rate_to_discovery = crst_active;
    assign link_reset_done   = crst_active_d_q & ~crst_active;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) crst_active_d_q <= 1'b0;
        else        crst_active_d_q <= crst_active;
    end

    // Host-writable TestPattern register (0x1001C) — bootstrap-regs
    // output feeding the TPG's cfg_testpat select.
    logic [31:0] bs_test_pattern;

    // ConnectionConfig write strobe: restarts the PacketTags.
    logic        bs_conn_cfg_wr;

    // The sensor metadata inputs as the DUT's cxp_meta_t; StreamID is one
    // byte on the wire.
    cxp_pkg::cxp_meta_t s_meta;

    assign s_meta = '{arbitrary: 1'b0,
                      streamid:  ext_meta_streamid[7:0],
                      sourcetag: ext_meta_sourcetag,
                      xsize:     ext_meta_xsize,
                      ysize:     ext_meta_ysize,
                      xoffs:     ext_meta_xoffs,
                      yoffs:     ext_meta_yoffs,
                      pixfmt:    ext_meta_pixfmt,
                      tapg:      ext_meta_tapg,
                      flags:     ext_meta_flags};

    // ------------------------------------------------------------------------
    // Configuration into the DUT as one cxp_cfg_t; its status unpacked.
    // No acquisition (the generator runs on cfg_run alone) and the stream
    // always enabled; TPG geometry, PixelFormat, TestPattern and TestMode
    // from the bootstrap registers; §5.1 extension-link strap 0 (the
    // master-link path; extension-link refusal is tested in
    // src/tb_unit/ctrl/cxp_ctrl_cmd_parser and src/tb_unit/ctrl/cxp_ctrl_plane).
    // ------------------------------------------------------------------------
    cxp_pkg::cxp_cfg_t    cfg;
    cxp_pkg::cxp_status_t status;

    assign cfg = '{use_tpg:       cfg_use_tpg,
                   run:           cfg_run,
                   acq_mode:      2'd0,
                   acq_frames:    16'd0,
                   stream_en:     1'b1,
                   xsize:         bs_tpg_width[15:0],
                   ysize:         bs_tpg_height[15:0],
                   pixfmt:        cxp_pkg::pfnc_to_pixelf(bs_pixel_format),
                   streamid:      8'h01,
                   xoffs:         16'h0,
                   yoffs:         16'h0,
                   srctag:        16'h0,
                   tapg:          16'h0,
                   flags:         8'h00,
                   testpat:       bs_test_pattern[1:0],
                   arbitrary:     cfg_arbitrary,
                   dsizeP:        cfg_dsizeP,
                   trig_polarity: cfg_trig_polarity,
                   test_mode:     bs_test_mode,
                   ext_link:      1'b0};

    assign rx_lock          = status.rx_lock;
    assign aligned          = status.aligned;
    assign link_detected    = status.link_detected;
    assign ctrl_reset_pulse = status.ctrl_reset_pulse;

    // ------------------------------------------------------------------------
    // DUT.  Everything single-clock for MVP.
    // ------------------------------------------------------------------------
    cxp_interface_top #(
        .p_TPG_X_SIZE       (8),
        .p_TPG_Y_SIZE       (4),
        .p_FIFO_DEPTH       (256),
        .p_CTRL_BUF_DEPTH   (64),
        .p_OS_RATIO         (16),
        .p_SAMP_LOCK_HITS   (2),
        .p_TRIG_ACK_TIMEOUT (64)
    ) cxp_interface_top_i (
        .app_clk            (clk),
        .app_rst_n          (rst_n),
        .tx_clk             (clk),
        .tx_rst_n           (rst_n),
        .rx_clk             (clk),
        .rx_rst_n           (rst_n),

        .cfg                (cfg),
        // No acquisition: the generator runs on cfg_run alone.
        .acq_start          (1'b0),
        .acq_stop           (1'b0),
        .clr_lt_err         (clr_lt_err),
        .clr_lt_pkt_tx      (clr_lt_pkt_tx),
        .clr_lt_pkt_rx      (clr_lt_pkt_rx),

        .s_pix_data         (s_pix_data),
        .s_pix_valid        (s_pix_valid),
        .s_pix_sof          (s_pix_sof),
        .s_pix_eol          (s_pix_eol),
        .s_pix_eof          (s_pix_eof),
        .s_pix_ready        (s_pix_ready),

        .s_meta             (s_meta),

        .rx_serial          (rx_serial),
        .conn_reset_active  (crst_active),
        .conn_reset_done    (crst_done),
        .conn_cfg_wr        (bs_conn_cfg_wr | conn_cfg_wr_inject),
        .trig_in            (trigger_in_app),

        .cxp_if_data_o      (cxp_if_data_o),
        .cxp_if_kmask_o     (cxp_if_kmask_o),

        .apb_psel           (psel),
        .apb_penable        (penable),
        .apb_pwrite         (pwrite),
        .apb_paddr          (paddr),
        .apb_pwdata         (pwdata),
        .apb_prdata         (prdata),
        .apb_pready         (pready),
        .apb_pslverr        (pslverr),

        // Every register access goes to the APB port (p_USER_SIZE default).
        .reg_req            (),
        .reg_we             (),
        .reg_addr           (),
        .reg_wdata          (),
        .reg_wstrb          (),
        .reg_ack            (1'b0),
        .reg_rdata          (32'h0),
        .reg_err            (8'h00),

        .trig_out                (),
        .trig_out_glitch_pulse   (),
        .sb_status               (status)
    );

    // ------------------------------------------------------------------------
    // APB3 → generic regfile bridge for cxp_ctrl_bootstrap_regs.
    //
    // cxp_ctrl_bootstrap_regs implements a single-cycle generic register-file
    // slave (addr / wdata / we / re / rdata / ready) — reads have 1 cycle
    // of latency.  An APB3 transaction is two phases: SETUP (psel=1,
    // penable=0) then ACCESS (psel=1, penable=1); the slave returns
    // pready=1 to complete.
    //
    // We pulse we/re for one cycle on the rising edge into ACCESS — the
    // exact moment the generic regfile expects to see the request — then
    // wait for `ready` (the regfile completion) and reflect it on pready.
    // ------------------------------------------------------------------------
    logic        bs_we, bs_re;
    logic [31:0] bs_rdata;
    logic        bs_ready;
    logic [7:0]  bs_err;

    logic        access_seen_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)      access_seen_q <= 1'b0;
        else if (pready) access_seen_q <= 1'b0;
        else if (psel & penable) access_seen_q <= 1'b1;
    end

    // Drive the regfile request only on the first cycle of ACCESS.  The
    // regfile's "ready" is combinational on writes (1 cycle, mirrors the
    // we strobe) and registered on reads (also 1 cycle).  We forward
    // bs_ready straight to pready so the APB master sees a single
    // ACCESS-cycle handshake.
    assign bs_we    = psel & penable & pwrite  & ~access_seen_q;
    assign bs_re    = psel & penable & ~pwrite & ~access_seen_q;
    assign pready   = bs_ready;
    assign prdata   = bs_rdata;
    assign pslverr  = (bs_err != 8'h00);   // register file refused the access

    // ------------------------------------------------------------------------
    // Bootstrap register file (lives outside cxp_interface_top per the
    // user's spec for this build).  Geometry leaves defaults from the
    // module — only the writable bits are observed by the TB.
    // ------------------------------------------------------------------------
    logic [127:0] dev_user_id;
    logic [127:0] dev_user_id_nv;
    assign dev_user_id_nv = 128'h0;

    logic [31:0] test_err_count    [1];
    logic [63:0] test_pkt_count_tx [1];
    logic [63:0] test_pkt_count_rx [1];
    assign test_err_count[0]    = 32'h0;
    // Live connection-test counts are exercised by the cxp_rx_linktest /
    // cxp_tx_linktest / cxp_ctrl_bootstrap_regs unit TBs; tied 0 here.
    assign test_pkt_count_tx[0] = 64'h0;
    assign test_pkt_count_rx[0] = 64'h0;

    cxp_ctrl_bootstrap_regs #(
        .p_NUM_LINKS                     (1),
        .p_LINK_RESET_CLEAR_CYCLES       (8),
        .p_XML_BLOB_MEM                  (p_XML_BLOB_MEM)
    ) cxp_ctrl_bootstrap_regs_i (
        .sys_clk                         (clk),
        .sys_rst_n                       (rst_n),

        .addr_i                          (paddr),
        .wdata_i                         (pwdata),
        .wstrb_i                         (4'hF),         // APB3: whole words
        .we_i                            (bs_we),
        .re_i                            (bs_re),
        .rdata_o                         (bs_rdata),
        .ready_o                         (bs_ready),
        .err_o                           (bs_err),

        // v1.1.1 Connection* port naming (CXP-001-2015 §10.3).
        .ctl_connection_reset_pulse_o    (),
        .ctl_connection_reset_active_o   (crst_active),
        .ctl_connection_config_wr_o      (bs_conn_cfg_wr),
        .ctl_device_connection_id_o      (),
        .ctl_master_host_connection_id_o (bs_master_host_link_id),
        .ctl_stream_pkt_dsize_o          (bs_stream_pkt_dsize),
        .ctl_tpg_width_o                 (bs_tpg_width),
        .ctl_tpg_height_o                (bs_tpg_height),
        .ctl_pixel_format_o              (bs_pixel_format),
        .ctl_test_pattern_o              (bs_test_pattern),
        .ctl_connection_config_o         (bs_link_config),
        .ctl_test_mode_o                 (bs_test_mode),
        .ctl_test_err_count_clr_o        (),
        .ctl_test_pkt_tx_clr_o           (),
        .ctl_test_pkt_rx_clr_o           (),
        .test_err_count_i                (test_err_count),
        .test_pkt_count_tx_i             (test_pkt_count_tx),
        .test_pkt_count_rx_i             (test_pkt_count_rx),
        .ctl_device_user_id_o            (dev_user_id),
        .device_user_id_nv_i             (dev_user_id_nv),

        // §10.3.28 ConnectionReset: the tests' request, the DUT's echo.
        .conn_reset_req_i                (link_reset_req),
        .conn_reset_done_i               (crst_done)
    );
endmodule

`default_nettype wire
