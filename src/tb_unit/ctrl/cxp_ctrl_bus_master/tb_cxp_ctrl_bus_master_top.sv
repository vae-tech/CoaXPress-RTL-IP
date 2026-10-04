//-----------------------------------------------------------------------------
// tb_cxp_ctrl_bus_master_top
//
// Cocotb wrapper for `cxp_ctrl_bus_master`, the control executor: struct
// ports as separate fields, a 16-word buffer, a 4 KiB user window at
// 0x20000, and a 1 kHz clock parameter so the Wait / timeout limits
// (20 / 50 ms) are 20 / 50 cycles and the slow-slave paths run quickly.
//
// The bench keeps the executor's earlier register-bus view of the user
// window: the executor's APB3 master is answered by a shim that shows each
// transfer as one `usr_req` cycle (SETUP) and completes it on `usr_ack`
// (PSLVERR when `usr_err` != 0).  `abort` sends a 0xFF command.  `busy`
// is 1 while the executor has a command, a waiting command or a response
// not yet taken; `wait_pulse` marks a Wait response being taken, and
// `wait_ms` holds the last Wait's payload.  The read buffer is read on the
// same clock, `rbuf_bank` selecting the bank.  Top-level `TESTCASE` is
// written by Python.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_ctrl_bus_master_top #(
    parameter int BUF_DEPTH = 16
) (
    input  wire  logic        clk,
    input  wire  logic        rst_n,

    input  wire  logic        cmd_valid,
    input  wire  logic [7:0]  cmd_op,
    input  wire  logic [23:0] cmd_size,
    input  wire  logic [31:0] cmd_addr,
    input  wire  logic [15:0] cmd_nwords,
    input  wire  logic [7:0]  cmd_err,
    input  wire  logic        cmd_wbank,
    input  wire  logic        abort,
    output logic              cmd_full,
    output logic              busy,

    output logic              rsp_valid,
    output logic [7:0]        rsp_code,
    output logic [15:0]       rsp_nwords,
    output logic [23:0]       rsp_size,
    output logic              rsp_timeout,
    output logic              rsp_rbank,
    input  wire  logic        rsp_ready,
    output logic              wait_pulse,
    output logic [31:0]       wait_ms,
    output logic              ctrl_reset_pulse,
    output logic              nack_pulse,
    output logic [7:0]        nack_code,

    output logic [$clog2(BUF_DEPTH):0] wbuf_addr,
    input  wire  logic [31:0] wbuf_data,

    output logic              reg_req,
    output logic              reg_we,
    output logic [31:0]       reg_addr,
    output logic [31:0]       reg_wdata,
    input  wire  logic        reg_ack,
    input  wire  logic [31:0] reg_rdata,
    input  wire  logic [7:0]  reg_err,

    output logic              usr_req,
    output logic              usr_we,
    output logic [31:0]       usr_addr,
    output logic [31:0]       usr_wdata,
    output logic [3:0]        usr_wstrb,      // PSTRB
    input  wire  logic        usr_ack,
    input  wire  logic [31:0] usr_rdata,
    input  wire  logic [7:0]  usr_err,
    output logic              usr_open,       // an APB transfer is in progress

    input  wire  logic        rbuf_bank,
    input  wire  logic [$clog2(BUF_DEPTH)-1:0] rbuf_addr,
    output logic [31:0]       rbuf_data
);
    import cxp_pkg::*;

    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    cxp_ctrl_cmd_t cmd;
    cxp_ctrl_rsp_t rsp;
    logic          psel, penable, pwrite;
    logic [31:0]   paddr, pwdata;
    logic [3:0]    pstrb;
    logic          executing;

    always_comb begin
        cmd        = '0;
        cmd.op     = abort ? CTRL_OP_RESET : cmd_op;
        cmd.size   = cmd_size;
        cmd.addr   = cmd_addr;
        cmd.nwords = cmd_nwords;
        cmd.err    = abort ? 8'h00 : cmd_err;
        cmd.wbank  = cmd_wbank;
    end

    assign rsp_code    = rsp.code;
    assign rsp_nwords  = rsp.nwords;
    assign rsp_size    = rsp.size;
    assign rsp_timeout = rsp.timeout;
    assign rsp_rbank   = rsp.rbank;
    assign wait_pulse  = rsp_valid & rsp_ready & (rsp.code == ACK_WAIT);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)                                 wait_ms <= 32'h0;
        else if (rsp_valid && rsp.code == ACK_WAIT) wait_ms <= rsp.wait_ms;
    end

    // Executor state, read through the hierarchy for `busy`.
    assign executing = (cxp_ctrl_bus_master_i.state_q != 3'd0)   // ST_IDLE
                     | ~cxp_ctrl_bus_master_i.free | cxp_ctrl_bus_master_i.pend_valid_q;
    assign busy      = executing;

    // APB shim: SETUP is the request cycle; PREADY is the slave's ack.
    assign usr_req   = psel & ~penable;
    assign usr_we    = pwrite;
    assign usr_addr  = paddr;
    assign usr_wdata = pwdata;
    assign usr_wstrb = pstrb;
    assign usr_open  = psel;

    cxp_ctrl_bus_master #(
        .p_BUF_DEPTH     (BUF_DEPTH),
        .p_USER_BASE     (32'h0002_0000),
        .p_USER_SIZE     (32'h0000_1000),
        .p_CLK_KHZ       (1),
        .p_WAIT_AFTER_MS (20),
        .p_TIMEOUT_MS    (50),
        .p_WAIT_MS       (1234)
    ) cxp_ctrl_bus_master_i (
        .clk                (clk),
        .rst_n              (rst_n),
        .cmd_valid_i        (cmd_valid | abort),
        .cmd_i              (cmd),
        .cmd_full_o         (cmd_full),
        .wbuf_addr_o        (wbuf_addr),
        .wbuf_data_i        (wbuf_data),
        .reg_req_o          (reg_req),
        .reg_we_o           (reg_we),
        .reg_addr_o         (reg_addr),
        .reg_wdata_o        (reg_wdata),
        .reg_wstrb_o        (),
        .reg_ack_i          (reg_ack),
        .reg_rdata_i        (reg_rdata),
        .reg_err_i          (reg_err),
        .apb_psel_o         (psel),
        .apb_penable_o      (penable),
        .apb_pwrite_o       (pwrite),
        .apb_paddr_o        (paddr),
        .apb_pwdata_o       (pwdata),
        .apb_pstrb_o        (pstrb),
        .apb_prdata_i       (usr_rdata),
        .apb_pready_i       (psel & penable & usr_ack),
        .apb_pslverr_i      (usr_err != 8'h00),
        .rsp_valid_o        (rsp_valid),
        .rsp_o              (rsp),
        .rsp_ready_i        (rsp_ready),
        .rbuf_clk           (clk),
        .rbuf_addr_i        ({rbuf_bank, rbuf_addr}),
        .rbuf_data_o        (rbuf_data),
        .ctrl_reset_pulse_o (ctrl_reset_pulse),
        .nack_pulse_o       (nack_pulse),
        .nack_code_o        (nack_code)
    );
endmodule

`default_nettype wire
