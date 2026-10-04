//-----------------------------------------------------------------------------
// cxp_reglog
//
// Opt-in register-access trace, bound into the control plane instead of
// living in the RTL.  Compile this file next to the design to get one line
// per control command record (cxp_ctrl_cmd_parser) and one line per register
// access (cxp_ctrl_bus_master); leave it out and nothing is printed.
//
//   src/emu/bridge:  make REGLOG=1 ...
//
// Each monitor samples on the same clock edge the RTL acts on, so the
// values printed are the ones the RTL used for that command / beat.
//-----------------------------------------------------------------------------
`default_nettype none

module cxp_reglog_cmd #(
    parameter int p_BUF_DEPTH = 64
) (
    input  wire  logic        clk,
    input  wire  logic        fire,          // one command record per packet
    input  wire  cxp_pkg::cxp_ctrl_cmd_t cmd,
    input  wire  logic [31:0] wbuf [2 * p_BUF_DEPTH]
);
    import cxp_pkg::*;

    always @(posedge clk) begin
        if (fire) begin
            string op_str;
            case (cmd.op)
                CTRL_OP_READ:  op_str = "READ";
                CTRL_OP_WRITE: op_str = "WRITE";
                CTRL_OP_RESET: op_str = "RESET";
                default:       op_str = "UNKNOWN";
            endcase
            $display("[%0t] cxp_ctrl_cmd_parser: RX cmd type=0x%02h (%s) addr=0x%08h size=%0dB%s%s",
                     $time, cmd.op, op_str, cmd.addr, cmd.size,
                     (cmd.err == ACK_ERR_CRC) ? " [CRC-ERR]" :
                     (cmd.err != 8'h00)       ? $sformatf(" [ANSWER 0x%02h]", cmd.err) : "",
                     (cmd.op == CTRL_OP_WRITE && cmd.nwords != 16'd0) ? "" : " (no data)");
            if (cmd.err == 8'h00 && cmd.op == CTRL_OP_WRITE) begin
                for (int di = 0; di < int'(cmd.nwords) && di < p_BUF_DEPTH; di++)
                    $display("[%0t] cxp_ctrl_cmd_parser:   data[%0d] = 0x%08h",
                             $time, di, wbuf[int'(cmd.wbank) * p_BUF_DEPTH + di]);
            end
        end
    end
endmodule

module cxp_reglog_bus (
    input  wire  logic        clk,
    input  wire  logic        beat,          // access answered
    input  wire  logic        is_write,
    input  wire  logic        to_user,       // user port, not the register file
    input  wire  logic [31:0] addr,
    input  wire  logic [31:0] wdata,
    input  wire  logic [31:0] rdata,
    input  wire  logic [7:0]  err
);
    always @(posedge clk) begin
        if (beat)
            $display("[%0t] cxp_ctrl_bus_master: %s%s addr=0x%08h data=0x%08h%s",
                     $time, is_write ? "WR" : "RD", to_user ? " user" : "", addr,
                     is_write ? wdata : rdata,
                     (err != 8'h00) ? $sformatf(" [ERR 0x%02h]", err) : "");
    end
endmodule

bind cxp_ctrl_cmd_parser cxp_reglog_cmd #(
    .p_BUF_DEPTH (p_BUF_DEPTH)
) u_reglog (
    .clk      (rx_clk),
    .fire     (cmd_valid_o),
    .cmd      (cmd_o),
    .wbuf     (wbuf)
);

bind cxp_ctrl_bus_master cxp_reglog_bus u_reglog (
    .clk      (clk),
    .beat     (state_q == ST_WAIT && ack),
    .is_write (is_write),
    .to_user  (to_user_q),
    .addr     (addr_q),
    .wdata    (wbuf_data_i),
    .rdata    (rdata),
    .err      (err)
);

`default_nettype wire
