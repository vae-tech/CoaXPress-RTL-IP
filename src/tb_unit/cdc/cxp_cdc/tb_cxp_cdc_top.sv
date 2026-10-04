//-----------------------------------------------------------------------------
// tb_cxp_cdc_top
//
// Cocotb wrapper for the four CDC primitives (cxp_cdc_sync / _pulse / _bus
// / _req), each from src_clk to dst_clk; Python runs the two clocks at
// unrelated periods.  Top-level `TESTCASE` is written by Python.
//-----------------------------------------------------------------------------

`default_nettype none

module tb_cxp_cdc_top (
    input  wire  logic        src_clk,
    input  wire  logic        src_rst_n,
    input  wire  logic        dst_clk,
    input  wire  logic        dst_rst_n,
    // cxp_cdc_sync (8 bits)
    input  wire  logic [7:0]  sync_d,
    output logic [7:0]        sync_q,
    // cxp_cdc_pulse
    input  wire  logic        pulse_in,
    output logic              pulse_out,
    // cxp_cdc_bus (32 bits, reset 0x5A5A5A5A)
    input  wire  logic [31:0] bus_d,
    output logic [31:0]       bus_q,
    // cxp_cdc_req (32-bit payload)
    input  wire  logic        req_valid,
    input  wire  logic [31:0] req_data,
    output logic              req_ready,
    output logic              dst_valid,
    output logic [31:0]       dst_data,
    input  wire  logic        dst_ready
);

    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */
    initial TESTCASE = 8'h00;

    cxp_cdc_sync #(.p_W(8)) u_sync (
        .clk (dst_clk), .rst_n (dst_rst_n), .d_i (sync_d), .q_o (sync_q)
    );

    cxp_cdc_pulse u_pulse (
        .src_clk (src_clk), .src_rst_n (src_rst_n), .pulse_i (pulse_in),
        .dst_clk (dst_clk), .dst_rst_n (dst_rst_n), .pulse_o (pulse_out)
    );

    cxp_cdc_bus #(.p_W(32), .p_RESET(32'h5A5A_5A5A)) u_bus (
        .src_clk (src_clk), .src_rst_n (src_rst_n), .d_i (bus_d),
        .dst_clk (dst_clk), .dst_rst_n (dst_rst_n), .q_o (bus_q)
    );

    cxp_cdc_req #(.p_W(32)) u_req (
        .src_clk     (src_clk),  .src_rst_n (src_rst_n),
        .src_valid_i (req_valid), .src_data_i (req_data), .src_ready_o (req_ready),
        .dst_clk     (dst_clk),  .dst_rst_n (dst_rst_n),
        .dst_valid_o (dst_valid), .dst_data_o (dst_data), .dst_ready_i (dst_ready)
    );

endmodule

`default_nettype wire
