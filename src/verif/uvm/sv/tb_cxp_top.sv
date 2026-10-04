// -----------------------------------------------------------------------------
// tb_cxp_top
//
// PyUVM TB shell for the top-level CoaXPress 1.1.1 device IP verification.
//
// The DUT is `cxp_device_top` itself — the IP boundary an integrator
// instantiates, register file and every register side effect included —
// so the environment tests the product's integration, not a copy of it.
// The shell adds only verification hooks around it:
//
//   * Three Python-controlled clock-stop gates (app/tx/rx) so the
//     clock/reset agent can change the clock ratios at runtime.
//   * The user window (p_USER_BASE / p_USER_SIZE, §10.3 manufacturer
//     range) is served by an APB3 slave here: USER_WORDS words of memory
//     (reset value 0xA500_0000 | index), an answer latency `usr_latency`
//     in rx cycles (0xFFFF = never answers) and an error bit `usr_slverr`
//     the Python agent sets per access.  `usr_wait` is the latency the
//     last access actually had.
//   * The sensor metadata ports are flat (`ext_meta_*`) and packed into
//     `cxp_meta_t` here.
//   * Status the product does not bring out — the register-bus accesses
//     between the control plane and the register file, the control-plane
//     and receiver status pulses, the connection-test counters and the
//     register values the datapath uses — is reached through hierarchical
//     references into the instance and mirrored to ports, so the Python
//     monitors sample ports only.  Every one of them is listed under
//     "Taps" below.
//
// Build knobs (src/verif/Makefile passes them as -G and picks a sim_build per
// set): OS_RATIO, RX_CLK_KHZ (the unit of the device's 100 ms Wait and
// 900 ms timeout), RX_LOSS_WORDS (words without IDLE before the link is
// lost), FIFO_DEPTH (stream FIFO).
// -----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_top #(
    parameter bit          p_ASYNC_CLOCKS  = 1'b1,
    parameter int          OS_RATIO        = 16,
    parameter int          RX_CLK_KHZ      = 20,       // 1 ms = 20 rx cycles
    parameter int          RX_LOSS_WORDS   = cxp_pkg::RX_LOSS_WORDS_DEFAULT,
    parameter int          FIFO_DEPTH      = 1024,
    parameter int          TPG_X_SIZE      = 8,
    parameter int          TPG_Y_SIZE      = 4,
    parameter logic [31:0] USER_BASE       = 32'h0002_0000,
    parameter int          USER_WORDS      = 256,
    parameter string       p_XML_BLOB_MEM  = cxp_regmap_pkg::XML_BLOB_MEM  // src/regmap/regmap.mk
) (
    // Three independently gateable clock inputs (driven by Python).
    input  wire  logic        app_clk_in,
    input  wire  logic        tx_clk_in,
    input  wire  logic        rx_clk_in,
    input  wire  logic        app_rst_n,
    input  wire  logic        tx_rst_n,
    input  wire  logic        rx_rst_n,

    // Per-domain clock-enable strobes (1 = pass clock through, 0 = freeze).
    input  wire  logic        app_clk_en,
    input  wire  logic        tx_clk_en,
    input  wire  logic        rx_clk_en,

    // -------- Configuration the device has no register for --------
    input  wire  logic        cfg_use_tpg,
    input  wire  logic        cfg_run,
    input  wire  logic        cfg_arbitrary,
    input  wire  logic [15:0] cfg_dsizeP,
    input  wire  logic        cfg_trig_polarity,
    input  wire  logic        from_extension_link,

    // -------- Device trigger pin (§8.3.2, device -> host) --------
    input  wire  logic        trigger_in_app,

    // -------- External single-pixel sensor port --------
    input  wire  logic [15:0] s_pix_data,
    input  wire  logic        s_pix_valid,
    input  wire  logic        s_pix_sof,
    input  wire  logic        s_pix_eol,
    input  wire  logic        s_pix_eof,
    output logic              s_pix_ready,

    // -------- External frame metadata --------
    input  wire  logic [23:0] ext_meta_xsize,
    input  wire  logic [23:0] ext_meta_ysize,
    input  wire  logic [23:0] ext_meta_xoffs,
    input  wire  logic [23:0] ext_meta_yoffs,
    input  wire  logic [15:0] ext_meta_pixfmt,
    input  wire  logic [15:0] ext_meta_tapg,
    input  wire  logic [15:0] ext_meta_streamid,
    input  wire  logic [15:0] ext_meta_sourcetag,
    input  wire  logic [7:0]  ext_meta_flags,

    // -------- Serial uplink --------
    input  wire  logic        rx_serial,

    // -------- Downlink word bus --------
    output logic [31:0]       cxp_if_data_o,
    output logic [3:0]        cxp_if_kmask_o,
    output logic              wire_busy,              // the word is not IDLE
    output logic [31:0]       tx_words,               // tx_clk edges since t = 0

    // -------- Status (product ports) --------
    output logic              sb_rx_lock,
    output logic              sb_aligned,
    output logic              sb_link_detected,
    output logic              sb_trigger_out_app,
    output logic              sb_trigger_glitch_pulse,
    output logic              sb_link_reset_active,
    output logic              sb_link_reset_done,     // 1 cycle: the bit cleared
    output logic              sb_rate_to_discovery,

    // -------- Taps (hierarchical, see below) --------
    output logic [31:0]       sb_lt_err_count,
    output logic [63:0]       sb_lt_pkt_count_tx,
    output logic [63:0]       sb_lt_pkt_count_rx,
    output logic              sb_ctrl_reset_pulse,
    output logic              sb_ctrl_nack_pulse,     // answered without an access
    output logic [7:0]        sb_ctrl_nack_code,      //   (0x42..0x47, 0x80)
    output logic              sb_pkt_err_pulse,
    output logic              sb_rx_code_err_pulse,
    output logic              sb_rx_disp_err_pulse,
    output logic              sb_pix_restart_pulse,   // app_clk: SOF inside an image
    output logic              sb_pix_stray_eof_pulse, // app_clk: EOF outside an image

    // Register bus between the control plane and the register file.
    output logic              rb_req,
    output logic              rb_we,
    output logic [31:0]       rb_addr,
    output logic [31:0]       rb_wdata,
    output logic [3:0]        rb_wstrb,
    output logic              rb_ack,
    output logic [31:0]       rb_rdata,
    output logic [7:0]        rb_err,

    // Register values the datapath takes.
    output logic [31:0]       bs_stream_pkt_dsize,
    output logic              bs_test_mode,
    output logic              bs_conn_cfg_wr,
    output logic [31:0]       bs_width,
    output logic [31:0]       bs_height,
    output logic [31:0]       bs_pixel_format,
    output logic [31:0]       bs_stream_id,

    // -------- User window: APB3 bus and the slave's knobs --------
    output logic              apb_psel,
    output logic              apb_penable,
    output logic              apb_pwrite,
    output logic [31:0]       apb_paddr,
    output logic [31:0]       apb_pwdata,
    output logic [3:0]        apb_pstrb,              // APB4 byte enables
    output logic [31:0]       apb_prdata,
    output logic              apb_pready,
    output logic              apb_pslverr,
    output logic              apb_done,
    output logic [31:0]       apb_done_rdata,
    output logic              apb_done_slverr,
    input  wire  logic [15:0] usr_latency,            // rx cycles, 0xFFFF = never
    input  wire  logic        usr_slverr,             // answer with PSLVERR
    output logic [15:0]       usr_wait                // cycles the last access took
);
    // Cocotb writes the current test-case number here at the start of
    // every test (consumed by cxp_testcase.cxp_test).  Visible in waves.
    /* verilator lint_off UNUSEDSIGNAL */
    int unsigned TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */
    initial TESTCASE = 0;

    localparam logic [31:0] USER_SIZE = 32'(USER_WORDS * 4);
    localparam int          UW        = $clog2(USER_WORDS);

    // -------------------------------------------------------------------------
    // Clock gating.  Python drives *_clk_in continuously and toggles
    // *_clk_en to freeze a domain.
    // -------------------------------------------------------------------------
    logic app_clk, tx_clk, rx_clk;
    assign app_clk = app_clk_in & app_clk_en;
    assign tx_clk  = tx_clk_in  & tx_clk_en;
    assign rx_clk  = rx_clk_in  & rx_clk_en;

    // -------------------------------------------------------------------------
    // Sensor metadata.
    // -------------------------------------------------------------------------
    cxp_pkg::cxp_meta_t s_meta;
    always_comb begin
        s_meta           = '0;
        s_meta.arbitrary = cfg_arbitrary;
        s_meta.streamid  = ext_meta_streamid[7:0];
        s_meta.sourcetag = ext_meta_sourcetag;
        s_meta.xsize     = ext_meta_xsize;
        s_meta.ysize     = ext_meta_ysize;
        s_meta.xoffs     = ext_meta_xoffs;
        s_meta.yoffs     = ext_meta_yoffs;
        s_meta.pixfmt    = ext_meta_pixfmt;
        s_meta.tapg      = ext_meta_tapg;
        s_meta.flags     = ext_meta_flags;
    end

    // -------------------------------------------------------------------------
    // User-window APB3 slave.
    // -------------------------------------------------------------------------
    logic        psel, penable, pwrite, pready;
    logic [31:0] paddr, pwdata, prdata;
    logic [3:0]  pstrb;                  // APB4 byte enables of a write
    logic [15:0] wait_q;
    logic [31:0] umem [USER_WORDS];
    logic [UW-1:0] uidx;

    assign uidx = paddr[UW+1:2];

    always_ff @(posedge rx_clk or negedge rx_rst_n) begin
        if (!rx_rst_n) begin
            wait_q   <= 16'd0;
            usr_wait <= 16'd0;
            apb_done <= 1'b0;
            apb_done_rdata <= 32'd0;
            apb_done_slverr <= 1'b0;
            for (int i = 0; i < USER_WORDS; i++) umem[i] <= 32'hA500_0000 | 32'(i);
        end else begin
            // Capture the handshake before the APB master drops PSEL on
            // this same edge.  The passive Python monitor reads this tap.
            apb_done <= pready;
            if (pready) begin
                apb_done_rdata <= prdata;
                apb_done_slverr <= usr_slverr;
            end
            if (psel && penable) begin
                wait_q <= wait_q + 16'd1;
                if (pready) begin
                    wait_q   <= 16'd0;
                    usr_wait <= wait_q;
                    if (pwrite && !usr_slverr)
                        for (int b = 0; b < 4; b++)
                            if (pstrb[b]) umem[uidx][8*b +: 8] <= pwdata[8*b +: 8];
                end
            end else begin
                wait_q <= 16'd0;
            end
        end
    end

    assign pready = psel & penable & (usr_latency != 16'hFFFF) & (wait_q >= usr_latency);
    assign prdata = umem[uidx];

    assign apb_psel    = psel;
    assign apb_penable = penable;
    assign apb_pwrite  = pwrite;
    assign apb_paddr   = paddr;
    assign apb_pwdata  = pwdata;
    assign apb_pstrb   = pstrb;
    assign apb_prdata  = prdata;
    assign apb_pready  = pready;
    assign apb_pslverr = usr_slverr & pready;

    // -------------------------------------------------------------------------
    // DUT
    // -------------------------------------------------------------------------
    logic link_reset_active_d_q;

    cxp_device_top #(
        .p_XML_BLOB_MEM     (p_XML_BLOB_MEM),
        .p_TPG_X_SIZE       (TPG_X_SIZE),
        .p_TPG_Y_SIZE       (TPG_Y_SIZE),
        .p_FIFO_DEPTH       (FIFO_DEPTH),
        .p_PIX_W            (16),
        .p_CTRL_BUF_DEPTH   (64),
        .p_OS_RATIO         (OS_RATIO),
        .p_SAMP_LOCK_HITS   (2),
        .p_RX_LOSS_WORDS    (RX_LOSS_WORDS),
        .p_RX_CLK_KHZ       (RX_CLK_KHZ),
        .p_USER_BASE        (USER_BASE),
        .p_USER_SIZE        (USER_SIZE),
        .p_ASYNC_CLOCKS     (p_ASYNC_CLOCKS)
    ) cxp_device_top_i (
        .app_clk               (app_clk),
        .app_rst_n             (app_rst_n),
        .tx_clk                (tx_clk),
        .tx_rst_n              (tx_rst_n),
        .rx_clk                (rx_clk),
        .rx_rst_n              (rx_rst_n),

        .cfg_use_tpg_i         (cfg_use_tpg),
        .cfg_run_i             (cfg_run),
        .cfg_arbitrary_i       (cfg_arbitrary),
        .cfg_trig_polarity_i   (cfg_trig_polarity),
        .from_extension_link_i (from_extension_link),

        .trig_i                (trigger_in_app),
        .trig_o                (sb_trigger_out_app),
        .trig_glitch_pulse_o   (sb_trigger_glitch_pulse),

        .s_pix_data_i          (s_pix_data),
        .s_pix_valid_i         (s_pix_valid),
        .s_pix_sof_i           (s_pix_sof),
        .s_pix_eol_i           (s_pix_eol),
        .s_pix_eof_i           (s_pix_eof),
        .s_pix_ready_o         (s_pix_ready),
        .s_meta_i              (s_meta),

        .rx_serial_i           (rx_serial),
        .cxp_if_data_o         (cxp_if_data_o),
        .cxp_if_kmask_o        (cxp_if_kmask_o),

        .m_apb_psel_o          (psel),
        .m_apb_penable_o       (penable),
        .m_apb_pwrite_o        (pwrite),
        .m_apb_paddr_o         (paddr),
        .m_apb_pwdata_o        (pwdata),
        .m_apb_pstrb_o         (pstrb),
        .m_apb_prdata_i        (prdata),
        .m_apb_pready_i        (pready),
        .m_apb_pslverr_i       (usr_slverr & pready),

        .device_user_id_nv_i   (128'h0),
        .device_user_id_o      (),

        .rx_lock_o             (sb_rx_lock),
        .aligned_o             (sb_aligned),
        .link_detected_o       (sb_link_detected),
        .link_reset_active_o   (sb_link_reset_active),
        .rate_to_discovery_o   (sb_rate_to_discovery)
    );

    // The ConnectionReset bit clearing, as a pulse (rx_clk).
    always_ff @(posedge rx_clk or negedge rx_rst_n) begin
        if (!rx_rst_n) link_reset_active_d_q <= 1'b0;
        else           link_reset_active_d_q <= sb_link_reset_active;
    end
    assign sb_link_reset_done = link_reset_active_d_q & ~sb_link_reset_active;

    // The downlink monitor sleeps through IDLE runs: it wakes on
    // `wire_busy` and reads the run length off `tx_words`.
    assign wire_busy = ~((cxp_if_kmask_o == 4'b0111) &&
                         (cxp_if_data_o  == {8'hB5, 8'h3C, 8'h3C, 8'hBC}));

    initial tx_words = 32'd0;
    always_ff @(posedge tx_clk) tx_words <= tx_words + 32'd1;

    // -------------------------------------------------------------------------
    // Taps.  Hierarchical references into the DUT for what the product does
    // not bring out; nothing here drives the DUT.
    // -------------------------------------------------------------------------
    assign sb_lt_err_count        = cxp_device_top_i.lt_err_count[0];
    assign sb_lt_pkt_count_tx     = cxp_device_top_i.lt_pkt_count_tx[0];
    assign sb_lt_pkt_count_rx     = cxp_device_top_i.lt_pkt_count_rx[0];
    assign sb_ctrl_reset_pulse    = cxp_device_top_i.cxp_interface_top_i.sb_status.ctrl_reset_pulse;
    assign sb_ctrl_nack_pulse     = cxp_device_top_i.cxp_interface_top_i.sb_status.ctrl_nack_pulse;
    assign sb_ctrl_nack_code      = cxp_device_top_i.cxp_interface_top_i.sb_status.ctrl_nack_code;
    assign sb_pkt_err_pulse       = cxp_device_top_i.cxp_interface_top_i.sb_status.pkt_err_pulse;
    assign sb_rx_code_err_pulse   = cxp_device_top_i.cxp_interface_top_i.sb_status.code_err_pulse;
    assign sb_rx_disp_err_pulse   = cxp_device_top_i.cxp_interface_top_i.sb_status.disp_err_pulse;
    assign sb_pix_restart_pulse   = cxp_device_top_i.cxp_interface_top_i.sb_pix_restart_pulse;
    assign sb_pix_stray_eof_pulse = cxp_device_top_i.cxp_interface_top_i.sb_pix_stray_eof_pulse;

    assign rb_req   = cxp_device_top_i.reg_req;
    assign rb_we    = cxp_device_top_i.reg_wr;
    assign rb_addr  = cxp_device_top_i.reg_addr;
    assign rb_wdata = cxp_device_top_i.reg_wdata;
    assign rb_wstrb = cxp_device_top_i.reg_wstrb;
    assign rb_ack   = cxp_device_top_i.reg_ready;
    assign rb_rdata = cxp_device_top_i.reg_rdata;
    assign rb_err   = cxp_device_top_i.reg_err;

    assign bs_stream_pkt_dsize = cxp_device_top_i.reg_stream_pkt_size;
    assign bs_test_mode        = cxp_device_top_i.reg_test_mode;
    assign bs_conn_cfg_wr      = cxp_device_top_i.reg_conn_cfg_wr;
    assign bs_width            = cxp_device_top_i.reg_width;
    assign bs_height           = cxp_device_top_i.reg_height;
    assign bs_pixel_format     = cxp_device_top_i.reg_pixfmt;
    assign bs_stream_id        = cxp_device_top_i.reg_stream_id;

endmodule

`default_nettype wire
