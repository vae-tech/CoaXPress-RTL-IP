/*
================================================================================
  cxp_sva
  Bound SVA contracts for the CoaXPress device IP.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-19

    Description:
      Properties the RTL guarantees on its own, whatever its inputs do, so
      they hold in every bench.  Each checker is bound into the module it
      watches; compile this file after src/rtl/cxp_ip.f (src/sva/cxp_sva.f)
      and build with assertions on (Verilator --assert).

        cxp_arbiter_sva      one long-packet source taken per word
        cxp_inserter_sva     a two-word packet is never split (a trigger
                             leader is followed by its Delay word); an I/O
                             acknowledgment offered is on its way within
                             3 words; at most 99 words between IDLEs
        cxp_tx_owner_sva     (in cxp_interface_top) the long packet that
                             owns the arbiter offers a word every cycle
        cxp_framer_sva       SOP / EOP only on valid words; no second SOP
                             inside a packet; header, CRC and EOP words hold
                             while not accepted
        cxp_short_pkt_sva    a word that is not accepted is held
        cxp_idle_rule_sva    §8.2.5.1: an IDLE word at least every 100 words
                             on the downlink
        cxp_cdc_stream_fifo_sva  the FIFO never holds more than p_DEPTH words and
                             never pops when empty (while both sides are up)
        cxp_rxlong_sva       uplink long packets: every SOP is closed by one
                             EOP before the next SOP; err only inside a
                             packet
        cxp_link_mon_sva     uplink framing: locked, at most p_BAD_WORDS
                             errored words without a clean IDLE before a
                             resync; every resync flushes
        cxp_ctrl_exec_sva    control executor: a command (and a drained
                             access) ends within the bus timeout, at most
                             one Wait per command and none after a 0xFF,
                             the response holds until taken, a waiting
                             command starts once the executor is free
        cxp_conn_reset_sva   ConnectionReset bit clears within its timeout
        cxp_reset_order_sva  domain resets are released rx, then tx, then
                             app, and asserted together

    Versions:
        2026-09-19 - 0.1:   - Init
        2026-09-19 - 0.2:   - cxp_rxlong_sva
        2026-09-19 - 0.3:   - Control-plane wait bounds
        2026-09-25 - 0.4:   - CXP_SVA_FAIL: fatal where the simulator
                              does not stop on $error
        2026-09-25 - 0.5:   - ConnectionReset lifetime, reset release order
        2026-09-26 - 0.6:   - Control executor owns the responses; router
                              properties folded into it
        2026-09-26 - 0.7:   - Transmit scheduler: long-packet arbiter,
                              inserter, owner liveness
        2026-09-26 - 0.8:   - Uplink framing bound (cxp_link_mon_sva)
        2026-10-04 - 0.9:   - cxp_link_mon_sva counts errored words in
                              both states (clean data between sparse IDLEs
                              is legal, §8.2.5.1)

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

// Failure action of every property.  Verilator (--assert) stops on the
// first $error; other simulators only log it, so a build that defines
// CXP_SVA_FATAL (the Questa flow in src/verif/common/cocotb_sim.mk) makes
// every failure fatal and the SVA gate holds on both.
`ifdef CXP_SVA_FATAL
`define CXP_SVA_FAIL $fatal(1,
`else
`define CXP_SVA_FAIL $error(
`endif

//=============================================================================
// TX arbiter
//=============================================================================

module cxp_arbiter_sva #(
    parameter int p_PORTS = 3
) (
    input  wire  logic               clk,
    input  wire  logic               rst_n,
    input  wire  logic [p_PORTS-1:0] ready
);
    a_one_grant: assert property (@(posedge clk) disable iff (!rst_n)
        $onehot0(ready))
        else `CXP_SVA_FAIL "cxp_tx_arbiter: more than one source taken");
endmodule

//=============================================================================
// TX inserter
//=============================================================================

module cxp_inserter_sva (
    input  wire  logic        clk,
    input  wire  logic        rst_n,
    input  wire  logic        trig_valid,
    input  wire  logic        trig_sop,
    input  wire  logic        trig_eop,
    input  wire  logic        trig_ready,
    input  wire  logic        ioack_valid,
    input  wire  logic        ioack_sop,
    input  wire  logic        ioack_eop,
    input  wire  logic        ioack_ready,
    input  wire  logic [6:0]  run
);
    // Table 16: the Delay word follows its leader on the next word.
    a_trig_contiguous: assert property (@(posedge clk) disable iff (!rst_n)
        (trig_ready && trig_sop && !trig_eop) |=> (trig_ready && trig_valid && trig_eop))
        else `CXP_SVA_FAIL "cxp_tx_inserter: trigger leader not followed by its Delay word");

    // Table 17: the code word follows its leader on the next word.
    a_ioack_contiguous: assert property (@(posedge clk) disable iff (!rst_n)
        (ioack_ready && ioack_sop && !ioack_eop) |=> (ioack_ready && ioack_valid && ioack_eop))
        else `CXP_SVA_FAIL "cxp_tx_inserter: I/O-ack leader not followed by its code word");

    // §8.2.4 / §8.3.3: an I/O acknowledgment is inserted, not queued —
    // its leader is sent at most 3 words after it is first offered.
    logic [2:0] ioack_wait_q;               // words the offered leader has waited

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)                                     ioack_wait_q <= '0;
        else if (ioack_valid && ioack_sop && !ioack_ready)
            ioack_wait_q <= (ioack_wait_q == 3'd7) ? ioack_wait_q : ioack_wait_q + 3'd1;
        else                                            ioack_wait_q <= '0;
    end

    a_ioack_within_3: assert property (@(posedge clk) disable iff (!rst_n)
        ioack_wait_q <= 3'd3)
        else `CXP_SVA_FAIL "cxp_tx_inserter: I/O acknowledgment waited more than 3 words");

    // §8.2.5.1: an IDLE at least every 100 words.
    a_run: assert property (@(posedge clk) disable iff (!rst_n)
        run <= 7'(cxp_pkg::IDLE_MAX_INTERVAL - 1))
        else `CXP_SVA_FAIL "cxp_tx_inserter: %0d words without an IDLE", run);
endmodule

//=============================================================================
// Long packet in flight offers a word every cycle (store and forward)
//=============================================================================

module cxp_tx_owner_sva #(
    parameter int p_PORTS = 3
) (
    input  wire  logic               clk,
    input  wire  logic               rst_n,
    input  wire  logic [1:0]         owner,         // port + 1, 0 = between packets
    input  wire  logic [p_PORTS-1:0] valid
);
    // The bind in cxp_tx_domain lists the three valids by hand.
    if (p_PORTS != 3) begin : g_chk_ports
        $error("cxp_tx_owner_sva: bound for 3 long-packet ports, got %0d", p_PORTS);
    end

    a_owner_valid: assert property (@(posedge clk) disable iff (!rst_n)
        (owner != 2'd0) |-> valid[owner - 2'd1])
        else `CXP_SVA_FAIL "cxp_tx_arbiter: port %0d owns the arbiter and offers no word", owner - 2'd1);
endmodule

//=============================================================================
// Long-packet framer
//=============================================================================

module cxp_framer_sva (
    input  wire  logic        clk,
    input  wire  logic        rst_n,
    input  wire  logic        in_data,      // payload passes through
    input  wire  logic [31:0] data,
    input  wire  logic [3:0]  kmask,
    input  wire  logic        valid,
    input  wire  logic        sop,
    input  wire  logic        eop,
    input  wire  logic        ready
);
    logic in_pkt_q;                         // SOP accepted, EOP not yet

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)                            in_pkt_q <= 1'b0;
        else if (valid && ready && eop)        in_pkt_q <= 1'b0;
        else if (valid && ready && sop)        in_pkt_q <= 1'b1;
    end

    a_flags_on_valid: assert property (@(posedge clk) disable iff (!rst_n)
        (sop || eop) |-> valid)
        else `CXP_SVA_FAIL "cxp_tx_pkt_framer: SOP/EOP on an invalid word");

    a_no_sop_in_packet: assert property (@(posedge clk) disable iff (!rst_n)
        in_pkt_q |-> !sop)
        else `CXP_SVA_FAIL "cxp_tx_pkt_framer: SOP inside a packet");

    a_hold_until_ready: assert property (@(posedge clk) disable iff (!rst_n)
        (valid && !ready && !in_data) |=>
            (valid && $stable(data) && $stable(kmask) && $stable(sop) && $stable(eop)))
        else `CXP_SVA_FAIL "cxp_tx_pkt_framer: word changed before it was accepted");
endmodule

//=============================================================================
// Two-word short-packet source
//=============================================================================

module cxp_short_pkt_sva (
    input  wire  logic        clk,
    input  wire  logic        rst_n,
    input  wire  logic [31:0] data,
    input  wire  logic [3:0]  kmask,
    input  wire  logic        valid,
    input  wire  logic        ready
);
    a_hold_until_ready: assert property (@(posedge clk) disable iff (!rst_n)
        (valid && !ready) |=> (valid && $stable(data) && $stable(kmask)))
        else `CXP_SVA_FAIL "cxp_tx_short_pkt: word changed before it was accepted");
endmodule

//=============================================================================
// §8.2.5.1 IDLE rule on the downlink
//=============================================================================

module cxp_idle_rule_sva #(
    parameter int p_MAX_RUN = cxp_pkg::IDLE_MAX_INTERVAL - 1
) (
    input  wire  logic        clk,
    input  wire  logic        rst_n,
    input  wire  logic [31:0] data,
    input  wire  logic [3:0]  kmask
);
    logic       is_idle;
    int unsigned run_q;                     // non-IDLE words since the last IDLE

    assign is_idle = (data == cxp_pkg::IDLE_WORD) && (kmask == cxp_pkg::KMASK_IDLE);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)       run_q <= 0;
        else if (is_idle) run_q <= 0;
        else              run_q <= run_q + 1;
    end

    a_idle_interval: assert property (@(posedge clk) disable iff (!rst_n)
        run_q <= p_MAX_RUN)
        else `CXP_SVA_FAIL "downlink: %0d words without an IDLE (§8.2.5.1)", run_q);
endmodule

//=============================================================================
// Stream FIFO
//=============================================================================

module cxp_cdc_stream_fifo_sva #(
    parameter int p_DEPTH = 4096,
    parameter int p_PTR_W = 13
) (
    input  wire  logic               app_clk,
    input  wire  logic               app_rst_n,
    input  wire  logic               tx_clk,
    input  wire  logic               tx_rst_n,
    input  wire  logic [p_PTR_W-1:0] fill_wr,
    input  wire  logic               emit_word,
    input  wire  logic               empty_rd,
    input  wire  logic               app_ok      // both sides up (not in a one-sided reset)
);
    // While one side is in reset the other side's view of its pointer is
    // stale by design; the writer stalls and the reader discards then.
    a_no_overflow: assert property (@(posedge app_clk) disable iff (!app_rst_n || !app_ok)
        fill_wr <= p_PTR_W'(p_DEPTH))
        else `CXP_SVA_FAIL "cxp_cdc_stream_fifo: %0d words in a %0d-word FIFO", fill_wr, p_DEPTH);

    a_no_underflow: assert property (@(posedge tx_clk) disable iff (!tx_rst_n)
        emit_word |-> !empty_rd)
        else `CXP_SVA_FAIL "cxp_cdc_stream_fifo: pop from an empty FIFO");
endmodule

//=============================================================================
// Uplink framing (cxp_rx_link_mon)
//=============================================================================

module cxp_link_mon_sva #(
    parameter int p_BAD_WORDS = 32
) (
    input  wire  logic clk,
    input  wire  logic rst_n,
    input  wire  logic rx_lock,
    input  wire  logic valid,
    input  wire  logic err,         // code or disparity error in this word
    input  wire  logic idle,        // this word is a clean IDLE
    input  wire  logic resync,
    input  wire  logic flush
);
    // Errored words seen locked since the last clean IDLE, counted here
    // independently of the monitor's own counter.  Clean words that are
    // not IDLE do not count: a low-speed host may send 10 000 of them
    // between IDLEs (§8.2.5.1).
    int bad_q;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)                              bad_q <= 0;
        else if (!rx_lock || resync || idle)     bad_q <= 0;
        else if (valid && err)                   bad_q <= bad_q + 1;
    end

    a_errors_bounded: assert property (@(posedge clk) disable iff (!rst_n)
        bad_q <= p_BAD_WORDS)
        else `CXP_SVA_FAIL "cxp_rx_link_mon: locked, %0d errored words without a resync",
                           bad_q);

    a_resync_flush: assert property (@(posedge clk) disable iff (!rst_n)
        resync |-> flush)
        else `CXP_SVA_FAIL "cxp_rx_link_mon: resync without flush");
endmodule

//=============================================================================
// Uplink long-packet stream (cxp_rx_packet_parser -> consumers)
//=============================================================================

module cxp_rxlong_sva (
    input  wire  logic clk,
    input  wire  logic rst_n,
    input  wire  logic valid,
    input  wire  logic sop,
    input  wire  logic eop,
    input  wire  logic err
);
    logic in_pkt_q;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)              in_pkt_q <= 1'b0;
        else if (valid && sop)   in_pkt_q <= 1'b1;
        else if (valid && eop)   in_pkt_q <= 1'b0;
    end

    a_sop_outside: assert property (@(posedge clk) disable iff (!rst_n)
        (valid && sop) |-> !in_pkt_q)
        else `CXP_SVA_FAIL "cxp_rx_packet_parser: SOP inside a packet (previous one not closed)");

    a_eop_inside: assert property (@(posedge clk) disable iff (!rst_n)
        (valid && eop) |-> in_pkt_q)
        else `CXP_SVA_FAIL "cxp_rx_packet_parser: EOP outside a packet");

    a_err_inside: assert property (@(posedge clk) disable iff (!rst_n)
        (valid && err) |-> in_pkt_q)
        else `CXP_SVA_FAIL "cxp_rx_packet_parser: error flag outside a packet");
endmodule

//=============================================================================
// Control-command executor
//=============================================================================

module cxp_ctrl_exec_sva #(
    parameter int p_LIMIT = 1                // command timeout, cycles
) (
    input  wire  logic        clk,
    input  wire  logic        rst_n,
    input  wire  logic        busy,          // accesses running or draining
    input  wire  logic        start,         // a command starts
    input  wire  logic        pending,       // a command waits
    input  wire  logic        free,          // nothing left to hand over
    input  wire  logic        idle,          // no command executing
    input  wire  logic        reset_cmd,     // 0xFF this cycle
    input  wire  logic        wait_load,     // a Wait enters the response register
    input  wire  logic        rsp_valid,
    input  wire  cxp_pkg::cxp_ctrl_rsp_t rsp,
    input  wire  logic        rsp_ready
);
    int unsigned busy_cnt_q;                 // cycles of the current command
    logic        waited_q;                   // a Wait went out for this command
    logic        reset_q;                    // a 0xFF since the last start

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            busy_cnt_q <= 0;
            waited_q   <= 1'b0;
            reset_q    <= 1'b0;
        end else begin
            busy_cnt_q <= busy ? busy_cnt_q + 1 : 0;
            if (start)          waited_q <= 1'b0;
            else if (wait_load) waited_q <= 1'b1;
            if (start)          reset_q  <= 1'b0;
            else if (reset_cmd) reset_q  <= 1'b1;
        end
    end

    a_cmd_bounded: assert property (@(posedge clk) disable iff (!rst_n)
        busy_cnt_q <= p_LIMIT + 4)
        else `CXP_SVA_FAIL "cxp_ctrl_bus_master: command or drained access past the timeout");

    a_one_wait: assert property (@(posedge clk) disable iff (!rst_n)
        wait_load |-> !waited_q)
        else `CXP_SVA_FAIL "cxp_ctrl_bus_master: second Wait for one command");

    a_no_wait_after_reset: assert property (@(posedge clk) disable iff (!rst_n)
        wait_load |-> !reset_q)
        else `CXP_SVA_FAIL "cxp_ctrl_bus_master: Wait after a control channel reset");

    a_rsp_held: assert property (@(posedge clk) disable iff (!rst_n)
        (rsp_valid && !rsp_ready) |=> (rsp_valid && $stable(rsp)))
        else `CXP_SVA_FAIL "cxp_ctrl_bus_master: response dropped or changed before taken");

    a_no_stranded_cmd: assert property (@(posedge clk) disable iff (!rst_n)
        (pending && idle && free && !reset_cmd) |-> start)
        else `CXP_SVA_FAIL "cxp_ctrl_bus_master: command left waiting for a free executor");
endmodule

//=============================================================================
// Binds
//=============================================================================

//=============================================================================
// ConnectionReset lifetime (cxp_ctrl_bootstrap_regs)
//=============================================================================

module cxp_conn_reset_sva #(
    parameter int p_LIMIT = 65535
) (
    input  wire  logic clk,
    input  wire  logic rst_n,
    input  wire  logic active,
    input  wire  logic apply
);
    int unsigned run_q;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)      run_q <= 0;
        else if (apply)  run_q <= 0;
        else if (active) run_q <= run_q + 1;
        else             run_q <= 0;
    end

    // §10.3.28: the bit returns to 0; a missing echo is bounded by the
    // timeout, never a wait for ever.
    a_crst_bounded: assert property (@(posedge clk) disable iff (!rst_n)
        run_q <= p_LIMIT + 1)
        else `CXP_SVA_FAIL "cxp_ctrl_bootstrap_regs: ConnectionReset set for %0d cycles", run_q);
endmodule

//=============================================================================
// Reset release order (cxp_cdc_reset)
//=============================================================================

module cxp_reset_order_sva (
    input  wire  logic rst_n,
    input  wire  logic tx_clk,
    input  wire  logic app_clk,
    input  wire  logic rx_rst_n,
    input  wire  logic tx_rst_n,
    input  wire  logic app_rst_n
);
    // tx never runs while rx is held, app never while tx is held: the
    // crossings always see their source domain come up first.
    a_tx_after_rx: assert property (@(posedge tx_clk) disable iff (!rst_n)
        tx_rst_n |-> rx_rst_n)
        else `CXP_SVA_FAIL "cxp_cdc_reset: tx released before rx");

    a_app_after_tx: assert property (@(posedge app_clk) disable iff (!rst_n)
        app_rst_n |-> tx_rst_n)
        else `CXP_SVA_FAIL "cxp_cdc_reset: app released before tx");
endmodule

bind cxp_tx_arbiter cxp_arbiter_sva #(
    .p_PORTS (p_PORTS)
) cxp_arbiter_sva_i (
    .clk   (tx_clk),
    .rst_n (tx_rst_n),
    .ready (ready_o)
);

bind cxp_tx_inserter cxp_inserter_sva cxp_inserter_sva_i (
    .clk         (tx_clk),
    .rst_n       (tx_rst_n),
    .trig_valid  (trig_i.valid),
    .trig_sop    (trig_i.sop),
    .trig_eop    (trig_i.eop),
    .trig_ready  (trig_ready_o),
    .ioack_valid (ioack_i.valid),
    .ioack_sop   (ioack_i.sop),
    .ioack_eop   (ioack_i.eop),
    .ioack_ready (ioack_ready_o),
    .run         (run_q)
);

bind cxp_tx_domain cxp_tx_owner_sva #(
    .p_PORTS (cxp_pkg::TX_PORTS)
) cxp_tx_owner_sva_i (
    .clk   (tx_clk),
    .rst_n (tx_rst_n),
    .owner (cxp_tx_arbiter_i.owner_q),
    .valid ({tx_src[2].valid, tx_src[1].valid, tx_src[0].valid})
);

bind cxp_tx_pkt_framer cxp_framer_sva cxp_framer_sva_i (
    .clk     (tx_clk),
    .rst_n   (tx_rst_n),
    .in_data (data_phase_o),
    .data    (m_data_o),
    .kmask   (m_kmask_o),
    .valid   (m_valid_o),
    .sop     (m_sop_o),
    .eop     (m_eop_o),
    .ready   (m_ready_i)
);

bind cxp_tx_short_pkt cxp_short_pkt_sva cxp_short_pkt_sva_i (
    .clk   (tx_clk),
    .rst_n (tx_rst_n),
    .data  (m_data_o),
    .kmask (m_kmask_o),
    .valid (m_valid_o),
    .ready (m_ready_i)
);

bind cxp_tx_domain cxp_idle_rule_sva cxp_idle_rule_sva_i (
    .clk   (tx_clk),
    .rst_n (tx_rst_n),
    .data  (m_data_o),
    .kmask (m_kmask_o)
);

bind cxp_cdc_stream_fifo cxp_cdc_stream_fifo_sva #(
    .p_DEPTH (p_DEPTH),
    .p_PTR_W (PTR_W)
) cxp_cdc_stream_fifo_sva_i (
    .app_clk   (app_clk),
    .app_rst_n (app_rst_n),
    .tx_clk    (tx_clk),
    .tx_rst_n  (tx_rst_n),
    .fill_wr   (fill_wr),
    .emit_word (emit_word),
    .empty_rd  (empty_rd),
    .app_ok    (app_ok)
);

bind cxp_rx_link_mon cxp_link_mon_sva #(
    .p_BAD_WORDS (p_BAD_WORDS)
) cxp_link_mon_sva_i (
    .clk     (rx_clk),
    .rst_n   (rx_rst_n),
    .rx_lock (rx_lock_i),
    .valid   (valid_i),
    .err     (err_i),
    .idle    (is_idle),
    .resync  (resync_o),
    .flush   (flush_o)
);

bind cxp_rx_packet_parser cxp_rxlong_sva cxp_rxlong_sva_i (
    .clk   (rx_clk),
    .rst_n (rx_rst_n),
    .valid (long_valid_o),
    .sop   (long_sop_o),
    .eop   (long_eop_o),
    .err   (long_err_o)
);

bind cxp_ctrl_bus_master cxp_ctrl_exec_sva #(
    .p_LIMIT (BUS_TIMEOUT)
) cxp_ctrl_exec_sva_i (
    .clk        (clk),
    .rst_n      (rst_n),
    .busy       (state_q != ST_IDLE),
    .start      (start),
    .pending    (pend_valid_q),
    .free       (free),
    .idle       (state_q == ST_IDLE),
    .reset_cmd  (new_reset),
    .wait_load  (load_rsp && !rst_pend_q && wait_pend_q),
    .rsp_valid  (rsp_valid_o),
    .rsp        (rsp_o),
    .rsp_ready  (rsp_ready_i)
);

bind cxp_ctrl_bootstrap_regs cxp_conn_reset_sva #(
    .p_LIMIT (p_CONN_RESET_TIMEOUT)
) cxp_conn_reset_sva_i (
    .clk    (sys_clk),
    .rst_n  (sys_rst_n),
    .active (reg_q[R_CONNECTION_RESET][0]),
    .apply  (crst_apply)
);

bind cxp_cdc_reset cxp_reset_order_sva cxp_reset_order_sva_i (
    .rst_n     (rst_n),
    .tx_clk    (tx_clk),
    .app_clk   (app_clk),
    .rx_rst_n  (rx_rst_n_o),
    .tx_rst_n  (tx_rst_n_o),
    .app_rst_n (app_rst_n_o)
);


`default_nettype wire
