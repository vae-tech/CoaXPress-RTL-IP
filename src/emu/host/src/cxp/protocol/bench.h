// The test bench around a device: what a lab connects besides the link.
//
// The UVM testbench (src/verif/) drives more than the CoaXPress link: the
// device's trigger input, its pixel port, the extension-link strap, a
// faulting register bus, the three clocks and the host's bit rate.  A lab
// does the same with a trigger generator, a pattern source and a
// programmable clock.  A CXB1 frame (magic 'CXB1') on the same pipes asks
// the device's bench for one of these, and carries what the bench observes
// (the device's trigger output) back:
//
//     host -> bench   [op, args...]
//     bench -> host   [op | 0x80, args...]      (replies and events)
//
// A device without a bench skips CXB1 frames as garbage; HELLO then goes
// unanswered, which the host reads as "no bench".  The capability bits in
// the HELLO reply say which ops the bench carries out; an op outside them
// is ignored.  The RTL bench is src/emu/bridge (dpi/cxp_fifo_dpi.c, tb/cxp_hw_env.sv),
// the in-process one the reference virtual camera.
#pragma once

#include <cstdint>

namespace cxp::bench {

inline constexpr uint32_t MAGIC = 0x43584231u;  // 'CXB1'
inline constexpr uint32_t VERSION = 1;
inline constexpr uint32_t REPLY = 0x80;

// -- host -> bench ------------------------------------------------------------
enum Op : uint32_t {
    HELLO = 1,        // [] -> HELLO|REPLY [version, caps]
    PIN = 2,          // [pin, value]            drive a device input
    REG_ERR = 3,      // [code]                  0 = off; else every register access answers `code`
                      // 4: retired (CLOCKS, clock periods; no case used it after the verif
                      //  clock sweeps became build knobs)
    UPLINK_PPM = 5,   // [ppm (int32)]           host bit-rate offset from nominal
    PIXEL_FRAME = 6,  // [xsize, ysize, xoffs, yoffs, pixfmt, tapg, streamid, sourcetag, flags,
                      //  valid_permille, npix, pixels (2 x 16 bit per word, first in 15:0)]
                      //  -> PIXEL_FRAME|REPLY [pixels accepted] when the last one is in
    SYNC = 7,         // [token] -> SYNC|REPLY [token] once every earlier op took effect;
                      //  CAP_TIMES: [token, time_ps lo, hi, ms_ps lo, hi], the bench time
                      //  and the length of the device's millisecond (its control time
                      //  limits; a simulated device may run them faster than real time)
    GET_PINS = 8,     // [] -> GET_PINS|REPLY [inputs]: bit (pin - 1) set for each input at 1
    RESET = 9,        // [inputs, domains] power-on reset with the inputs at these levels (bit
                      //  pin - 1), REG_ERR / REG_STALL off, queued pixel frames dropped;
                      //  clocks and the host bit rate stay -> RESET|REPLY [] once the device
                      //  is out of reset.  domains (CAP_RESET_DOMAINS): the reset inputs
                      //  pulsed, bit 0 app, 1 tx, 2 rx; 0 or absent = all three
    REG_STALL = 10,   // [ms, pslverr] the user-window register slave answers each access
                      //  after ms of the device's milliseconds (SYNC) (0xFFFFFFFF: never),
                      //  with PSLVERR = pslverr;
                      //  [0, 0] = at once, the power-on behaviour
    PIXEL_BEATS = 11, // [xsize, ysize, xoffs, yoffs, pixfmt, tapg, streamid, sourcetag, flags,
                      //  valid_permille, nbeats, beats (bits 15:0 pixel, 16 SOF, 17 EOL,
                      //  18 EOF; one per word)]: a pixel-port frame with the framing as
                      //  given, well formed or not -> PIXEL_FRAME|REPLY [beats accepted]
    UPLINK_BITS = 12, // [mode, n] the host's serial line: 0 drop the next n bits, 1 send the
                      //  current bit n more times, 2 / 3 hold the line at 0 / 1 for n bits
                      //  (0xFFFFFFFF: until the next UPLINK_BITS) while the characters wait
    DL_STATS = 13,    // [] -> DL_STATS|REPLY [packets, IDLE words inside packets, short
                      //  packets inside packets, short packets between packets], counted
                      //  since the previous DL_STATS
};

// -- bench -> host (unsolicited) ----------------------------------------------
enum Event : uint32_t {
    PIN_EDGE = 0x90,     // [pin, value, time_ns lo, time_ns hi]  a device output changed
    SHORT_DL = 0x91,     // [word 0, word 1, time_ps lo, time_ps hi, inside, index] after the
                         //  CXC1 frame of a downlink short packet: the bench time of its first
                         //  word; inside = 1 when it was inserted into a packet, then index =
                         //  the words of that packet sent before it (SOP included)
    UPLINK_MARK = 0x92,  // [char | K << 8, time_ps lo, time_ps hi, bit_ps] the first character of
                         //  a run of K27.7, K29.7, K28.2, K28.4 or K28.6 went on the uplink at
                         //  time_ps (its first bit), one bit lasting bit_ps
    FRAME_DL = 0x93,     // [type, sop_ps lo, hi, eop_ps lo, hi, words] after every downlink
                         //  packet that is not a stream packet: its TYPE and bench times
};

// Device inputs (PIN) and outputs (PIN_EDGE).
enum Pin : uint32_t {
    TRIG_IN = 1,         // trig_i: device -> host trigger source (§8.3.2)
    EXT_LINK = 2,        // from_extension_link_i: the command link is an extension (§5.1)
    TRIG_POLARITY = 3,   // cfg_trig_polarity_i
    USE_TPG = 4,         // cfg_use_tpg_i: 1 test pattern, 0 pixel port
    TPG_RUN = 5,         // cfg_run_i
    ARBITRARY = 6,       // cfg_arbitrary_i: arbitrary image header / line markers
    TRIG_OUT = 16,       // trig_o: the trigger the device recreated from the host's
    TRIG_GLITCH = 17,    // trig_glitch_pulse_o
};

// HELLO capability bits.
enum Cap : uint32_t {
    CAP_CHARS = 1u << 0,       // takes CXC1 uplink frames; hands over downlink short packets
    CAP_TRIG_IN = 1u << 1,
    CAP_TRIG_OUT = 1u << 2,    // reports TRIG_OUT edges
    CAP_EXT_LINK = 1u << 3,
    CAP_TRIG_POLARITY = 1u << 4,
    CAP_PIXEL = 1u << 5,       // USE_TPG, TPG_RUN and PIXEL_FRAME
    CAP_ARBITRARY = 1u << 6,
    CAP_REG_ERR = 1u << 7,
    //               1u << 8: retired (CAP_CLOCKS)
    CAP_UPLINK_PPM = 1u << 9,
    CAP_SYNC = 1u << 10,       // SYNC and GET_PINS
    CAP_RESET = 1u << 11,
    CAP_REG_STALL = 1u << 12,      // REG_STALL (a user register window behind a slow slave)
    CAP_PIXEL_BEATS = 1u << 13,    // PIXEL_BEATS
    CAP_RESET_DOMAINS = 1u << 14,  // RESET with a domain mask
    CAP_UPLINK_BITS = 1u << 15,    // UPLINK_BITS
    CAP_TIMES = 1u << 16,          // SHORT_DL, UPLINK_MARK and FRAME_DL events, DL_STATS
};

// The user register window of a bench with CAP_REG_STALL: word-addressed
// read/write memory behind the device's register bus.
inline constexpr uint32_t USER_BASE = 0x00020000u;
inline constexpr uint32_t USER_SIZE = 0x00001000u;

}  // namespace cxp::bench
