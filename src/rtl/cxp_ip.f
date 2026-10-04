// =============================================================================
// cxp_ip.f — the one RTL file list for the CoaXPress device IP.
//
// Dependency order: packages first, then leaves, then the blocks that
// instantiate them.  Paths are relative to this directory (one folder per
// module-name group: pkg, gen, cdc, lib, app, tx, rx, ctrl, top), so pass it
// with -F (resolve relative to the file), not -f:
//
//   verilator --lint-only -F src/rtl/cxp_ip.f --top-module cxp_interface_top
//   vlog -sv -F src/rtl/cxp_ip.f
//
// Makefiles read it through cxp_ip.mk (next to this file), which strips the
// comments and prefixes RTL_DIR.  Add or remove an RTL file here and nowhere
// else.
// =============================================================================

// ---- Packages -------------------------------------------------------------
pkg/cxp_pkg.sv
pkg/cxp_util_pkg.sv
gen/cxp_regmap_pkg.sv

// ---- Shared leaves --------------------------------------------------------
cdc/cxp_cdc_sync.sv
cdc/cxp_cdc_link.sv
cdc/cxp_cdc_pulse.sv
cdc/cxp_cdc_bus.sv
cdc/cxp_cdc_req.sv
lib/cxp_lib_crc32.sv
tx/cxp_tx_pkt_framer.sv
tx/cxp_tx_short_pkt.sv
app/cxp_app_marker_seq.sv

// ---- Register file (table-driven, limits from src/regmap/cxp_regmap.yaml) ----
ctrl/cxp_ctrl_bootstrap_regs.sv

// ---- Pixel / stream path (app_clk -> tx_clk) ------------------------------
app/cxp_app_acq_ctrl.sv
app/cxp_app_tpg.sv
app/cxp_app_pixel_ingress.sv
app/cxp_app_pixel_packer.sv
app/cxp_app_image_header.sv
app/cxp_app_line_marker.sv
cdc/cxp_cdc_stream_fifo.sv
tx/cxp_tx_stream_pkt.sv
app/cxp_app_stream.sv

// ---- TX packet sources, arbiter, inserter (tx_clk) ------------------------
tx/cxp_tx_linktest.sv
tx/cxp_tx_trigger_hs.sv
tx/cxp_tx_io_ack.sv
tx/cxp_tx_ctrl_ack.sv
tx/cxp_tx_arbiter.sv
tx/cxp_tx_inserter.sv

// ---- RX uplink (rx_clk) ---------------------------------------------------
rx/cxp_rx_lspd_sampler.sv
rx/cxp_rx_8b10b_decoder.sv
rx/cxp_rx_link_mon.sv
rx/cxp_rx_packet_parser.sv
rx/cxp_rx_trigger_lspd.sv
rx/cxp_rx_linktest.sv
ctrl/cxp_ctrl_cmd_parser.sv
ctrl/cxp_ctrl_bus_master.sv
ctrl/cxp_ctrl_apb_bridge.sv
rx/cxp_rx_link.sv
ctrl/cxp_ctrl_plane.sv

// ---- One block per clock, the crossing layer ------------------------------
app/cxp_app_domain.sv
tx/cxp_tx_domain.sv
rx/cxp_rx_domain.sv
cdc/cxp_cdc_layer.sv

// ---- Reset / top ----------------------------------------------------------
cdc/cxp_cdc_reset.sv
top/cxp_interface_top.sv
top/cxp_device_top.sv
