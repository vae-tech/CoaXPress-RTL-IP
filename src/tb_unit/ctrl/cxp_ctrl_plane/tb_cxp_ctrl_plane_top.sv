//-----------------------------------------------------------------------------
// tb_cxp_ctrl_plane_top
//
// Cocotb wrapper for `cxp_ctrl_plane` (command parser + executor): the
// long-packet struct as separate fields, a 16-word buffer and a matching
// packet size limit (88 bytes), a 1 kHz clock parameter so the Wait /
// timeout limits (20 / 50 ms) are 20 / 50 cycles, and a user window of 64
// words at 0x0002_0000 served by an APB slave here.  The slave answers
// after `apb_latency` cycles of ACCESS (0xFFFF = never) and counts the
// transfers it completed (`apb_accesses`).  The read buffer is read on
// the same clock through `rbuf_bank` / `rbuf_addr`.  Top-level `TESTCASE`
// is written by Python.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_ctrl_plane_top #(
    parameter int BUF_DEPTH    = 16,
    parameter int PKT_SIZE_MAX = (BUF_DEPTH + 6) * 4
) (
    input  wire  logic        clk,
    input  wire  logic        rst_n,

    input  wire  logic [31:0] long_data,
    input  wire  logic [3:0]  long_kmask,
    input  wire  logic        long_valid,
    input  wire  logic        long_sop,
    input  wire  logic        long_eop,
    input  wire  logic        long_err,
    input  wire  logic [7:0]  long_type,
    input  wire  logic        from_extension_link,

    output logic              reg_req,
    output logic              reg_we,
    output logic [31:0]       reg_addr,
    output logic [31:0]       reg_wdata,
    input  wire  logic        reg_ack,
    input  wire  logic [31:0] reg_rdata,
    input  wire  logic [7:0]  reg_err,

    input  wire  logic [15:0] apb_latency,
    output logic [15:0]       apb_accesses,

    input  wire  logic        rbuf_bank,
    input  wire  logic [$clog2(BUF_DEPTH)-1:0] rbuf_addr,
    output logic [31:0]       rbuf_data,
    output logic              rsp_valid,
    output logic [7:0]        rsp_code,
    output logic [23:0]       rsp_size,
    output logic [31:0]       rsp_wait_ms,
    output logic              rsp_rbank,
    input  wire  logic        rsp_ready,

    output logic              ctrl_reset_pulse,
    output logic              nack_pulse,
    output logic [7:0]        nack_code
);
    import cxp_pkg::*;

    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    cxp_rxlong_t   long_w;
    cxp_ctrl_rsp_t rsp;

    always_comb begin
        long_w       = '0;
        long_w.data  = long_data;
        long_w.kmask = long_kmask;
        long_w.valid = long_valid;
        long_w.sop   = long_sop;
        long_w.eop   = long_eop;
        long_w.err   = long_err;
        long_w.ptype = long_type;
    end

    assign rsp_code    = rsp.code;
    assign rsp_size    = rsp.size;
    assign rsp_wait_ms = rsp.wait_ms;
    assign rsp_rbank   = rsp.rbank;

    // User-window APB slave: 64 words, reads return the stored word; a
    // write changes the bytes PSTRB enables.
    logic        psel, penable, pwrite, pready;
    logic [31:0] paddr, pwdata, prdata;
    logic [3:0]  pstrb;
    logic [15:0] wait_q;
    logic [31:0] umem [64];

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wait_q       <= 16'd0;
            apb_accesses <= 16'd0;
            for (int i = 0; i < 64; i++) umem[i] <= 32'hA500_0000 | 32'(i);
        end else if (psel && penable) begin
            wait_q <= wait_q + 16'd1;
            if (pready) begin
                wait_q       <= 16'd0;
                apb_accesses <= apb_accesses + 16'd1;
                if (pwrite)
                    for (int b = 0; b < 4; b++)
                        if (pstrb[b]) umem[paddr[7:2]][8*b +: 8] <= pwdata[8*b +: 8];
            end
        end else begin
            wait_q <= 16'd0;
        end
    end

    assign pready = psel & penable & (apb_latency != 16'hFFFF) & (wait_q >= apb_latency);
    assign prdata = umem[paddr[7:2]];

    cxp_ctrl_plane #(
        .p_BUF_DEPTH     (BUF_DEPTH),
        .p_PKT_SIZE_MAX  (PKT_SIZE_MAX),
        .p_USER_BASE     (32'h0002_0000),
        .p_USER_SIZE     (32'h0000_0100),
        .p_CLK_KHZ       (1),
        .p_WAIT_AFTER_MS (20),
        .p_TIMEOUT_MS    (50),
        .p_WAIT_MS       (1234)
    ) cxp_ctrl_plane_i (
        .rx_clk                (clk),
        .rx_rst_n              (rst_n),
        .long_i                (long_w),
        .from_extension_link_i (from_extension_link),
        .reg_req_o             (reg_req),
        .reg_we_o              (reg_we),
        .reg_addr_o            (reg_addr),
        .reg_wdata_o           (reg_wdata),
        .reg_wstrb_o           (),
        .reg_ack_i             (reg_ack),
        .reg_rdata_i           (reg_rdata),
        .reg_err_i             (reg_err),
        .apb_psel_o            (psel),
        .apb_penable_o         (penable),
        .apb_pwrite_o          (pwrite),
        .apb_paddr_o           (paddr),
        .apb_pwdata_o          (pwdata),
        .apb_pstrb_o           (pstrb),
        .apb_prdata_i          (prdata),
        .apb_pready_i          (pready),
        .apb_pslverr_i         (1'b0),
        .rbuf_clk              (clk),
        .rbuf_addr_i           ({rbuf_bank, rbuf_addr}),
        .rbuf_data_o           (rbuf_data),
        .rsp_valid_o           (rsp_valid),
        .rsp_o                 (rsp),
        .rsp_ready_i           (rsp_ready),
        .ctrl_reset_pulse_o    (ctrl_reset_pulse),
        .nack_pulse_o          (nack_pulse),
        .nack_code_o           (nack_code)
    );
endmodule

`default_nettype wire
