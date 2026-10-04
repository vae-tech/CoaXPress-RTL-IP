//-----------------------------------------------------------------------------
// tb_cxp_stream_top
//
// Cocotb wrapper around the stream path: `cxp_app_stream` ->
// `cxp_cdc_stream_fifo` (256 words) -> `cxp_tx_stream_pkt`, as
// cxp_interface_top builds it across its app / tx blocks, with the
// PacketTag reset, TestMode hold and flush tied off.  (The bench keeps
// the name of the former cxp_stream_top, which held the three.)  Pins X_SIZE /
// Y_SIZE to small values (8 x 4 px) so a couple of frames simulate in a few
// thousand cycles, and exposes only flat-scalar ports so the cocotb driver
// stays simulator-agnostic.
//
// Two pixel sources are visible to cocotb:
//   pix_sel = 0  ->  a local cxp_app_tpg -> cxp_app_pixel_packer ->
//                    byte swap drives the pipeline; frame / line start are
//                    the packer's SOF / SOL flags gated by the accept
//                    handshake (same-cycle pulses, as in cxp_interface_top).
//   pix_sel = 1  ->  the ext_pix_* / ext_meta_* inputs drive the pipeline
//                    directly.  Tests use this path to drive the
//                    same-cycle-pulse contract that cxp_app_pixel_ingress
//                    produces (and a pulse-ahead variant), exercising the
//                    skid register inside cxp_app_stream.
// The unselected source's ready is held low.  Top-level `TESTCASE` register
// is written by Python so the current test number is visible in waves.
//-----------------------------------------------------------------------------
`default_nettype none

module tb_cxp_stream_top (
    input  wire  logic        app_clk,
    input  wire  logic        app_rst_n,
    input  wire  logic        tx_clk,
    input  wire  logic        tx_rst_n,

    input  wire  logic        cfg_run,
    input  wire  logic        cfg_arbitrary,
    input  wire  logic [15:0] cfg_dsizeP,

    // 0 = internal TPG, 1 = external ext_pix_* / ext_meta_* drive.
    input  wire  logic        pix_sel,

    // External pixel-stream inputs (used when pix_sel=1).
    input  wire  logic [31:0] ext_pix_word_data,
    input  wire  logic        ext_pix_word_valid,
    output logic              ext_pix_word_ready,
    input  wire  logic        ext_pix_frame_start,
    input  wire  logic        ext_pix_word_eof,
    input  wire  logic        ext_pix_line_start,

    input  wire  logic [23:0] ext_meta_xsize,
    input  wire  logic [23:0] ext_meta_ysize,
    input  wire  logic [23:0] ext_meta_xoffs,
    input  wire  logic [23:0] ext_meta_yoffs,
    input  wire  logic [15:0] ext_meta_pixfmt,
    input  wire  logic [15:0] ext_meta_tapg,
    input  wire  logic [15:0] ext_meta_streamid,
    input  wire  logic [15:0] ext_meta_sourcetag,
    input  wire  logic [7:0]  ext_meta_flags,

    output logic [31:0]       m_data,
    output logic [3:0]        m_kmask,
    output logic              m_valid,
    output logic              m_sop,
    output logic              m_eop,
    input  wire  logic        m_ready
);
    /* verilator lint_off UNUSEDSIGNAL */
    logic [7:0] TESTCASE /* verilator public_flat_rw */;
    /* verilator lint_on UNUSEDSIGNAL */

    initial TESTCASE = 8'h00;

    // ------------------------------------------------------------------------
    // Test-pattern generator → cxp_app_pixel_packer.  The TPG now emits one
    // pixel/cycle; the packer produces the 32-bit P0..P3 words and the
    // frame/line start pulses are derived from the packer's SOF/SOL flags
    // (gated by the accept handshake), mirroring cxp_interface_top.
    // ------------------------------------------------------------------------
    logic [15:0] tpg_pix_data;
    logic        tpg_pix_valid;
    logic        tpg_pix_ready;
    logic        tpg_pix_sof, tpg_pix_sol, tpg_pix_eol, tpg_pix_eof;

    logic [31:0] tpg_word_data;
    logic        tpg_word_valid;
    logic        tpg_word_ready;
    logic        tpg_frame_start;
    logic        tpg_line_start;

    cxp_pkg::cxp_meta_t tpg_meta;

    cxp_app_tpg #(
        .p_X_SIZE    (8),
        .p_Y_SIZE    (4),
        .p_PIXFMT    (16'h0101)     // GenICam Mono8
    ) cxp_app_tpg_i (
        .app_clk          (app_clk),
        .app_rst_n        (app_rst_n),
        .cfg_run_i        (cfg_run),
        .cfg_xsize_i      (16'd0),
        .cfg_ysize_i      (16'd0),
        .cfg_pixfmt_i     (16'd0),
        .cfg_xoffs_i      (16'd0),
        .cfg_yoffs_i      (16'd0),
        .cfg_srctag_i     (16'd0),
        .cfg_streamid_i   (8'h01),
        .cfg_tapg_i       (16'h0000),
        .cfg_flags_i      (8'h00),

        .m_pix_data_o     (tpg_pix_data),
        .m_pix_valid_o    (tpg_pix_valid),
        .m_pix_ready_i    (tpg_pix_ready),
        .m_pix_sof_o      (tpg_pix_sof),
        .m_pix_sol_o      (tpg_pix_sol),
        .m_pix_eol_o      (tpg_pix_eol),
        .m_pix_eof_o      (tpg_pix_eof),

        .meta_o           (tpg_meta)
    );

    // ------------------------------------------------------------------------
    // Pixel packer.  Byte-reversed so the first-transmitted pixel sits in
    // word[7:0] (same convention as cxp_interface_top); for Mono8 this is
    // bit-identical to the legacy 4-px/word layout.
    // ------------------------------------------------------------------------
    logic [31:0] pk_word_data;
    logic [3:0]  pk_word_lane_vld;
    logic        pk_word_valid;
    logic        pk_word_sol, pk_word_eol, pk_word_sof, pk_word_eof;
    logic        pk_word_ready;

    cxp_app_pixel_packer cxp_app_pixel_packer_i (
        .app_clk           (app_clk),
        .app_rst_n         (app_rst_n),
        .cfg_pixfmt_i      (tpg_meta.pixfmt),

        .s_pix_data_i      (tpg_pix_data),
        .s_pix_w_i         (5'd0),
        .s_pix_valid_i     (tpg_pix_valid),
        .s_pix_sol_i       (tpg_pix_sol),
        .s_pix_eol_i       (tpg_pix_eol),
        .s_pix_sof_i       (tpg_pix_sof),
        .s_pix_eof_i       (tpg_pix_eof),
        .s_pix_ready_o     (tpg_pix_ready),

        .m_word_data_o     (pk_word_data),
        .m_word_lane_vld_o (pk_word_lane_vld),
        .m_word_valid_o    (pk_word_valid),
        .m_word_sol_o      (pk_word_sol),
        .m_word_eol_o      (pk_word_eol),
        .m_word_sof_o      (pk_word_sof),
        .m_word_eof_o      (pk_word_eof),
        .m_word_ready_i    (pk_word_ready)
    );

    assign tpg_word_data   = {pk_word_data[7:0],   pk_word_data[15:8],
                              pk_word_data[23:16],  pk_word_data[31:24]};
    assign tpg_word_valid  = pk_word_valid;
    assign pk_word_ready   = tpg_word_ready;
    assign tpg_frame_start = pk_word_valid & pk_word_sof & tpg_word_ready;
    assign tpg_line_start  = pk_word_valid & pk_word_sol & tpg_word_ready;

    /* verilator lint_off UNUSEDSIGNAL */
    logic _unused_pk;
    assign _unused_pk = &{1'b0, pk_word_lane_vld, pk_word_eol};
    /* verilator lint_on UNUSEDSIGNAL */

    // ------------------------------------------------------------------------
    // Mux between TPG (pix_sel=0) and external drive (pix_sel=1).
    // The unselected source's *_ready handshake is held low so its FSM stays
    // parked.
    // ------------------------------------------------------------------------
    logic [31:0] st_pix_word_data;
    logic        st_pix_word_valid;
    logic        st_pix_word_ready;
    logic        st_pix_frame_start;
    logic        st_pix_line_start;
    logic        st_pix_word_eof;
    cxp_pkg::cxp_meta_t ext_meta;
    cxp_pkg::cxp_meta_t st_meta;

    assign st_pix_word_data    = pix_sel ? ext_pix_word_data    : tpg_word_data;
    assign st_pix_word_valid   = pix_sel ? ext_pix_word_valid   : tpg_word_valid;
    assign st_pix_frame_start  = pix_sel ? ext_pix_frame_start  : tpg_frame_start;
    assign st_pix_line_start   = pix_sel ? ext_pix_line_start   : tpg_line_start;
    assign st_pix_word_eof     = pix_sel ? ext_pix_word_eof     : pk_word_eof;

    assign ext_meta = '{arbitrary: 1'b0,
                        streamid:  ext_meta_streamid[7:0],
                        sourcetag: ext_meta_sourcetag,
                        xsize:     ext_meta_xsize,
                        ysize:     ext_meta_ysize,
                        xoffs:     ext_meta_xoffs,
                        yoffs:     ext_meta_yoffs,
                        pixfmt:    ext_meta_pixfmt,
                        tapg:      ext_meta_tapg,
                        flags:     ext_meta_flags};

    always_comb begin
        st_meta           = pix_sel ? ext_meta : tpg_meta;
        st_meta.arbitrary = cfg_arbitrary;
    end

    assign tpg_word_ready      = pix_sel ? 1'b0 : st_pix_word_ready;
    assign ext_pix_word_ready  = pix_sel ? st_pix_word_ready : 1'b0;

    // The DUT's packet word, unpacked to the flat ports the tests read.
    cxp_pkg::cxp_txw_t m_w;

    assign m_data  = m_w.data;
    assign m_kmask = m_w.kmask;
    assign m_valid = m_w.valid;
    assign m_sop   = m_w.sop;
    assign m_eop   = m_w.eop;

    // The stream path as cxp_interface_top builds it: cxp_app_stream
    // (app_clk) -> cxp_cdc_stream_fifo -> cxp_tx_stream_pkt (tx_clk), with the
    // PacketTag reset, TestMode hold and flush tied off and the stream
    // enabled.
    logic [31:0] fifo_s_data;
    logic [3:0]  fifo_s_kmask;
    logic        fifo_s_valid, fifo_s_sop, fifo_s_eop, fifo_s_ready, fifo_s_flush;
    logic [7:0]  fifo_s_streamid;
    logic [31:0] fifo_m_data;
    logic [3:0]  fifo_m_kmask;
    logic        fifo_m_valid, fifo_m_sop, fifo_m_eop, fifo_m_ready;
    logic        fifo_m_pkt_avail, fifo_m_busy;
    logic [15:0] fifo_m_len;
    logic [7:0]  fifo_m_streamid;

    cxp_app_stream #(
        .p_FIFO_DEPTH (256)
    ) cxp_app_stream_i (
        .app_clk             (app_clk),
        .app_rst_n           (app_rst_n),
        .cfg_dsizeP_i        (cfg_dsizeP),

        .pix_word_data_i     (st_pix_word_data),
        .pix_word_valid_i    (st_pix_word_valid),
        .pix_word_eof_i      (st_pix_word_eof),
        .pix_word_ready_o    (st_pix_word_ready),
        .pix_frame_start_i   (st_pix_frame_start),
        .pix_line_start_i    (st_pix_line_start),

        .meta_i              (st_meta),

        .m_data_o            (fifo_s_data),
        .m_kmask_o           (fifo_s_kmask),
        .m_valid_o           (fifo_s_valid),
        .m_sop_o             (fifo_s_sop),
        .m_eop_o             (fifo_s_eop),
        .m_streamid_o        (fifo_s_streamid),
        .m_ready_i           (fifo_s_ready),
        .flush_i             (fifo_s_flush)
    );

    cxp_cdc_stream_fifo #(
        .p_DEPTH (256)
    ) cxp_cdc_stream_fifo_i (
        .app_clk       (app_clk),
        .app_rst_n     (app_rst_n),
        .tx_clk        (tx_clk),
        .tx_rst_n      (tx_rst_n),

        .s_data_i      (fifo_s_data),
        .s_kmask_i     (fifo_s_kmask),
        .s_valid_i     (fifo_s_valid),
        .s_sop_i       (fifo_s_sop),
        .s_eop_i       (fifo_s_eop),
        .s_streamid_i  (fifo_s_streamid),
        .s_ready_o     (fifo_s_ready),

        .m_data_o      (fifo_m_data),
        .m_kmask_o     (fifo_m_kmask),
        .m_valid_o     (fifo_m_valid),
        .m_sop_o       (fifo_m_sop),
        .m_eop_o       (fifo_m_eop),
        .m_ready_i     (fifo_m_ready),
        .m_pkt_avail_o (fifo_m_pkt_avail),
        .m_len_o       (fifo_m_len),
        .m_streamid_o  (fifo_m_streamid),
        .m_busy_i      (fifo_m_busy),
        .flush_i       (1'b0),
        .flush_ack_o   (),
        .s_flush_o     (fifo_s_flush)
    );

    cxp_tx_stream_pkt cxp_tx_stream_pkt_i (
        .tx_clk              (tx_clk),
        .tx_rst_n            (tx_rst_n),
        .stream_ctrl_reset_i (1'b0),
        .stream_en_i         (1'b1),
        .suppress_stream_i   (1'b0),

        .s_data_i            (fifo_m_data),
        .s_kmask_i           (fifo_m_kmask),
        .s_valid_i           (fifo_m_valid),
        .s_sop_i             (fifo_m_sop),
        .s_eop_i             (fifo_m_eop),
        .s_streamid_i        (fifo_m_streamid),
        .s_len_i             (fifo_m_len),
        .s_pkt_avail_i       (fifo_m_pkt_avail),
        .s_ready_o           (fifo_m_ready),

        .m_o                 (m_w),
        .m_ready_i           (m_ready),
        .busy_o              (fifo_m_busy)
    );
endmodule

`default_nettype wire
