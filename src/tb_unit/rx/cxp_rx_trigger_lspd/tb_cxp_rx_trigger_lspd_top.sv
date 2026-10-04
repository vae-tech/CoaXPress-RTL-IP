//-----------------------------------------------------------------------------
// tb_cxp_rx_trigger_lspd_top
//
// Cocotb wrapper for the low-speed trigger receiver.  Flattens the DUT ports
// to suffix-free names so Python can stand in for cxp_rx_lspd_sampler (the
// strobe, the edge and the three Delay characters as 10b symbols).
// OS_RATIO = 8: one Delay unit is 1/3 rx_clk cycle.  No glue logic.
// Top-level `TESTCASE` register is written by Python so the current test
// number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_rx_trigger_lspd_top #(
    parameter int OS_RATIO = 8
) (
    input  wire  logic        rx_clk,
    input  wire  logic        rx_rst_n,
    input  wire  logic        cfg_polarity,
    input  wire  logic        deassert,
    input  wire  logic        trig_valid,
    input  wire  logic [1:0]  trig_edge,
    input  wire  logic [29:0] trig_dly,
    output logic              trig_ok,
    output logic              trigger_out_app,
    output logic              trigger_glitch_pulse
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    cxp_rx_trigger_lspd #(
        .p_OS_RATIO (OS_RATIO)
    ) cxp_rx_trigger_lspd_i (
        .rx_clk                 (rx_clk),
        .rx_rst_n               (rx_rst_n),
        .cfg_polarity_i         (cfg_polarity),
        .deassert_i             (deassert),
        .trig_valid_i           (trig_valid),
        .trig_edge_i            (trig_edge),
        .trig_dly_i             (trig_dly),
        .trig_ok_o              (trig_ok),
        .trigger_out_app_o      (trigger_out_app),
        .trigger_glitch_pulse_o (trigger_glitch_pulse)
    );
endmodule

`default_nettype wire
