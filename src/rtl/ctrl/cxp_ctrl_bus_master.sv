/*
================================================================================
  cxp_ctrl_bus_master
  CoaXPress 1.1.1 (CXP-001-2015) control-command executor (§8.6.1).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-15

    Description:
      Takes every command cxp_ctrl_cmd_parser decoded (valid ones and the
      ones it answered itself, err != 0) in order, executes it on the
      register bus and owns the one response register towards the
      acknowledgment framer.  Each command gets exactly one final
      response (§8.6.1.1), at most one Wait before it.

        err != 0                    -> that code, no access (0x80,
                                       0x42 .. 0x47, and 0x01 for a write
                                       an extension link ignores)
        0xFF control channel reset  -> abandons the command in progress
                                       and the one waiting, drops their
                                       responses not yet handed over, 0x03
                                       (§8.6.1.2)
        read / write                -> nwords single-word accesses; the
                                       first access with a non-zero code
                                       ends the command with it, otherwise
                                       0x00 with the data / 0x01

      Address decode: a word in [p_USER_BASE, p_USER_BASE + p_USER_SIZE)
      goes to the user window — an APB master (APB3 + PSTRB) through
      cxp_ctrl_apb_bridge, present only when p_USER_SIZE != 0 — every
      other word to the register file.  A write of B bytes writes only
      those bytes: wstrb on the register file, PSTRB on APB.

      One command at a time.  A command that arrives while one executes
      waits in a one-deep slot (a host re-sending after its 200 ms
      timeout, §8.6.1.1); cmd_full_o tells the parser, which then drops
      the next one (a 0xFF excepted) with no response.  The next command
      starts only once the response register is empty — its response
      has been handed to the framer — and the read data alternate
      between two banks of the read buffer, so a read never overwrites
      words the framer is still sending.

      Time limits (§8.6.1.1) run per command, in milliseconds of a
      p_CLK_KHZ clock, from its first access: after p_WAIT_AFTER_MS one
      Wait (0x04, carrying p_WAIT_MS); after p_TIMEOUT_MS the command ends
      with 0x40 (rsp_o.timeout) and the user-window access is abandoned,
      so its late answer never completes another command.  A 0xFF
      immediately abandons a user-window access; a bootstrap register
      access already issued is drained until it answers or times out.
      An abandoned APB transfer is not cut short (APB has no abort): it
      stays open until PREADY and its answer is discarded.  Meanwhile
      commands to the register file run as usual; a user-window access
      waits in ST_REQ until the bridge is idle, within its command's time
      limits (a slave that never answers costs each later user-window
      command its 0x40, nothing else).

    Versions:
        2026-05-15 - 0.1:   - Init (APB3 master)
        2026-09-19 - 0.2:   - Register bus with Table 22 error codes, user
                              window, Wait acknowledgment, access timeout,
                              0xFF abort; dual-clock read buffer
        2026-09-19 - 0.3:   - Time limits in ms of p_CLK_KHZ; timeout flag
        2026-09-22 - 0.4:   - Byte enables for the last word of a write
        2026-09-26 - 0.5:   - The executor: every command and its one
                              response register (the router folded in),
                              per-command time limits, drained accesses,
                              banked buffers, APB bridge inside
        2026-10-04 - 0.6:   - APB transfers held until PREADY when given
                              up; PSTRB; only user accesses wait for the
                              bridge

================================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_ctrl_bus_master
    import cxp_pkg::*;
#(
    parameter int          p_BUF_DEPTH     = 64,              // command buffer (words)
    parameter logic [31:0] p_USER_BASE     = 32'h0002_0000,   // user window base
    parameter logic [31:0] p_USER_SIZE     = 32'h0000_0000,   // user window bytes, 0 = off
    parameter int          p_CLK_KHZ       = 125_000,         // clk frequency (kHz)
    parameter int          p_WAIT_AFTER_MS = 100,             // command time before the Wait
    parameter int          p_TIMEOUT_MS    = 900,             // command time before 0x40
    parameter int          p_WAIT_MS       = 1000             // Wait ack payload (ms)
) (
    input  wire  logic                        clk,              // control-plane clock
    input  wire  logic                        rst_n,            // async reset, active-low

    // Commands from cxp_ctrl_cmd_parser, valid or refused (cmd_i.err)
    input  wire  logic                        cmd_valid_i,      // 1-cycle
    input  var   cxp_ctrl_cmd_t               cmd_i,
    output logic                              cmd_full_o,       // no room for another command

    // Write data from cxp_ctrl_cmd_parser: {bank, word}
    output logic [$clog2(p_BUF_DEPTH):0]      wbuf_addr_o,
    input  wire  logic [31:0]                 wbuf_data_i,      // 1-cycle latency

    // Register file port
    output logic                              reg_req_o,
    output logic                              reg_we_o,
    output logic [31:0]                       reg_addr_o,
    output logic [31:0]                       reg_wdata_o,
    output logic [3:0]                        reg_wstrb_o,      // byte enables
    input  wire  logic                        reg_ack_i,
    input  wire  logic [31:0]                 reg_rdata_i,
    input  wire  logic [7:0]                  reg_err_i,

    // User window: APB master, APB3 + PSTRB (PSLVERR answers 0x40)
    output logic                              apb_psel_o,
    output logic                              apb_penable_o,
    output logic                              apb_pwrite_o,
    output logic [31:0]                       apb_paddr_o,
    output logic [31:0]                       apb_pwdata_o,
    output logic [3:0]                        apb_pstrb_o,
    input  wire  logic [31:0]                 apb_prdata_i,
    input  wire  logic                        apb_pready_i,
    input  wire  logic                        apb_pslverr_i,

    // The response register, towards cxp_tx_ctrl_ack
    output logic                              rsp_valid_o,      // held until taken
    output cxp_ctrl_rsp_t                     rsp_o,
    input  wire  logic                        rsp_ready_i,      // 1-cycle: handed over

    // Read buffer {bank, word}, read from cxp_tx_ctrl_ack's clock domain
    input  wire  logic                        rbuf_clk,
    input  wire  logic [$clog2(p_BUF_DEPTH):0] rbuf_addr_i,
    output logic [31:0]                       rbuf_data_o,      // 1-cycle latency

    // Status
    output logic                              ctrl_reset_pulse_o, // 0xFF executed
    output logic                              nack_pulse_o,     // a code decided without
    output logic [7:0]                        nack_code_o       //   an access (0x4x, 0x80)
);

    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    localparam int BUF_AW      = idx_w(p_BUF_DEPTH);
    localparam int WAIT_CYCLES = p_CLK_KHZ * p_WAIT_AFTER_MS;
    localparam int BUS_TIMEOUT = p_CLK_KHZ * p_TIMEOUT_MS;
    localparam int TW          = cnt_w(BUS_TIMEOUT);

    // ------ Types ------

    typedef enum logic [2:0] {
        ST_IDLE,      // next command, once the response register is free
        ST_FETCH,     // write: wbuf word in flight
        ST_REQ,       // issue the access
        ST_WAIT,      // access outstanding
        ST_DRAIN      // command over, its access still outstanding
    } state_t;

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    // §8.6.1.1: one Wait within 200 ms, then the final ack within the
    // time the Wait announced (100 ms .. 10 s, Table 22).
    if (p_WAIT_AFTER_MS < 1 || p_WAIT_AFTER_MS >= 200) begin : g_chk_wait_after
        $error("cxp_ctrl_bus_master: p_WAIT_AFTER_MS (=%0d) must be 1..199", p_WAIT_AFTER_MS);
    end

    if (p_BUF_DEPTH < 2 || (p_BUF_DEPTH & (p_BUF_DEPTH - 1)) != 0) begin : g_chk_buf_depth
        $error("cxp_ctrl_bus_master: p_BUF_DEPTH (=%0d) must be a power of two >= 2 (banked)",
               p_BUF_DEPTH);
    end

    if (p_WAIT_MS < 100 || p_WAIT_MS > 10_000) begin : g_chk_wait_ms
        $error("cxp_ctrl_bus_master: p_WAIT_MS (=%0d) must be 100..10000", p_WAIT_MS);
    end

    if (p_TIMEOUT_MS <= p_WAIT_AFTER_MS || p_TIMEOUT_MS >= p_WAIT_AFTER_MS + p_WAIT_MS)
    begin : g_chk_timeout
        $error("cxp_ctrl_bus_master: p_TIMEOUT_MS (=%0d) must lie between the Wait and its end",
               p_TIMEOUT_MS);
    end

    //=======================================================================
    // Signals
    //=======================================================================

    state_t        state_q;
    cxp_ctrl_cmd_t cur_q;          // command executing
    logic          pend_valid_q;   // a command waits
    cxp_ctrl_cmd_t pend_q;
    logic [15:0]   idx_q;          // word index
    logic [31:0]   addr_q;         // current word address
    logic          to_user_q;      // current access goes to the user window
    logic [TW-1:0] cmd_t_q;        // cycles since the command's first access
    logic          rbank_q;        // bank of the last read
    logic          wait_pend_q;    // a Wait wants the response register
    logic          fin_pend_q;     // a final response wants it
    cxp_ctrl_rsp_t fin_q;
    logic          rst_pend_q;     // a 0x03 wants it
    logic          rsp_valid_q;
    cxp_ctrl_rsp_t rsp_q;

    logic          is_write;
    logic          last;
    logic          ack;            // the addressed slave answered
    logic [31:0]   rdata;
    logic [7:0]    err;
    logic          timeout;
    logic          rd_ok;          // a read that completed without error
    logic [3:0]    wstrb;
    logic          user_hit;       // cur_q.addr in the user window
    logic          user_next;      // addr_q + 4 in the user window
    logic          new_reset;      // a 0xFF arrives
    logic          new_cmd;        // any other command arrives
    logic          running;        // executing a command's accesses
    logic          start;          // the waiting command starts now
    logic          free;           // nothing of the last command still to hand over
    logic          load_rsp;       // the response register takes a response

    logic          usr_req, usr_ack, usr_abort;
    logic          usr_busy;       // the APB bridge still has a transfer open
    logic          issued;         // the access of ST_REQ is taken this cycle
    logic [31:0]   usr_rdata;
    logic [7:0]    usr_err;

    logic [31:0]   rbuf [2 * p_BUF_DEPTH];

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign is_write  = (cur_q.op == CTRL_OP_WRITE);
    assign last      = (idx_q + 16'd1 >= cur_q.nwords);
    assign rd_ok     = ~is_write & (err == 8'h00);
    assign ack       = to_user_q ? usr_ack   : reg_ack_i;
    assign rdata     = to_user_q ? usr_rdata : reg_rdata_i;
    assign err       = to_user_q ? usr_err   : reg_err_i;
    assign timeout   = (cmd_t_q == TW'(BUS_TIMEOUT));
    assign running     = (state_q == ST_FETCH) | (state_q == ST_REQ) | (state_q == ST_WAIT);

    assign new_reset = cmd_valid_i & (cmd_i.err == 8'h00) & (cmd_i.op == CTRL_OP_RESET);
    assign new_cmd   = cmd_valid_i & ~new_reset;
    assign free      = ~rsp_valid_q & ~fin_pend_q & ~wait_pend_q & ~rst_pend_q;
    // A user-window access waits in ST_REQ while an abandoned transfer is
    // still open (usr_busy); a command does not wait for the bridge.
    assign start     = (state_q == ST_IDLE) & pend_valid_q & free & ~new_reset;
    assign issued    = ~(to_user_q & usr_busy);
    // Full: a command waits and does not start now.
    assign cmd_full_o = pend_valid_q & ~start;

    assign wbuf_addr_o = {cur_q.wbank, idx_q[BUF_AW-1:0]};

    assign reg_req_o   = (state_q == ST_REQ) & ~to_user_q & ~new_reset;
    assign usr_req     = (state_q == ST_REQ) &  to_user_q & ~new_reset;
    assign reg_we_o    = is_write;
    assign reg_addr_o  = addr_q;
    // wbuf_data_i is wbuf[idx_q] from ST_REQ on (idx_q is stable).
    assign reg_wdata_o = wbuf_data_i;

    // B bytes from the lowest address: the last word keeps B mod 4 bytes.
    always_comb begin
        wstrb = 4'hF;
        if (last) begin
            unique case (cur_q.size[1:0])
                2'd1:    wstrb = 4'b1000;
                2'd2:    wstrb = 4'b1100;
                2'd3:    wstrb = 4'b1110;
                default: wstrb = 4'hF;
            endcase
        end
    end
    assign reg_wstrb_o = wstrb;

    // User-window hits.  Unsigned wrap: an address below the base gives a
    // huge offset.
    if (p_USER_SIZE == 32'h0) begin : g_no_user
        assign user_hit  = 1'b0;
        assign user_next = 1'b0;
    end else begin : g_user
        assign user_hit  = (pend_q.addr - p_USER_BASE) < p_USER_SIZE;
        assign user_next = (addr_q + 32'd4 - p_USER_BASE) < p_USER_SIZE;
    end

    // A user-window access given up: at the command's timeout, or at the
    // end of a drain.
    assign usr_abort = to_user_q & ~ack &
                       ((new_reset & (state_q == ST_WAIT)) |
                        (timeout & ((state_q == ST_WAIT) | (state_q == ST_DRAIN))));

    // The response register, reloaded in the cycle it is taken: a 0x03
    // first, then a Wait, then the final.
    assign load_rsp    = (~rsp_valid_q | rsp_ready_i) & (rst_pend_q | wait_pend_q | fin_pend_q);
    assign rsp_valid_o = rsp_valid_q;
    assign rsp_o       = rsp_q;

    //=======================================================================
    // Read buffer, two banks (write: this clock; read: rbuf_clk)
    //=======================================================================

    always_ff @(posedge clk) begin
        if ((state_q == ST_WAIT) && ack && rd_ok)
            rbuf[{rbank_q, idx_q[BUF_AW-1:0]}] <= rdata;
    end

    always_ff @(posedge rbuf_clk) begin
        rbuf_data_o <= rbuf[rbuf_addr_i];
    end

    //=======================================================================
    // Commands, execution, responses
    //=======================================================================

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state_q            <= ST_IDLE;
            cur_q              <= '0;
            pend_valid_q       <= 1'b0;
            pend_q             <= '0;
            idx_q              <= 16'd0;
            addr_q             <= 32'h0;
            to_user_q          <= 1'b0;
            cmd_t_q            <= '0;
            rbank_q            <= 1'b0;
            wait_pend_q        <= 1'b0;
            fin_pend_q         <= 1'b0;
            fin_q              <= '0;
            rst_pend_q         <= 1'b0;
            rsp_valid_q        <= 1'b0;
            rsp_q              <= '0;
            ctrl_reset_pulse_o <= 1'b0;
            nack_pulse_o       <= 1'b0;
            nack_code_o        <= 8'h00;
        end else begin
            ctrl_reset_pulse_o <= 1'b0;
            nack_pulse_o       <= 1'b0;

            //---- Response register ----------------------------------------
            if (rsp_valid_q && rsp_ready_i) rsp_valid_q <= 1'b0;
            if (load_rsp) begin
                rsp_valid_q <= 1'b1;
                rsp_q       <= '0;
                if (rst_pend_q) begin
                    rsp_q.code  <= ACK_RESET_DONE;
                    rst_pend_q  <= 1'b0;
                end else if (wait_pend_q) begin
                    rsp_q.code    <= ACK_WAIT;
                    rsp_q.nwords  <= 16'd1;
                    rsp_q.size    <= 24'd4;
                    rsp_q.wait_ms <= 32'(p_WAIT_MS);
                    wait_pend_q   <= 1'b0;
                end else begin
                    rsp_q      <= fin_q;
                    fin_pend_q <= 1'b0;
                end
            end

            //---- One-deep command slot -------------------------------------
            if (start) pend_valid_q <= 1'b0;
            if (new_cmd && (!pend_valid_q || start)) begin
                pend_valid_q <= 1'b1;
                pend_q       <= cmd_i;
            end

            //---- Execution ------------------------------------------------
            // The command's time runs every cycle from its first access to
            // its end (or the end of a drain), up to the timeout.
            if ((running || state_q == ST_DRAIN) && !timeout) cmd_t_q <= cmd_t_q + 1'b1;

            unique case (state_q)
                ST_IDLE: begin
                    if (start) begin
                        cur_q     <= pend_q;
                        idx_q     <= 16'd0;
                        addr_q    <= pend_q.addr;
                        to_user_q <= user_hit;
                        cmd_t_q   <= '0;
                        fin_q     <= '0;
                        if (pend_q.err != 8'h00) begin
                            // Answered by the parser: its code, no access.
                            fin_pend_q   <= 1'b1;
                            fin_q.code   <= pend_q.err;
                            nack_pulse_o <= (pend_q.err != ACK_WRITE_OK);
                            nack_code_o  <= pend_q.err;
                        end else begin
                            if (pend_q.op != CTRL_OP_WRITE) rbank_q <= ~rbank_q;
                            state_q <= (pend_q.op == CTRL_OP_WRITE) ? ST_FETCH : ST_REQ;
                        end
                    end
                end

                ST_FETCH: begin
                    // wbuf_addr_o = {bank, idx_q}; its data arrive next cycle.
                    state_q <= ST_REQ;
                end

                ST_REQ: begin
                    if (issued) state_q <= ST_WAIT;
                end

                ST_WAIT: begin
                    if (ack) begin
                        if (err != 8'h00 || last) begin
                            fin_pend_q    <= 1'b1;
                            fin_q.code    <= (err != 8'h00) ? err
                                           : is_write       ? ACK_WRITE_OK
                                           :                  ACK_OK;
                            fin_q.nwords  <= rd_ok ? cur_q.nwords : 16'd0;
                            fin_q.size    <= rd_ok ? cur_q.size   : 24'd0;
                            fin_q.rbank   <= rbank_q;
                            state_q       <= ST_IDLE;
                        end else if (timeout) begin
                            // Out of time between words: no further access.
                            fin_pend_q    <= 1'b1;
                            fin_q         <= '0;
                            fin_q.code    <= ACK_ERR_BAD_ADDR;
                            fin_q.timeout <= 1'b1;
                            state_q       <= ST_IDLE;
                        end else begin
                            idx_q     <= idx_q + 16'd1;
                            addr_q    <= addr_q + 32'd4;
                            to_user_q <= user_next;
                            state_q   <= is_write ? ST_FETCH : ST_REQ;
                        end
                    end
                end

                ST_DRAIN: begin
                    // The abandoned access answers, or is given up at the
                    // command's timeout (usr_abort).
                    if (ack || timeout) state_q <= ST_IDLE;
                end

                default: state_q <= ST_IDLE;
            endcase

            //---- Per-command time limits (§8.6.1.1) -----------------------
            // One Wait per command: the time passes WAIT_CYCLES once.  The
            // timeout ends the command only while an access is unanswered,
            // or a user-window access still waits for the bridge (never in
            // the cycle an access is issued); between words it ends it in
            // ST_WAIT above.
            if (running && !new_reset) begin
                if (cmd_t_q == TW'(WAIT_CYCLES)) wait_pend_q <= 1'b1;
                if (timeout && ((state_q == ST_WAIT && !ack)
                                || (state_q == ST_REQ && !issued))) begin
                    fin_pend_q    <= 1'b1;
                    fin_q         <= '0;
                    fin_q.code    <= ACK_ERR_BAD_ADDR;
                    fin_q.timeout <= 1'b1;
                    state_q       <= ST_IDLE;
                end
            end

            //---- §8.6.1.2 control channel reset ----------------------------
            // Everything of the abandoned commands not yet handed over is
            // dropped; the 0x03 goes out after what already was.
            if (new_reset) begin
                pend_valid_q       <= 1'b0;
                wait_pend_q        <= 1'b0;
                fin_pend_q         <= 1'b0;
                rst_pend_q         <= 1'b1;
                ctrl_reset_pulse_o <= 1'b1;
                // Abandon a user-window access immediately (its APB
                // transfer runs to PREADY unseen); drain a register-file
                // access.
                if (to_user_q || state_q != ST_DRAIN)
                    state_q <= ((state_q == ST_WAIT) && !ack && !to_user_q && !timeout)
                             ? ST_DRAIN : ST_IDLE;
            end
        end
    end

    //=======================================================================
    // User window: APB bridge
    //=======================================================================

    if (p_USER_SIZE != 32'h0) begin : g_apb
        cxp_ctrl_apb_bridge #(
            .p_SLVERR_CODE (ACK_ERR_BAD_ADDR)
        ) cxp_ctrl_apb_bridge_i (
            .clk       (clk),
            .rst_n     (rst_n),
            .abort_i   (usr_abort),
            .req_i     (usr_req),
            .we_i      (is_write),
            .addr_i    (addr_q),
            .wdata_i   (wbuf_data_i),
            .wstrb_i   (wstrb),
            .ack_o     (usr_ack),
            .rdata_o   (usr_rdata),
            .err_o     (usr_err),
            .psel_o    (apb_psel_o),
            .penable_o (apb_penable_o),
            .pwrite_o  (apb_pwrite_o),
            .paddr_o   (apb_paddr_o),
            .pwdata_o  (apb_pwdata_o),
            .pstrb_o   (apb_pstrb_o),
            .prdata_i  (apb_prdata_i),
            .pready_i  (apb_pready_i),
            .pslverr_i (apb_pslverr_i),
            .busy_o    (usr_busy)
        );
    end else begin : g_no_apb
        assign usr_ack       = 1'b0;
        assign usr_rdata     = 32'h0;
        assign usr_err       = 8'h00;
        assign apb_psel_o    = 1'b0;
        assign apb_penable_o = 1'b0;
        assign apb_pwrite_o  = 1'b0;
        assign apb_paddr_o   = 32'h0;
        assign apb_pwdata_o  = 32'h0;
        assign apb_pstrb_o   = 4'h0;
        assign usr_busy      = 1'b0;
    end

endmodule

`default_nettype wire
