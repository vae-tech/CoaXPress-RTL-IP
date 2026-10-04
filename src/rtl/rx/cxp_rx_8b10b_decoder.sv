/*
================================================================================
  cxp_rx_8b10b_decoder
  CoaXPress 1.1.1 (CXP-001-2015) §8.2.1 — single-lane combinational 8B/10B dec.
================================================================================

    Author:   Artem Voropaev
    Email:    voropaev.art@gmail.com
    Created:  2026-05-27

    Description:
      Input  : a 10-bit symbol "abcdei fghj", with bit 'a' transmitted first.
               Mapping into the input vector: din_i[0]=a, [1]=b, [2]=c,
               [3]=d, [4]=e, [5]=i, [6]=f, [7]=g, [8]=h, [9]=j.  Plus a
               single rd_in_i bit (0 = RD-, 1 = RD+) giving the running
               disparity ENTERING this symbol.
      Output : 8-bit decoded byte (HGFEDCBA), k_out_o flag, disp_err_o,
               code_err_o, and rd_out_o — the running disparity LEAVING
               this symbol, for chaining into the next decoder instance.

      Purely combinational so a 4-lane parallel deserialiser can chain
      four instances with a single RD register at the top level (see
      cxp_rx_link).  Single-lane callers can register the outputs
      themselves.

      Disparity-error detection (§36.2.4.4 of IEEE 802.3-2008):
        * A non-neutral sub-block of intrinsic disparity +/-2 is only
          legal when the running disparity entering that sub-block is the
          opposite sign.  Any other combination flags `disp_err_o`.
        * D.7 (111000 / 000111) and D.x.3 (1100 / 0011) are neutral but
          still have one form per running disparity; the other form
          flags `disp_err_o` too.
        * The decoder still produces the dout_o/k_out_o value of the
          matched pattern when disp_err_o is set, since the bit pattern
          is uniquely decodable; the error indication tells higher layers
          to treat the symbol as unreliable.

      Code-error detection: a sub-block pattern not present in the
      encoder table, or a y = 7 form the table does not pair with that x
      and RD (P7 where A7 is due, A7 on another data x), flags
      `code_err_o` and dout_o / k_out_o are forced to zero.

    Versions:
        2026-05-27 - 0.1:   - Restyle to coding-style template
        2026-09-19 - 0.2:   - Wrong-RD forms of D.7 and D.x.3 flag disp_err_o
        2026-09-26 - 0.3:   - y = 7 forms checked against x and RD (code_err_o)

================================================================*/

`timescale 1ns / 1ns

`default_nettype none

module cxp_rx_8b10b_decoder (
    input  wire  logic [9:0] din_i,                             // Raw 10b symbol
    input  wire  logic       rd_in_i,                           // 0 = RD-, 1 = RD+ entering

    output logic [7:0]       dout_o,                            // Decoded byte (HGFEDCBA)
    output logic             k_out_o,                           // 1 = K-character
    output logic             disp_err_o,                        // Running-disparity error
    output logic             code_err_o,                        // Pattern not in table
    output logic             rd_out_o                           // RD leaving this symbol
);

    import cxp_util_pkg::*;

    //=======================================================================
    // Local Parameters and Type and Function Definitions
    //=======================================================================

    // ------ Types ------

    typedef enum logic [1:0] {
        POL_NEUT = 2'b00,
        POL_MIN  = 2'b01,
        POL_PLU  = 2'b10
    } pol_t;

    // bitrev6() / bitrev4() come from cxp_util_pkg.

    //=======================================================================
    // Signals
    //=======================================================================

    // Bit-order convention.  din_i[0] is the first-transmitted bit of the
    // 10b symbol (i.e. din_i[0] = 'a').  The IEEE 802.3 tables, however,
    // write the symbol left-to-right as "abcdei fghj" — so the case
    // statements below are most readable if 'a' lands at the MSB of the
    // 6b/4b literal.  We therefore bit-reverse the input to form sub6
    // and sub4: sub6[5] = a, sub6[4] = b, ..., sub6[0] = i, etc.
    logic [5:0] sub6;                 // 6b sub-block, MSB-first 'a'
    logic [3:0] sub4;                 // 4b sub-block, MSB-first 'f'

    logic       k28_rdp_form;         // sub6 == 110000 (K28.x RD+)
    logic [4:0] dec5;                 // 5b decoded value
    pol_t       pol5;                 // Sub-block polarity for 5b/6b
    logic       dec5_err;             // 5b sub-block pattern unknown
    logic       k28_marker;           // K28.x detected at 5b/6b stage

    logic [2:0] dec3;                 // 3b decoded value (uncorrected)
    pol_t       pol3;                 // Sub-block polarity for 3b/4b
    logic       dec3_err;             // 3b sub-block pattern unknown
    logic       k_y7_alt;             // y=7 alternate K-form detected
    logic       kxx_y7_legal;         // Legal x-value when y=7 alt used

    logic       rd_mid;               // Running disparity between sub-blocks

    logic       k28_alias_sub4;       // Aliased sub4 needs y-correction
    logic       a7_due;               // D.x.7 takes the alternate form here
    logic       y7_err;               // y = 7 form not allowed for this x / RD
    logic [2:0] dec3_corr;            // dec3 after K28 RD+ y-aliasing fix

    //=======================================================================
    // Signal Assignments
    //=======================================================================

    assign sub6 = bitrev6(din_i[5:0]);
    assign sub4 = bitrev4(din_i[9:6]);

    // K28.x at RD+ uses the bit-complement of the K28.x RD- fghj sub-block
    // (IEEE 802.3 Table 36-2): e.g. K28.5 RD- fghj = 1010, K28.5 RD+ fghj
    // = 0101.  Combined with the K28-only sub6 = 110000, this swaps the
    // decoded y value through the (neutral) D.x.y pairs (1<->6, 2<->5).
    // pol3 derived from sub4 reflects the actual sub-block disparity so
    // the RD chain is correct without intervention; we only need to undo
    // the y-aliasing on the decoded dec3.
    assign k28_rdp_form = (sub6 == 6'b110000);

    // K-code identification.
    assign kxx_y7_legal = (dec5 == 5'd23) | (dec5 == 5'd27)
                        | (dec5 == 5'd29) | (dec5 == 5'd30);

    // K28 RD+ y-aliasing correction.
    //
    // For K28.{1,2,5,6} (i.e. the four y-values whose D.x.y sub4 has a
    // single neutral form) the IEEE encoding uses the bit-complement of
    // that neutral form at RD+:
    //   K28.1 RD+ fghj = 0110   (D.x.1 fghj would be 1001)
    //   K28.2 RD+ fghj = 1010   (D.x.2 fghj would be 0101)
    //   K28.5 RD+ fghj = 0101   (D.x.5 fghj would be 1010)
    //   K28.6 RD+ fghj = 1001   (D.x.6 fghj would be 0110)
    // After the rev4 bit-reverse this aliases the dec3 lookup to 7-dec3.
    // K28.{0,3,4,7} RD+ are unaffected (polarised or double-neutral
    // forms).  Undo the alias only when:
    //   * sub6 = 110000 (K28 RD+ form), AND
    //   * sub4 in {0110, 1010, 0101, 1001} (the four aliased patterns).
    assign k28_alias_sub4 = (sub4 == 4'b0110) | (sub4 == 4'b1010)
                          | (sub4 == 4'b0101) | (sub4 == 4'b1001);
    assign dec3_corr      = (k28_rdp_form & k28_marker & k28_alias_sub4)
                            ? (3'd7 - dec3) : dec3;

    // IEEE 802.3 Table 36-1: D.x.7 is sent as A7 (0111 / 1000) for x = 17,
    // 18, 20 after RD- and x = 11, 13, 14 after RD+, as P7 otherwise; A7
    // also ends K23.7, K27.7, K29.7, K30.7 and K28.7.  Any other y = 7
    // pairing is not a code-group (and P7 after one of those x makes a
    // false comma).
    assign a7_due = rd_mid ? ((dec5 == 5'd11) | (dec5 == 5'd13) | (dec5 == 5'd14))
                           : ((dec5 == 5'd17) | (dec5 == 5'd18) | (dec5 == 5'd20));
    assign y7_err = k_y7_alt ? ~(k28_marker | kxx_y7_legal | a7_due)
                             : (dec3 == 3'd7) & (k28_marker | a7_due);

    assign code_err_o = dec5_err | dec3_err | y7_err;
    assign dout_o     = code_err_o ? 8'h00 : {dec3_corr, dec5};
    assign k_out_o    = code_err_o ? 1'b0  : k28_marker | (k_y7_alt & kxx_y7_legal);

    //=======================================================================
    // 5b/6b decode
    //=======================================================================

    always_comb begin
        dec5       = 5'h00;
        pol5       = POL_NEUT;
        dec5_err   = 1'b0;
        k28_marker = 1'b0;
        unique case (sub6)
            6'b100111: begin dec5 = 5'd0;  pol5 = POL_MIN;  end // D.0
            6'b011000: begin dec5 = 5'd0;  pol5 = POL_PLU;  end
            6'b011101: begin dec5 = 5'd1;  pol5 = POL_MIN;  end // D.1
            6'b100010: begin dec5 = 5'd1;  pol5 = POL_PLU;  end
            6'b101101: begin dec5 = 5'd2;  pol5 = POL_MIN;  end // D.2
            6'b010010: begin dec5 = 5'd2;  pol5 = POL_PLU;  end
            6'b110001: begin dec5 = 5'd3;  pol5 = POL_NEUT; end // D.3
            6'b110101: begin dec5 = 5'd4;  pol5 = POL_MIN;  end // D.4
            6'b001010: begin dec5 = 5'd4;  pol5 = POL_PLU;  end
            6'b101001: begin dec5 = 5'd5;  pol5 = POL_NEUT; end // D.5
            6'b011001: begin dec5 = 5'd6;  pol5 = POL_NEUT; end // D.6
            6'b111000: begin dec5 = 5'd7;  pol5 = POL_NEUT; end // D.7 (RD-)
            6'b000111: begin dec5 = 5'd7;  pol5 = POL_NEUT; end // D.7 (RD+)
            6'b111001: begin dec5 = 5'd8;  pol5 = POL_MIN;  end // D.8
            6'b000110: begin dec5 = 5'd8;  pol5 = POL_PLU;  end
            6'b100101: begin dec5 = 5'd9;  pol5 = POL_NEUT; end // D.9
            6'b010101: begin dec5 = 5'd10; pol5 = POL_NEUT; end // D.10
            6'b110100: begin dec5 = 5'd11; pol5 = POL_NEUT; end // D.11
            6'b001101: begin dec5 = 5'd12; pol5 = POL_NEUT; end // D.12
            6'b101100: begin dec5 = 5'd13; pol5 = POL_NEUT; end // D.13
            6'b011100: begin dec5 = 5'd14; pol5 = POL_NEUT; end // D.14
            6'b010111: begin dec5 = 5'd15; pol5 = POL_MIN;  end // D.15
            6'b101000: begin dec5 = 5'd15; pol5 = POL_PLU;  end
            6'b011011: begin dec5 = 5'd16; pol5 = POL_MIN;  end // D.16
            6'b100100: begin dec5 = 5'd16; pol5 = POL_PLU;  end
            6'b100011: begin dec5 = 5'd17; pol5 = POL_NEUT; end // D.17
            6'b010011: begin dec5 = 5'd18; pol5 = POL_NEUT; end // D.18
            6'b110010: begin dec5 = 5'd19; pol5 = POL_NEUT; end // D.19
            6'b001011: begin dec5 = 5'd20; pol5 = POL_NEUT; end // D.20
            6'b101010: begin dec5 = 5'd21; pol5 = POL_NEUT; end // D.21
            6'b011010: begin dec5 = 5'd22; pol5 = POL_NEUT; end // D.22
            6'b111010: begin dec5 = 5'd23; pol5 = POL_MIN;  end // D.23
            6'b000101: begin dec5 = 5'd23; pol5 = POL_PLU;  end
            6'b110011: begin dec5 = 5'd24; pol5 = POL_MIN;  end // D.24
            6'b001100: begin dec5 = 5'd24; pol5 = POL_PLU;  end
            6'b100110: begin dec5 = 5'd25; pol5 = POL_NEUT; end // D.25
            6'b010110: begin dec5 = 5'd26; pol5 = POL_NEUT; end // D.26
            6'b110110: begin dec5 = 5'd27; pol5 = POL_MIN;  end // D.27
            6'b001001: begin dec5 = 5'd27; pol5 = POL_PLU;  end
            6'b001110: begin dec5 = 5'd28; pol5 = POL_NEUT; end // D.28
            6'b001111: begin dec5 = 5'd28; pol5 = POL_MIN;  k28_marker = 1'b1; end // K28 (RD-)
            6'b110000: begin dec5 = 5'd28; pol5 = POL_PLU;  k28_marker = 1'b1; end // K28 (RD+)
            6'b101110: begin dec5 = 5'd29; pol5 = POL_MIN;  end // D.29
            6'b010001: begin dec5 = 5'd29; pol5 = POL_PLU;  end
            6'b011110: begin dec5 = 5'd30; pol5 = POL_MIN;  end // D.30
            6'b100001: begin dec5 = 5'd30; pol5 = POL_PLU;  end
            6'b101011: begin dec5 = 5'd31; pol5 = POL_MIN;  end // D.31
            6'b010100: begin dec5 = 5'd31; pol5 = POL_PLU;  end
            default:   begin dec5_err = 1'b1; end
        endcase
    end

    //=======================================================================
    // 3b/4b decode
    //=======================================================================

    always_comb begin
        dec3     = 3'd0;
        pol3     = POL_NEUT;
        dec3_err = 1'b0;
        k_y7_alt = 1'b0;
        unique case (sub4)
            4'b1011: begin dec3 = 3'd0; pol3 = POL_MIN;  end // D.x.0
            4'b0100: begin dec3 = 3'd0; pol3 = POL_PLU;  end
            4'b1001: begin dec3 = 3'd1; pol3 = POL_NEUT; end // D.x.1
            4'b0101: begin dec3 = 3'd2; pol3 = POL_NEUT; end // D.x.2
            4'b1100: begin dec3 = 3'd3; pol3 = POL_NEUT; end // D.x.3 (RD-)
            4'b0011: begin dec3 = 3'd3; pol3 = POL_NEUT; end // D.x.3 (RD+)
            4'b1101: begin dec3 = 3'd4; pol3 = POL_MIN;  end // D.x.4
            4'b0010: begin dec3 = 3'd4; pol3 = POL_PLU;  end
            4'b1010: begin dec3 = 3'd5; pol3 = POL_NEUT; end // D.x.5
            4'b0110: begin dec3 = 3'd6; pol3 = POL_NEUT; end // D.x.6
            4'b1110: begin dec3 = 3'd7; pol3 = POL_MIN;  end // D.x.7 (primary RD-)
            4'b0001: begin dec3 = 3'd7; pol3 = POL_PLU;  end // D.x.7 (primary RD+)
            4'b0111: begin dec3 = 3'd7; pol3 = POL_MIN;  k_y7_alt = 1'b1; end // K-alt RD-
            4'b1000: begin dec3 = 3'd7; pol3 = POL_PLU;  k_y7_alt = 1'b1; end // K-alt RD+
            default: dec3_err = 1'b1;
        endcase
    end

    //=======================================================================
    // Disparity tracking
    //=======================================================================
    //
    // Convention used by the case tables above:
    //   POL_MIN  = "the form used when the running disparity entering is
    //              RD- (the +2-disparity / minimum-encoded form).  Legal
    //              only at rd_in_i=0; exits at RD+."
    //   POL_PLU  = "the form used when entering at RD+ (the -2-disparity
    //              / plus-encoded form).  Legal only at rd_in_i=1; exits
    //              at RD-."
    //   POL_NEUT = "neutral form, both encodings have disparity 0; RD
    //              unchanged through the sub-block."
    always_comb begin
        rd_mid = (pol5 == POL_NEUT) ? rd_in_i :
                 (pol5 == POL_MIN)  ? 1'b1    : 1'b0;
        rd_out_o = (pol3 == POL_NEUT) ? rd_mid  :
                   (pol3 == POL_MIN)  ? 1'b1    : 1'b0;

        disp_err_o = ((pol5 == POL_MIN) & (rd_in_i == 1'b1))
                   | ((pol5 == POL_PLU) & (rd_in_i == 1'b0))
                   | ((pol3 == POL_MIN) & (rd_mid  == 1'b1))
                   | ((pol3 == POL_PLU) & (rd_mid  == 1'b0))
                   // Neutral, but one form per RD: D.7 and D.x.3.
                   | ((sub6 == 6'b111000) & (rd_in_i == 1'b1))
                   | ((sub6 == 6'b000111) & (rd_in_i == 1'b0))
                   | ((sub4 == 4'b1100)   & (rd_mid  == 1'b1))
                   | ((sub4 == 4'b0011)   & (rd_mid  == 1'b0));
    end

endmodule

`default_nettype wire
