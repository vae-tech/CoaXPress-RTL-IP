//-----------------------------------------------------------------------------
// tb_cxp_device_top
//
// Cocotb wrapper around `cxp_device_top` built with p_ASYNC_CLOCKS = 1, so
// the test can run app_clk, tx_clk and rx_clk at unrelated periods.  The
// uplink is oversampled 4x, the test-pattern image is capped at 64 x 32,
// the stream FIFO holds 256 words and `cfg_use_tpg` picks the TPG or the
// sensor port, whose pixels, frame flags and geometry are exposed.  A user
// window of 64 words at 0x0002_0000 is served by an APB slave here whose
// answer latency (`apb_latency` rx cycles, 0xFFFF = never) and error bit
// (`apb_slverr`) the test sets; RX_CLK_KHZ = 10 makes the device's 100 ms
// Wait and 900 ms timeout 1000 and 9000 rx cycles.  TRIG_ACK_TIMEOUT = 800
// tx cycles is the device trigger's wait for the host's I/O
// acknowledgment.  `apb_accesses` counts completed transfers.  Status
// is re-exposed under the names common/cxp_host.py expects
// (`cxp_if_data`, `cxp_if_kmask`, `sb_link_detected`).  Top-level
// `TESTCASE` register is written by Python so the current test number is
// visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_device_top #(
    parameter int OS_RATIO    = 4,
    parameter int RX_CLK_KHZ  = 10,
    parameter int TRIG_ACK_TIMEOUT = 800,
    parameter string p_XML_BLOB_MEM = cxp_regmap_pkg::XML_BLOB_MEM  // src/regmap/regmap.mk
) (
    input  wire  logic        app_clk,
    input  wire  logic        app_rst_n,
    input  wire  logic        tx_clk,
    input  wire  logic        tx_rst_n,
    input  wire  logic        rx_clk,
    input  wire  logic        rx_rst_n,

    input  wire  logic        cfg_run,
    input  wire  logic        cfg_use_tpg,
    input  wire  logic [15:0] s_pix_data,
    input  wire  logic        s_pix_valid,
    input  wire  logic        s_pix_sof,
    input  wire  logic        s_pix_eol,
    input  wire  logic        s_pix_eof,
    output logic              s_pix_ready,
    input  wire  logic [23:0] s_meta_xsize,
    input  wire  logic [23:0] s_meta_ysize,
    input  wire  logic [15:0] s_meta_pixfmt,
    input  wire  logic [15:0] s_meta_sourcetag,
    input  wire  logic        rx_serial,
    input  wire  logic        trig_in,
    output logic              trig_out,

    input  wire  logic [15:0] apb_latency,
    input  wire  logic        apb_slverr,
    output logic [15:0]       apb_accesses,

    output logic [31:0]       cxp_if_data,
    output logic [3:0]        cxp_if_kmask,
    output logic              sb_link_detected
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    // Sensor metadata: geometry and format from the bench, StreamID 1.
    cxp_pkg::cxp_meta_t s_meta;
    always_comb begin
        s_meta          = '0;
        s_meta.xsize    = s_meta_xsize;
        s_meta.ysize    = s_meta_ysize;
        s_meta.pixfmt   = s_meta_pixfmt;
        s_meta.sourcetag = s_meta_sourcetag;
        s_meta.streamid = 8'd1;
    end

    // User-window APB slave: 64 words, reads return the stored word; a
    // write changes the bytes PSTRB enables.
    logic        psel, penable, pwrite, pready;
    logic [31:0] paddr, pwdata, prdata;
    logic [3:0]  pstrb;
    logic [15:0] wait_q;
    logic [31:0] umem [64];

    always_ff @(posedge rx_clk or negedge rx_rst_n) begin
        if (!rx_rst_n) begin
            wait_q       <= 16'd0;
            apb_accesses <= 16'd0;
            for (int i = 0; i < 64; i++) umem[i] <= 32'hA500_0000 | 32'(i);
        end else if (psel && penable) begin
            wait_q <= wait_q + 16'd1;
            if (pready) begin
                wait_q       <= 16'd0;
                apb_accesses <= apb_accesses + 16'd1;
                if (pwrite)
                    for (int b = 0; b < 4; b++)
                        if (pstrb[b]) umem[paddr[7:2]][8*b +: 8] <= pwdata[8*b +: 8];
            end
        end else begin
            wait_q <= 16'd0;
        end
    end

    assign pready = psel & penable & (apb_latency != 16'hFFFF) & (wait_q >= apb_latency);
    assign prdata = umem[paddr[7:2]];

    cxp_device_top #(
        .p_XML_BLOB_MEM   (p_XML_BLOB_MEM),
        .p_USER_BASE      (32'h0002_0000),
        .p_USER_SIZE      (32'h0000_0100),
        .p_RX_CLK_KHZ     (RX_CLK_KHZ),
        .p_TRIG_ACK_TIMEOUT (TRIG_ACK_TIMEOUT),
        .p_TPG_X_SIZE     (64),
        .p_TPG_Y_SIZE     (32),
        .p_FIFO_DEPTH     (256),
        .p_OS_RATIO       (OS_RATIO),
        .p_ASYNC_CLOCKS   (1'b1)
    ) cxp_device_top_i (
        .app_clk               (app_clk),
        .app_rst_n             (app_rst_n),
        .tx_clk                (tx_clk),
        .tx_rst_n              (tx_rst_n),
        .rx_clk                (rx_clk),
        .rx_rst_n              (rx_rst_n),

        .cfg_use_tpg_i         (cfg_use_tpg),
        .cfg_run_i             (cfg_run),
        .cfg_arbitrary_i       (1'b0),
        .cfg_trig_polarity_i   (1'b0),
        .from_extension_link_i (1'b0),

        .trig_i                (trig_in),
        .trig_o                (trig_out),
        .trig_glitch_pulse_o   (),

        .s_pix_data_i          (s_pix_data),
        .s_pix_valid_i         (s_pix_valid),
        .s_pix_sof_i           (s_pix_sof),
        .s_pix_eol_i           (s_pix_eol),
        .s_pix_eof_i           (s_pix_eof),
        .s_pix_ready_o         (s_pix_ready),
        .s_meta_i              (s_meta),

        .rx_serial_i           (rx_serial),
        .cxp_if_data_o         (cxp_if_data),
        .cxp_if_kmask_o        (cxp_if_kmask),

        .m_apb_psel_o          (psel),
        .m_apb_penable_o       (penable),
        .m_apb_pwrite_o        (pwrite),
        .m_apb_paddr_o         (paddr),
        .m_apb_pwdata_o        (pwdata),
        .m_apb_pstrb_o         (pstrb),
        .m_apb_prdata_i        (prdata),
        .m_apb_pready_i        (pready),
        .m_apb_pslverr_i       (apb_slverr & pready),

        .device_user_id_nv_i   (128'h0),
        .device_user_id_o      (),

        .rx_lock_o             (),
        .aligned_o             (),
        .link_detected_o       (sb_link_detected),
        .link_reset_active_o   (),
        .rate_to_discovery_o   ()
    );
endmodule

`default_nettype wire
