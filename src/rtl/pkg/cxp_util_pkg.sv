/*
================================================================================
  cxp_util_pkg
  Generic, protocol-agnostic RTL helper functions.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-28

    Description:
      Reusable combinational helpers that were previously copy-pasted (or
      open-coded inline) across the cxp_* modules: byte broadcast, 32-bit
      byte-swap, sub-block bit-reversal, 4x-replicated-byte majority vote,
      and the two width-guard helpers.

      Width helpers — these are NOT interchangeable, pick by intent:
        * idx_w(n) : bits needed to INDEX n items   (indices 0 .. n-1).
        * cnt_w(n) : bits needed to HOLD a value n  (counts  0 .. n).
      The codebase historically used `(n<=1)?1:$clog2(n)` for the former and
      `(n<=1)?1:$clog2(n+1)` for the latter; keep that mapping when migrating.

    Versions:
        2026-05-28 - 0.1:   - Init

================================================================*/

`timescale 1ns / 1ns

package cxp_util_pkg;

    // Replicate a byte into a 32-bit word (P0..P3).
    function automatic logic [31:0] rep4(input logic [7:0] b);
        rep4 = {b, b, b, b};
    endfunction

    // Reverse the byte order of a 32-bit word (big-endian <-> little-endian).
    function automatic logic [31:0] bswap32(input logic [31:0] w);
        bswap32 = {w[7:0], w[15:8], w[23:16], w[31:24]};
    endfunction

    // Bit-reverse a 6-bit / 4-bit 8B/10B sub-block.
    function automatic logic [5:0] bitrev6(input logic [5:0] x);
        bitrev6 = {x[0], x[1], x[2], x[3], x[4], x[5]};
    endfunction
    function automatic logic [3:0] bitrev4(input logic [3:0] x);
        bitrev4 = {x[0], x[1], x[2], x[3]};
    endfunction

    // Per-byte majority vote over four 4x-replicated header copies.
    function automatic logic [7:0] mvote(input logic [7:0] x0,
                                         input logic [7:0] x1,
                                         input logic [7:0] x2,
                                         input logic [7:0] x3);
        logic [7:0] r;
        for (int i = 0; i < 8; i++) begin
            logic [2:0] ones;
            ones = {2'd0, x0[i]} + {2'd0, x1[i]} + {2'd0, x2[i]} + {2'd0, x3[i]};
            r[i] = (ones >= 3'd3);
        end
        mvote = r;
    endfunction

    // Width to index n items: indices 0 .. n-1.
    function automatic int idx_w(input int n);
        idx_w = (n <= 1) ? 1 : $clog2(n);
    endfunction

    // Width to hold a value n: counts 0 .. n.
    function automatic int cnt_w(input int n);
        cnt_w = (n <= 1) ? 1 : $clog2(n + 1);
    endfunction

endpackage
