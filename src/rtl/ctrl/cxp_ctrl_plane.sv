/*
================================================================================
  cxp_ctrl_plane
  CoaXPress 1.1.1 (CXP-001-2015) — control-command executor (§8.6).
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-09-19

    Description:
      Everything between a received long packet and the register bus, on
      rx_clk, in two blocks:

        long packets (cxp_rx_link)
           |
           v  cxp_ctrl_cmd_parser          type 0x02: CRC, size, extension-link
           |                           check, two write-buffer banks; one
           |                           command per packet, err = its code
           v  cxp_ctrl_bus_master      the executor: one command at a time,
           |                           one pending, the response register
           |                           and its order (0x03, Wait, final),
           |                           per-command Wait / timeout in ms of
           |                           p_CLK_KHZ, two read-buffer banks
           +--> reg_*                  register file (Table 22 code per access)
           +--> apb_*                  user window [p_USER_BASE, +p_USER_SIZE)
           +--> rsp_* / rbuf_*         to cxp_tx_ctrl_ack

      The response is held on rsp_valid_o until rsp_ready_i.

    Versions:
        2026-09-19 - 0.1:   - Init (lifted out of cxp_rx_link)
        2026-09-19 - 0.2:   - cxp_ctrl_bus_master replaces the APB master;
                              held response with Wait
        2026-09-22 - 0.3:   - Byte enables on both register ports
        2026-09-26 - 0.4:   - Parser and executor only; the APB bridge
                              inside the executor; one status pulse for a
                              command answered without an access

================================================================*/

`timescale 1ns / 1ns
`default_nettype none

module cxp_ctrl_plane #(
    parameter int          p_BUF_DEPTH     = 64,            // ctrl rd/wr buffer depth (dwords)
    parameter int          p_PKT_SIZE_MAX  = int'(cxp_regmap_pkg::CONTROL_PACKET_SIZE_MAX_VALUE),
    parameter logic [31:0] p_USER_BASE     = 32'h0002_0000, // user window base
    parameter logic [31:0] p_USER_SIZE     = 32'h0000_0000, // user window bytes, 0 = off
    parameter int          p_CLK_KHZ       = 125_000,       // rx_clk frequency (kHz)
    parameter int          p_WAIT_AFTER_MS = 100,           // command time before the Wait
    parameter int          p_TIMEOUT_MS    = 900,           // command time before 0x40
    parameter int          p_WAIT_MS       = 1000           // Wait ack payload (ms)
) (
    input  wire  logic                           rx_clk,                  // RX clock
    input  wire  logic                           rx_rst_n,                // async active-low reset

    // Long packets from cxp_rx_link
    input  wire  cxp_pkg::cxp_rxlong_t           long_i,                  // packet words + type

    input  wire  logic                           from_extension_link_i,   // 1 = writes refused

    // Register file port (answers in one cycle with a Table 22 code)
    output logic                                 reg_req_o,
    output logic                                 reg_we_o,
    output logic [31:0]                          reg_addr_o,
    output logic [31:0]                          reg_wdata_o,
    output logic [3:0]                           reg_wstrb_o,   // byte enables
    input  wire  logic                           reg_ack_i,
    input  wire  logic [31:0]                    reg_rdata_i,
    input  wire  logic [7:0]                     reg_err_i,

    // User window: APB3 master + PSTRB (tied off when p_USER_SIZE = 0)
    output logic                                 apb_psel_o,
    output logic                                 apb_penable_o,
    output logic                                 apb_pwrite_o,
    output logic [31:0]                          apb_paddr_o,
    output logic [31:0]                          apb_pwdata_o,
    output logic [3:0]                           apb_pstrb_o,
    input  wire  logic [31:0]                    apb_prdata_i,
    input  wire  logic                           apb_pready_i,
    input  wire  logic                           apb_pslverr_i,

    // Response (to cxp_tx_ctrl_ack).  The read-buffer port {bank, word}
    // runs on the acknowledge framer's clock.
    input  wire  logic                           rbuf_clk,                // read-port clock
    input  wire  logic [$clog2(p_BUF_DEPTH):0]   rbuf_addr_i,             // read-buffer addr
    output logic [31:0]                          rbuf_data_o,             // read-buffer data
    output logic                                 rsp_valid_o,             // response held
    output cxp_pkg::cxp_ctrl_rsp_t               rsp_o,
    input  wire  logic                           rsp_ready_i,             // framer took it

    // Status
    output logic                                 ctrl_reset_pulse_o,      // 0xFF executed
    output logic                                 nack_pulse_o,            // answered without an
    output logic [7:0]                           nack_code_o              //   access (0x4x, 0x80)
);

    import cxp_pkg::*;

    //=======================================================================
    // Signals
    //=======================================================================

    logic                            cmd_valid;
    cxp_ctrl_cmd_t                   cmd;
    logic                            cmd_full;
    logic [$clog2(p_BUF_DEPTH):0]    wbuf_addr;
    logic [31:0]                     wbuf_data;

    //=======================================================================
    // Control command parser (gates internally on type 0x02)
    //=======================================================================

    cxp_ctrl_cmd_parser #(
        .p_BUF_DEPTH    (p_BUF_DEPTH),
        .p_PKT_SIZE_MAX (p_PKT_SIZE_MAX)
    ) cxp_ctrl_cmd_parser_i (
        .rx_clk                (rx_clk),
        .rx_rst_n              (rx_rst_n),
        .long_data_i           (long_i.data),
        .long_kmask_i          (long_i.kmask),
        .long_valid_i          (long_i.valid),
        .long_sop_i            (long_i.sop),
        .long_eop_i            (long_i.eop),
        .long_err_i            (long_i.err),
        .long_type_i           (long_i.ptype),
        .from_extension_link_i (from_extension_link_i),
        .cmd_valid_o           (cmd_valid),
        .cmd_o                 (cmd),
        .cmd_full_i            (cmd_full),
        .wbuf_addr_i           (wbuf_addr),
        .wbuf_data_o           (wbuf_data)
    );

    //=======================================================================
    // Executor
    //=======================================================================

    cxp_ctrl_bus_master #(
        .p_BUF_DEPTH     (p_BUF_DEPTH),
        .p_USER_BASE     (p_USER_BASE),
        .p_USER_SIZE     (p_USER_SIZE),
        .p_CLK_KHZ       (p_CLK_KHZ),
        .p_WAIT_AFTER_MS (p_WAIT_AFTER_MS),
        .p_TIMEOUT_MS    (p_TIMEOUT_MS),
        .p_WAIT_MS       (p_WAIT_MS)
    ) cxp_ctrl_bus_master_i (
        .clk                (rx_clk),
        .rst_n              (rx_rst_n),
        .cmd_valid_i        (cmd_valid),
        .cmd_i              (cmd),
        .cmd_full_o         (cmd_full),
        .wbuf_addr_o        (wbuf_addr),
        .wbuf_data_i        (wbuf_data),
        .reg_req_o          (reg_req_o),
        .reg_we_o           (reg_we_o),
        .reg_addr_o         (reg_addr_o),
        .reg_wdata_o        (reg_wdata_o),
        .reg_wstrb_o        (reg_wstrb_o),
        .reg_ack_i          (reg_ack_i),
        .reg_rdata_i        (reg_rdata_i),
        .reg_err_i          (reg_err_i),
        .apb_psel_o         (apb_psel_o),
        .apb_penable_o      (apb_penable_o),
        .apb_pwrite_o       (apb_pwrite_o),
        .apb_paddr_o        (apb_paddr_o),
        .apb_pwdata_o       (apb_pwdata_o),
        .apb_pstrb_o        (apb_pstrb_o),
        .apb_prdata_i       (apb_prdata_i),
        .apb_pready_i       (apb_pready_i),
        .apb_pslverr_i      (apb_pslverr_i),
        .rsp_valid_o        (rsp_valid_o),
        .rsp_o              (rsp_o),
        .rsp_ready_i        (rsp_ready_i),
        .rbuf_clk           (rbuf_clk),
        .rbuf_addr_i        (rbuf_addr_i),
        .rbuf_data_o        (rbuf_data_o),
        .ctrl_reset_pulse_o (ctrl_reset_pulse_o),
        .nack_pulse_o       (nack_pulse_o),
        .nack_code_o        (nack_code_o)
    );

endmodule

`default_nettype wire
