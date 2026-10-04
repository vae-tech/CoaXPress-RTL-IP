//-----------------------------------------------------------------------------
// tb_cxp_rx_packet_parser_top
//
// Cocotb wrapper for the uplink packet parser.  Flattens the DUT ports to
// suffix-free names so Python can drive the word / kmask stream and read the
// I/O-ack and long-packet outputs directly; no glue logic.  Top-level
// `TESTCASE` register is written by Python so the current test number is
// visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_rx_packet_parser_top (
    input  wire  logic        rx_clk,
    input  wire  logic        rx_rst_n,
    input  wire  logic [31:0] d_in,
    input  wire  logic [3:0]  d_kmask,
    input  wire  logic        d_err,
    input  wire  logic        d_valid,
    input  wire  logic        flush,
    output logic              ioack,
    output logic [31:0]       long_data,
    output logic [3:0]        long_kmask,
    output logic              long_valid,
    output logic              long_sop,
    output logic              long_eop,
    output logic              long_err,
    output logic [7:0]        long_type,
    output logic              pkt_err_pulse
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    cxp_rx_packet_parser cxp_rx_packet_parser_i (
        .rx_clk            (rx_clk),
        .rx_rst_n          (rx_rst_n),
        .d_in_i            (d_in),
        .d_kmask_i         (d_kmask),
        .d_err_i           (d_err),
        .d_valid_i         (d_valid),
        .flush_i           (flush),
        .ioack_o           (ioack),
        .long_data_o       (long_data),
        .long_kmask_o      (long_kmask),
        .long_valid_o      (long_valid),
        .long_sop_o        (long_sop),
        .long_eop_o        (long_eop),
        .long_err_o        (long_err),
        .long_type_o       (long_type),
        .pkt_err_pulse_o   (pkt_err_pulse)
    );
endmodule

`default_nettype wire
