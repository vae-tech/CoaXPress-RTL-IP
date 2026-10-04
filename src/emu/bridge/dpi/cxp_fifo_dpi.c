// =============================================================================
// cxp_fifo_dpi.c — Linux-FIFO <-> CoaXPress RTL bridge (DPI-C side)
//
// This is the C half of the pure-SystemVerilog `hw/` environment.  It owns
// ALL CoaXPress link logic so the SystemVerilog testbench can stay a thin
// clock/reset + DUT + bit-pacing shell:
//
//   * Uplink   (host -> camera, low-speed control):
//       reads link frames from the `cxp.h2c` named pipe, deframes the
//       MAGIC/NWORDS envelope into CoaXPress characters (CXP1 word frames,
//       CXC1 character frames), 8B/10B-encodes each character
//       (cxp_8b10b.h, checked against the golden cxp_protocol vectors) and
//       feeds the resulting bits to rx_serial one at a time via
//       cxp_uplink_bit().  When no host data is pending it streams the
//       CoaXPress IDLE word so the device's soft sampler keeps lock.
//
//   * Downlink (camera -> host, high-speed stream / control-ack):
//       cxp_downlink_word() is called every tx_clk with the DUT's raw
//       32-bit word + kmask.  IDLE link fill is stripped; SOP..EOP
//       packets are re-enveloped (MAGIC/NWORDS) and written to `cxp.c2h`.
//       Trigger and I/O-ack packets are taken out, even from inside a
//       packet, and handed over as CXC1 character frames.
//
//   * Bench (CXB1 frames, src/emu/host src/cxp/protocol/bench.h): the device
//       inputs a lab would drive (trigger in, extension-link strap, pixel
//       port, register-bus fault, a slow user register slave, clock periods,
//       host bit rate and bit slips), polled by cxp_hw_env.sv through the
//       cxp_bench_* functions; the device's trigger output comes back as
//       PIN_EDGE events, the sim time of the short packets on either link
//       as SHORT_DL / UPLINK_MARK events.
//
// The envelope and on-wire conventions match exactly:
//   * sw/cxp/transport/fifo.py        (MAGIC 0x43585031, <II header, LE)
//   * sw/cxp/protocol/packets.py      (SOP/EOP words, 4x byte replication)
//   * src/verif/uvm/agents/host_uplink_agent.py (per-word kmask, bit order)
//   * cxp_8b10b.h (8B/10B; `make -C src/emu/bridge check_8b10b` runs it against
//     the golden vectors from cxp_protocol)
//
// All pipe I/O is non-blocking and lazily (re)opened, so the simulator
// never stalls and the host stack may (re)start independently.
// =============================================================================

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// Verilator compiles this translation unit with g++; the DPI entry points
// must keep C linkage so the generated wrappers can resolve them.
#ifdef __cplusplus
#define DPI_LINK extern "C"
#else
#define DPI_LINK
#endif

// ---------------------------------------------------------------------------
// On-wire constants (mirror cxp/protocol/constants.py & cxp_pkg.py)
// ---------------------------------------------------------------------------
#define FRAME_MAGIC 0x43585031u            // b'CXP1' little-endian

#define K28_0 0x1Cu
#define K28_1 0x3Cu
#define K28_2 0x5Cu
#define K28_4 0x9Cu
#define K28_5 0xBCu
#define K27_7 0xFBu
#define K29_7 0xFDu
#define D21_5 0xB5u

// SOP = 4xK27.7, EOP = 4xK29.7, IDLE = K28.5 K28.1 K28.1 D21.5 (P0 in LSB).
#define SOP_WORD  0xFBFBFBFBu
#define EOP_WORD  0xFDFDFDFDu
#define IDLE_WORD ((D21_5 << 24) | (K28_1 << 16) | (K28_1 << 8) | K28_5)
#define IDLE_KMASK 0x7u

#define MAX_FRAME_WORDS (1u << 20)         // sanity bound (matches fifo.py)

#include "cxp_8b10b.h"

// =============================================================================
// Linux FIFO endpoints (non-blocking, lazily (re)opened)
// =============================================================================
static char g_h2c_path[1024];
static char g_c2h_path[1024];
static int  g_h2c_fd = -1;   // host -> camera (we read)
static int  g_c2h_fd = -1;   // camera -> host (we write)
static int  g_dbg    = 0;    // CXP_HW_DEBUG=1 -> per-frame tracing

static void try_open_h2c(void) {
    if (g_h2c_fd >= 0) return;
    int fd = open(g_h2c_path, O_RDONLY | O_NONBLOCK);
    if (fd >= 0) {
        g_h2c_fd = fd;
        fprintf(stderr, "[cxp-hw] h2c open: %s\n", g_h2c_path);
    }
}

static void try_open_c2h(void) {
    if (g_c2h_fd >= 0) return;
    // O_WRONLY|O_NONBLOCK fails with ENXIO until the host opens the read
    // end; we simply retry on the next downlink word.
    int fd = open(g_c2h_path, O_WRONLY | O_NONBLOCK);
    if (fd >= 0) {
        g_c2h_fd = fd;
        fprintf(stderr, "[cxp-hw] c2h open: %s\n", g_c2h_path);
    }
}

// ----------------------------------------------------------------------------
// Uplink: byte buffer -> deframed character queue
//
// The h2c pipe carries three kinds of link frame, told apart by the magic
// (src/emu/host src/cxp/protocol/chars.h, bench.h):
//   CXP1  a word frame; its first word's K27.7 lanes and its last word's
//         K29.7 lanes are K characters (frame_kmask), the rest data;
//   CXC1  characters, one per word: bits 7:0 the character, bit 8 the K
//         flag — Table 15 triggers, Table 17 acks, a trigger inside a
//         command (§8.2.4), anything that is not whole SOP..EOP words;
//   CXB1  a bench op (bench_op below), carried out at once, not sent.
// Characters leave in queue order; IDLE words fill the gaps.
// ----------------------------------------------------------------------------
#define CHARS_MAGIC 0x43584331u            // 'CXC1'
#define BENCH_MAGIC 0x43584231u            // 'CXB1'

static unsigned char *u_buf = NULL;   // raw bytes from h2c
static size_t u_len = 0, u_cap = 0;

typedef struct { uint16_t *c; size_t n, cap, head; } cq_t;  // char | K << 8
static cq_t u_q;                      // pending uplink characters (FIFO)

static void cq_push(cq_t *q, uint16_t v) {
    if (q->head > 0 && q->n + q->head == q->cap) {
        memmove(q->c, q->c + q->head, q->n * sizeof(uint16_t));
        q->head = 0;
    }
    if (q->n + q->head == q->cap) {
        q->cap = q->cap ? q->cap * 2 : 16384;
        q->c = (uint16_t *)realloc(q->c, q->cap * sizeof(uint16_t));
    }
    q->c[q->head + q->n++] = v;
}
static int cq_pop(cq_t *q, uint16_t *v) {
    if (q->n == 0) return 0;
    *v = q->c[q->head++];
    q->n--;
    if (q->n == 0) q->head = 0;
    return 1;
}

static uint32_t rd_le32(const unsigned char *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static int known_magic(uint32_t m) {
    return m == FRAME_MAGIC || m == CHARS_MAGIC || m == BENCH_MAGIC;
}

// Lanes of w (P0 = LSB) that carry byte k.
static unsigned lanes_equal(uint32_t w, uint8_t k) {
    unsigned m = 0;
    for (int i = 0; i < 4; i++)
        if (((w >> (8 * i)) & 0xFFu) == k) m |= 1u << i;
    return m;
}

static int popcount4(unsigned m) {
    return (int)(m & 1u) + (int)(m >> 1 & 1u) + (int)(m >> 2 & 1u) + (int)(m >> 3 & 1u);
}

// kmask for a frame word at position idx: the lanes of the first word that
// carry K27.7 and of the last word that carry K29.7 are K characters, when
// at least 3 of the 4 do; everything else is data (0).  A clean SOP/EOP is
// all-K (0xF), as host_uplink_agent._build_*_words sends it; a host that
// corrupts one character of it (CXP-CAM-PROT-006) gets exactly that one
// character sent as data next to three good K characters, as on a real
// link.  Fewer than 3 matching lanes is not a delimiter: all data.
static unsigned frame_kmask(uint32_t w, size_t idx, size_t len) {
    unsigned m = 0;
    if (idx == 0)
        m = lanes_equal(w, (uint8_t)SOP_WORD);
    else if (idx + 1 == len)
        m = lanes_equal(w, (uint8_t)EOP_WORD);
    return popcount4(m) >= 3 ? m : 0x0;
}

static void bench_op(const uint32_t *w, uint32_t n);

// Pull whatever bytes are available, then take every complete frame.
static void u_refill(void) {
    try_open_h2c();
    if (g_h2c_fd < 0) return;

    unsigned char tmp[8192];
    for (;;) {
        ssize_t r = read(g_h2c_fd, tmp, sizeof(tmp));
        if (r > 0) {
            if (u_len + (size_t)r > u_cap) {
                while (u_len + (size_t)r > u_cap) u_cap = u_cap ? u_cap * 2 : 65536;
                u_buf = (unsigned char *)realloc(u_buf, u_cap);
            }
            memcpy(u_buf + u_len, tmp, (size_t)r);
            u_len += (size_t)r;
            continue;                 // drain everything currently available
        }
        if (r == 0)                   // no writer currently attached: a
            break;                    // FIFO read returns 0 with no peer —
                                      // keep the fd; a new writer resumes it
        break;                        // EAGAIN / EWOULDBLOCK -> nothing more
    }

    // Deframe: [MAGIC u32][NWORDS u32][NWORDS x u32], LE. Resync on garbage.
    size_t off = 0;
    for (;;) {
        if (u_len - off < 4) break;
        if (!known_magic(rd_le32(u_buf + off))) {
            // Slide one byte at a time to the next plausible magic.
            size_t i = off + 1;
            for (; i + 4 <= u_len; i++)
                if (known_magic(rd_le32(u_buf + i))) break;
            off = (i + 4 <= u_len) ? i : (u_len >= 3 ? u_len - 3 : off);
            if (i + 4 > u_len) break;
            continue;
        }
        if (u_len - off < 8) break;
        const uint32_t magic = rd_le32(u_buf + off);
        uint32_t nwords = rd_le32(u_buf + off + 4);
        if (nwords > MAX_FRAME_WORDS) { off += 4; continue; } // bad len
        size_t need = 8 + (size_t)nwords * 4;
        if (u_len - off < need) break;
        const unsigned char *body = u_buf + off + 8;
        if (magic == FRAME_MAGIC) {
            for (uint32_t k = 0; k < nwords; k++) {
                const uint32_t w = rd_le32(body + 4 * k);
                const unsigned km = frame_kmask(w, k, nwords);
                for (int l = 0; l < 4; l++)
                    cq_push(&u_q, (uint16_t)(((w >> (8 * l)) & 0xFFu) | (((km >> l) & 1u) << 8)));
            }
        } else if (magic == CHARS_MAGIC) {
            for (uint32_t k = 0; k < nwords; k++)
                cq_push(&u_q, (uint16_t)(rd_le32(body + 4 * k) & 0x1FFu));
        } else {
            uint32_t *w = (uint32_t *)malloc((nwords ? nwords : 1) * sizeof(uint32_t));
            for (uint32_t k = 0; k < nwords; k++) w[k] = rd_le32(body + 4 * k);
            bench_op(w, nwords);
            free(w);
        }
        if (g_dbg)
            fprintf(stderr, "[cxp-hw] h2c %s in: %u words (w0=%08X)\n",
                    magic == FRAME_MAGIC ? "frame" : magic == CHARS_MAGIC ? "chars" : "bench",
                    nwords, nwords > 0 ? rd_le32(body) : 0);
        off += need;
    }
    if (off > 0) {
        memmove(u_buf, u_buf + off, u_len - off);
        u_len -= off;
    }
}

// Serializer state: the 10-bit symbol being shifted out bit by bit.
static unsigned cur_sym = 0;
static int      cur_bit = 10;         // forces a fresh fetch on first call
static int      g_rd = 0;             // running disparity (0 = RD-)
static int      idle_pos = 0;         // next character of the IDLE word, 0 = none started
static long long g_now_ps = 0;        // bench time of the bit being fetched
static int      g_bit_ps = 0;         // the host bit period in effect
static int      prev_short_k = 0;     // the run the previous character belongs to (UPLINK_MARK), 0 none

// Bench UPLINK_BITS (bench.h): the host line dropping, repeating or holding bits.
static uint32_t ub_mode = 0, ub_n = 0;
static int      ub_last = 1;          // the last bit on the line

static void bench_uplink_mark(uint16_t c);

// Fetch the next character, encode it, prime cur_sym/cur_bit.  An IDLE
// word, once started, is sent whole; queued characters follow it.
static void next_symbol(void) {
    uint16_t c;
    if (idle_pos == 0) {
        if (u_q.n == 0) u_refill();
        if (cq_pop(&u_q, &c)) {
            // The first character of a Table 15 trigger or Table 17 ack.
            // The first character of a run of K27.7 (SOP), of K29.7 (EOP), of
            // K28.2 / K28.4 (a Table 15 trigger) or of K28.6 (a Table 17 ack).
            const unsigned ch = c & 0xFFu;
            const int mark_k = !(c & 0x100u) ? 0
                             : (ch == K28_2 || ch == K28_4) ? 1
                             : ch == 0xDCu ? 2 : ch == K27_7 ? 3 : ch == K29_7 ? 4 : 0;
            if (mark_k && mark_k != prev_short_k) bench_uplink_mark(c);
            prev_short_k = mark_k;
            cur_sym = encode_byte(c & 0xFFu, (c >> 8) & 1u, &g_rd);
            cur_bit = 0;
            return;
        }
    }
    prev_short_k = 0;
    const unsigned w = (unsigned)IDLE_WORD;
    cur_sym = encode_byte((w >> (8 * idle_pos)) & 0xFFu, (IDLE_KMASK >> idle_pos) & 1u, &g_rd);
    idle_pos = (idle_pos + 1) & 3;
    cur_bit = 0;
}

// ----------------------------------------------------------------------------
// Downlink: SOP..EOP reassembly + envelope writer
// ----------------------------------------------------------------------------
static uint32_t *d_frame = NULL;
static size_t    d_n = 0, d_cap = 0;
static int       d_in_frame = 0;

static unsigned char *d_pending = NULL;   // bytes queued for c2h
static size_t d_plen = 0, d_pcap = 0;
#define D_PENDING_MAX (16u << 20)

static void d_pending_push(const unsigned char *p, size_t n) {
    if (d_plen + n > D_PENDING_MAX) { d_plen = 0; } // overflow -> resync
    if (d_plen + n > d_pcap) {
        while (d_plen + n > d_pcap) d_pcap = d_pcap ? d_pcap * 2 : 65536;
        d_pending = (unsigned char *)realloc(d_pending, d_pcap);
    }
    memcpy(d_pending + d_plen, p, n);
    d_plen += n;
}

static void d_flush(void) {
    try_open_c2h();
    if (g_c2h_fd < 0 || d_plen == 0) return;
    size_t off = 0;
    while (off < d_plen) {
        ssize_t w = write(g_c2h_fd, d_pending + off, d_plen - off);
        if (w > 0) { off += (size_t)w; continue; }
        if (w < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) break;
        // EPIPE / other: host went away -> drop, reopen next time.
        close(g_c2h_fd);
        g_c2h_fd = -1;
        off = d_plen;                 // discard; a fresh reader resyncs
        break;
    }
    if (off > 0) {
        memmove(d_pending, d_pending + off, d_plen - off);
        d_plen -= off;
    }
}

static void wr_le32(unsigned char *p, uint32_t v) {
    p[0] = v & 0xFF; p[1] = (v >> 8) & 0xFF;
    p[2] = (v >> 16) & 0xFF; p[3] = (v >> 24) & 0xFF;
}

static void d_emit(uint32_t magic, const uint32_t *w, size_t n) {
    size_t bytes = 8 + n * 4;
    unsigned char *e = (unsigned char *)malloc(bytes);
    wr_le32(e, magic);
    wr_le32(e + 4, (uint32_t)n);
    for (size_t i = 0; i < n; i++)
        wr_le32(e + 8 + 4 * i, w[i]);
    d_pending_push(e, bytes);
    free(e);
    d_flush();
}

static long long d_sop_ps = 0;        // bench time of the open packet's SOP
static void bench_frame_event(long long end_ps);

static void d_emit_frame(void) { d_emit(FRAME_MAGIC, d_frame, d_n); }

// A Table 16 trigger or Table 17 I/O ack (§8.3.2/§8.3.3): 4 x K28.4 / K28.2 /
// K28.6 then 4 data characters, possibly inserted into a packet at a word
// boundary (§8.2.4).  It is taken out of the packet and handed to the host
// as one CXC1 frame of its 8 characters; the packet continues intact.
#define K28_6 0xDCu
static int      d_short = 0;          // first word of a short packet seen
static uint32_t d_short_w0 = 0;
static unsigned d_short_k0 = 0;
static long long d_short_ps = 0;      // bench time of its first word
static int      d_short_in = 0;       // inserted into an open packet
static size_t   d_short_idx = 0;      // words of that packet before it

// Bench DL_STATS counters (bench.h), since the previous DL_STATS.
static uint32_t st_packets = 0, st_idle_in = 0, st_short_in = 0, st_short_out = 0;

static void bench_short_event(uint32_t w0, uint32_t w1);

static int is_short_head(uint32_t data, unsigned kmask) {
    if (kmask != 0xF) return 0;
    return popcount4(lanes_equal(data, (uint8_t)K28_4)) >= 3 ||
           popcount4(lanes_equal(data, (uint8_t)K28_2)) >= 3 ||
           popcount4(lanes_equal(data, (uint8_t)K28_6)) >= 3;
}

static void d_emit_short(uint32_t w1, unsigned k1) {
    uint32_t c[8];
    for (int l = 0; l < 4; l++) {
        c[l]     = ((d_short_w0 >> (8 * l)) & 0xFFu) | (((d_short_k0 >> l) & 1u) << 8);
        c[4 + l] = ((w1 >> (8 * l)) & 0xFFu) | (((k1 >> l) & 1u) << 8);
    }
    if (g_dbg)
        fprintf(stderr, "[cxp-hw] c2h short packet out: %08X %08X\n", d_short_w0, w1);
    d_emit(CHARS_MAGIC, c, 8);
    if (d_short_in) st_short_in++; else st_short_out++;
    bench_short_event(d_short_w0, w1);
}

// =============================================================================
// Bench: what a lab wires to the device besides the link
// (src/emu/host src/cxp/protocol/bench.h).  The SV top polls the state below and
// reports the device's trigger output back through cxp_bench_edge().
// =============================================================================
enum { OP_HELLO = 1, OP_PIN = 2, OP_REG_ERR = 3, OP_UPLINK_PPM = 5,   /* 4 retired */
       OP_PIXEL_FRAME = 6, OP_SYNC = 7, OP_GET_PINS = 8, OP_RESET = 9, OP_REG_STALL = 10,
       OP_PIXEL_BEATS = 11, OP_UPLINK_BITS = 12, OP_DL_STATS = 13, OP_REPLY = 0x80,
       EV_PIN_EDGE = 0x90, EV_SHORT_DL = 0x91, EV_UPLINK_MARK = 0x92, EV_FRAME_DL = 0x93 };
enum { PIN_TRIG_IN = 1, PIN_EXT_LINK = 2, PIN_TRIG_POLARITY = 3, PIN_USE_TPG = 4,
       PIN_TPG_RUN = 5, PIN_ARBITRARY = 6 };
#define BENCH_VERSION 1u
#define BENCH_CAPS 0x1FEFFu // every capability of bench.h: CHARS .. TIMES (bit 8 retired)

static uint32_t b_pins[8];            // indexed by PIN_*
static uint32_t b_reg_err = 0;
static const uint32_t b_clk_ps[3] = {10000, 10000, 10000};   // app, tx, rx periods
static int32_t  b_ppm = 0;
static uint32_t b_rx_khz = 1000;      // the device's rx_clk cycles per millisecond (cxp_hw_env)
static uint32_t b_stall_ms = 0;       // REG_STALL: user-window answer delay, 0xFFFFFFFF never
static uint32_t b_stall_err = 0;      //            and its PSLVERR

typedef struct {
    uint32_t meta[9];                 // xsize ysize xoffs yoffs pixfmt tapg streamid sourcetag flags
    uint32_t permille;
    uint32_t npix;
    uint16_t *px;
    uint8_t *fl;                      // PIXEL_BEATS: each beat's SOF / EOL / EOF, else NULL
} pixframe_t;
static pixframe_t *b_frames = NULL;   // queued pixel-port frames
static size_t b_nframes = 0, b_frame_cap = 0;
static size_t b_pix = 0;              // next pixel of b_frames[0]
static uint32_t b_rng = 0x2545F491u;
static uint32_t b_last_meta[9];       // the pixel port's metadata while no frame is queued
static int b_reset_req = 0;          // RESET domains asked for (bit 0 app, 1 tx, 2 rx), not yet taken

static void bench_reply(const uint32_t *w, size_t n) { d_emit(BENCH_MAGIC, w, n); }

static void frame_push(pixframe_t f) {
    if (b_nframes == b_frame_cap) {
        b_frame_cap = b_frame_cap ? b_frame_cap * 2 : 8;
        b_frames = (pixframe_t *)realloc(b_frames, b_frame_cap * sizeof(pixframe_t));
    }
    b_frames[b_nframes++] = f;
}

static void frames_drop(void) {
    for (size_t i = 0; i < b_nframes; i++) { free(b_frames[i].px); free(b_frames[i].fl); }
    b_nframes = 0;
    b_pix = 0;
}

static uint32_t bench_pin_bits(void) {
    uint32_t v = 0;
    for (int p = 1; p <= PIN_ARBITRARY; p++) v |= (b_pins[p] ? 1u : 0u) << (p - 1);
    return v;
}

static void bench_op(const uint32_t *w, uint32_t n) {
    if (n == 0) return;
    const uint32_t a1 = n > 1 ? w[1] : 0, a2 = n > 2 ? w[2] : 0;
    switch (w[0]) {
    case OP_HELLO: {
        const uint32_t r[3] = {OP_HELLO | OP_REPLY, BENCH_VERSION, BENCH_CAPS};
        bench_reply(r, 3);
        break;
    }
    case OP_PIN:
        if (a1 < 8) b_pins[a1] = a2;
        break;
    case OP_REG_ERR:
        b_reg_err = a1 & 0xFFu;
        break;
    case OP_UPLINK_PPM:
        b_ppm = (int32_t)a1;
        break;
    case OP_PIXEL_FRAME: {
        if (n < 12) break;
        pixframe_t f;
        memcpy(f.meta, w + 1, sizeof(f.meta));
        f.permille = w[10] ? w[10] : 1000;
        f.npix = w[11];
        f.px = (uint16_t *)calloc(f.npix ? f.npix : 1, sizeof(uint16_t));
        f.fl = NULL;
        for (uint32_t i = 0; i < f.npix && 12 + i / 2 < n; i++)
            f.px[i] = (uint16_t)(w[12 + i / 2] >> (16 * (i % 2)));
        frame_push(f);
        break;
    }
    case OP_PIXEL_BEATS: {
        if (n < 12) break;
        pixframe_t f;
        memcpy(f.meta, w + 1, sizeof(f.meta));
        f.permille = w[10] ? w[10] : 1000;
        f.npix = w[11];
        f.px = (uint16_t *)calloc(f.npix ? f.npix : 1, sizeof(uint16_t));
        f.fl = (uint8_t *)calloc(f.npix ? f.npix : 1, 1);
        for (uint32_t i = 0; i < f.npix && 12 + i < n; i++) {
            f.px[i] = (uint16_t)w[12 + i];
            f.fl[i] = (uint8_t)((w[12 + i] >> 16) & 0x7u);
        }
        frame_push(f);
        break;
    }
    case OP_REG_STALL:
        b_stall_ms = a1;
        b_stall_err = a2 & 1u;
        break;
    case OP_UPLINK_BITS:
        ub_mode = a1;
        ub_n = a2;
        break;
    case OP_DL_STATS: {
        const uint32_t r[5] = {OP_DL_STATS | OP_REPLY, st_packets, st_idle_in, st_short_in, st_short_out};
        st_packets = st_idle_in = st_short_in = st_short_out = 0;
        bench_reply(r, 5);
        break;
    }
    case OP_SYNC: {
        // With the bench time (the uplink's last bit) and the length of the
        // device's millisecond in ps, for CAP_TIMES hosts.
        const unsigned long long ms_ps = (unsigned long long)b_rx_khz * b_clk_ps[2];
        const uint32_t r[6] = {OP_SYNC | OP_REPLY, a1, (uint32_t)g_now_ps,
                               (uint32_t)((unsigned long long)g_now_ps >> 32), (uint32_t)ms_ps,
                               (uint32_t)(ms_ps >> 32)};
        bench_reply(r, 6);
        break;
    }
    case OP_GET_PINS: {
        const uint32_t r[2] = {OP_GET_PINS | OP_REPLY, bench_pin_bits()};
        bench_reply(r, 2);
        break;
    }
    case OP_RESET:
        // Power-on state: the inputs as asked, no register fault, no frame
        // queued.  The SV top pulses the reset and calls cxp_bench_reset_done().
        for (int p = 1; p <= PIN_ARBITRARY; p++) b_pins[p] = (a1 >> (p - 1)) & 1u;
        b_reg_err = 0;
        b_stall_ms = b_stall_err = 0;
        frames_drop();
        memset(b_last_meta, 0, sizeof(b_last_meta));
        b_reset_req = (n > 2 && (a2 & 7u)) ? (int)(a2 & 7u) : 7;
        break;
    default:
        fprintf(stderr, "[cxp-hw] bench: op %u not known\n", w[0]);
    }
}

// =============================================================================
// DPI-C entry points (called from cxp_hw_env.sv)
// =============================================================================
DPI_LINK void cxp_fifo_init(const char *h2c, const char *c2h) {
    // A host that closes its read end mid-stream must not kill the
    // simulator: take EPIPE on write() instead of the default SIGPIPE.
    signal(SIGPIPE, SIG_IGN);
    { const char *e = getenv("CXP_HW_DEBUG"); g_dbg = (e && *e && *e != '0'); }
    snprintf(g_h2c_path, sizeof(g_h2c_path), "%s", h2c);
    snprintf(g_c2h_path, sizeof(g_c2h_path), "%s", c2h);
    fprintf(stderr, "[cxp-hw] bridge init  h2c=%s  c2h=%s\n",
            g_h2c_path, g_c2h_path);
    try_open_h2c();
    try_open_c2h();
}

// Next bit to drive onto rx_serial (LSB-first within each 10-bit symbol,
// matching host_uplink_agent's `(sym40 >> bitn) & 1`).  Always returns a
// valid bit: IDLE words are synthesised whenever the host is quiet.
static int serial_bit(void) {
    if (cur_bit >= 10) next_symbol();
    int b = (int)((cur_sym >> cur_bit) & 1u);
    cur_bit++;
    return b;
}

DPI_LINK int cxp_uplink_bit(long long time_ps, int bit_ps) {
    g_now_ps = time_ps;
    g_bit_ps = bit_ps;
    if (ub_n == 0) ub_mode = 0;
    switch (ub_mode) {
    case 0:                           // drop n bits: they never reach the line
        for (; ub_n > 0; ub_n--) (void)serial_bit();
        break;
    case 1:                           // the current bit n more times
        if (ub_n != 0xFFFFFFFFu) ub_n--;
        return ub_last;
    case 2:
    case 3:                           // the line held at a level
        if (ub_n != 0xFFFFFFFFu) ub_n--;
        return ub_last = (int)(ub_mode & 1u);
    default:
        ub_n = 0;
    }
    return ub_last = serial_bit();
}

// One DUT TX beat (raw 32-bit word + 4-bit kmask) -> c2h.  IDLE link fill
// is stripped; SOP..EOP packets are re-enveloped for the host stack.
DPI_LINK void cxp_downlink_word(unsigned int data, unsigned int kmask, long long time_ps) {
    kmask &= 0xF;
    if (d_short) {                    // second word of a trigger / I/O ack
        d_emit_short(data, kmask);
        d_short = 0;
        return;
    }
    if (is_short_head(data, kmask)) {
        d_short = 1;
        d_short_w0 = data;
        d_short_k0 = kmask;
        d_short_ps = time_ps;
        d_short_in = d_in_frame;
        d_short_idx = d_n;
        return;
    }
    int is_idle = (kmask == IDLE_KMASK) && (data == (uint32_t)IDLE_WORD);
    int is_sop  = (kmask == 0xF) && (data == SOP_WORD);
    int is_eop  = (kmask == 0xF) && (data == EOP_WORD);

    if (d_in_frame && is_sop && d_n > 0) {
        // K27.7 inside an open packet: the device cut that packet short
        // (no K29.7).  Only trigger and I/O-ack packets, which carry no
        // SOP, may be inserted into a packet (§8.2.4), so this SOP starts
        // the next packet.  Hand the cut one over as it is, without EOP,
        // for the host to flag, instead of swallowing the next packet.
        if (g_dbg)
            fprintf(stderr, "[cxp-hw] c2h frame cut short: n=%zu (no EOP)\n", d_n);
        d_emit_frame();
        d_in_frame = 0;
    }
    if (!d_in_frame) {
        if (!is_sop) return;          // outside a packet: IDLE / garbage
        d_in_frame = 1;
        d_n = 0;
        d_sop_ps = time_ps;
    }
    if (is_idle) {                    // mid-packet IDLE insert -> drop fill
        st_idle_in++;
        return;
    }

    if (d_n >= d_cap) {
        d_cap = d_cap ? d_cap * 2 : 8192;
        if (d_cap > MAX_FRAME_WORDS + 2) { // runaway -> abort frame, resync
            d_in_frame = 0; d_n = 0; return;
        }
        d_frame = (uint32_t *)realloc(d_frame, d_cap * sizeof(uint32_t));
    }
    d_frame[d_n++] = data;

    if (is_eop) {
        // body[0] = TYPE word (4x replicated); low byte is the type.
        unsigned tb = (d_n >= 2) ? (d_frame[1] & 0xFF) : 0xFF;
        if (g_dbg && tb != 0x01)    // not a video-stream packet -> notable
            fprintf(stderr, "[cxp-hw] c2h frame out: TYPE=0x%02X n=%zu\n",
                    tb, d_n);
        d_emit_frame();
        st_packets++;
        if (tb != 0x01) bench_frame_event(time_ps);
        d_in_frame = 0;
        d_n = 0;
    }
}

// Opportunistic c2h drain — called periodically so a late-connecting or
// back-pressured host eventually receives buffered stream bytes.
DPI_LINK void cxp_fifo_pump(void) {
    u_refill();                       // bench ops take effect while the uplink is busy
    d_flush();
}

// -- bench, polled by cxp_hw_env.sv ----------------------------------------
DPI_LINK void cxp_bench_init(int use_tpg, int run, int arbitrary, int rx_khz) {
    if (rx_khz > 0) b_rx_khz = (uint32_t)rx_khz;
    b_pins[PIN_USE_TPG] = (uint32_t)use_tpg;
    b_pins[PIN_TPG_RUN] = (uint32_t)run;
    b_pins[PIN_ARBITRARY] = (uint32_t)arbitrary;
}

// A bench RESET is pending: returns its domain mask (bit 0 app, 1 tx, 2 rx)
// once, when the SV top takes it, else 0.
DPI_LINK int cxp_bench_reset_req(void) {
    const int r = b_reset_req;
    b_reset_req = 0;
    return r;
}

// The device is out of the bench's reset.  A packet the reset cut is
// dropped (it was the device before the reset), then the host is told.
DPI_LINK void cxp_bench_reset_done(void) {
    d_in_frame = 0;
    d_n = 0;
    d_short = 0;
    const uint32_t r[1] = {OP_RESET | OP_REPLY};
    bench_reply(r, 1);
}

// The device inputs: bit (pin - 1) of the result.
DPI_LINK int cxp_bench_pins(void) { return (int)bench_pin_bits(); }

DPI_LINK int cxp_bench_reg_err(void) { return (int)b_reg_err; }

// REG_STALL: the user-window slave's answer delay (ms, -1 never) and PSLVERR.
DPI_LINK int cxp_bench_stall_ms(void) { return b_stall_ms == 0xFFFFFFFFu ? -1 : (int)b_stall_ms; }
DPI_LINK int cxp_bench_stall_err(void) { return (int)b_stall_err; }

// Half period of clock `which` (0 app, 1 tx, 2 rx), in ps.
DPI_LINK int cxp_bench_half_ps(int which) { return (int)(b_clk_ps[(which >= 0 && which < 3) ? which : 0] / 2); }

// Host bit period in ps: OS_RATIO rx periods (the nominal follows rx, as
// host_uplink_agent's does), offset by the host's ppm.
DPI_LINK int cxp_bench_bit_ps(int os_ratio) {
    const double nominal = (double)os_ratio * (double)b_clk_ps[2];
    return (int)(nominal * (1.0 + (double)b_ppm * 1e-6) + 0.5);
}

// A device output changed (pin: 16 TRIG_OUT, 17 TRIG_GLITCH).
DPI_LINK void cxp_bench_edge(int pin, int value, long long time_ns) {
    const uint32_t r[5] = {EV_PIN_EDGE, (uint32_t)pin, (uint32_t)value, (uint32_t)time_ns,
                           (uint32_t)((unsigned long long)time_ns >> 32)};
    bench_reply(r, 5);
}

// Pixel port: the metadata of the frame being sent; with none queued, the
// last frame's (a sensor holds its metadata lines between frames), zeros
// after power-on and a bench RESET (b_last_meta).
DPI_LINK void cxp_bench_meta(int *xsize, int *ysize, int *xoffs, int *yoffs, int *pixfmt,
                             int *tapg, int *streamid, int *sourcetag, int *flags) {
    if (b_nframes) memcpy(b_last_meta, b_frames[0].meta, sizeof(b_last_meta));
    const uint32_t *m = b_last_meta;
    *xsize = m ? (int)m[0] : 0;      *ysize = m ? (int)m[1] : 0;
    *xoffs = m ? (int)m[2] : 0;      *yoffs = m ? (int)m[3] : 0;
    *pixfmt = m ? (int)m[4] : 0;     *tapg = m ? (int)m[5] : 0;
    *streamid = m ? (int)m[6] : 0;   *sourcetag = m ? (int)m[7] : 0;
    *flags = m ? (int)m[8] : 0;
}

// The pixel offered this cycle, if any: 1 with *data and *flags (bit 0 SOF,
// 1 EOL, 2 EOF), 0 for an idle cycle (no frame, or the frame's valid
// density said no).  The same pixel is offered until cxp_bench_pix_taken().
DPI_LINK int cxp_bench_pix_beat(int *data, int *flags) {
    if (!b_nframes) return 0;
    const pixframe_t *f = &b_frames[0];
    b_rng ^= b_rng << 13; b_rng ^= b_rng >> 17; b_rng ^= b_rng << 5;
    if (b_rng % 1000u >= f->permille) return 0;
    const uint32_t xs = f->meta[0] ? f->meta[0] : 1;
    const size_t i = b_pix;
    *data = i < f->npix ? f->px[i] : 0;
    if (f->fl)
        *flags = i < f->npix ? f->fl[i] : 0;
    else
        *flags = (i == 0 ? 1 : 0) | ((i % xs) == xs - 1 ? 2 : 0) | (i + 1 == f->npix ? 4 : 0);
    return 1;
}

// The offered pixel was accepted; after a frame's last one, the host gets
// PIXEL_FRAME|REPLY with the count.
DPI_LINK void cxp_bench_pix_taken(void) {
    if (!b_nframes) return;
    if (++b_pix < b_frames[0].npix) return;
    const uint32_t r[2] = {OP_PIXEL_FRAME | OP_REPLY, b_frames[0].npix};
    free(b_frames[0].px);
    free(b_frames[0].fl);
    memmove(b_frames, b_frames + 1, (b_nframes - 1) * sizeof(pixframe_t));
    b_nframes--;
    b_pix = 0;
    bench_reply(r, 2);
}

// Timed events (CAP_TIMES).
static void bench_short_event(uint32_t w0, uint32_t w1) {
    const uint32_t r[7] = {EV_SHORT_DL, w0, w1, (uint32_t)d_short_ps,
                           (uint32_t)((unsigned long long)d_short_ps >> 32), (uint32_t)d_short_in,
                           (uint32_t)d_short_idx};
    bench_reply(r, 7);
}

static void bench_frame_event(long long end_ps) {
    const uint32_t tb = d_n >= 2 ? (d_frame[1] & 0xFFu) : 0xFFu;
    const uint32_t r[7] = {EV_FRAME_DL, tb, (uint32_t)d_sop_ps, (uint32_t)((unsigned long long)d_sop_ps >> 32),
                           (uint32_t)end_ps, (uint32_t)((unsigned long long)end_ps >> 32), (uint32_t)d_n};
    bench_reply(r, 7);
}

static void bench_uplink_mark(uint16_t c) {
    const uint32_t r[5] = {EV_UPLINK_MARK, (uint32_t)c, (uint32_t)g_now_ps,
                           (uint32_t)((unsigned long long)g_now_ps >> 32), (uint32_t)g_bit_ps};
    bench_reply(r, 5);
}

DPI_LINK void cxp_fifo_close(void) {
    d_flush();
    if (g_h2c_fd >= 0) { close(g_h2c_fd); g_h2c_fd = -1; }
    if (g_c2h_fd >= 0) { close(g_c2h_fd); g_c2h_fd = -1; }
    fprintf(stderr, "[cxp-hw] bridge closed\n");
}
