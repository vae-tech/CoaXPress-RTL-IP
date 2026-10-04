"""The concrete UVM test classes.

Each class extends CxpTopTest and overrides main_seq() to start the
appropriate virtual sequence.

A class carrying `EXPECT_FAIL` is red today for a finding that is already
written down.  The tag names the scoreboard *and the error kind* it
tolerates, so everything else in the test still gates; the test also fails
if the tagged kind stops firing, so a tag cannot outlive its finding.
Eleven tests carry one; they come off with the stream-path rework and the
scoreboard's pixel-format model.

Every class also carries `PLAN`, the validation-plan test cases it serves,
and `PLAN_PARTIAL` for those it runs only part of the procedure for (see
`CxpTopTest`).  The mapping is taken from the "Existing" column of
`docs/verification/validation/cxp_validation_test_proposal_260921.md`.
"""

from __future__ import annotations

from cocotb.triggers import Timer
from pyuvm import uvm_sequence

from uvm.tests.base_test import CxpTopTest
from uvm.seqs.virtual_seqs import (
    VsSmoke, VsStressConcurrent, VsLinkResetStorm,
    VsStreamPlusCtrl, VsStreamPlusTrigger, VsStreamPlusLinkReset,
    VsFullConcurrent,
)
from uvm.agents.host_uplink_agent import (
    UplinkIdleSeq, UplinkCtrlRandomSeq,
    UplinkTriggerRandomSeq, UplinkLinktestSeq, UplinkErrorInjectSeq,
    UplinkRegSeq, UplinkItem,
)
from uvm.agents.io_agent   import IoTriggerSeq
from uvm.agents.video_agent import VideoRandomSeq, VideoItem
from uvm.agents.cfg_agent   import CfgToggleSeq
from uvm.agents.apb_slave_agent import (
    ApbResponderPerfectSeq, ApbResponderPslverrSeq, ApbResponderWaitstateSeq,
    USER_BASE,
)

# Four words of the user window (the TB shell's APB slave).
USER_ADDRS = tuple(USER_BASE + 4 * i for i in (0, 1, 7, 100))
from uvm.common.cxp_pkg import BootstrapAddr, UplinkKind

from cxp_protocol import regmap
from cxp_protocol import stream as gs


# Findings the tags below wait for.  One string per finding, so the tag
# that names it reads as a reference rather than a paraphrase.


# Validation-plan rows every stream test serves (plan map, "Existing"
# column: the stream scoreboard's format, framing and bit-exact checks run
# in each of them).
_PLAN_STREAM = ("CXP-CAM-DATA-001", "CXP-CAM-IMG-002", "CXP-CAM-IMG-011")
_PARTIAL_STREAM = {
    "CXP-CAM-DATA-001": "packet format checked only on the packets that "
                        "reach the wire; Mono8, one DsizeP per run",
    "CXP-CAM-IMG-011":  "bit-exact on Mono8 frames only; one pixel source "
                        "and geometry set per run",
}


class _CtrlResetSeq(uvm_sequence):
    """Inline sequence used by test_ctrl_reset_op.  Just sends N
    CTRL_CMD_RESET items through the uplink sequencer."""

    def __init__(self, name="_ctrl_reset_seq", n=2):
        super().__init__(name)
        self.n = n

    async def body(self):
        for _ in range(self.n):
            item = UplinkItem("u")
            await self.start_item(item)
            item.xact.kind = UplinkKind.CTRL_CMD_RESET
            await self.finish_item(item)


class _RalSweepSeq(uvm_sequence):
    """Inline sequence used by test_ral_sweep.  For each RW address,
    walks the supplied patterns: write, mirror locally, then read back
    so reg_scoreboard can verify the APB read returns the mirror value.
    """

    def __init__(self, name="_ral_sweep_seq", ral=None,
                 addrs=None, patterns=(0xFFFF_FFFF, 0x0000_0000, 0xA5A5_A5A5)):
        super().__init__(name)
        self.ral = ral
        self.addrs = addrs or []
        self.patterns = patterns

    async def body(self):
        for addr in self.addrs:
            for pattern in self.patterns:
                # Write
                item = UplinkItem("u")
                await self.start_item(item)
                item.xact.kind = UplinkKind.CTRL_CMD_WRITE
                item.xact.address = int(addr)
                item.xact.nwords  = 1
                item.xact.payload = [pattern]
                await self.finish_item(item)
                if self.ral is not None:
                    self.ral.write_mirror(int(addr), pattern)
                # IDLE gap so the ack of the previous cmd fully drains
                # before the next cmd reuses apb_master's shared rbuf.
                # See known RTL limit: apb_master writes rbuf before
                # ack_tx finishes reading it on back-to-back cmds.
                await UplinkIdleSeq(n=16).start(self.sequencer)
                # Readback
                item = UplinkItem("u")
                await self.start_item(item)
                item.xact.kind = UplinkKind.CTRL_CMD_READ
                item.xact.address = int(addr)
                item.xact.nwords  = 1
                await self.finish_item(item)
                await UplinkIdleSeq(n=16).start(self.sequencer)


# -----------------------------------------------------------------------------
# 1. IDLE baseline
# -----------------------------------------------------------------------------
class test_idle_baseline(CxpTopTest):
    # Serves no validation-plan row of its own (plan map, "Existing").
    PLAN = ()

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await UplinkIdleSeq(n=8).start(self.env.uplink_ag.seqr)
        # Just let the wire run idle for ~2 us.
        await Timer(2_000, unit="ns")


# -----------------------------------------------------------------------------
# 2. Stream / TPG smoke
# -----------------------------------------------------------------------------
class test_stream_tpg(CxpTopTest):
    PLAN = _PLAN_STREAM
    PLAN_PARTIAL = _PARTIAL_STREAM

    async def main_seq(self):
        await VsSmoke().start(self.env.vseqr)
        await Timer(5_000, unit="ns")


# -----------------------------------------------------------------------------
# 3. Random pixel-ingress video
# -----------------------------------------------------------------------------
class test_stream_video(CxpTopTest):
    PKT_DSIZE_P = 64  # smaller-than-default packet size — exercises adaptive scoreboard check
    PLAN = _PLAN_STREAM
    PLAN_PARTIAL = _PARTIAL_STREAM

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await CfgToggleSeq(use_tpg=0, arbitrary=0).start(self.env.cfg_ag.seqr)
        # Eight random-size frames back to back: each image's last packet
        # closes at its end, so the next frame never overruns it.
        await VideoRandomSeq(n_frames=8).start(self.env.video_ag.seqr)
        await Timer(50_000, unit="ns")


# -----------------------------------------------------------------------------
# 3b. Ragged frames: every tail length, a width that is not a multiple of 4,
#     and DsizeP shrinking between frames (§8.5.2, §8.5.3).
# -----------------------------------------------------------------------------
class test_stream_video_ragged(CxpTopTest):
    """Eight sensor frames back to back whose last packets are 1, 2 and
    DsizeP − 1 words long, at DsizeP 64 and then 16, with widths 4 and 5:
    every DsizeP header equals its payload, the tags run on without a gap
    and every frame is bit-exact."""

    PKT_DSIZE_P = 64
    PLAN = _PLAN_STREAM
    PLAN_PARTIAL = _PARTIAL_STREAM

    # (Xsize, Ysize): 25 header words + Ysize x (2 + ceil(Xsize / 4)) words.
    FRAMES_64 = ((4, 4), (5, 4), (29, 4), (49, 7), (57, 6))   # tails 49, 53, 1, 2, 63
    FRAMES_16 = ((21, 1), (25, 1), (13, 1))                    # tails 1, 2, 15

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await CfgToggleSeq(use_tpg=0, arbitrary=0).start(self.env.cfg_ag.seqr)
        for x, y in self.FRAMES_64:
            await _OneFrameSeq(xsize=x, ysize=y).start(self.env.video_ag.seqr)
        await Timer(20_000, unit="ns")
        # DsizeP 16: StreamPacketSizeMax for 16 payload words, written as a
        # host does, between images.
        await self.env.host.write_ok(regmap.STREAM_PACKET_SIZE_MAX, [self.spsm_for(16)])
        for x, y in self.FRAMES_16:
            await _OneFrameSeq(xsize=x, ysize=y).start(self.env.video_ag.seqr)
        await Timer(30_000, unit="ns")


# -----------------------------------------------------------------------------
# 4. Arbitrary header + line marker
# -----------------------------------------------------------------------------
class test_arbitrary_image(CxpTopTest):
    PLAN = ("CXP-CAM-IMG-007", "CXP-CAM-DATA-001", "CXP-CAM-IMG-011")
    PLAN_PARTIAL = {"CXP-CAM-IMG-007": "constant geometry on every line; per-line "
                                       "geometry never driven",
                    **_PARTIAL_STREAM}

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await CfgToggleSeq(use_tpg=0, arbitrary=1).start(self.env.cfg_ag.seqr)
        await VideoRandomSeq(n_frames=1).start(self.env.video_ag.seqr)


# -----------------------------------------------------------------------------
# 5. Ctrl-cmd read
# -----------------------------------------------------------------------------
class test_ctrl_cmd_read(CxpTopTest):
    PLAN = ("CXP-CAM-CTRL-001",)

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await UplinkCtrlRandomSeq(n=4, write=False).start(self.env.uplink_ag.seqr)


# -----------------------------------------------------------------------------
# 6. Ctrl-cmd write
# -----------------------------------------------------------------------------
class test_ctrl_cmd_write(CxpTopTest):
    PLAN = ("CXP-CAM-CTRL-002",)

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await UplinkCtrlRandomSeq(n=4, write=True).start(self.env.uplink_ag.seqr)


# -----------------------------------------------------------------------------
# 7. Ctrl reset op (0xFF)
# -----------------------------------------------------------------------------
class test_ctrl_reset_op(CxpTopTest):
    PLAN = ("CXP-CAM-CTRL-006",)
    PLAN_PARTIAL = {"CXP-CAM-CTRL-006": "0xFF between commands only; never during one"}

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await _CtrlResetSeq(n=2).start(self.env.uplink_ag.seqr)


# -----------------------------------------------------------------------------
# 8. Trigger uplink
# -----------------------------------------------------------------------------
class test_trigger_uplink(CxpTopTest):
    PLAN = ("CXP-CAM-TRIG-007",)
    PLAN_PARTIAL = {"CXP-CAM-TRIG-007": "4 triggers, spaced, Delay 0; no overlap, "
                                        "retrigger or rate stress"}

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await UplinkTriggerRandomSeq(n=4).start(self.env.uplink_ag.trig_seqr)


# -----------------------------------------------------------------------------
# 10a. Linktest — clean (T10.1)
# -----------------------------------------------------------------------------
class test_linktest_clean(CxpTopTest):
    """N clean §6.7.4 link-test packets must leave sb_lt_err_count = 0.

    The host runs 200 ppm off the device's bit rate — §6.7 allows 100 ppm
    at each end — with 0.02 UI of random jitter (seeded like every
    host)."""

    HOST_PPM = 200.0
    HOST_JITTER_UI = 0.02

    PLAN = ("CXP-CAM-CT-004",)
    PLAN_PARTIAL = {"CXP-CAM-CT-004": "64-word bodies, not 1024; the last two words "
                                      "of a packet never compared"}

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await UplinkLinktestSeq(n=4).start(self.env.uplink_ag.seqr)
        # Settle window so the sideband monitor republishes the final
        # counter value before LinktestScoreboard.check_phase fires.
        await Timer(2_000, unit="ns")


# -----------------------------------------------------------------------------
# 10b. Linktest with word-pattern error injection (T10.2)
# -----------------------------------------------------------------------------
class test_linktest_inject(CxpTopTest):
    """Drive 1 clean prefill + 3 injected link-test packets, separated by
    IDLE bursts (CXP §6.7.4 requires ≥16-word spacing between link-test
    packets anyway). `sb_lt_err_count` must equal the total number of
    corrupted data words — 3 packets × 3 errors = 9.

    The prefill packet primes the parser's latched `long_type_q` to
    0x04 so the link-test gate (`cxp_rx_link.sv:321`) is open by the
    time the first injected SOP arrives."""

    PLAN = ("CXP-CAM-CT-004",)
    PLAN_PARTIAL = {"CXP-CAM-CT-004": "64-word bodies, not 1024; the last two words "
                                      "of a packet never compared"}

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await UplinkLinktestSeq(n=1).start(self.env.uplink_ag.seqr)
        for _ in range(3):
            await UplinkIdleSeq(n=4).start(self.env.uplink_ag.seqr)
            await UplinkErrorInjectSeq(
                n=1, mode="lt_word",
                kind=UplinkKind.LINKTEST, n_word_errors=3,
            ).start(self.env.uplink_ag.seqr)
        await Timer(2_000, unit="ns")


# -----------------------------------------------------------------------------
# 11. Byte-replication robustness (1-bit errors recovered)
# -----------------------------------------------------------------------------
class test_byte_replication_robust(CxpTopTest):
    PLAN = ("CXP-CAM-PROT-006",)
    PLAN_PARTIAL = {"CXP-CAM-PROT-006": "one bit in one replica of the type word only"}

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await UplinkErrorInjectSeq(n=4, mode="replica").start(self.env.uplink_ag.seqr)


# -----------------------------------------------------------------------------
# 12. CRC error injection
# -----------------------------------------------------------------------------
class test_crc_error(CxpTopTest):
    PLAN = ("CXP-CAM-NEG-001",)

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await UplinkErrorInjectSeq(n=4, mode="crc").start(self.env.uplink_ag.seqr)


# -----------------------------------------------------------------------------
# 13. PSLVERR burst
# -----------------------------------------------------------------------------
class test_pslverr_burst(CxpTopTest):
    """User-window writes answered with PSLVERR: each is acknowledged 0x40
    and the slave memory keeps its value."""
    # Serves no validation-plan row of its own (plan map, "Existing").
    PLAN = ()

    async def main_seq(self):
        await ApbResponderPslverrSeq().start(self.env.apb_ag.seqr)
        await UplinkCtrlRandomSeq(n=4, write=True,
                                  addrs=USER_ADDRS).start(self.env.uplink_ag.seqr)

# -----------------------------------------------------------------------------
# 15. Arbiter preemption — all four sources at once
# -----------------------------------------------------------------------------
class test_arbiter_preempt(CxpTopTest):
    PLAN = _PLAN_STREAM
    PLAN_PARTIAL = _PARTIAL_STREAM

    async def main_seq(self):
        await VsStressConcurrent().start(self.env.vseqr)
        await Timer(10_000, unit="ns")


# -----------------------------------------------------------------------------
# 16. Control-channel reset storm (three 0xFF; the ConnectionReset storm is C-07)
# -----------------------------------------------------------------------------
class test_ctrl_reset_storm(CxpTopTest):
    PLAN = ("CXP-CAM-REC-002",)
    PLAN_PARTIAL = {"CXP-CAM-REC-002": "three 0xFF control resets; no "
                                       "ConnectionReset, no rediscovery"}

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        # VsLinkResetStorm is a virtual sequence — start it on the
        # env's virtual sequencer so its body can dispatch
        # _ResetBurstSeq onto vseqr.uplink_seqr.
        await VsLinkResetStorm().start(self.env.vseqr)

# -----------------------------------------------------------------------------
# 18. RAL sweep — hw_reset + bit_bash equivalents
# -----------------------------------------------------------------------------
class test_ral_sweep(CxpTopTest):
    PLAN = ("CXP-CAM-BOOT-001", "CXP-CAM-BND-003")
    PLAN_PARTIAL = {"CXP-CAM-BOOT-001": "3 hand-picked RW addresses, not the map",
                    "CXP-CAM-BND-003": "patterns only; StreamPacketSizeMax "
                                       "extremes not driven"}

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        # Full-width RW registers only — TEST_MODE is excluded so the
        # sweep never flips TestMode and injects link-test packets into a
        # register test; ConnectionConfig takes only its supported value.
        addrs = [BootstrapAddr.MST_HOST_ID, BootstrapAddr.STR_PKT_DSIZE,
                 BootstrapAddr.TEST_ERR_SEL]
        await _RalSweepSeq(ral=self.env.ral, addrs=addrs).start(
            self.env.uplink_ag.seqr)

# =============================================================================
# Additional v1.1.1 coverage — features with no prior top-level test.
# =============================================================================

# -----------------------------------------------------------------------------
# 20. I/O-acknowledgment (§8.3.3) — host trigger -> device K28.6 ack.
# -----------------------------------------------------------------------------
class test_io_ack(CxpTopTest):
    """Every trigger packet the host sends must be answered by a K28.6
    I/O-ack packet (code 0x01) — cxp_tx_io_ack, Table 17."""

    PLAN = ("CXP-CAM-TRIG-001", "CXP-CAM-TRIG-006")
    PLAN_PARTIAL = {"CXP-CAM-TRIG-006": "acks counted; no latency bound (D6 open)"}

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await UplinkTriggerRandomSeq(n=6).start(self.env.uplink_ag.trig_seqr)
        await Timer(4_000, unit="ns")


# -----------------------------------------------------------------------------
# 21. Device->host HS trigger packets (§8.3.2) — cxp_tx_trigger_hs.
# -----------------------------------------------------------------------------
class test_tx_trigger(CxpTopTest):
    """The device's local trigger_in_app reaches the host as 2-word trigger
    packets (asserted -> K28.4, de-asserted -> K28.2, Delay = 0); edges
    faster than the host's I/O acknowledgments merge (§8.3.3) and the host
    ends at the pin's level."""

    PLAN = ("CXP-CAM-TRIG-004",)
    PLAN_PARTIAL = {"CXP-CAM-TRIG-004": "Delay 0; pacing against the acknowledgment "
                                        "checked in the unit and device_top benches"}

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await IoTriggerSeq(n_edges=6).start(self.env.io_ag.seqr)
        await Timer(4_000, unit="ns")


# -----------------------------------------------------------------------------
# 22. Device->host link-test under host TestMode (§8.7 / §10.3.35/38).
# -----------------------------------------------------------------------------
class test_tx_linktest_mode(CxpTopTest):
    """A host write of 1 to the TestMode register makes the device stream
    type-0x04 connection-test packets; §10.3.38 TestPacketCountTx must
    equal the number transmitted.  A write of 0 stops it cleanly."""

    PLAN = ("CXP-CAM-CT-001",)

    async def main_seq(self):
        self.env.sb_linktest.expect_tx_linktest = True
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await UplinkRegSeq(addr=int(BootstrapAddr.TEST_MODE), data=1,
                           write=True).start(self.env.uplink_ag.seqr)
        # Hold the link alive with IDLE words (NOT a bare Timer) so the
        # word aligner keeps lock — several 1027-word link-test packets
        # go out during this window — then disable TestMode.
        await UplinkIdleSeq(n=8).start(self.env.uplink_ag.seqr)
        await UplinkRegSeq(addr=int(BootstrapAddr.TEST_MODE), data=0,
                           write=True).start(self.env.uplink_ag.seqr)
        await Timer(20_000, unit="ns")


# -----------------------------------------------------------------------------
# 23. Register-router rejection (§5.1) — extension-link write -> ack 0x43.
# -----------------------------------------------------------------------------
class test_router_reject(CxpTopTest):
    """With from_extension_link=1 the register router rejects every host
    write with ack 0x43 + router_reject_pulse; reads are still forwarded.

    Then decision D3 from the extension connection: a ConnectionReset
    write and a MasterHostConnectionID write are acknowledged 0x01 and
    not executed (§10.3.28 / §10.3.30 notes) — no reset window opens, the
    ID reads back unchanged; a 0xFF is executed (0x03: it resets the
    control channel, not the connection)."""

    PLAN = ("CXP-CAM-CTRL-007",)

    async def main_seq(self):
        env, h, t = self.env, self.env.host, self.env.sb_test
        env.sb_control.extension_link_mode = True
        await ApbResponderPerfectSeq().start(env.apb_ag.seqr)
        await CfgToggleSeq(use_tpg=0, from_extension_link=1).start(
            env.cfg_ag.seqr)
        await UplinkCtrlRandomSeq(n=3, write=True).start(env.uplink_ag.seqr)
        await UplinkCtrlRandomSeq(n=2, write=False).start(env.uplink_ag.seqr)
        # The open-loop commands' acknowledgments first (matched by order).
        for _ in range(400):
            if not env.sb_control.pending_count():
                break
            await Timer(1_000, unit="ns")
        await Timer(4_000, unit="ns")

        mhcid0 = (await h.read(regmap.MASTER_HOST_CONNECTION_ID))[1]
        windows0 = env.sb_linkreset.active_windows
        code = await h.write(regmap.MASTER_HOST_CONNECTION_ID, [0x5A5A_0001])
        t.check(code == 0x01, "ext_mhcid_ack", f"MasterHostConnectionID write "
                f"from the extension connection answered {code!r}, D3 wants 0x01")
        mhcid1 = (await h.read(regmap.MASTER_HOST_CONNECTION_ID))[1]
        t.check(mhcid1 == mhcid0, "ext_mhcid_value", f"MasterHostConnectionID "
                f"{mhcid0} -> {mhcid1} after a write from the extension connection")
        code = await h.write(regmap.CONNECTION_RESET, [1])
        t.check(code == 0x01, "ext_crst_ack", f"ConnectionReset write from the "
                f"extension connection answered {code!r}, D3 wants 0x01")
        await Timer(20_000, unit="ns")
        t.check(env.sb_linkreset.active_windows == windows0, "ext_crst_executed",
                "a ConnectionReset written on the extension connection opened a "
                "reset window")
        code = await h.reset()
        t.check(code == 0x03, "ext_ctrl_reset", f"0xFF from the extension "
                f"connection answered {code!r}, D3 wants it executed (0x03)")
        await Timer(4_000, unit="ns")


# -----------------------------------------------------------------------------
# 24. §10.3.28 link reset — host ConnectionReset write -> clear-list.
# -----------------------------------------------------------------------------
class test_link_reset(CxpTopTest):
    """A host write of 1 to ConnectionReset (0x4000) opens a link-reset
    window, broadcasts the §10.3.28 clear-list (MasterHostConnectionID
    et al. forced to 0) and fires a type-0x03 reset-done ack."""

    PLAN = ("CXP-CAM-INIT-002",)
    PLAN_PARTIAL = {"CXP-CAM-INIT-002": "one §10.3.28 post-condition read back; no "
                                        "rate fallback, no 200 ms deadline"}

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        # Program a register, ConnectionReset, then read it back as 0.
        await UplinkRegSeq(addr=int(BootstrapAddr.MST_HOST_ID),
                           data=0xA5A5_A5A5, write=True).start(
            self.env.uplink_ag.seqr)
        await UplinkIdleSeq(n=8).start(self.env.uplink_ag.seqr)
        await UplinkRegSeq(addr=int(BootstrapAddr.LINK_RESET), data=1,
                           write=True).start(self.env.uplink_ag.seqr)
        await UplinkIdleSeq(n=8).start(self.env.uplink_ag.seqr)
        await UplinkRegSeq(addr=int(BootstrapAddr.MST_HOST_ID),
                           write=False).start(self.env.uplink_ag.seqr)
        await Timer(4_000, unit="ns")


# -----------------------------------------------------------------------------
# 25. TPG reconfiguration via the §10.3.19-27 device-control registers.
# -----------------------------------------------------------------------------
class test_tpg_config(CxpTopTest):
    """Host reprograms the TPG test pattern through the bootstrap
    device-control registers while the stream free-runs — exercises
    register -> datapath propagation through the integration."""

    PLAN = ("CXP-CAM-IMG-002", "CXP-CAM-IMG-011")

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await CfgToggleSeq(use_tpg=1).start(self.env.cfg_ag.seqr)
        for pat in (1, 2, 3, 0):   # bars / flat / grey-bars / gradient
            await UplinkRegSeq(addr=int(BootstrapAddr.MFR_TESTPATTERN),
                               data=pat, write=True).start(
                self.env.uplink_ag.seqr)
            await UplinkIdleSeq(n=6).start(self.env.uplink_ag.seqr)
        await Timer(6_000, unit="ns")


# -----------------------------------------------------------------------------
# 25b. TPG pixel formats via PixelFormat (§9.4.2 packing).
# -----------------------------------------------------------------------------
class test_tpg_formats(CxpTopTest):
    """Host reprograms PixelFormat while the TPG free-runs; every frame is
    reassembled at its header's pixel width and compared word for word
    with the TPG output packed by the golden §9.4.2 packer.

    Image1StreamID is set to 5 first (§10.3.26): every test-pattern
    packet's StreamID word (Table 19) and header StreamID (Table 37) must
    carry the register's value (stream scoreboard, golden from the
    register)."""

    PLAN = ("CXP-CAM-IMG-002", "CXP-CAM-IMG-011")

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await UplinkRegSeq(addr=regmap.IMAGE1_STREAM_ID_ALIAS, data=5,
                           write=True).start(self.env.uplink_ag.seqr)
        await UplinkIdleSeq(n=6).start(self.env.uplink_ag.seqr)
        await CfgToggleSeq(use_tpg=1).start(self.env.cfg_ag.seqr)
        for fmt in (0x0101, 0x0102, 0x0103, 0x0104, 0x0105, 0x0101):
            await UplinkRegSeq(addr=int(BootstrapAddr.FEAT_PIXFMT),
                               data=gs.PIXELF_TO_PFNC[fmt], write=True).start(
                self.env.uplink_ag.seqr)
            await UplinkIdleSeq(n=6).start(self.env.uplink_ag.seqr)
        await Timer(6_000, unit="ns")


# =============================================================================
# Cross-interface interaction tests — simultaneous activity on multiple
# interfaces, verifying synchronisation / arbitration / data integrity.
# =============================================================================

# -----------------------------------------------------------------------------
# 26. Image stream + concurrent register read/write.
# -----------------------------------------------------------------------------
class test_xifc_stream_ctrl(CxpTopTest):
    """The canonical cross-interface scenario: receive an image stream
    while performing register read/write operations to the device."""

    PLAN = ("CXP-CAM-CTRL-010",) + _PLAN_STREAM
    PLAN_PARTIAL = {"CXP-CAM-CTRL-010": "acks under stream load counted; no "
                                        "latency bound",
                    **_PARTIAL_STREAM}

    async def main_seq(self):
        await VsStreamPlusCtrl(n_cmds=8).start(self.env.vseqr)
        await Timer(10_000, unit="ns")


# -----------------------------------------------------------------------------
# 27. Image stream + concurrent device->host HS triggers.
# -----------------------------------------------------------------------------
class test_xifc_stream_trigger(CxpTopTest):
    """Trigger packets are inserted into the stream at word boundaries
    (§8.3.2); stream packets must stay CRC-clean and the host must end at
    the pin's trigger level."""

    PLAN = _PLAN_STREAM
    PLAN_PARTIAL = _PARTIAL_STREAM

    async def main_seq(self):
        await VsStreamPlusTrigger(n_edges=8).start(self.env.vseqr)
        await Timer(10_000, unit="ns")


# -----------------------------------------------------------------------------
# 28. Image stream interrupted by a host link reset.
# -----------------------------------------------------------------------------
class test_xifc_stream_linkreset(CxpTopTest):
    """A ConnectionReset arrives mid-stream — the §10.3.28 clear window
    must not corrupt the wire and the stream must resume cleanly: the
    packet already on the wire keeps its PacketTag, the first packet that
    starts after the window opens carries tag 0 (§8.5.3)."""

    PLAN = ("CXP-CAM-DATA-003",) + _PLAN_STREAM
    PLAN_PARTIAL = {**_PARTIAL_STREAM}

    async def main_seq(self):
        await VsStreamPlusLinkReset(host=self.env.host,
                                    spsm=self.spsm_for(self.pkt_dsize)).start(self.env.vseqr)
        await Timer(10_000, unit="ns")


# -----------------------------------------------------------------------------
# 29. Full concurrency — stream + ctrl + host trigger + device trigger.
# -----------------------------------------------------------------------------
class test_xifc_full(CxpTopTest):
    """Four interfaces active at once: the TPG stream on the wire, host
    control commands and host triggers on the uplink, and the device's
    own HS trigger on a separate pin.  Verifies arbitration, preemption
    and data integrity hold under simultaneous traffic."""

    PLAN = _PLAN_STREAM
    PLAN_PARTIAL = _PARTIAL_STREAM

    async def main_seq(self):
        await VsFullConcurrent().start(self.env.vseqr)
        await Timer(15_000, unit="ns")


# =============================================================================
# Arbiter liveness — a frame whose word count is not a multiple of
# cfg_dsizeP.
#
# The frame's last stream packet is short.  It once started before all of
# its words were in the FIFO and wedged the arbiter's stream slot with no
# EOP, starving the I/O-ack (§8.3.3) and control-ack sources behind it.
# The stream path now closes the packet at the image's last word and
# starts a packet only once all of it is in the FIFO, so an owner always
# has its next word (checked by an SVA); traffic issued afterwards must
# still reach the wire.
# =============================================================================
class _OneFrameSeq(uvm_sequence):
    """Inline directed video sequence — one rectangular frame of a fixed
    geometry.  The geometry is chosen (with PKT_DSIZE_P=64 below) so the
    frame spans >1 stream packet — 169 merged words = 2 full 64-word
    packets + a 41-word remainder — so the wire is NOT silent (the stream
    scoreboard sees real packets) and the last packet is short."""

    def __init__(self, name="_one_frame_seq", xsize=64, ysize=8):
        super().__init__(name)
        self.xsize = xsize
        self.ysize = ysize

    async def body(self):
        item = VideoItem("v")
        await self.start_item(item)
        x = item.xact
        x.xsize        = self.xsize
        x.ysize        = self.ysize
        x.pixfmt       = 0x0101      # Mono8
        x.arbitrary    = False
        x.dval_density = 1.0         # no ingress gaps — deterministic timing
        x.fill_ramp()
        await self.finish_item(item)


# -----------------------------------------------------------------------------
# 30. Arbiter recovery after a stream-packet underflow — I/O-ack (§8.3.3).
# -----------------------------------------------------------------------------
class test_arbiter_stream_underflow(CxpTopTest):
    """Drive one frame whose tail underfills the final cfg_dsizeP packet,
    then send host triggers: every one must still be answered with its
    K28.6 I/O-ack."""

    PKT_DSIZE_P = 64   # frame tail (41 words) underfills a 64-word packet

    PLAN = _PLAN_STREAM
    PLAN_PARTIAL = _PARTIAL_STREAM

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await CfgToggleSeq(use_tpg=0, arbitrary=0).start(self.env.cfg_ag.seqr)
        # Frame -> 2 full stream packets + a short last one.
        await _OneFrameSeq().start(self.env.video_ag.seqr)
        # Hold the uplink at IDLE so the device RX keeps word-lock.
        await UplinkIdleSeq(n=256).start(self.env.uplink_ag.seqr)
        # The arbiter must now be free: every host trigger gets its ack.
        await UplinkTriggerRandomSeq(n=4).start(self.env.uplink_ag.trig_seqr)
        await Timer(6_000, unit="ns")


# -----------------------------------------------------------------------------
# 31. Arbiter recovery after a stream-packet underflow — ctrl-ack (type 0x03).
# -----------------------------------------------------------------------------
class test_arbiter_underflow_ctrl(CxpTopTest):
    """Companion to test_arbiter_stream_underflow for the priority-2
    ctrl-ack source: after the same short last packet, host control reads
    must still be acknowledged on the wire.  Reads are spaced with IDLE
    gaps so the separate back-to-back ack_busy limit is not in play — a
    missing ack here is an arbiter dead-lock and nothing else."""

    PKT_DSIZE_P = 64

    PLAN = _PLAN_STREAM
    PLAN_PARTIAL = _PARTIAL_STREAM

    async def main_seq(self):
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await CfgToggleSeq(use_tpg=0, arbitrary=0).start(self.env.cfg_ag.seqr)
        await _OneFrameSeq().start(self.env.video_ag.seqr)
        await UplinkIdleSeq(n=256).start(self.env.uplink_ag.seqr)
        # Spaced host ctrl reads — each must come back with a type-0x03 ack.
        for _ in range(4):
            await UplinkRegSeq(addr=int(BootstrapAddr.MST_HOST_ID),
                               write=False).start(self.env.uplink_ag.seqr)
            await UplinkIdleSeq(n=16).start(self.env.uplink_ag.seqr)
        await Timer(6_000, unit="ns")



# -----------------------------------------------------------------------------
# 32. §8.2.4 insertion — a trigger inside a control command.
# -----------------------------------------------------------------------------
class _OneCtrlReadSeq(uvm_sequence):
    """One control read, on the packet lane."""

    def __init__(self, name="_one_ctrl_read_seq", addr=regmap.MASTER_HOST_CONNECTION_ID):
        super().__init__(name)
        self.addr = addr

    async def body(self):
        item = UplinkItem("u")
        await self.start_item(item)
        item.xact.kind = UplinkKind.CTRL_CMD_READ
        item.xact.address = int(self.addr)
        item.xact.nwords = 1
        await self.finish_item(item)


class _OneTriggerSeq(uvm_sequence):
    """One rising trigger, on the §8.2.4 insertion lane."""

    async def body(self):
        item = UplinkItem("u")
        await self.start_item(item)
        item.xact.kind = UplinkKind.TRIGGER_RISE
        await self.finish_item(item)


class test_trigger_in_ctrl_packet(CxpTopTest):
    """§8.2.4: a trigger indication may be inserted between two words of a
    long packet, and both survive — the trigger fires and is I/O-acked,
    the command still executes and is acknowledged 0x00.

    The host sends a control read and, three word times in, inserts a
    Table 15 trigger (six characters, at a character boundary), as a
    compliant host does.
    """

    # Serves no validation-plan row of its own (plan map, "Existing").
    PLAN = ()

    async def main_seq(self):
        import cocotb
        await ApbResponderPerfectSeq().start(self.env.apb_ag.seqr)
        await UplinkIdleSeq(n=8).start(self.env.uplink_ag.seqr)
        cmd = cocotb.start_soon(
            _OneCtrlReadSeq().start(self.env.uplink_ag.seqr))
        # Three word times into the six-word command packet.
        await Timer(20_000, unit="ns")
        trig = cocotb.start_soon(
            _OneTriggerSeq().start(self.env.uplink_ag.trig_seqr))
        await cmd
        await trig
        await Timer(40_000, unit="ns")

