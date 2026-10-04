//-----------------------------------------------------------------------------
// tb_cxp_lib_crc32_top
//
// Cocotb wrapper around `cxp_lib_crc32`.  Instantiates both the 32-bit
// (word-wise stream-packet) and 8-bit (byte-wise short-packet) variants so a
// single cocotb run can exercise both without re-elaborating.  Both
// instances share the same clock and reset; per-instance `init` / `valid` /
// `data` / `byte-enable` ports are exposed independently so tests can
// cross-check that identical byte streams produce identical CRC values.
// Top-level `TESTCASE` register is written by Python so the current test
// number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_lib_crc32_top (
    input  wire  logic        clk,
    input  wire  logic        rst_n,

    // --- 32-bit (wordwise) instance ---------------------------------------
    input  wire  logic        w_init,
    input  wire  logic [31:0] w_din,
    input  wire  logic [3:0]  w_din_be,
    input  wire  logic        w_din_valid,
    output logic [31:0]       w_crc,

    // --- 8-bit (bytewise) instance ----------------------------------------
    input  wire  logic        b_init,
    input  wire  logic [7:0]  b_din,
    input  wire  logic        b_din_be,
    input  wire  logic        b_din_valid,
    output logic [31:0]       b_crc
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    cxp_lib_crc32 #(.p_IN_W(32)) cxp_lib_crc32_w_i (
        .clk         (clk),
        .rst_n       (rst_n),
        .init_i      (w_init),
        .din_i       (w_din),
        .din_be_i    (w_din_be),
        .din_valid_i (w_din_valid),
        .crc_o       (w_crc)
    );

    cxp_lib_crc32 #(.p_IN_W(8)) cxp_lib_crc32_b_i (
        .clk         (clk),
        .rst_n       (rst_n),
        .init_i      (b_init),
        .din_i       (b_din),
        .din_be_i    (b_din_be),
        .din_valid_i (b_din_valid),
        .crc_o       (b_crc)
    );
endmodule

`default_nettype wire
