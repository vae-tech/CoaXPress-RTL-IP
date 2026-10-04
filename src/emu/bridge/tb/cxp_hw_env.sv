// =============================================================================
// cxp_hw_env — pure-SystemVerilog CoaXPress IP hardware environment
//
// Wraps the complete CoaXPress 1.1.1 device IP (`cxp_device_top`: link,
// datapath and register file) and bridges the device's serial low-speed
// uplink and parallel high-speed downlink to two Linux named pipes:
//
//     cxp.h2c   host  -> camera   driven onto rx_serial   (control uplink)
//     cxp.c2h   camera -> host    from cxp_if_data_o/kmask (HS stream/ack)
//
// All CoaXPress link logic (envelope (de)framing, 8B/10B, IDLE keep-alive,
// SOP..EOP reassembly) lives in the DPI-C side, `bridge/dpi/cxp_fifo_dpi.c`.
// This module is only clock/reset + the DUT + serial bit-pacing, so the
// environment runs in any DPI-capable simulator (default: Verilator
// `--binary`, matching the rest of the repo's tooling).
//
// The C++ host (src/emu/host) talks to this exactly as it talks to its
// built-in virtual camera:
//
//     make -C src/emu/bridge run FIFO_DIR=/tmp/cxp              # terminal 1
//     src/emu/host/build/cxp --fifo-dir /tmp/cxp read 0x0000    # terminal 2
//
// Clocks: app_clk, tx_clk and rx_clk run from three generators (10 ns each
// by default) with p_ASYNC_CLOCKS = 1, as the UVM testbench runs them; the
// host's serial bit clock is its own, OS_RATIO rx periods long.  The bench
// (dpi/cxp_fifo_dpi.c, src/emu/host src/cxp/protocol/bench.h) retunes all of them
// and drives the device inputs a lab would: trig_i, from_extension_link_i,
// cfg_trig_polarity_i, cfg_use_tpg_i / cfg_run_i / cfg_arbitrary_i, the
// pixel port, and a faulting register bus (the register file's error code
// forced).  trig_o and trig_glitch_pulse_o edges go back to the host.
// =============================================================================
`default_nettype none
`timescale 1ns/1ps

// The TPG's maximum geometry and the power-on values of PixelFormat and
// Image1StreamID are cxp_device_top elaboration parameters, so they are
// exposed as env parameters here and forwarded.  Override at build time
// via top-module parameter flags (Verilator -GTPG_X_SIZE=128 / vopt
// -gTPG_X_SIZE=128), most easily through the Makefile WIDTH/HEIGHT/
// PIXFMT/STREAMID knobs.  Every other header field of a TPG image is a
// register.  Run-time selection (cfg_run / cfg_use_tpg / cfg_arbitrary)
// stays a +plusarg.
module cxp_hw_env #(
    parameter int          TPG_X_SIZE    = 4096,          // = the XML's Width Max
    parameter int          TPG_Y_SIZE    = 4096,          // = the XML's Height Max
    parameter logic [31:0] TPG_PIXFMT    = cxp_pkg::PFNC_MONO8, // PixelFormat power-on (PFNC)
    parameter logic [7:0]  TPG_STREAMID  = 8'h01,         // Image1StreamID power-on
    parameter string       p_XML_BLOB_MEM = cxp_regmap_pkg::XML_BLOB_MEM  // src/regmap/regmap.mk
);

    // OS_RATIO must match the parameter compiled into cxp_rx_link via
    // cxp_device_top: each recovered serial bit is held for OS_RATIO
    // os_clk cycles (same contract as host_uplink_agent).
    localparam int OS_RATIO = 16;

    // The device's millisecond: its control time limits (§8.6.1.1: the Wait
    // after 100 ms, the timeout at 900 ms) count p_RX_CLK_KHZ rx_clk cycles
    // per ms.  The bench declares 1000 (1 ms = 10 us at the default 10 ns
    // rx_clk), a device 100 times faster than real time, so a 2 s register
    // stall costs seconds, not hours, of simulation.  The bench tells the host
    // the scale (SYNC reply), and REG_STALL counts in the same milliseconds.
    localparam int RX_CLK_KHZ = 1000;

    // -------------------------------------------------------------------------
    // DPI-C bridge (bridge/dpi/cxp_fifo_dpi.c)
    // -------------------------------------------------------------------------
    import "DPI-C" function void cxp_fifo_init(input string h2c,
                                               input string c2h);
    import "DPI-C" function int  cxp_uplink_bit(input longint time_ps, input int bit_ps);
    import "DPI-C" function void cxp_downlink_word(input int unsigned data,
                                                   input int unsigned kmask,
                                                   input longint time_ps);
    import "DPI-C" function void cxp_fifo_pump();
    import "DPI-C" function void cxp_fifo_close();
    import "DPI-C" function void cxp_bench_init(input int use_tpg, input int run,
                                                input int arbitrary, input int rx_khz);
    import "DPI-C" function int  cxp_bench_pins();
    import "DPI-C" function int  cxp_bench_reset_req();
    import "DPI-C" function void cxp_bench_reset_done();
    import "DPI-C" function int  cxp_bench_reg_err();
    import "DPI-C" function int  cxp_bench_stall_ms();
    import "DPI-C" function int  cxp_bench_stall_err();
    import "DPI-C" function int  cxp_bench_half_ps(input int which);
    import "DPI-C" function int  cxp_bench_bit_ps(input int os_ratio);
    import "DPI-C" function void cxp_bench_edge(input int pin, input int value,
                                                input longint time_ns);
    import "DPI-C" function void cxp_bench_meta(output int xsize, output int ysize,
                                                output int xoffs, output int yoffs,
                                                output int pixfmt, output int tapg,
                                                output int streamid, output int sourcetag,
                                                output int flags);
    import "DPI-C" function int  cxp_bench_pix_beat(output int data, output int flags);
    import "DPI-C" function void cxp_bench_pix_taken();

    // -------------------------------------------------------------------------
    // Clocks / reset.  Each generator reads its half period from the bench
    // (10 ns for all three).
    // -------------------------------------------------------------------------
    logic app_clk = 1'b0, tx_clk = 1'b0, rx_clk = 1'b0;
    int   app_half_ps = 5000, tx_half_ps = 5000, rx_half_ps = 5000;
    /* verilator lint_off ZERODLY */  // the bench never returns a 0 ps period
    initial forever begin #(app_half_ps * 1ps); app_clk = ~app_clk; app_half_ps = cxp_bench_half_ps(0); end
    initial forever begin #(tx_half_ps * 1ps);  tx_clk  = ~tx_clk;  tx_half_ps  = cxp_bench_half_ps(1); end
    initial forever begin #(rx_half_ps * 1ps);  rx_clk  = ~rx_clk;  rx_half_ps  = cxp_bench_half_ps(2); end
    /* verilator lint_on ZERODLY */

    // Reset: 8 app_clk cycles at power-on, and again on each bench RESET
    // (the host's stand-in for UVM's per-test reset).  One process drives
    // the three reset inputs; a RESET names the ones it pulses (bit 0 app,
    // 1 tx, 2 rx).  rst_n is low while any of them is.  The process tells
    // the bench when a RESET it asked for is over.
    logic rst_n = 1'b0;
    logic [2:0] rst_dom_n = 3'b000;   // {rx, tx, app}
    int   rst_cnt = 8;
    bit   rst_bench = 1'b0;

    always @(posedge app_clk) begin
        int req;
        req = cxp_bench_reset_req();
        if (req != 0) begin
            rst_dom_n <= rst_dom_n & ~req[2:0];
            rst_n     <= 1'b0;
            rst_cnt   <= 8;
            rst_bench <= 1'b1;
        end else if (rst_cnt > 0) begin
            rst_cnt <= rst_cnt - 1;
            if (rst_cnt == 1) begin
                rst_dom_n <= 3'b111;
                rst_n     <= 1'b1;
                if (rst_bench) begin
                    rst_bench <= 1'b0;
                    cxp_bench_reset_done();
                end
            end
        end
    end

    // Runtime knobs (plusargs).
    string h2c_path;
    string c2h_path;
    int    run_arg;          // cfg_run    (TPG free-run enable)
    int    use_tpg_arg;      // cfg_use_tpg(1 = internal TPG, 0 = ext port)
    int    arb_arg;          // cfg_arbitrary (0 = rectangular headers)

    // The camera RTL runs until killed (Ctrl-C); bounding a run is the
    // host's job — the Python stack applies a device-ack wait timeout
    // (cxp.cli --timeout). This keeps the SV env free of sim-time knobs.
    initial begin
        if (!$value$plusargs("h2c=%s", h2c_path))
            h2c_path = "/tmp/cxp/cxp.h2c";
        if (!$value$plusargs("c2h=%s", c2h_path))
            c2h_path = "/tmp/cxp/cxp.c2h";
        if (!$value$plusargs("run=%d", run_arg))
            run_arg = 1;             // free-run frames by default
        if (!$value$plusargs("use_tpg=%d", use_tpg_arg))
            use_tpg_arg = 1;         // internal TPG source by default
        if (!$value$plusargs("arbitrary=%d", arb_arg))
            arb_arg = 0;             // rectangular header/markers

        cxp_fifo_init(h2c_path, c2h_path);
        cxp_bench_init(use_tpg_arg, run_arg, arb_arg, RX_CLK_KHZ);

        wait (rst_n);
        $display({"[cxp-hw] DUT out of reset; TPG %0dx%0d @0x%04h ",
                  "use_tpg=%0d run=%0d arbitrary=%0d (packet size: the ",
                  "host's StreamPacketSizeMax)"},
                 TPG_X_SIZE, TPG_Y_SIZE, TPG_PIXFMT,
                 use_tpg_arg, run_arg, arb_arg);
    end

    final begin
        cxp_fifo_close();
    end

    // VCD dump.  Only compiled in when the Verilator build enabled tracing
    // (Makefile TRACE=1 -> --trace + +define+CXP_TRACE); armed at run time
    // by the +trace plusarg so a trace-capable binary can still run lean.
    // Optional +vcd=<path> overrides the default (build/cxp_hw_env.vcd,
    // relative to $(HW_DIR) where the binary is launched).
`ifdef CXP_TRACE
    string vcd_path;
    initial begin
        if ($test$plusargs("trace")) begin
            if (!$value$plusargs("vcd=%s", vcd_path))
                vcd_path = "build/cxp_hw_env.vcd";
            $dumpfile(vcd_path);
            $dumpvars(0, cxp_hw_env);
            $display("[cxp-hw] VCD tracing -> %s", vcd_path);
        end
    end
`endif

    // -------------------------------------------------------------------------
    // Device configuration.
    //   cfg_use_tpg / cfg_run / cfg_arbitrary : the +plusargs set the
    //     power-on value; the bench (PIN ops) takes over, with trig_i,
    //     from_extension_link_i and cfg_trig_polarity_i (all 0 at power-on).
    //   Stream packet size: the bootstrap StreamPacketSizeMax register
    //     (§10.3.32) inside cxp_device_top; while it reads 0 nothing
    //     streams.
    //   Width / Height / PixelFormat / TestPattern / TestMode come from
    //   the host-writable registers inside cxp_device_top.
    // -------------------------------------------------------------------------
    logic cfg_use_tpg, cfg_run, cfg_arbitrary;
    logic trig_in, ext_link, trig_polarity;
    int   bench_pins;
    always @(posedge rx_clk) bench_pins <= cxp_bench_pins();
    assign trig_in       = bench_pins[0];
    assign ext_link      = bench_pins[1];
    assign trig_polarity = bench_pins[2];
    assign cfg_use_tpg   = bench_pins[3];
    assign cfg_run       = bench_pins[4];
    assign cfg_arbitrary = bench_pins[5];

    // -------------------------------------------------------------------------
    // Uplink: drive rx_serial from the DPI bit source on the host's own bit
    // clock (OS_RATIO rx periods, offset by the bench's ppm), not on rx_clk
    // edges: the device's sampler recovers it, as from host_uplink_agent.
    // -------------------------------------------------------------------------
    logic rx_serial = 1'b1;
    int   bit_ps;

    /* verilator lint_off ZERODLY */
    initial begin
        wait (rst_n);
        forever begin
            bit_ps = cxp_bench_bit_ps(OS_RATIO);
            #(bit_ps * 1ps);
            rx_serial = cxp_uplink_bit(longint'($realtime * 1000.0), bit_ps) ? 1'b1 : 1'b0;
        end
    end
    /* verilator lint_on ZERODLY */

    // -------------------------------------------------------------------------
    // Pixel port: the bench's PIXEL_FRAME frames, one pixel per app_clk when
    // the frame's valid density says so, held until s_pix_ready takes it.
    // -------------------------------------------------------------------------
    logic        pix_valid, pix_sof, pix_eol, pix_eof;
    logic [15:0] pix_data;
    logic        s_pix_ready;
    cxp_pkg::cxp_meta_t pix_meta;

    always @(posedge app_clk) begin
        if (!rst_n) begin
            pix_valid <= 1'b0;
            pix_sof   <= 1'b0;
            pix_eol   <= 1'b0;
            pix_eof   <= 1'b0;
            pix_data  <= '0;
        end else begin
            int d, f, xs, ys, xo, yo, pf, tg, sid, stag, fl;
            if (pix_valid && s_pix_ready) cxp_bench_pix_taken();
            cxp_bench_meta(xs, ys, xo, yo, pf, tg, sid, stag, fl);
            if (!(pix_valid && !s_pix_ready)) begin
                pix_valid <= cxp_bench_pix_beat(d, f) != 0;
                // The bench's pixel is a sample of the image's format width;
                // the sensor port is 16 bits (p_PIX_W = 16), MSB-aligned.
                pix_data  <= d[15:0] << (16 - int'(cxp_pkg::pixfmt_bits(pf[15:0])));
                pix_sof   <= f[0];
                pix_eol   <= f[1];
                pix_eof   <= f[2];
            end
            pix_meta.arbitrary <= 1'b0;             // the top takes it from cfg_arbitrary
            pix_meta.streamid  <= sid[7:0];
            pix_meta.sourcetag <= stag[15:0];
            pix_meta.xsize     <= xs[23:0];
            pix_meta.ysize     <= ys[23:0];
            pix_meta.xoffs     <= xo[23:0];
            pix_meta.yoffs     <= yo[23:0];
            pix_meta.pixfmt    <= pf[15:0];
            pix_meta.tapg      <= tg[15:0];
            pix_meta.flags     <= fl[7:0];
        end
    end

    // -------------------------------------------------------------------------
    // Register-bus fault: while the bench asks for it, every register access
    // answers with that Table 22 code (the UVM APB responder's PSLVERR).
    // -------------------------------------------------------------------------
    int reg_err_code;
    initial reg_err_code = 0;
    /* verilator lint_off BLKSEQ */  // the force takes the code at once
    always @(posedge rx_clk) begin
        int e;
        e = cxp_bench_reg_err();
        if (e != reg_err_code) begin
            reg_err_code = e;
            if (e != 0) force dut.reg_err = reg_err_code[7:0];
            else        release dut.reg_err;
        end
    end
    /* verilator lint_on BLKSEQ */

    // -------------------------------------------------------------------------
    // User register window (bench REG_STALL): 1024 words of read/write
    // memory on the device's APB master (rx_clk).  Each transfer is answered
    // stall_ms device milliseconds (RX_CLK_KHZ rx_clk cycles each) after its
    // SETUP cycle (-1: never), with PSLVERR as the bench says; a write changes
    // the bytes PSTRB enables.  The master never withdraws a transfer; one it
    // gave up completes here when the stall ends and its answer is dropped.
    // -------------------------------------------------------------------------
    localparam logic [31:0] USER_BASE = 32'h0002_0000;   // = bench.h USER_BASE
    localparam logic [31:0] USER_SIZE = 32'h0000_1000;   // = bench.h USER_SIZE

    logic        apb_psel, apb_penable, apb_pwrite, apb_pready, apb_pslverr;
    logic [31:0] apb_paddr, apb_pwdata, apb_prdata;
    logic [3:0]  apb_pstrb;
    logic [31:0] user_mem [0:1023];
    logic        apb_open, apb_rdy;
    longint      apb_cycles;

    initial for (int i = 0; i < 1024; i++) user_mem[i] = 32'h0;

    always @(posedge rx_clk) begin
        int stall_ms;
        stall_ms = cxp_bench_stall_ms();
        if (!rst_n || !apb_psel) begin
            apb_open <= 1'b0;
            apb_rdy  <= 1'b0;
        end else if (!apb_open) begin               // SETUP
            apb_open   <= 1'b1;
            apb_rdy    <= 1'b0;
            apb_cycles <= 1;
        end else if (apb_penable && apb_rdy) begin  // ACCESS completes on this edge
            if (apb_pwrite)
                for (int b = 0; b < 4; b++)
                    if (apb_pstrb[b]) user_mem[apb_paddr[11:2]][8*b +: 8] <= apb_pwdata[8*b +: 8];
            apb_open <= 1'b0;
            apb_rdy  <= 1'b0;
        end else begin
            apb_cycles <= apb_cycles + 1;
            if (apb_penable && stall_ms >= 0 && apb_cycles >= longint'(stall_ms) * RX_CLK_KHZ)
                apb_rdy <= 1'b1;
        end
    end
    assign apb_pready  = apb_rdy;
    assign apb_prdata  = user_mem[apb_paddr[11:2]];
    assign apb_pslverr = apb_rdy && (cxp_bench_stall_err() != 0);

    // -------------------------------------------------------------------------
    // DUT — cxp_device_top (three clocks; TPG or bench pixel port)
    // -------------------------------------------------------------------------
    logic [31:0] cxp_if_data_o;
    logic [3:0]  cxp_if_kmask_o;

    logic trig_out, trig_glitch;

    cxp_device_top #(
        .p_XML_BLOB_MEM            (p_XML_BLOB_MEM),
        .p_RX_CLK_KHZ              (RX_CLK_KHZ),  // the bench's device millisecond (above)
        .p_ASYNC_CLOCKS            (1'b1),
        .p_FIFO_DEPTH              (1024),
        .p_CTRL_BUF_DEPTH          (64),
        .p_OS_RATIO                (OS_RATIO),
        .p_SAMP_LOCK_HITS          (2),
        .p_LINK_RESET_CLEAR_CYCLES (8),
        .p_TPG_X_SIZE              (TPG_X_SIZE),
        .p_TPG_Y_SIZE              (TPG_Y_SIZE),
        .p_PIXEL_FORMAT_RESET      (TPG_PIXFMT),
        .p_IMAGE1_STREAM_ID_RESET  (TPG_STREAMID),
        .p_USER_BASE               (USER_BASE),
        .p_USER_SIZE               (USER_SIZE)
    ) dut (
        .app_clk               (app_clk),
        .app_rst_n             (rst_dom_n[0]),
        .tx_clk                (tx_clk),
        .tx_rst_n              (rst_dom_n[1]),
        .rx_clk                (rx_clk),
        .rx_rst_n              (rst_dom_n[2]),

        .cfg_use_tpg_i         (cfg_use_tpg),
        .cfg_run_i             (cfg_run),
        .cfg_arbitrary_i       (cfg_arbitrary),
        .cfg_trig_polarity_i   (trig_polarity),
        .from_extension_link_i (ext_link),

        // Local trigger I/O, driven and watched by the bench.
        .trig_i                (trig_in),
        .trig_o                (trig_out),
        .trig_glitch_pulse_o   (trig_glitch),

        // Pixel port (cfg_use_tpg = 0), driven by the bench.
        .s_pix_data_i          (pix_data),
        .s_pix_valid_i         (pix_valid),
        .s_pix_sof_i           (pix_sof),
        .s_pix_eol_i           (pix_eol),
        .s_pix_eof_i           (pix_eof),
        .s_pix_ready_o         (s_pix_ready),
        .s_meta_i              (pix_meta),

        .rx_serial_i           (rx_serial),
        .cxp_if_data_o         (cxp_if_data_o),
        .cxp_if_kmask_o        (cxp_if_kmask_o),

        .m_apb_psel_o          (apb_psel),           // the bench's user window
        .m_apb_penable_o       (apb_penable),
        .m_apb_pwrite_o        (apb_pwrite),
        .m_apb_paddr_o         (apb_paddr),
        .m_apb_pwdata_o        (apb_pwdata),
        .m_apb_pstrb_o         (apb_pstrb),
        .m_apb_prdata_i        (apb_prdata),
        .m_apb_pready_i        (apb_pready),
        .m_apb_pslverr_i       (apb_pslverr),

        .device_user_id_nv_i   (128'h0),
        .device_user_id_o      (),

        .rx_lock_o             (),
        .aligned_o             (),
        .link_detected_o       (),
        .link_reset_active_o   (),
        .rate_to_discovery_o   ()
    );

    // The device's trigger outputs, reported to the host with the sim time.
    always @(trig_out) if (rst_n) cxp_bench_edge(16, int'(trig_out), longint'($time));
    always @(posedge trig_glitch) if (rst_n) cxp_bench_edge(17, 1, longint'($time));

    // -------------------------------------------------------------------------
    // Downlink: hand every DUT TX beat to the bridge; pump c2h periodically
    // so a late-connecting / back-pressured host still drains the stream.
    // -------------------------------------------------------------------------
    logic [9:0] pump_div = '0;

`ifdef VERILATOR
    // Under a converged-eval simulator the clocked read of the registered
    // TX beat is delta-stable, so call the bridge directly from the edge.
    always_ff @(posedge tx_clk) begin
        if (rst_n) begin
            cxp_downlink_word(cxp_if_data_o, {28'h0, cxp_if_kmask_o}, longint'($realtime * 1000.0));
            pump_div <= pump_div + 1'b1;
            if (pump_div == '0)
                cxp_fifo_pump();
        end
    end
`else
    // QuestaSim path: IEEE-1800 region scheduling lets an Active-region
    // read race the NBA update of the sampled signals. Decouple capture
    // from the DPI call through a mailbox: the producer snapshots the beat
    // on the edge, a separate consumer drains it (and pumps c2h) outside
    // the clocked region, so the bridge always sees a settled value.
    typedef struct packed {
        logic [31:0] data;
        logic [3:0]  kmask;
        logic [63:0] time_ps;
    } tx_beat_t;
    mailbox #(tx_beat_t) tx_mbox = new();

    always_ff @(posedge tx_clk) begin
        if (rst_n) begin
            tx_beat_t b;
            b.data    = cxp_if_data_o;
            b.kmask   = cxp_if_kmask_o;
            b.time_ps = 64'(longint'($realtime * 1000.0));
            tx_mbox.put(b);
            pump_div <= pump_div + 1'b1;
        end
    end

    initial begin
        tx_beat_t b;
        forever begin
            tx_mbox.get(b);
            cxp_downlink_word(b.data, {28'h0, b.kmask}, longint'(b.time_ps));
            if (pump_div == '0)
                cxp_fifo_pump();
        end
    end
`endif

endmodule

`default_nettype wire
