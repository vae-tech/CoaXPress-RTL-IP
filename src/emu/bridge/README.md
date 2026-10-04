# `src/emu/bridge/` — the RTL as a camera (Verilator + DPI over named pipes)

A **pure-SystemVerilog + DPI-C** simulation environment that wraps the
complete CoaXPress 1.1.1 device IP (`cxp_device_top`) and exposes it to the
C++ host in `src/emu/host` over two Linux named pipes, the same wire format
the host's built-in virtual camera (`cxp sim`) speaks. The RTL *is* the camera.

```
            cxp.h2c (host -> camera)                 cxp.c2h (camera -> host)
  ┌────────┐   framed words      ┌────────────────────────────┐   framed words  ┌────────┐
  │  cxp   │ ───────────────────►│ cxp_fifo_dpi.c             │                 │  cxp   │
  │  CLI / │                     │  deframe → 8B/10B → bits   │                 │  CLI / │
  │  GUI / │◄─────────────────── │  SOP..EOP reassembly       │ ───────────────►│  GUI / │
  │validate│                     └─────────────┬──────────────┘                 │validate│
  └────────┘                        rx_serial │  ▲ cxp_if_data_o / kmask        └────────┘
                                 ┌────────────▼──┴──────────────────────────┐
                                 │ tb/cxp_hw_env.sv                         │
                                 │   cxp_device_top (three clocks, async)   │
                                 │   internal TPG or bench-driven pixel port│
                                 └──────────────────────────────────────────┘
```

## Files

| Path                  | Role                                                        |
|-----------------------|-------------------------------------------------------------|
| `tb/cxp_hw_env.sv`    | SV top: clocks / resets, `cxp_device_top`, serial bit pacing, the bench pins (trigger, pixel port, straps, faulting register bus, user-window APB memory). |
| `dpi/cxp_fifo_dpi.c`  | All CoaXPress link logic: FIFO I/O, envelope (de)framing, 8B/10B (`dpi/cxp_8b10b.h`), IDLE keep-alive, SOP..EOP reassembly, the bench ops. |
| `dpi/check_8b10b.c`   | Checks `cxp_8b10b.h` against the golden `cxp_protocol` vectors (`make check_8b10b`). |
| `Makefile`            | Verilator `--binary` build + run helpers; `SIM=vsim` ModelSim / QuestaSim flow (optimized / unoptimized). |

The split is deliberate: SV is only clock/reset/DUT/pacing, so the
environment runs in any DPI-capable simulator. Default tooling is
Verilator 5 (`--binary --timing`), matching the rest of the repo.

## Run

From the repo root, the usual way (the camera and the host in one command;
`tools/run_emu.sh` starts the camera on a private FIFO pair and stops it
when the host exits):

```bash
make emu-build                         # this bridge + the C++ host
make emu-cli                           # RTL camera, `cxp discover --params`
make emu-cli CMD="read 0x0000"         # any cxp command
make emu-gui                           # RTL camera + cxp-gui device explorer
make emu-cli WAVE=vcd                  # also dump build/cxp_hw_env.vcd (WAVE=fsdb converts it)
make emu-rtl                           # the validation campaign on the RTL, gated (~25 min)
```

By hand, in this directory:

```bash
make run                       # build, create FIFOs in /tmp/cxp, run until Ctrl-C
make run TRACE=1               # also dump build/cxp_hw_env.vcd
make run REGLOG=1              # log every control command / register access
make run FIFO_DIR=/tmp/mycxp
make run OPT_LEVEL=0           # unoptimised build (faster compile, debug)
make check_8b10b               # C 8B/10B tables vs the golden vectors
make clean
```

### ModelSim / QuestaSim

The SV top is self-driven (no cocotb), so it runs natively under
`vsim -c` — this sidesteps the cocotb/VPI crash that affects the
`src/tb_unit` cocotb flow on Questa. Both an optimized and an unoptimized
build are provided:

```bash
make run SIM=vsim              # full optimization (vopt, default)
make run SIM=vsim OPT_LEVEL=0  # no optimization, full debug visibility
make run SIM=vsim TRACE=1      # also dump build/cxp_hw_env.vcd
```

`OPT_LEVEL=1` builds `vopt cxp_hw_env -o cxp_hw_env_opt`; `OPT_LEVEL=0` builds
`vopt -O0 +acc cxp_hw_env -o cxp_hw_env_noopt`. The DPI bridge is
compiled to a shared object and bound via `vsim -sv_lib`.

Then drive it from the C++ host in another terminal:

```bash
src/emu/host/build/cxp --fifo-dir /tmp/cxp read 0x0000
src/emu/host/build/cxp --fifo-dir /tmp/cxp stream-capture --frames 4 --out caps
src/emu/host/build/cxp --fifo-dir /tmp/cxp trace --pcap cxp.pcap --seconds 2
src/emu/host/build/cxp --fifo-dir /tmp/cxp validate --runnable --json results.json
src/emu/host/build/cxp-gui --fifo-dir /tmp/cxp
```

The simulation runs forever; Ctrl-C stops it. Bounding a run is the
host's job — the host applies a device-ack wait timeout (`cxp --timeout`,
seconds). The bridge ignores `SIGPIPE` and lazily (re)opens both pipes, so
the host may start, stop and restart independently of the simulator.

Set `CXP_HW_DEBUG=1` for per-frame stderr tracing (uplink frames in,
non-stream downlink frames out).

## Configuration

* Clocks: `app_clk`, `tx_clk` and `rx_clk` come from three generators,
  10 ns each by default, with `p_ASYNC_CLOCKS = 1` (as the UVM testbench
  runs the device). The host's serial bit clock is its own: `OS_RATIO`
  rx periods, not locked to `rx_clk` edges.
* Pixel source: internal test-pattern generator (`cfg_use_tpg=1`,
  free-running), rectangular image header/line markers, unless the bench
  says otherwise.
* The TPG's maximum geometry is `cxp_device_top` `p_TPG_X_SIZE` /
  `p_TPG_Y_SIZE`, and the power-on PixelFormat / Image1StreamID are
  `p_PIXEL_FORMAT_RESET` / `p_IMAGE1_STREAM_ID_RESET`, forwarded from the
  env's `TPG_*` parameters and set at build time via the Makefile knobs
  (`make run WIDTH=128 HEIGHT=64 PIXFMT=0x0101`).
  `WIDTH`/`HEIGHT` are the compiled *maximum*, 4096 × 4096 by default
  (= the XML's Width/Height Max, so every value the register file
  accepts is streamed as written); the active size is the bootstrap
  Width/Height registers (reset 640×480, 1..4096, host-writable at
  `0x10000`/`0x10004`). `PIXFMT` and `STREAMID` are the power-on
  values of the PixelFormat and Image1StreamID registers; TapG and Flags
  come from TapGeometry (`0x10028`, 0 only) and StreamFlags (`0x10034`).
* The GenICam XML ROM is `src/rtl/gen/cxp_camera_xml.mem`, generated by
  `make regmap`; the Makefile passes its absolute path to the env's
  `p_XML_BLOB_MEM` (`src/regmap/regmap.mk`, `-G` / `vopt -g`), so the
  binary may run from any directory and nothing is copied here.
* Image offsets and SourceTag are registers, not build knobs: OffsetX /
  OffsetY (`0x10020`/`0x10024`, 0..4095) go into the TPG image header,
  and a write of a new SourceTag (`0x10030`) presets the tag of the next
  image; the TPG counts images from 0 after reset.
* Stream-packet size comes from the bootstrap StreamPacketSizeMax
  register: bytes of the whole packet.  It powers up at 0 (power-up is a
  connection reset, §10.3.28) and a ConnectionReset clears it; while it is
  below 36 bytes no image enters and no stream packet is sent (Table 44).
* A host write of 1 to ConnectionReset (`0x4000`) is applied in
  `cxp_ctrl_bootstrap_regs`, whose ConnectionReset level restarts the
  PacketTags, flushes the stream and clears TestPacketCountTx in the tx
  domain; the bit clears once the tx domain echoes it back.
* `cfg_test_mode` is driven by the host-writable bootstrap `TestMode`
  bit, exactly as in the unit integration TB.

## Bench

The C++ host (`src/emu/host/src/cxp/protocol/bench.h`) drives what a lab wires
to the device besides the link, with `CXB1` frames on the same `h2c` pipe;
the bridge carries each op out at once (`bench_op` in `dpi/cxp_fifo_dpi.c`)
and the SV top polls it:

| Op | Effect |
|----|--------|
| `HELLO` | reply: version, capability bits (all of them; op 4 and bit 8, the former `CLOCKS`, are retired: the clocks are 10 ns each) |
| `PIN` | `trig_i`, `from_extension_link_i`, `cfg_trig_polarity_i`, `cfg_use_tpg_i`, `cfg_run_i`, `cfg_arbitrary_i` |
| `REG_ERR` | every register access answers this Table 22 code (the register file's `reg_err` forced; 0 releases it) |
| `UPLINK_PPM` | host bit-rate offset |
| `PIXEL_FRAME` | one frame on `s_pix_*` with its `s_meta_i`, at a given valid density; reply when the last pixel is taken |
| `SYNC` | reply once the earlier ops are in |
| `GET_PINS` | reply: the input pins' levels |
| `RESET` | power-on reset (8 `app_clk` cycles) with the inputs at the given levels, `REG_ERR` / `REG_STALL` off, queued frames dropped; clocks and bit rate stay; an optional domain mask pulses only the app / tx / rx reset inputs it names; reply once out of reset |
| `REG_STALL` | the user register window (`0x20000`, 4 KB of memory on the APB3 master, `p_USER_SIZE` set here) answers each transfer after the given milliseconds, or never, with the given PSLVERR |
| `PIXEL_BEATS` | a pixel-port frame whose SOF / EOL / EOF are given per beat (malformed framing); reply like `PIXEL_FRAME` |
| `UPLINK_BITS` | the host's serial line drops bits, repeats one, or holds a level for a number of bits (or until the next op) while the characters wait |
| `DL_STATS` | reply: downlink packets, IDLE words inside packets and short packets inside / between packets since the last `DL_STATS` |

`trig_o` edges and `trig_glitch_pulse_o` pulses go back as `PIN_EDGE`
events with the sim time.  After the `CXC1` frame of each downlink trigger
or I/O acknowledgment a `SHORT_DL` event gives its sim time (ps) and whether
it was inserted into a packet (and after how many of its words); each
Table 15 trigger or Table 17 acknowledgment the bridge starts on the uplink
gives an `UPLINK_MARK` event with the sim time of its first bit.

## Wire contract

Identical to what the C++ host speaks against its virtual camera:

* **Envelope** (`src/emu/host/src/cxp/transport/fifo.*`): `MAGIC(0x43585031) |
  NWORDS | NWORDS×u32`, all little-endian; resync on magic mismatch.
* **Uplink** words are 8B/10B-encoded (`dpi/cxp_8b10b.h`, IEEE tables
  with the D.x.A7 rule, checked against the golden `cxp_protocol`
  vectors by `make check_8b10b`) character by character and serialized
  LSB-first onto `rx_serial` on the host's bit clock (`OS_RATIO` (16) rx
  periods), as `src/verif/uvm/agents/host_uplink_agent.py` does. A `CXC1` frame
  carries characters one per word (bits 7:0, K flag in bit 8) and goes out
  exactly as given: Table 15 triggers, a trigger inside a command, I/O acks.
  IDLE words are streamed whenever the host is quiet so the device's
  soft sampler keeps lock.  K flags are set per character. In a frame's
  first word, the lanes carrying K27.7 are K characters; in its last
  word, the lanes carrying K29.7 are. This applies only when at least 3
  of the 4 lanes match. A host that corrupts one delimiter character
  therefore sends one bad character beside three good K characters.
* **Downlink** `cxp_if_data_o/kmask` is sampled every `tx_clk`; IDLE
  link fill is stripped, `SOP..EOP` packets are re-enveloped to `cxp.c2h`.
  A K27.7 SOP inside an open packet means the device cut that packet
  short. Only trigger and I/O-ack packets, which have no SOP, may be
  inserted into a packet (§8.2.4). The cut packet goes to the host as it
  is, without EOP, and the SOP starts the next one. Trigger and I/O-ack
  packets (4 x K28.4 / K28.2 / K28.6 + 4 data characters) are taken out,
  also from inside a packet, and go to the host as `CXC1` frames of their 8
  characters. A host that knows only `CXP1` skips them as garbage.

## Verified status

Against the C++ host (`src/emu/host`):

* **Downlink and uplink work end to end**: control reads and writes are
  serialised onto `rx_serial`, decoded by the RTL and acknowledged; the
  TPG stream arrives as well-formed type-0x01 packets and reassembled
  images.
* `make emu-rtl` runs every runnable validation case (plan cases,
  emulator-only cases and the PyUVM mirrors) against this bridge and gates
  the verdicts with `tools/check_emu_results.py`; cases red for an open RTL
  finding are listed in `src/emu/host/validation/expected_fail.json`.
