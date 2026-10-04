// check_8b10b.c — run the DPI 8B/10B encoder against the golden vectors.
//
//   python3 -m cxp_protocol.vectors --8b10b-text | ./check_8b10b
//
// Each input line is "byte k rd_in sym rd_out" (decimal).  Exits non-zero
// on the first mismatch.
#include <stdio.h>
#include "cxp_8b10b.h"

int main(void) {
    unsigned b, k, rd_in, sym, rd_out, n = 0;
    while (scanf("%u %u %u %u %u", &b, &k, &rd_in, &sym, &rd_out) == 5) {
        int rd = (int)rd_in;
        unsigned got = encode_byte(b, (int)k, &rd);
        if (got != sym || (unsigned)rd != rd_out) {
            fprintf(stderr, "8b10b mismatch: byte 0x%02X k=%u rd=%u: got %03X/%d want %03X/%u\n",
                    b, k, rd_in, got, rd, sym, rd_out);
            return 1;
        }
        n++;
    }
    printf("check_8b10b: %u vectors OK\n", n);
    return n ? 0 : 2;
}
