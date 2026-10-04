# `src/rtl/` — the CoaXPress device IP

46 SystemVerilog files, one module per file (file name = module name), in one
folder per group. The build inputs sit at this level:

| File | Role |
|------|------|
| `cxp_ip.f` | The one RTL file list, dependency order; entries are `<group>/<file>.sv`, so use it with `-F` |
| `cxp_ip.mk` | Make fragment: `RTL_SOURCES` / `SVA_SOURCES` from `cxp_ip.f` and `../sva/cxp_sva.f` (set `RTL_DIR` first) |
| `cxp_ip.vlt` | Verilator lint waivers, each naming its finding |

Lint, the unit benches (`src/verif/common/cocotb_sim.mk`), `src/verif/uvm`,
`src/emu/bridge` and `tools/check_style.py` all read the RTL through `cxp_ip.f`.
Add or remove an RTL file there and nowhere else.

```
verilator --lint-only -F src/rtl/cxp_ip.f -F src/sva/cxp_sva.f --top-module cxp_device_top
```

## Groups

File names are `cxp_<group>_<function>.sv`: the group is the block that
instantiates the module, which is also its clock domain.

| Folder | Contents | Wrapper |
|--------|----------|---------|
| `pkg/` | `cxp_pkg` (protocol constants, types), `cxp_util_pkg` (helper functions) | — |
| `gen/` | `cxp_regmap_pkg.sv`, `cxp_camera_xml.mem` — written by `make regmap`, do not edit | — |
| `top/` | `cxp_device_top` (IP + register file + reset), `cxp_interface_top` (link + datapath) | — |
| `app/` | `cxp_app_*`: pixel sources, packer, image header / line markers (`app_clk`) | `cxp_app_domain` |
| `cdc/` | `cxp_cdc_*`: clock crossings, stream FIFO, reset synchroniser | `cxp_cdc_layer` |
| `tx/` | `cxp_tx_*`: downlink packet sources, framer, arbiter, inserter (`tx_clk`) | `cxp_tx_domain` |
| `rx/` | `cxp_rx_*`: uplink sampler, decoder, link monitor, parser, trigger, test receiver (`rx_clk`) | `cxp_rx_domain` → `cxp_rx_link` |
| `ctrl/` | `cxp_ctrl_*`: control plane and register file (`rx_clk`) | `cxp_ctrl_plane` |
| `lib/` | `cxp_lib_*`: leaves shared by more than one group | — |

## Hierarchy

```
cxp_device_top                        product shell (register file + reset)
├── cxp_cdc_reset                     one synchronised reset per clock
├── cxp_ctrl_bootstrap_regs           Table 45 register file (rx_clk)
└── cxp_interface_top                 link + datapath, three clocks
    ├── cxp_app_domain       app_clk  app_acq_ctrl, app_tpg, app_pixel_ingress,
    │                                 app_pixel_packer, app_stream
    │                                 (app_image_header, app_line_marker
    │                                  -> app_marker_seq)
    ├── cxp_cdc_layer                 cdc_bus / req / pulse / sync (-> cdc_link),
    │                                 cdc_stream_fifo (app -> tx)
    ├── cxp_tx_domain        tx_clk   tx_stream_pkt, tx_ctrl_ack, tx_linktest
    │                                 (-> tx_pkt_framer -> lib_crc32),
    │                                 tx_trigger_hs, tx_io_ack (-> tx_short_pkt),
    │                                 tx_arbiter, tx_inserter
    └── cxp_rx_domain        rx_clk   rx_link (rx_lspd_sampler, rx_8b10b_decoder,
                                      rx_link_mon, rx_packet_parser,
                                      rx_trigger_lspd, rx_linktest);
                                      ctrl_plane (ctrl_cmd_parser -> lib_crc32,
                                      ctrl_bus_master -> ctrl_apb_bridge)
```

Interactive hierarchy, ports, nets and block diagrams:
[`docs/design/cxp_rtl_map.html`](../../docs/design/cxp_rtl_map.html).
Coding style: `docs/style/rtl_coding_style.sv` (`make style`, `make lint`).
Module notes: `docs/design/modules/<group>/<module>.md`, indexed in
[`docs/design/modules_list.md`](../../docs/design/modules_list.md). Unit benches:
`src/tb_unit/<group>/<module>/`. Bound assertions: `src/sva/`.
