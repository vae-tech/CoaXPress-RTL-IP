//-----------------------------------------------------------------------------
// tb_cxp_rx_link_top
//
// Cocotb integration wrapper around `cxp_rx_link` and the `cxp_ctrl_plane`
// that executes its control packets.  Shrinks the oversampling
// ratio (OS_RATIO = 8) for fast simulation and pins BUF_DEPTH = 16; the
// link-loss window is the RTL default (20 000 words without IDLE).  Re-exposes the DUT ports
// without the `_i`/`_o` suffixes so the test keeps the pre-restyle names.
// Top-level `TESTCASE` register is written by Python so the current test
// number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_rx_link_top #(
    parameter int OS_RATIO       = 8,
    parameter int SAMP_LOCK_HITS = 2,
    parameter int BUF_DEPTH      = 16,
    parameter int APB_AW         = 32,
    parameter int APB_DW         = 32
) (
    input  wire  logic                       rx_clk,
    input  wire  logic                       rx_rst_n,
    input  wire  logic                       rx_serial,

    output logic                             rx_lock,
    output logic                             aligned,
    output logic                             link_detected,

    input  wire  logic                       cfg_trig_polarity,
    output logic                             trigger_out_app,
    output logic                             trigger_glitch_pulse,
    output logic                             trig_pkt_rcvd,
    output logic                             ioack_rcvd,

    input  wire  logic                       clr_lt_err,
    input  wire  logic                       clr_lt_pkt,
    output logic [31:0]                      lt_err_count,
    output logic [63:0]                      lt_pkt_count_rx,

    input  wire  logic                       from_extension_link,

    output logic                             psel,
    output logic                             penable,
    output logic                             pwrite,
    output logic [APB_AW-1:0]                paddr,
    output logic [APB_DW-1:0]                pwdata,
    input  wire  logic [APB_DW-1:0]          prdata,
    input  wire  logic                       pready,
    input  wire  logic                       pslverr,

    input  wire  logic [$clog2(BUF_DEPTH)-1:0] rbuf_addr,
    output logic [31:0]                      rbuf_data,
    output logic [23:0]                      rsp_size,
    output logic [31:0]                      rsp_wait_ms,
    output logic [7:0]                       rsp_code,
    output logic                             rsp_valid,

    output logic                             ctrl_reset_pulse,
    output logic                             cmd_crc_err_pulse,
    output logic                             cmd_logical_err_pulse,
    output logic [7:0]                       cmd_logical_err_code,
    output logic                             router_reject_pulse,
    output logic [7:0]                       router_reject_code,
    output logic                             pkt_err_pulse,
    output logic                             rx_code_err_pulse,
    output logic                             rx_disp_err_pulse
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    cxp_pkg::cxp_rxlong_t rx_long;

    cxp_rx_link #(
        .p_OS_RATIO       (OS_RATIO),
        .p_SAMP_LOCK_HITS (SAMP_LOCK_HITS)
    ) cxp_rx_link_i (
        .rx_clk                  (rx_clk),
        .rx_rst_n                (rx_rst_n),
        .rx_serial_i             (rx_serial),
        .rx_lock_o               (rx_lock),
        .aligned_o               (aligned),
        .link_detected_o         (link_detected),
        .cfg_trig_polarity_i     (cfg_trig_polarity),
        .trig_enable_i           (1'b1),
        .trig_deassert_i         (1'b0),
        .trigger_out_app_o       (trigger_out_app),
        .trigger_glitch_pulse_o  (trigger_glitch_pulse),
        .trig_pkt_rcvd_o         (trig_pkt_rcvd),
        .ioack_rcvd_o            (ioack_rcvd),
        .clr_lt_err_i            (clr_lt_err),
        .clr_lt_pkt_i            (clr_lt_pkt),
        .lt_err_count_o          (lt_err_count),
        .lt_pkt_count_rx_o       (lt_pkt_count_rx),
        .long_o                  (rx_long),
        .pkt_err_pulse_o         (pkt_err_pulse),
        .rx_code_err_pulse_o     (rx_code_err_pulse),
        .rx_disp_err_pulse_o     (rx_disp_err_pulse)
    );

    // The control plane's register port goes through cxp_ctrl_apb_bridge so the
    // Python APB slave model answers it; the user window is off.  CLK_KHZ =
    // 50 makes 50 rx_clk cycles a millisecond for the Wait / timeout limits
    // (Wait after 5000 cycles, timeout after 45000).
    logic        reg_req, reg_we, reg_ack;
    logic [31:0] reg_addr, reg_wdata, reg_rdata;
    logic [7:0]  reg_err;
    cxp_pkg::cxp_ctrl_rsp_t rsp;

    assign rsp_code = rsp.code;
    assign rsp_size    = rsp.size;
    assign rsp_wait_ms = rsp.wait_ms;

    // The plane reports a command answered without an access as one nack
    // code; the bench's three status outputs are decoded from it.  The
    // read buffer is read in the bank of the last response.
    logic       nack_p;
    logic [7:0] nack_c;
    logic       rbank_q;
    assign cmd_crc_err_pulse     = nack_p & (nack_c == 8'h80);
    assign cmd_logical_err_pulse = nack_p & (nack_c[7:4] == 4'h4) & (nack_c != 8'h43);
    assign cmd_logical_err_code  = nack_c;
    assign router_reject_pulse   = nack_p & (nack_c == 8'h43);
    assign router_reject_code    = nack_c;
    always_ff @(posedge rx_clk or negedge rx_rst_n) begin
        if (!rx_rst_n)      rbank_q <= 1'b0;
        else if (rsp_valid) rbank_q <= rsp.rbank;
    end

    cxp_ctrl_plane #(
        .p_BUF_DEPTH    (BUF_DEPTH),
        .p_PKT_SIZE_MAX ((BUF_DEPTH + 6) * 4),
        .p_USER_SIZE    (32'h0),
        .p_CLK_KHZ      (50)
    ) cxp_ctrl_plane_i (
        .rx_clk                  (rx_clk),
        .rx_rst_n                (rx_rst_n),
        .long_i                  (rx_long),
        .from_extension_link_i   (from_extension_link),
        .reg_req_o               (reg_req),
        .reg_we_o                (reg_we),
        .reg_addr_o              (reg_addr),
        .reg_wdata_o             (reg_wdata),
        .reg_ack_i               (reg_ack),
        .reg_rdata_i             (reg_rdata),
        .reg_err_i               (reg_err),
        .reg_wstrb_o             (),
        .apb_psel_o              (),
        .apb_penable_o           (),
        .apb_pwrite_o            (),
        .apb_paddr_o             (),
        .apb_pwdata_o            (),
        .apb_prdata_i            (32'h0),
        .apb_pready_i            (1'b0),
        .apb_pslverr_i           (1'b0),
        .rbuf_clk                (rx_clk),
        .rbuf_addr_i             ({rbank_q, rbuf_addr}),
        .rbuf_data_o             (rbuf_data),
        .rsp_valid_o             (rsp_valid),
        .rsp_o                   (rsp),
        .rsp_ready_i             (1'b1),
        .ctrl_reset_pulse_o      (ctrl_reset_pulse),
        .nack_pulse_o            (nack_p),
        .nack_code_o             (nack_c)
    );

    cxp_ctrl_apb_bridge cxp_ctrl_apb_bridge_i (
        .clk       (rx_clk),
        .rst_n     (rx_rst_n),
        .abort_i   (1'b0),
        .req_i     (reg_req),
        .we_i      (reg_we),
        .addr_i    (reg_addr),
        .wdata_i   (reg_wdata),
        .ack_o     (reg_ack),
        .rdata_o   (reg_rdata),
        .err_o     (reg_err),
        .psel_o    (psel),
        .penable_o (penable),
        .pwrite_o  (pwrite),
        .paddr_o   (paddr),
        .pwdata_o  (pwdata),
        .prdata_i  (prdata),
        .pready_i  (pready),
        .pslverr_i (pslverr)
    );
endmodule

`default_nettype wire
