"""cxp_coverage_subscriber — single sink for every monitor analysis port.

Implements §11.1 covergroups in plain Python (no cocotb_coverage hard
dependency — the env logs hits to a dict and emits a JSON summary at
end-of-test which the regression dashboard merges).

If cocotb_coverage is installed, the bins are also published via
@CoverPoint decorators so the merge tool produces a proper HTML page.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import os

from pyuvm import (
    uvm_analysis_export, uvm_component, uvm_subscriber, uvm_tlm_analysis_fifo,
)

from uvm.common.cxp_pkg import (
    WireBeatKind, PacketType, UplinkKind,
)


# Optional cocotb_coverage import.
try:
    from cocotb_coverage.coverage import CoverPoint, CoverCross
    HAVE_COV = True
except ImportError:
    HAVE_COV = False


class CovSubscriber(uvm_component):
    def build_phase(self):
        self.beat_fifo     = uvm_tlm_analysis_fifo("beat_fifo",     self)
        self.pkt_fifo      = uvm_tlm_analysis_fifo("pkt_fifo",      self)
        self.short_fifo    = uvm_tlm_analysis_fifo("short_fifo",    self)
        self.uplink_fifo   = uvm_tlm_analysis_fifo("uplink_fifo",   self)
        self.sideband_fifo = uvm_tlm_analysis_fifo("sideband_fifo", self)
        self.apb_fifo      = uvm_tlm_analysis_fifo("apb_fifo",      self)
        self.reg_fifo      = uvm_tlm_analysis_fifo("reg_fifo",      self)

        self.beat_xp     = self.beat_fifo.analysis_export
        self.pkt_xp      = self.pkt_fifo.analysis_export
        self.short_xp    = self.short_fifo.analysis_export
        self.uplink_xp   = self.uplink_fifo.analysis_export
        self.sideband_xp = self.sideband_fifo.analysis_export
        self.apb_xp      = self.apb_fifo.analysis_export
        self.reg_xp      = self.reg_fifo.analysis_export

        self.bins = defaultdict(int)   # bin_name -> hit count
        # Sticky link-reset window edge detect.
        self._prev_lr_active = 0

    async def run_phase(self):
        import cocotb
        cocotb.start_soon(self._beat_loop())
        cocotb.start_soon(self._pkt_loop())
        cocotb.start_soon(self._short_loop())
        cocotb.start_soon(self._uplink_loop())
        cocotb.start_soon(self._sideband_loop())
        cocotb.start_soon(self._reg_loop())
        await self._apb_loop()

    async def _beat_loop(self):
        # cg_packet_type — sample each beat
        while True:
            ev = await self.beat_fifo.get()
            self.bins[f"cg_packet_type.{ev.kind.name}"] += 1

    async def _pkt_loop(self):
        # cg_packet_type cross at packet granularity
        while True:
            pkt = await self.pkt_fifo.get()
            if pkt.type_byte == PacketType.STREAM:
                self.bins["cg_packet_type.STREAM_0x01"] += 1
            elif pkt.type_byte == PacketType.CTRL_ACK:
                self.bins["cg_packet_type.CTRL_ACK_0x03"] += 1
            else:
                self.bins[f"cg_packet_type.OTHER_0x{pkt.type_byte:02x}"] += 1

    async def _short_loop(self):
        # cg_short_packet — §8.3.2 trigger / §8.3.3 I/O-ack 2-word packets.
        while True:
            pkt = await self.short_fifo.get()
            self.bins[f"cg_short_packet.{pkt.kind.name}"] += 1

    async def _uplink_loop(self):
        # cg_uplink_packet — sample on each issued uplink txn (driver
        # writes to ap_sent before encoding).
        while True:
            cmd = await self.uplink_fifo.get()
            self.bins[f"cg_uplink_packet.{cmd.kind.name}"] += 1

    async def _sideband_loop(self):
        while True:
            evt = await self.sideband_fifo.get()
            if evt.sb_cmd_crc_err_pulse:
                self.bins["cg_errors.cmd_crc_err"] += 1
            if evt.sb_cmd_logical_err_pulse:
                self.bins[
                    f"cg_errors.cmd_logical_err.{evt.sb_cmd_logical_err_code:02x}"
                ] += 1
            if evt.sb_pkt_err_pulse:
                self.bins["cg_errors.pkt_err"] += 1
            if evt.sb_rx_code_err_pulse:
                self.bins["cg_errors.rx_code_err"] += 1
            if evt.sb_rx_disp_err_pulse:
                self.bins["cg_errors.rx_disp_err"] += 1
            if evt.sb_trigger_glitch_pulse:
                self.bins["cg_errors.trigger_glitch"] += 1
            # §5.1 register-router decode rejection.
            if evt.sb_router_reject_pulse:
                self.bins[
                    f"cg_router.reject.{evt.sb_router_reject_code:02x}"
                ] += 1
            # §10.3.28 link-reset window edges.
            if evt.sb_link_reset_active and not self._prev_lr_active:
                self.bins["cg_link_reset.window_open"] += 1
            self._prev_lr_active = evt.sb_link_reset_active
            if evt.sb_link_reset_done:
                self.bins["cg_link_reset.done"] += 1

    async def _reg_loop(self):
        while True:
            t = await self.reg_fifo.get()
            self.bins[f"cg_regbus.{'write' if t.write else 'read'}"] += 1
            if t.err:
                self.bins[f"cg_regbus.err.{t.err:02x}"] += 1

    async def _apb_loop(self):
        while True:
            txn = await self.apb_fifo.get()
            if txn.write:
                self.bins["cg_apb.write_single"] += 1
            else:
                self.bins["cg_apb.read_single"] += 1
            if txn.pslverr:
                self.bins["cg_apb.pslverr"] += 1
            # Measured latency of the transfer (not the knob).
            if not txn.completed:
                self.bins["cg_apb.pready_wait.never"] += 1
            elif txn.wait_cycles == 0:
                self.bins["cg_apb.pready_wait.0"] += 1
            elif txn.wait_cycles <= 15:
                self.bins["cg_apb.pready_wait.1_15"] += 1
            else:
                self.bins["cg_apb.pready_wait.gt15"] += 1

    def report_phase(self):
        # The functional coverage model (crosses), sampled from what the
        # monitors and scoreboards recorded.
        try:
            from uvm.coverage.model import collect
            for k, v in collect(self.get_parent()).items():
                self.bins[k] += v
        except Exception as exc:          # never lose the counters for it
            self.logger.error(f"coverage model failed: {exc!r}")
            self.bins["cg_model_error"] += 1
        # Dump a json summary the regression dashboard merges across runs.
        out_path = os.environ.get("CXP_COV_JSON", "cov_summary.json")
        try:
            test_name = os.environ.get("UVM_TESTNAME", "unknown")
            payload = {
                "test": test_name,
                "bins": dict(self.bins),
            }
            with open(out_path, "w") as f:
                json.dump(payload, f, indent=2)
            self.logger.info(f"coverage written to {out_path}")
        except Exception as exc:
            self.logger.warning(f"coverage dump failed: {exc}")
