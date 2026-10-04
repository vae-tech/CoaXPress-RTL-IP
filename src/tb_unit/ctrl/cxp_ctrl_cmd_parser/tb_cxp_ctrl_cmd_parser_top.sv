//-----------------------------------------------------------------------------
// tb_cxp_ctrl_cmd_parser_top
//
// Cocotb wrapper for the control-command parser.  Flattens the DUT ports
// to un-suffixed names and shrinks the write buffer (BUF_DEPTH = 16
// instead of the RTL default 64) and the packet size limit to match
// (PKT_SIZE_MAX = (16 + 6) * 4 = 88 bytes) so the oversize test stays short.  The
// DUT's own `cxp_lib_crc32` instance is compiled in from the Makefile.
// Top-level `TESTCASE` register is written by Python so the current test
// number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_ctrl_cmd_parser_top #(
    parameter int BUF_DEPTH    = 16,
    parameter int PKT_SIZE_MAX = (BUF_DEPTH + 6) * 4
) (
    // --- system ---
    input  wire  logic                         rx_clk,
    input  wire  logic                         rx_rst_n,

    // --- long-packet body in ---
    input  wire  logic [31:0]                  long_data,
    input  wire  logic [3:0]                   long_kmask,
    input  wire  logic                         long_valid,
    input  wire  logic                         long_sop,
    input  wire  logic                         long_eop,
    input  wire  logic                         long_err,
    input  wire  logic [7:0]                   long_type,

    // --- command out ---
    output logic                               cmd_valid,
    output logic [7:0]                         cmd_op,
    output logic [23:0]                        cmd_size,
    output logic [31:0]                        cmd_addr,
    output logic [15:0]                        cmd_word_count,

    // --- write buffer read port ---
    input  wire  logic [$clog2(BUF_DEPTH)-1:0] wbuf_addr,
    output logic [31:0]                        wbuf_data,

    // --- error pulses ---
    output logic                               cmd_crc_err_pulse,
    output logic                               cmd_logical_err_pulse,
    output logic [7:0]                         cmd_logical_err_code,

    // Executor side: extension-link strap, executor full, raw record
    input  wire  logic                         from_extension_link,
    input  wire  logic                         cmd_full,
    input  wire  logic                         wbuf_raw,      // 1: read bank wbuf_bank
    input  wire  logic                         wbuf_bank,
    output logic                               cmd_any,
    output logic [7:0]                         cmd_err,
    output logic                               cmd_wbank
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    // The parser emits one command record per packet; the outputs the
    // tests read are decoded from it: an executable command (err 0), a
    // CRC failure (0x80), or a 0x4x code.  The bank of the last executable
    // write selects the buffer half `wbuf_addr` reads.
    cxp_pkg::cxp_ctrl_cmd_t cmd;
    logic                   cmd_out;
    logic                   wbank_q;
    logic [7:0]             code_q;

    assign cmd_any               = cmd_out;
    assign cmd_err               = cmd.err;
    assign cmd_wbank             = cmd.wbank;
    assign cmd_valid             = cmd_out & (cmd.err == 8'h00);
    assign cmd_op                = cmd.op;
    assign cmd_size              = cmd.size;
    assign cmd_addr              = cmd.addr;
    assign cmd_word_count        = cmd.nwords;
    assign cmd_crc_err_pulse     = cmd_out & (cmd.err == cxp_pkg::ACK_ERR_CRC);
    assign cmd_logical_err_pulse = cmd_out & (cmd.err[7:4] == 4'h4);
    assign cmd_logical_err_code  = cmd_logical_err_pulse ? cmd.err : code_q;

    always_ff @(posedge rx_clk or negedge rx_rst_n) begin
        if (!rx_rst_n) begin
            wbank_q <= 1'b0;
            code_q  <= 8'h00;
        end else begin
            if (cmd_valid && cmd.op == cxp_pkg::CTRL_OP_WRITE) wbank_q <= cmd.wbank;
            if (cmd_logical_err_pulse) code_q <= cmd.err;
        end
    end

    cxp_ctrl_cmd_parser #(
        .p_BUF_DEPTH    (BUF_DEPTH),
        .p_PKT_SIZE_MAX (PKT_SIZE_MAX)
    ) cxp_ctrl_cmd_parser_i (
        .rx_clk                (rx_clk),
        .rx_rst_n              (rx_rst_n),
        .long_data_i           (long_data),
        .long_kmask_i          (long_kmask),
        .long_valid_i          (long_valid),
        .long_sop_i            (long_sop),
        .long_eop_i            (long_eop),
        .long_err_i            (long_err),
        .long_type_i           (long_type),
        .from_extension_link_i (from_extension_link),
        .cmd_valid_o           (cmd_out),
        .cmd_o                 (cmd),
        .cmd_full_i            (cmd_full),
        .wbuf_addr_i           ({wbuf_raw ? wbuf_bank : wbank_q, wbuf_addr}),
        .wbuf_data_o           (wbuf_data)
    );
endmodule

`default_nettype wire
