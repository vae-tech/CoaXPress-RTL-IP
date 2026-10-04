//-----------------------------------------------------------------------------
// tb_cxp_ctrl_bootstrap_regs_top
//
// Cocotb wrapper for the bootstrap register file `cxp_ctrl_bootstrap_regs`.
// The DUT's test-counter ports are unpacked arrays
// (`logic [31:0] test_err_count [NUM_LINKS]`, likewise the 64-bit
// `test_pkt_count_tx/rx`), which are not portable as cocotb top-level
// ports across Verilator and Questa; the wrapper sets NUM_LINKS = 1 and
// exposes flattened link-0 inputs instead.  LINK_RESET_CLEAR_CYCLES is
// reduced to 8 and CONN_RESET_TIMEOUT to 64 so the ConnectionReset tests run
// in a handful of cycles.  The ConnectionReset request and the tx-domain
// echo (`conn_reset_req`, `conn_reset_done`) are top-level inputs; Python
// echoes `ctl_connection_reset_active_o` back unless a test withholds it.
// `ctl_test_pattern_o` is left open.  Top-level `TESTCASE` register is written by Python so the
// current test number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_ctrl_bootstrap_regs_top #(
    parameter string p_XML_BLOB_MEM = cxp_regmap_pkg::XML_BLOB_MEM  // src/regmap/regmap.mk
) (
    input  wire  logic         sys_clk,
    input  wire  logic         sys_rst_n,

    input  wire  logic [31:0]  addr,
    input  wire  logic [31:0]  wdata,
    input  wire  logic         we,
    input  wire  logic         re,
    output logic [31:0]        rdata,
    output logic               ready,
    output logic [7:0]         err,

    output logic               ctl_connection_reset_pulse_o,
    output logic               ctl_connection_config_wr_o,
    output logic [31:0]        ctl_device_connection_id_o,
    output logic [31:0]        ctl_master_host_connection_id_o,
    output logic [31:0]        ctl_stream_pkt_dsize_o,
    output logic [31:0]        ctl_tpg_width_o,
    output logic [31:0]        ctl_tpg_height_o,
    output logic [31:0]        ctl_pixel_format_o,
    output logic [31:0]        ctl_acquisition_mode_o,
    output logic [31:0]        ctl_acquisition_start_o,
    output logic [31:0]        ctl_acquisition_stop_o,
    output logic               ctl_acquisition_start_wr_o,
    output logic               ctl_acquisition_stop_wr_o,
    output logic [31:0]        ctl_frame_count_o,
    output logic [31:0]        ctl_tpg_run_o,
    output logic [31:0]        ctl_tap_geometry_o,
    output logic [31:0]        ctl_image1_stream_id_o,
    output logic [31:0]        ctl_connection_config_o,
    output logic               ctl_test_mode_o,
    output logic               ctl_test_err_count_clr_o,
    output logic               ctl_test_pkt_tx_clr_o,
    output logic               ctl_test_pkt_rx_clr_o,
    input  wire  logic [31:0]  test_err_count_link0,
    input  wire  logic [63:0]  test_pkt_count_tx_link0,
    input  wire  logic [63:0]  test_pkt_count_rx_link0,

    output logic [127:0]       ctl_device_user_id_o,
    input  wire  logic [127:0] device_user_id_nv,

    // §10.3.28 ConnectionReset: local request, level out, its echo in.
    input  wire  logic         conn_reset_req,
    output logic               ctl_connection_reset_active_o,
    input  wire  logic         conn_reset_done
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    localparam int NUM_LINKS = 1;

    logic [31:0] err_arr    [NUM_LINKS];
    logic [63:0] pkt_tx_arr [NUM_LINKS];
    logic [63:0] pkt_rx_arr [NUM_LINKS];
    assign err_arr[0]    = test_err_count_link0;
    assign pkt_tx_arr[0] = test_pkt_count_tx_link0;
    assign pkt_rx_arr[0] = test_pkt_count_rx_link0;

    cxp_ctrl_bootstrap_regs #(
        .p_NUM_LINKS                     (NUM_LINKS),
        .p_LINK_RESET_CLEAR_CYCLES       (8),
        .p_CONN_RESET_TIMEOUT            (64),
        .p_XML_BLOB_MEM                  (p_XML_BLOB_MEM)
    ) cxp_ctrl_bootstrap_regs_i (
        .sys_clk                         (sys_clk),
        .sys_rst_n                       (sys_rst_n),
        .addr_i                          (addr),
        .wdata_i                         (wdata),
        .wstrb_i                         (4'hF),
        .we_i                            (we),
        .re_i                            (re),
        .rdata_o                         (rdata),
        .ready_o                         (ready),
        .err_o                           (err),
        .ctl_connection_reset_pulse_o    (ctl_connection_reset_pulse_o),
        .ctl_connection_reset_active_o   (ctl_connection_reset_active_o),
        .ctl_connection_config_wr_o      (ctl_connection_config_wr_o),
        .ctl_device_connection_id_o      (ctl_device_connection_id_o),
        .ctl_master_host_connection_id_o (ctl_master_host_connection_id_o),
        .ctl_stream_pkt_dsize_o          (ctl_stream_pkt_dsize_o),
        .ctl_tpg_width_o                 (ctl_tpg_width_o),
        .ctl_tpg_height_o                (ctl_tpg_height_o),
        .ctl_pixel_format_o              (ctl_pixel_format_o),
        .ctl_acquisition_mode_o          (ctl_acquisition_mode_o),
        .ctl_acquisition_start_o         (ctl_acquisition_start_o),
        .ctl_acquisition_stop_o          (ctl_acquisition_stop_o),
        .ctl_acquisition_start_wr_o      (ctl_acquisition_start_wr_o),
        .ctl_acquisition_stop_wr_o       (ctl_acquisition_stop_wr_o),
        .ctl_frame_count_o               (ctl_frame_count_o),
        .ctl_tpg_run_o                   (ctl_tpg_run_o),
        .ctl_tap_geometry_o              (ctl_tap_geometry_o),
        .ctl_image1_stream_id_o          (ctl_image1_stream_id_o),
        .ctl_connection_config_o         (ctl_connection_config_o),
        .ctl_test_mode_o                 (ctl_test_mode_o),
        .ctl_test_err_count_clr_o        (ctl_test_err_count_clr_o),
        .ctl_test_pkt_tx_clr_o           (ctl_test_pkt_tx_clr_o),
        .ctl_test_pkt_rx_clr_o           (ctl_test_pkt_rx_clr_o),
        .test_err_count_i                (err_arr),
        .test_pkt_count_tx_i             (pkt_tx_arr),
        .test_pkt_count_rx_i             (pkt_rx_arr),
        .ctl_device_user_id_o            (ctl_device_user_id_o),
        .device_user_id_nv_i             (device_user_id_nv),
        .conn_reset_req_i                (conn_reset_req),
        .conn_reset_done_i               (conn_reset_done)
    );
endmodule

`default_nettype wire
