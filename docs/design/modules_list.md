# CXP IP — module list

An index of the RTL modules in `src/rtl/`, with a one-line role for each and a
link to its engineering note under [`modules/`](modules/). The notes give the
ports, internal operation, spec references, verification and open findings.
The folders match `src/rtl/`: each clock domain has one wrapper (`*_domain`,
`cxp_cdc_layer`) that the two tops instantiate.

Related: [RTL guide](../../src/rtl/README.md) ·
[interactive RTL map](cxp_rtl_map.html) ·
[register map](modules/pkg/cxp_regmap.md) ·
[CoaXPress 1.1.1 specification](../spec/CXP-001-2015.pdf)

## Hierarchy

```text
cxp_device_top                        IP boundary (register file + reset)
├── cxp_cdc_reset                     one synchronised reset per clock
├── cxp_ctrl_bootstrap_regs           Table 45 register file (rx_clk)
└── cxp_interface_top                 link + datapath, three clocks
    ├── cxp_app_domain       app_clk  pixel sources, acquisition gate, packer, markers
    ├── cxp_cdc_layer                 clock crossings and stream FIFO (app -> tx)
    ├── cxp_tx_domain        tx_clk   packet sources, framer, arbiter, inserter
    └── cxp_rx_domain        rx_clk   uplink receiver (cxp_rx_link) + control plane
```

## Top — `src/rtl/top/`

| Module | Role |
|---|---|
| [`cxp_device_top`](modules/top/cxp_device_top.md) | The IP boundary an integrator instantiates: reset controller, `cxp_interface_top` and the register file. Serial uplink and pixels in, 32-bit downlink word + K flags out, optional APB3 user window. |
| [`cxp_interface_top`](modules/top/cxp_interface_top.md) | Link and datapath top: pixel source select, stream packing and framing, LS uplink receiver, control plane and the downlink word output. |

## Application domain — `src/rtl/app/` (`app_clk`)

| Module | Role |
|---|---|
| [`cxp_app_domain`](modules/app/cxp_app_domain.md) | Wrapper for all `app_clk` logic: pixel sources, source mux, acquisition gate, packer and the write side of the stream path. |
| [`cxp_app_tpg`](modules/app/cxp_app_tpg.md) | Synthesisable test-pattern generator: one pixel per cycle with frame/line flags and image-header metadata. |
| [`cxp_app_pixel_ingress`](modules/app/cxp_app_pixel_ingress.md) | Sensor-side single-pixel bus adapter: 1-entry register, start-of-line flag, frame gating and per-frame geometry latch. |
| [`cxp_app_acq_ctrl`](modules/app/cxp_app_acq_ctrl.md) | Acquisition control and whole-image pixel gate (AcquisitionStart/Stop, Continuous/Single/MultiFrame). |
| [`cxp_app_pixel_packer`](modules/app/cxp_app_pixel_packer.md) | Packs pixels into 32-bit P0..P3 words per pixel format; each line starts in P0 and ends zero-padded (§9.4.2). |
| [`cxp_app_stream`](modules/app/cxp_app_stream.md) | Merges image headers, line markers and pixel words into one flow and cuts it into DsizeP-word blocks for the stream FIFO. |
| [`cxp_app_image_header`](modules/app/cxp_app_image_header.md) | Lays out the image-header byte vector (Table 38) for one frame. |
| [`cxp_app_line_marker`](modules/app/cxp_app_line_marker.md) | Lays out the line-marker byte vector for one line. |
| [`cxp_app_marker_seq`](modules/app/cxp_app_marker_seq.md) | Shared §9.4 marker sequencer: 4×K28.3 then one replicated byte per word. |

## Clock crossings — `src/rtl/cdc/`

| Module | Role |
|---|---|
| [`cxp_cdc_layer`](modules/cdc/cxp_cdc_layer.md) | The only multi-clock block: every crossing between `rx_clk`, `app_clk` and `tx_clk`, plus the stream FIFO. |
| [`cxp_cdc_sync`, `cxp_cdc_pulse`, `cxp_cdc_bus`, `cxp_cdc_req`, `cxp_cdc_link`](modules/cdc/cxp_cdc.md) | Clock-crossing primitives (level, pulse, bus, request/acknowledge) and the link that lets the two-sided ones survive a one-sided reset. |
| [`cxp_cdc_stream_fifo`](modules/cdc/cxp_cdc_stream_fifo.md) | Gray-code two-clock FIFO carrying stream payload from `app_clk` to `tx_clk`, with complete-packet count and flush. |
| [`cxp_cdc_reset`](modules/cdc/cxp_cdc_reset.md) | One reset request in, one synchronised reset per clock domain out, released in a fixed order. |

## Transmit domain — `src/rtl/tx/` (`tx_clk`)

| Module | Role |
|---|---|
| [`cxp_tx_domain`](modules/tx/cxp_tx_domain.md) | Wrapper for all `tx_clk` logic: the five packet sources, arbiter, inserter and stream flush request. |
| [`cxp_tx_stream_pkt`](modules/tx/cxp_tx_stream_pkt.md) | Frames FIFO payload into type-0x01 stream data packets with one PacketTag counter per StreamID. |
| [`cxp_tx_ctrl_ack`](modules/tx/cxp_tx_ctrl_ack.md) | Frames type-0x03 control acknowledgments (data ack or immediate ack). |
| [`cxp_tx_linktest`](modules/tx/cxp_tx_linktest.md) | §8.7 connection-test generator: type-0x04 packets of 1024 counting words while TestMode is set. |
| [`cxp_tx_pkt_framer`](modules/tx/cxp_tx_pkt_framer.md) | Common §8.2.2 long-packet framer: word order, CRC and K27.7/K29.7 framing for every long packet. |
| [`cxp_tx_trigger_hs`](modules/tx/cxp_tx_trigger_hs.md) | Device→host high-speed trigger packets (Table 16), gated by the host I/O acknowledgment (§8.3.3). |
| [`cxp_tx_io_ack`](modules/tx/cxp_tx_io_ack.md) | One 2-word I/O-acknowledgment packet per host trigger received. |
| [`cxp_tx_short_pkt`](modules/tx/cxp_tx_short_pkt.md) | Shared 2-word "leader + code" packet source used by the trigger and I/O-ack paths. |
| [`cxp_tx_arbiter`](modules/tx/cxp_tx_arbiter.md) | Chooses the next long packet (Table 13 priority 2) and holds the grant until its end of packet. |
| [`cxp_tx_inserter`](modules/tx/cxp_tx_inserter.md) | Inserts triggers, I/O acks and IDLE words into the long-packet flow; its register is the downlink output. |

## Receive domain — `src/rtl/rx/` (`rx_clk`)

| Module | Role |
|---|---|
| [`cxp_rx_domain`](modules/rx/cxp_rx_domain.md) | Wrapper for all `rx_clk` logic: the low-speed uplink receiver and the control plane. |
| [`cxp_rx_link`](modules/rx/cxp_rx_link.md) | Uplink receive wrapper: serial line in; trigger, I/O-ack, connection-test channels and long-packet words out. |
| [`cxp_rx_lspd_sampler`](modules/rx/cxp_rx_lspd_sampler.md) | Oversampling soft receiver for the 20.83 Mbps uplink: bit recovery, K28.5 alignment, Table 15 trigger extraction. |
| [`cxp_rx_8b10b_decoder`](modules/rx/cxp_rx_8b10b_decoder.md) | Combinational single-character 8B/10B decoder with running disparity and error flags. |
| [`cxp_rx_link_mon`](modules/rx/cxp_rx_link_mon.md) | Uplink IDLE monitor: link up/lost state and character/word realignment requests. |
| [`cxp_rx_packet_parser`](modules/rx/cxp_rx_packet_parser.md) | Classifies decoded words into IDLE, I/O acknowledgment or long-packet body by K-character pattern. |
| [`cxp_rx_trigger_lspd`](modules/rx/cxp_rx_trigger_lspd.md) | Recreates the host trigger from a Table 15 packet with constant event-to-output latency (Figure 20). |
| [`cxp_rx_linktest`](modules/rx/cxp_rx_linktest.md) | §8.7.1 test receiver: checks host test packets against Table 23 and counts errors and packets. |

## Control plane — `src/rtl/ctrl/` (`rx_clk`)

| Module | Role |
|---|---|
| [`cxp_ctrl_plane`](modules/ctrl/cxp_ctrl_plane.md) | Executes host control commands (§8.6) and holds one final response per command for the ack framer. |
| [`cxp_ctrl_cmd_parser`](modules/ctrl/cxp_ctrl_cmd_parser.md) | Parses and validates type-0x02 control-command packets and emits one command record each. |
| [`cxp_ctrl_bus_master`](modules/ctrl/cxp_ctrl_bus_master.md) | Command executor: single-word accesses on the register port or the APB user window, read buffer for the ack. |
| [`cxp_ctrl_apb_bridge`](modules/ctrl/cxp_ctrl_apb_bridge.md) | One register-bus access to one APB3 transfer for the user register window. |
| [`cxp_ctrl_bootstrap_regs`](modules/ctrl/cxp_ctrl_bootstrap_regs.md) | Bootstrap register file (Table 45), GenICam XML ROM, camera features and ConnectionReset (§10.3.28). |

## Library — `src/rtl/lib/`

| Module | Role |
|---|---|
| [`cxp_lib_crc32`](modules/lib/cxp_lib_crc32.md) | Parallel CRC-32 accumulator shared by the TX framer and the RX command parser. |

## Packages — `src/rtl/pkg/`, `src/rtl/gen/`

| Package | Role |
|---|---|
| [`cxp_pkg`, `cxp_util_pkg`](modules/pkg/cxp_pkg.md) | On-wire constants, transmit port table, bus structs and shared functions; generic helpers (`rep4`, `bswap32`, …). |
| [`cxp_regmap_pkg`](modules/pkg/cxp_regmap.md) | Generated register addresses, reset values and XML ROM geometry; the note is the generated register table (`make regmap`). |

Notes for deleted modules (`cxp_scfifo`, `cxp_stream_mux`, `cxp_tx_8b10b`,
`cxp_8b10b_encoder`) are in [`docs/archive/retired_modules/`](../archive/retired_modules/).
