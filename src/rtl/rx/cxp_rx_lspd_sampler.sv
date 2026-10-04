/*
================================================================================
  cxp_rx_lspd_sampler
  CoaXPress 1.1.1 (CXP-001-2015) §6.7 / §8.2.1 / §8.2.5 — device LS soft sampler.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-27

    Description:
      Device-side low-speed (~20.83 Mbps) uplink soft sampler.
      Implements the "PHY-RX" function described in
      `cxp_camera_ip_modules.md` §4.1 / §4.3 / §4.5 collapsed into one
      block:

        serial bit  -> oversample + edge-aligned phase counter (data CDR)
                    -> bit-serial -> 10b shift register
                    -> K28.5 comma detect -> symbol boundary lock
                    -> Table 15 trigger extractor (3-character window)
                    -> 4x symbol packer -> 40-bit word out (4x10b, P0 = [9:0])

      Spec references:
        * §6.7 / Table 6 — low-speed bit rate, tolerance ±100 ppm
        * §8.2.1         — 8B/10B mapping (K28.5 comma)
        * §8.2.5         — character / IDLE alignment
        * §10.2          — loss of lock (decided by cxp_rx_link_mon)

      Bit recovery:
        The serial line is 2-FF synchronised into the os_clk domain.  A
        free-running phase counter [0..p_OS_RATIO-1] is hard-reset to 0
        on every detected edge of the synchronised serial input.  The
        recovered-bit sample-instant is `phase == p_OS_RATIO/2 - 1`: the
        counter starts one cycle after the synchronised edge is seen, so
        the line is read p_OS_RATIO/2 .. p_OS_RATIO/2 + 1 os-cycles after
        its edge, the middle of the bit.  Bang-bang data oversampling
        CDR; with runs of at most five equal bits (8B/10B) it takes bits
        about 7.5 % short or 12 % long at p_OS_RATIO = 8, and a ±200 ppm
        host with jitter at p_OS_RATIO = 4.

      Symbol boundary lock:
        After each recovered bit we examine the 10-bit window
        `{rx_q2, bit_sr_q[9:1]}` (i.e. what bit_sr_q is about to
        become).  When that window matches K28.5 we tentatively lock
        the symbol boundary at the next-bit instant.  After
        `p_LOCK_HITS` K28.5 hits spaced exactly 10 bits apart the FSM
        transitions to LOCKED and starts packing symbols into 40-bit
        words with the captured K28.5 placed in P0 (matching the
        CoaXPress IDLE word layout K28.5 / K28.1 / K28.1 / K28.1).

      Low-speed trigger (§8.2.4, §8.3.2.1, Table 15):
        The host inserts a six-character trigger packet at any character
        boundary, also inside a word or a packet: leader K28.2 K28.4 K28.4
        (rising) or K28.4 K28.2 K28.2 (falling), then the Delay character
        three times.  Every locked character goes through a 3-character
        window before the packer; when the window holds a leader — two of
        its three characters in place are enough (§8.2.2: one bit error
        must not cost the packet or shift the words) — the leader and the
        three characters after it are taken out, so the words keep the
        lane phase the host's stream had without the trigger.
        A two-of-three match is decided one character later: if the
        window one character on holds a leader exactly (three of three),
        that is the leader and the oldest character was data — a bit
        error that turned D28.2 / D28.4 just before a leader into K28.2 /
        K28.4 would otherwise take the leader one character early, with
        the opposite edge.  Otherwise the two-of-three leader stands and
        the character just received is its first Delay character.
        `trig_valid_o` pulses on the last Delay character, a fixed 60 bit
        intervals after the first leader character started, with the edge
        and the three Delay characters as received (10b; decoded and
        voted by cxp_rx_trigger_lspd).
        The six characters change the running disparity by a known
        amount: once for the rising leader (one K28.2; K28.4 is neutral),
        not for the falling one (two K28.2), and once more if the Delay
        character is not neutral (two of the three received characters
        decide).  That flip is carried on the next character's lane
        (`sym_rd_flip_o`), so the decoder chain continues from the RD it
        had before the trigger even when a trigger character was hit.
        The window adds 3 characters of latency to the uplink.

      Loss of lock:
        The sampler does not judge the link itself: cxp_rx_link_mon
        watches the decoded words for IDLE (§8.2.5.1, §10.2) and pulses
        `resync_i`, which drops the FSM back to HUNT from any state.

      Bit-order convention (matches the rest of the cxp_rx_* RTL):
        * Bit 'a' of the symbol abcdei.fghj is the first bit on the
          serial line, and lands at bit 0 of the 10-bit din format.
        * In a 40-bit word, P0 occupies [9:0] (= the first 10 bits
          received), and P3 occupies [39:30] (= the last 10 bits).

    Versions:
        2026-05-27 - 0.1:   - Restyle to coding-style template
        2026-09-19 - 0.2:   - Loss of lock owned by cxp_rx_link_mon
                              (resync_i); no miss counter here; p_OS_RATIO
                              check; §6.7 tolerance
        2026-09-22 - 0.3:   - Table 15 trigger extracted at character level;
                              per-lane RD override
        2026-09-26 - 0.4:   - Leader voted 2 of 3; the three Delay characters
                              out as received; RD carried as a flip
        2026-09-27 - 0.5:   - Sample instant one cycle earlier: mid-bit
        2026-10-04 - 0.6:   - A two-of-three leader waits one character for
                              an exact leader one character later

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_rx_lspd_sampler #(
    // Oversampling ratio: os_clk frequency = p_OS_RATIO x recovered-bit
    // rate.  Must be even and at least 4 (g_chk_os_ratio).
    parameter int p_OS_RATIO  = 16,
    // Consecutive K28.5 observations (spaced exactly 10 bits apart)
    // before we declare rx_lock_o.  2 hits is robust against a single
    // bit-error K28.5 alias while keeping lock acquisition under 30
    // IDLE bits.
    parameter int p_LOCK_HITS = cxp_pkg::RX_LOCK_HITS_DEFAULT
) (
    // Oversampling clock domain
    input  wire  logic        os_clk,                           // Oversample clock
    input  wire  logic        os_rst_n,                         // Active-low sync reset
    input  wire  logic        rx_serial_i,                      // Raw uplink serial line
    input  wire  logic        resync_i,                         // Drop to HUNT (link monitor)

    // 4x10b parallel word interface (mirrors a vendor SerDes;
    // feeds the 8B/10B decoders in cxp_rx_link).
    output logic [39:0]       sym_out_o,                        // Packed 4x10b word
    output logic              sym_valid_o,                      // One-pulse-per-word strobe
    output logic [3:0]        sym_rd_flip_o,                    // Lane's entering RD inverted
    output logic              rx_lock_o,                        // FSM == ST_LOCKED

    // Table 15 trigger taken out of the character stream
    output logic              trig_valid_o,                     // Pulse on the last Delay char
    output logic [1:0]        trig_edge_o,                      // 01 rising, 10 falling
    output logic [29:0]       trig_dly_o                        // 3 Delay chars, first in [9:0]
);

    import cxp_pkg::*;
    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Localparam ------

    // K28_5_NEG / K28_5_POS (10-bit comma symbols) come from cxp_pkg.
    localparam int                    PHASE_W   = idx_w(p_OS_RATIO);
    localparam logic [PHASE_W-1:0]    SAMPLE_AT = PHASE_W'(p_OS_RATIO/2 - 1);
    localparam logic [PHASE_W-1:0]    PHASE_MAX = PHASE_W'(p_OS_RATIO-1);

    // ------ Types ------

    typedef enum logic [1:0] {
        ST_HUNT    = 2'd0,
        ST_PRELOCK = 2'd1,
        ST_LOCKED  = 2'd2
    } state_t;

    // One character in the trigger window, with the RD flip it carries
    // to the packer.
    typedef struct packed {
        logic [9:0] c;       // 10b character
        logic       v;       // slot holds a character
        logic       f;       // RD entering this character is inverted
    } slot_t;

    // is_k28_5() / is_k28_2() / is_k28_4() come from cxp_pkg.

    // At least two of three.
    function automatic logic maj3(input logic a, input logic b, input logic c);
        maj3 = (a & b) | (a & c) | (b & c);
    endfunction

    // A character that changes the running disparity (not five ones).
    function automatic logic flips(input logic [9:0] c);
        flips = ($countones(c) != 5);
    endfunction

    //=======================================================================
    // Elaboration Checks
    //=======================================================================

    if (p_OS_RATIO < 4 || (p_OS_RATIO % 2) != 0) begin : g_chk_os_ratio
        $error("cxp_rx_lspd_sampler: p_OS_RATIO (=%0d) must be even and >= 4", p_OS_RATIO);
    end
    if (p_LOCK_HITS < 2) begin : g_chk_lock_hits
        $error("cxp_rx_lspd_sampler: p_LOCK_HITS (=%0d) must be >= 2", p_LOCK_HITS);
    end

    //=======================================================================
    // Signals
    //=======================================================================

    logic                                   rx_q1;         // Serial sync stage 1
    logic                                   rx_q2;         // Serial sync stage 2 (used)
    logic                                   rx_q3;         // rx_q2 delayed for edge detect
    logic                                   edge_seen;     // rx_q2 xor rx_q3

    logic [PHASE_W-1:0]                     phase_q;       // Edge-aligned phase counter
    logic                                   bit_sample_pulse; // phase == SAMPLE_AT

    logic [9:0]                             bit_sr_q;      // 10-bit recovered-bit SR
    logic [9:0]                             bit_sr_next;   // {rx_q2, bit_sr_q[9:1]}

    state_t                                 state_q;       // FSM state
    logic [3:0]                             bit_in_sym_q;  // 0..9 — bit within symbol
    logic [1:0]                             word_lane_q;   // 0..3 — lane within word
    logic [39:0]                            word_asm_q;    // Word assembly buffer
    logic [$clog2(p_LOCK_HITS+1)-1:0]       hit_cnt_q;     // PRELOCK boundary K28.5 count
    logic                                   sym_valid_q;   // Reg for sym_valid_o
    logic [39:0]                            sym_out_q;     // Reg for sym_out_o

    logic                                   k28_5_now;     // bit_sr_next == K28.5

    logic                                   sym_stb;       // symbol boundary this cycle
    logic                                   lock_stb;      // PRELOCK -> LOCKED on this K28.5
    logic                                   char_stb;      // locked character complete

    // Table 15 trigger window: [0] oldest, [2] newest
    slot_t                                  win_q [3];
    logic                                   lead_rise;     // window + new char: rising leader 2/3
    logic                                   lead_fall;     // window + new char: falling leader 2/3
    logic                                   exact_rise;    // ... all three in place
    logic                                   exact_fall;
    logic                                   tent_q;        // a 2/3 leader waits one character
    logic [1:0]                             tent_edge_q;   // its edge
    logic                                   tent_keep;     // ... and stands (no exact one after)
    logic [1:0]                             drop_cnt_q;    // Delay characters still to take out
    logic [9:0]                             dly_q [2];     // first two Delay characters
    logic [1:0]                             lead_edge_q;   // edge of the leader being taken out
    logic                                   carry_q;       // flip held by characters taken out
    logic                                   flip_pend_q;   // next character carries an RD flip
    logic                                   pop_v;         // window's oldest goes to the packer
    logic [3:0]                             flip_asm_q;    // RD flips of the word being built
    logic [3:0]                             sym_rd_flip_q;
    logic                                   trig_valid_q;
    logic [1:0]                             trig_edge_q;
    logic [29:0]                            trig_dly_q;

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign edge_seen        = (rx_q2 ^ rx_q3);
    assign bit_sample_pulse = (phase_q == SAMPLE_AT);
    assign bit_sr_next      = {rx_q2, bit_sr_q[9:1]};
    assign k28_5_now        = is_k28_5(bit_sr_next);

    assign sym_out_o        = sym_out_q;
    assign sym_valid_o      = sym_valid_q;
    assign sym_rd_flip_o    = sym_rd_flip_q;
    assign rx_lock_o        = (state_q == ST_LOCKED);

    assign trig_valid_o     = trig_valid_q;
    assign trig_edge_o      = trig_edge_q;
    assign trig_dly_o       = trig_dly_q;

    assign sym_stb   = bit_sample_pulse & (bit_in_sym_q == 4'd9) & ~resync_i;
    assign lock_stb  = sym_stb & (state_q == ST_PRELOCK) & k28_5_now
                       & (hit_cnt_q == ($clog2(p_LOCK_HITS+1))'(p_LOCK_HITS-1));
    assign char_stb  = sym_stb & (state_q == ST_LOCKED);

    // Two of the three leader characters in place (§8.2.2).  The two
    // leaders differ in every position, so both can never match.
    assign lead_rise = win_q[1].v & win_q[2].v
                       & maj3(is_k28_2(win_q[1].c), is_k28_4(win_q[2].c),
                              is_k28_4(bit_sr_next));
    assign lead_fall = win_q[1].v & win_q[2].v
                       & maj3(is_k28_4(win_q[1].c), is_k28_2(win_q[2].c),
                              is_k28_2(bit_sr_next));
    assign exact_rise = win_q[1].v & win_q[2].v & is_k28_2(win_q[1].c)
                        & is_k28_4(win_q[2].c) & is_k28_4(bit_sr_next);
    assign exact_fall = win_q[1].v & win_q[2].v & is_k28_4(win_q[1].c)
                        & is_k28_2(win_q[2].c) & is_k28_2(bit_sr_next);
    // The waiting 2-of-3 leader is the window's three characters; the
    // new one is its first Delay character.
    assign tent_keep = tent_q & ~(exact_rise | exact_fall);
    // The oldest character leaves for the packer unless it is a leader
    // character (tent_keep).
    assign pop_v     = char_stb & (drop_cnt_q == 2'd0) & win_q[0].v & ~tent_keep;

    //=======================================================================
    // Serial input synchroniser (2-FF + edge-detect delay)
    //=======================================================================

    // Serial input synchroniser (2-FF) + one extra delay for edge detect.
    // rx_q2 is the synchronised serial sample used by the rest of the
    // block; rx_q3 is rx_q2 delayed by one os_clk so we can detect
    // edges.
    always_ff @(posedge os_clk or negedge os_rst_n) begin
        if (!os_rst_n) begin
            rx_q1 <= 1'b0;
            rx_q2 <= 1'b0;
            rx_q3 <= 1'b0;
        end else begin
            rx_q1 <= rx_serial_i;
            rx_q2 <= rx_q1;
            rx_q3 <= rx_q2;
        end
    end

    //=======================================================================
    // Edge-aligned phase counter (bang-bang CDR)
    //=======================================================================

    // Hard-resets to 0 on every detected edge so the sample instant
    // (phase == p_OS_RATIO/2 - 1) lands mid-bit.  Between edges the counter
    // free-wheels modulo p_OS_RATIO.
    always_ff @(posedge os_clk or negedge os_rst_n) begin
        if (!os_rst_n)                       phase_q <= '0;
        else if (edge_seen)                  phase_q <= '0;
        else if (phase_q == PHASE_MAX)       phase_q <= '0;
        else                                 phase_q <= phase_q + 1'b1;
    end

    //=======================================================================
    // 10-bit recovered-bit shift register
    //=======================================================================

    // New bit (rx_q2) enters at bit[9]; bit[0] therefore holds the
    // oldest of the last 10 bits.  After 10 bit_sample_pulses since
    // reset, bit_sr_q is a valid din-format 10-bit symbol.
    always_ff @(posedge os_clk or negedge os_rst_n) begin
        if (!os_rst_n)              bit_sr_q <= '0;
        else if (bit_sample_pulse)  bit_sr_q <= bit_sr_next;
    end

    //=======================================================================
    // Lock FSM + symbol-bit counter
    //=======================================================================

    always_ff @(posedge os_clk or negedge os_rst_n) begin
        if (!os_rst_n) begin
            state_q       <= ST_HUNT;
            bit_in_sym_q  <= '0;
            hit_cnt_q     <= '0;
        end else if (resync_i) begin
            // The link monitor saw no IDLE for too long (§10.2).
            state_q      <= ST_HUNT;
            hit_cnt_q    <= '0;
            bit_in_sym_q <= '0;
        end else begin
            if (bit_sample_pulse) begin
                case (state_q)

                    //=======================================================
                    // Look for any K28.5; on hit, enter PRELOCK and start
                    // counting bits to the next expected K28.5.
                    //
                    ST_HUNT: begin
                        if (k28_5_now) begin
                            state_q      <= ST_PRELOCK;
                            bit_in_sym_q <= '0;
                            hit_cnt_q    <= '0;
                        end
                    end

                    //=======================================================
                    // Confirm the symbol boundary.  K28.5 may appear any
                    // integer number of symbols later (e.g. on the next
                    // IDLE word, 4 symbols = 40 bits later).  Each time
                    // bit_in_sym wraps from 9 we check the just-formed
                    // 10-bit window: if it is K28.5 we count a hit; after
                    // p_LOCK_HITS - 1 boundary-aligned K28.5 sightings we
                    // transition to LOCKED.  If K28.5 instead appears
                    // OFF-boundary, the original anchor was a false comma
                    // alias — restart counting from this K28.5.
                    //
                    ST_PRELOCK: begin
                        if (k28_5_now && (bit_in_sym_q != 4'd9)) begin
                            // Off-boundary K28.5 — re-anchor.
                            bit_in_sym_q <= '0;
                            hit_cnt_q    <= '0;
                        end else if (bit_in_sym_q == 4'd9) begin
                            bit_in_sym_q <= '0;
                            if (k28_5_now) begin
                                if (lock_stb) begin
                                    // This K28.5 is the first character
                                    // of the first word (window block).
                                    state_q           <= ST_LOCKED;
                                end else begin
                                    hit_cnt_q         <= hit_cnt_q + 1'b1;
                                end
                            end
                            // Non-K28.5 at the boundary: just keep waiting
                            // for the next IDLE word.  Do NOT reset
                            // hit_cnt — between two K28.5 hits the symbol
                            // at the boundary is K28.1 (three times) or
                            // arbitrary data.  Resetting would prevent
                            // ever locking on real IDLE traffic.
                        end else begin
                            bit_in_sym_q <= bit_in_sym_q + 1'b1;
                        end
                    end

                    //=======================================================
                    // Every 10 bits a character is complete (char_stb):
                    // the window and packer blocks below take it.
                    //
                    ST_LOCKED: begin
                        if (bit_in_sym_q == 4'd9) begin
                            bit_in_sym_q <= '0;
                        end else begin
                            bit_in_sym_q <= bit_in_sym_q + 1'b1;
                        end
                    end

                    default: state_q <= ST_HUNT;
                endcase
            end
        end
    end

    //=======================================================================
    // Table 15 trigger window
    //=======================================================================

    // Each locked character enters the window; the oldest leaves it for
    // the packer (pop_v).  A leader completed by the new character
    // empties the window and the next three characters (Delay) are
    // taken out too.  An RD flip still riding on a character that is
    // taken out (a trigger right behind another) is carried over.
    always_ff @(posedge os_clk or negedge os_rst_n) begin
        if (!os_rst_n) begin
            win_q        <= '{default: '0};
            drop_cnt_q   <= '0;
            dly_q        <= '{default: '0};
            lead_edge_q  <= TRIG_EDGE_NONE;
            carry_q      <= 1'b0;
            flip_pend_q  <= 1'b0;
            tent_q       <= 1'b0;
            tent_edge_q  <= TRIG_EDGE_NONE;
            trig_valid_q <= 1'b0;
            trig_edge_q  <= TRIG_EDGE_NONE;
            trig_dly_q   <= '0;
        end else if (resync_i) begin
            win_q        <= '{default: '0};
            drop_cnt_q   <= '0;
            tent_q       <= 1'b0;
            carry_q      <= 1'b0;
            flip_pend_q  <= 1'b0;
            trig_valid_q <= 1'b0;
        end else begin
            trig_valid_q <= 1'b0;    // pulse default
            if (lock_stb) begin
                win_q       <= '{default: '0};
                win_q[2]    <= '{c: bit_sr_next, v: 1'b1, f: 1'b0};
                drop_cnt_q  <= '0;
                tent_q      <= 1'b0;
                carry_q     <= 1'b0;
                flip_pend_q <= 1'b0;
            end else if (char_stb) begin
                if (drop_cnt_q != 2'd0) begin
                    // A Delay character.
                    drop_cnt_q <= drop_cnt_q - 2'd1;
                    if (drop_cnt_q == 2'd3) dly_q[0] <= bit_sr_next;
                    if (drop_cnt_q == 2'd2) dly_q[1] <= bit_sr_next;
                    if (drop_cnt_q == 2'd1) begin
                        trig_valid_q <= 1'b1;
                        trig_edge_q  <= lead_edge_q;
                        trig_dly_q   <= {bit_sr_next, dly_q[1], dly_q[0]};
                        flip_pend_q  <= carry_q ^ (lead_edge_q == TRIG_EDGE_RISE)
                                      ^ maj3(flips(dly_q[0]), flips(dly_q[1]),
                                             flips(bit_sr_next));
                    end
                end else if (tent_keep) begin
                    // The waiting 2-of-3 leader stands: the window is the
                    // leader, this character its first Delay character.
                    win_q       <= '{default: '0};
                    tent_q      <= 1'b0;
                    drop_cnt_q  <= 2'd2;
                    dly_q[0]    <= bit_sr_next;
                    lead_edge_q <= tent_edge_q;
                    carry_q     <= win_q[0].f ^ win_q[1].f ^ win_q[2].f ^ flip_pend_q;
                    flip_pend_q <= 1'b0;
                end else if (exact_rise | exact_fall) begin
                    win_q       <= '{default: '0};
                    tent_q      <= 1'b0;
                    drop_cnt_q  <= 2'd3;
                    lead_edge_q <= exact_rise ? TRIG_EDGE_RISE : TRIG_EDGE_FALL;
                    carry_q     <= win_q[1].f ^ win_q[2].f ^ flip_pend_q;
                    flip_pend_q <= 1'b0;
                end else begin
                    // A 2-of-3 leader waits one character (see above).
                    tent_q      <= lead_rise | lead_fall;
                    tent_edge_q <= lead_rise ? TRIG_EDGE_RISE : TRIG_EDGE_FALL;
                    win_q[0]    <= win_q[1];
                    win_q[1]    <= win_q[2];
                    win_q[2]    <= '{c: bit_sr_next, v: 1'b1, f: flip_pend_q};
                    flip_pend_q <= 1'b0;
                end
            end
        end
    end

    //=======================================================================
    // Word packer: 4 characters from the window, P0 first
    //=======================================================================

    always_ff @(posedge os_clk or negedge os_rst_n) begin
        if (!os_rst_n) begin
            word_lane_q   <= '0;
            word_asm_q    <= '0;
            flip_asm_q    <= '0;
            sym_out_q     <= '0;
            sym_rd_flip_q <= '0;
            sym_valid_q   <= 1'b0;
        end else if (resync_i) begin
            word_lane_q  <= '0;
            sym_valid_q  <= 1'b0;
        end else begin
            sym_valid_q <= 1'b0;     // pulse default
            if (lock_stb) begin
                word_lane_q <= '0;
            end else if (pop_v) begin
                word_asm_q[10*word_lane_q +: 10] <= win_q[0].c;
                flip_asm_q[word_lane_q]          <= win_q[0].f;
                word_lane_q                      <= word_lane_q + 2'd1;
                if (word_lane_q == 2'd3) begin
                    sym_out_q     <= {win_q[0].c, word_asm_q[29:0]};
                    sym_rd_flip_q <= {win_q[0].f, flip_asm_q[2:0]};
                    sym_valid_q   <= 1'b1;
                end
            end
        end
    end

endmodule

`default_nettype wire
