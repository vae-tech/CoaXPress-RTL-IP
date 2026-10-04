"""cxp_env — root UVM environment.

Builds every agent, scoreboard, and the coverage subscriber, and wires
analysis ports per the §4 block diagram.  The env is shared by every
test class — tests differ only in the virtual sequence they start.
"""

from __future__ import annotations

import cocotb
from cocotb.triggers import RisingEdge, Timer
from pyuvm import uvm_env, uvm_sequencer

from uvm.common.handles import get_dut

from uvm.agents.video_agent       import VideoAgent
from uvm.agents.cfg_agent         import CfgAgent
from uvm.agents.host_uplink_agent import DeviceTriggerResponder, UplinkAgent
from uvm.agents.tx_wire_agent     import TxWireAgent
from uvm.agents.apb_slave_agent   import ApbSlaveAgent
from uvm.agents.reg_bus_agent     import RegBusAgent
from uvm.agents.host_ctrl         import HostCtrl
from uvm.agents.sideband_monitor  import SidebandAgent
from uvm.agents.clk_rst_agent     import ClkRstAgent
from uvm.agents.io_agent          import IoAgent

from uvm.scoreboards.stream_scoreboard        import StreamScoreboard
from uvm.scoreboards.link_protocol_scoreboard import LinkProtocolScoreboard
from uvm.scoreboards.control_scoreboard       import ControlScoreboard
from uvm.scoreboards.rx_trigger_scoreboard    import RxTriggerScoreboard
from uvm.scoreboards.linktest_scoreboard      import LinktestScoreboard
from uvm.scoreboards.reg_scoreboard           import RegScoreboard
from uvm.scoreboards.io_ack_scoreboard        import IoAckScoreboard
from uvm.scoreboards.tx_trigger_scoreboard    import TxTriggerScoreboard
from uvm.scoreboards.link_reset_scoreboard    import LinkResetScoreboard
from uvm.scoreboards.link_state_scoreboard    import LinkStateScoreboard
from uvm.scoreboards.link_error_scoreboard    import LinkErrorScoreboard
from uvm.scoreboards.sb_base                  import TestChecks

from uvm.coverage.cov_subscriber import CovSubscriber
from uvm.subscribers.stream_dump_subscriber import StreamDumpSubscriber
from uvm.subscribers.packet_log import PacketLog

from uvm.ral.cxp_reg_block import CxpRegBlock


class VirtualSequencer(uvm_sequencer):
    """Per §5 — passes p_sequencer handles to the virtual sequences."""

    def build_phase(self):
        super().build_phase()
        self.video_seqr  = None
        self.cfg_seqr    = None
        self.uplink_seqr = None
        self.uplink_trig_seqr = None
        self.apb_seqr    = None
        self.io_seqr     = None


class CxpEnv(uvm_env):
    def build_phase(self):
        # Agents
        self.video_ag    = VideoAgent("video_ag", self)
        self.cfg_ag      = CfgAgent("cfg_ag", self)
        self.uplink_ag   = UplinkAgent("uplink_ag", self)
        self.txwire_ag   = TxWireAgent("txwire_ag", self)
        self.apb_ag      = ApbSlaveAgent("apb_ag", self)
        self.sideband_ag = SidebandAgent("sideband_ag", self)
        self.clkrst_ag   = ClkRstAgent("clkrst_ag", self)
        self.io_ag       = IoAgent("io_ag", self)
        self.reg_ag      = RegBusAgent("reg_ag", self)
        # The host acknowledges the device's triggers (§8.3.3).
        self.trig_resp   = DeviceTriggerResponder("trig_resp", self)
        # The host's control channel, closed-loop (§8.6.1.1).
        self.host        = HostCtrl("host", self)

        # Scoreboards
        self.sb_stream   = StreamScoreboard("sb_stream", self)
        self.sb_linkpro  = LinkProtocolScoreboard("sb_linkpro", self)
        self.sb_control  = ControlScoreboard("sb_control", self)
        self.sb_rxtrig   = RxTriggerScoreboard("sb_rxtrig", self)
        self.sb_linktest = LinktestScoreboard("sb_linktest", self)
        self.sb_reg      = RegScoreboard("sb_reg", self)
        self.sb_ioack    = IoAckScoreboard("sb_ioack", self)
        self.sb_txtrig   = TxTriggerScoreboard("sb_txtrig", self)
        self.sb_linkreset = LinkResetScoreboard("sb_linkreset", self)
        self.sb_linkstate = LinkStateScoreboard("sb_linkstate", self)
        self.sb_linkerr   = LinkErrorScoreboard("sb_linkerr", self)
        self.sb_test      = TestChecks("sb_test", self)

        # Coverage
        self.cov = CovSubscriber("cov", self)

        # Stream / CXP-protocol dump subscriber — writes decoded non-IDLE
        # downlink packets to a per-test text log (archived next to
        # results.xml by the Makefile).
        self.stream_dump = StreamDumpSubscriber("stream_dump", self)
        self.pkt_log = PacketLog("pkt_log", self)

        # RAL block (software mirror; not a uvm_component in PyUVM).
        # Exposed as self.env.ral on the test class — no ConfigDB hop.
        self.ral = CxpRegBlock()

        # Virtual sequencer
        self.vseqr = VirtualSequencer("vseqr", self)

    def connect_phase(self):
        # Stream scoreboard.  Two golden sources, one per pixel source:
        # the s_pix_* monitor for cfg_use_tpg = 0, the TPG output monitor
        # for cfg_use_tpg = 1.  Only one of them ever sees beats.
        self.video_ag.mon.ap.connect(self.sb_stream.video_xp)
        self.video_ag.tpg_mon.ap.connect(self.sb_stream.video_xp)
        self.txwire_ag.mon.ap_pkt.connect(self.sb_stream.wire_xp)
        self.sideband_ag.mon.ap.connect(self.sb_stream.sideband_xp)

        # Link-protocol scoreboard
        self.txwire_ag.mon.ap_beat.connect(self.sb_linkpro.beat_xp)

        # Control scoreboard: commands, both buses, acknowledgments.
        self.uplink_ag.mon.ap.connect(self.sb_control.uplink_xp)
        self.reg_ag.mon.ap.connect(self.sb_control.reg_xp)
        self.apb_ag.mon.ap.connect(self.sb_control.apb_xp)
        self.apb_ag.mon.ap_start.connect(self.sb_control.apb_start_xp)
        self.txwire_ag.mon.ap_pkt.connect(self.sb_control.wire_xp)
        self.sideband_ag.mon.ap.connect(self.sb_control.sideband_xp)

        # RX trigger scoreboard (host trigger packet -> sb_trigger_out_app)
        self.uplink_ag.mon.ap.connect(self.sb_rxtrig.uplink_xp)
        self.sideband_ag.mon.ap.connect(self.sb_rxtrig.sideband_xp)

        # Linktest scoreboard — RX (uplink) + sideband + TX type-0x04 pkts
        # + register reads of the counters.
        self.uplink_ag.mon.ap.connect(self.sb_linktest.uplink_xp)
        self.sideband_ag.mon.ap.connect(self.sb_linktest.sideband_xp)
        self.txwire_ag.mon.ap_pkt.connect(self.sb_linktest.pkt_xp)
        self.reg_ag.mon.ap.connect(self.sb_linktest.reg_xp)

        # Reg scoreboard — the register bus against the register map.
        self.reg_ag.mon.ap.connect(self.sb_reg.reg_xp)
        self.sideband_ag.mon.ap.connect(self.sb_reg.sideband_xp)
        self.sb_reg.sb_mon = self.sideband_ag.mon

        # I/O-ack scoreboard — host triggers (uplink) vs device K28.6 acks.
        self.uplink_ag.mon.ap.connect(self.sb_ioack.uplink_xp)
        self.txwire_ag.mon.ap_short.connect(self.sb_ioack.short_xp)

        # TX-trigger scoreboard — io_agent edges vs device K28.4/K28.2
        # packets, gated by the host's I/O acknowledgments.
        self.io_ag.mon.ap.connect(self.sb_txtrig.io_xp)
        self.txwire_ag.mon.ap_short.connect(self.sb_txtrig.short_xp)
        self.uplink_ag.mon.ap.connect(self.sb_txtrig.uplink_xp)
        self.sideband_ag.mon.ap.connect(self.sb_txtrig.sideband_xp)

        # Link state and the receiver's error pulses.
        self.sideband_ag.mon.ap.connect(self.sb_linkstate.sideband_xp)
        self.uplink_ag.mon.ap.connect(self.sb_linkerr.uplink_xp)
        self.sideband_ag.mon.ap.connect(self.sb_linkerr.sideband_xp)

        # Link-reset scoreboard — host 0x4000 writes vs §10.3.28 sequencing.
        self.uplink_ag.mon.ap.connect(self.sb_linkreset.uplink_xp)
        self.sideband_ag.mon.ap.connect(self.sb_linkreset.sideband_xp)

        # Stream dump — packet-level only (idle filtering happens inside).
        self.txwire_ag.mon.ap_pkt.connect(self.stream_dump.pkt_xp)
        self.txwire_ag.mon.ap_pkt.connect(self.pkt_log.pkt_xp)
        self.txwire_ag.mon.ap_short.connect(self.pkt_log.short_xp)

        # Coverage — subscribes to everything.
        self.txwire_ag.mon.ap_beat.connect(self.cov.beat_xp)
        self.txwire_ag.mon.ap_pkt.connect(self.cov.pkt_xp)
        self.txwire_ag.mon.ap_short.connect(self.cov.short_xp)
        self.uplink_ag.mon.ap.connect(self.cov.uplink_xp)
        self.sideband_ag.mon.ap.connect(self.cov.sideband_xp)
        self.apb_ag.mon.ap.connect(self.cov.apb_xp)
        self.reg_ag.mon.ap.connect(self.cov.reg_xp)

        # The host's bit clock follows a clock retune (its ppm offset is
        # the test's business, not the clock's).
        self.clkrst_ag.uplink_drv = self.uplink_ag.drv

        # Device triggers are acknowledged through the insertion lane.
        self.txwire_ag.mon.ap_short.connect(self.trig_resp.short_xp)
        self.trig_resp.uplink_drv = self.uplink_ag.drv

        # The reactive host: commands on the packet lane, acknowledgments
        # off the downlink.
        self.txwire_ag.mon.ap_pkt.connect(self.host.pkt_xp)
        self.host.uplink_drv = self.uplink_ag.drv

        # A device reset returns every model to its power-on state.
        self.clkrst_ag.on_reset_start.append(self.reset_starts)
        self.clkrst_ag.on_reset.append(self.reset_models)

        # Virtual sequencer handles.
        self.vseqr.video_seqr  = self.video_ag.seqr
        self.vseqr.cfg_seqr    = self.cfg_ag.seqr
        self.vseqr.uplink_seqr = self.uplink_ag.seqr
        self.vseqr.uplink_trig_seqr = self.uplink_ag.trig_seqr
        self.vseqr.apb_seqr    = self.apb_ag.seqr
        self.vseqr.io_seqr     = self.io_ag.seqr

    def start_of_simulation_phase(self):
        drv = self.uplink_ag.drv
        self.sb_rxtrig.jitter_ui = drv.jitter_ui
        self.sb_ioack.char_ns = drv.char_ns   # the host's character time (D6)

    async def quiesce(self, timeout_ns: int = 400_000, idle_words: int = 64,
                      stop_source: bool = True) -> bool:
        """Wait until the device has nothing left to say.

        A test used to end on a fixed `Timer`, which is generous where
        nothing is happening and a race where something is: with a free
        running pixel source, whether the last frame counted as delivered
        or lost depended on where the timer happened to fall.

        This stops the pixel source (so the source stops producing
        expectations), then waits for the downlink to have been IDLE for
        `idle_words` words *and* for every scoreboard to report no
        outstanding expectation.  Returns False on timeout — the
        scoreboards' own end-of-test checks then say what was still owed,
        which is the finding, not this.
        """
        dut = get_dut()
        if stop_source:
            dut.cfg_run.value = 0
            dut.s_pix_valid.value = 0
        mon = self.txwire_ag.mon
        deadline = timeout_ns
        step = 200
        while deadline > 0:
            await Timer(step, unit="ns")
            deadline -= step
            if mon.idle_run < idle_words:
                continue
            if any(sb.pending_count() for sb in self.scoreboards().values()):
                continue
            return True
        self.logger.warning(
            f"quiesce: {timeout_ns} ns elapsed with idle_run={mon.idle_run} "
            + " ".join(f"{n}={sb.pending_count()}"
                       for n, sb in self.scoreboards().items()
                       if sb.pending_count())
        )
        return False

    def reset_starts(self, domains: str = "all") -> None:
        """A reset is about to be driven: whatever is on the wire and the
        link go down with it."""
        from cocotb.utils import get_sim_time
        from uvm.scoreboards.link_state_scoreboard import UP_WORDS, word_ns
        t = get_sim_time("ns")
        self.sb_stream.flush_window(t)
        self.sb_linkstate.allow_drop(t, t + 2_000)
        self.sb_linkerr.allow(t, t + (UP_WORDS + 16) * word_ns())

    def reset_models(self, domains: str = "all") -> None:
        """The device was reset (any domain resets it whole, cxp_cdc_reset):
        register models, slave memory and the stream's expectations go back
        to power-on."""
        self.sb_control.reset_model()
        self.sb_reg.reset_model()
        self.sb_rxtrig.device_reset()
        self.apb_ag.mon.reset_mem()
        self.sb_linkstate.device_reset()
        self.sb_stream.device_reset()
        self.sb_stream.close_flush_window()

    def scoreboards(self) -> dict:
        """name -> scoreboard.  The name is what an ``EXPECT_FAIL`` tag
        uses, so it is the attribute name and nothing derived."""
        return {
            "sb_stream":    self.sb_stream,
            "sb_linkpro":   self.sb_linkpro,
            "sb_control":   self.sb_control,
            "sb_rxtrig":    self.sb_rxtrig,
            "sb_linktest":  self.sb_linktest,
            "sb_reg":       self.sb_reg,
            "sb_ioack":     self.sb_ioack,
            "sb_txtrig":    self.sb_txtrig,
            "sb_linkreset": self.sb_linkreset,
            "sb_linkstate": self.sb_linkstate,
            "sb_linkerr":   self.sb_linkerr,
            "sb_test":      self.sb_test,
        }

    def error_kinds(self) -> dict:
        """(scoreboard name, error kind) -> count, over every scoreboard.

        Finalises each scoreboard on the way through, so the "nothing
        ever arrived" checks run exactly once and land in the result.
        """
        out: dict = {}
        for name, sb in self.scoreboards().items():
            for kind, n in sb.finalize().items():
                out[(name, kind)] = n
        return out

    def all_scoreboards_ok(self) -> bool:
        return not any(self.error_kinds().values())
