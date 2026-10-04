"""Virtual sequences (§9.2) — compose per-agent base sequences.

Each class follows the proposal naming so test files read like the
testplan table.  All take a `vsequencer` argument exposing
v.<agent>_seqr attributes.
"""

from __future__ import annotations

import random

from pyuvm import uvm_sequence

from uvm.agents.video_agent       import VideoRandomSeq, VideoItem
from uvm.agents.cfg_agent         import CfgToggleSeq, CfgItem
from uvm.agents.host_uplink_agent import (
    UplinkIdleSeq, UplinkCtrlRandomSeq,
    UplinkTriggerRandomSeq, UplinkLinktestSeq, UplinkErrorInjectSeq,
    UplinkRegSeq, UplinkItem,
)
from uvm.agents.io_agent          import IoTriggerSeq
from uvm.agents.apb_slave_agent   import (
    ApbResponderPerfectSeq, ApbResponderPslverrSeq, ApbResponderWaitstateSeq,
)
from uvm.common.cxp_pkg import UplinkKind, BootstrapAddr


class VsSmoke(uvm_sequence):
    """One Mono8 frame + one ctrl-cmd read."""
    async def body(self):
        v = self.sequencer  # virtual sequencer
        await ApbResponderPerfectSeq().start(v.apb_seqr)
        await CfgToggleSeq(use_tpg=1).start(v.cfg_seqr)
        await UplinkCtrlRandomSeq(n=1, write=False).start(v.uplink_seqr)


class VsStressConcurrent(uvm_sequence):
    def __init__(self, name="vs_stress_concurrent", seed=2):
        super().__init__(name)
        self.seed = seed

    async def body(self):
        import cocotb
        v = self.sequencer
        await ApbResponderPerfectSeq().start(v.apb_seqr)
        await CfgToggleSeq(use_tpg=0).start(v.cfg_seqr)
        f1 = cocotb.start_soon(
            VideoRandomSeq(n_frames=2, rng_seed=self.seed).start(v.video_seqr))
        f2 = cocotb.start_soon(
            UplinkCtrlRandomSeq(n=4, seed=self.seed + 1).start(v.uplink_seqr))
        f3 = cocotb.start_soon(
            # Between packets, not inside one: a trigger inserted into a
            # long uplink packet is swallowed today, and
            # test_trigger_in_ctrl_packet owns that case on its own.
            UplinkTriggerRandomSeq(n=2, seed=self.seed + 3).start(
                v.uplink_seqr))
        f4 = cocotb.start_soon(
            UplinkLinktestSeq(n=1).start(v.uplink_seqr))
        for f in (f1, f2, f3, f4):
            await f


class _ResetBurstSeq(uvm_sequence):
    """Inline child sequence for VsLinkResetStorm — runs on the uplink
    sequencer; emits N back-to-back CTRL_CMD_RESET items."""

    def __init__(self, name="_reset_burst_seq", n=3):
        super().__init__(name)
        self.n = n

    async def body(self):
        for _ in range(self.n):
            item = UplinkItem("u")
            await self.start_item(item)
            item.xact.kind = UplinkKind.CTRL_CMD_RESET
            await self.finish_item(item)


class VsLinkResetStorm(uvm_sequence):
    async def body(self):
        v = self.sequencer
        await _ResetBurstSeq(n=3).start(v.uplink_seqr)


# =============================================================================
# Cross-interface virtual sequences (§ concurrent multi-interface activity).
#
# Once the TPG is enabled with cfg_run=1 the stream datapath is autonomous
# device hardware: it keeps framing type-0x01 packets onto the wire for the
# whole test.  These sequences therefore drive the *other* interfaces while
# the stream runs underneath, so the scoreboards verify synchronisation,
# arbitration, and data integrity during genuinely concurrent operation.
# =============================================================================
class VsStreamPlusCtrl(uvm_sequence):
    """Receive an image stream while performing register read/write — the
    canonical cross-interface scenario.  The TPG free-runs on the wire;
    host control commands flow on the serial uplink concurrently."""

    def __init__(self, name="vs_stream_plus_ctrl", n_cmds=8, seed=11):
        super().__init__(name)
        self.n_cmds = n_cmds
        self.seed = seed

    async def body(self):
        v = self.sequencer
        await ApbResponderPerfectSeq().start(v.apb_seqr)
        await CfgToggleSeq(use_tpg=1).start(v.cfg_seqr)   # stream free-runs
        await UplinkCtrlRandomSeq(n=self.n_cmds, seed=self.seed).start(
            v.uplink_seqr)


class VsStreamPlusTrigger(uvm_sequence):
    """Image stream + device->host HS trigger packets.  Triggers preempt
    the stream at word boundaries (§8.3.2); the stream packets must stay
    CRC-clean and every trigger edge must produce its K28.4/K28.2 packet."""

    def __init__(self, name="vs_stream_plus_trigger", n_edges=8, seed=12):
        super().__init__(name)
        self.n_edges = n_edges
        self.seed = seed

    async def body(self):
        v = self.sequencer
        await ApbResponderPerfectSeq().start(v.apb_seqr)
        await CfgToggleSeq(use_tpg=1).start(v.cfg_seqr)
        await IoTriggerSeq(n_edges=self.n_edges, gap=200).start(v.io_seqr)


class VsStreamPlusLinkReset(uvm_sequence):
    """Image stream interrupted by a host ConnectionReset mid-flight.  The
    §10.3.28 clear window must not corrupt the wire; the device then holds
    the stream (StreamPacketSizeMax is 0 again) until the host writes it,
    and resumes with tag 0 (§8.5.3)."""

    def __init__(self, name="vs_stream_plus_link_reset", seed=13, host=None,
                 spsm=None):
        super().__init__(name)
        self.seed = seed
        self.host = host
        self.spsm = spsm

    async def body(self):
        v = self.sequencer
        await ApbResponderPerfectSeq().start(v.apb_seqr)
        await CfgToggleSeq(use_tpg=1).start(v.cfg_seqr)
        await UplinkIdleSeq(n=24).start(v.uplink_seqr)            # stream runs
        await self.host.write(int(BootstrapAddr.LINK_RESET), [1])
        await UplinkIdleSeq(n=24).start(v.uplink_seqr)            # recover
        # Rediscovery: the host writes StreamPacketSizeMax again.
        await self.host.write_ok(int(BootstrapAddr.STR_PKT_DSIZE), [self.spsm])
        await UplinkIdleSeq(n=24).start(v.uplink_seqr)


class VsFullConcurrent(uvm_sequence):
    """Maximum cross-interface load: the TPG stream runs on the wire while
    host control commands and host triggers flow on the uplink and the
    device's own HS trigger fires on a separate pin — four interfaces
    active at once.  Verifies arbitration, preemption and data integrity
    hold under simultaneous traffic."""

    def __init__(self, name="vs_full_concurrent", seed=14):
        super().__init__(name)
        self.seed = seed

    async def body(self):
        import cocotb
        v = self.sequencer
        await ApbResponderPerfectSeq().start(v.apb_seqr)
        await CfgToggleSeq(use_tpg=1).start(v.cfg_seqr)
        f_ctrl = cocotb.start_soon(
            UplinkCtrlRandomSeq(n=4, seed=self.seed).start(v.uplink_seqr))
        f_htrig = cocotb.start_soon(
            # See VsStressConcurrent: between packets, not inside one.
            UplinkTriggerRandomSeq(n=2, seed=self.seed + 1).start(
                v.uplink_seqr))
        f_dtrig = cocotb.start_soon(
            IoTriggerSeq(n_edges=6, gap=300).start(v.io_seqr))
        for f in (f_ctrl, f_htrig, f_dtrig):
            await f
