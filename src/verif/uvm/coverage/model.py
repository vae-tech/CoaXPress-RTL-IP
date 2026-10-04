"""Functional coverage model (catalogue U3, `prompts/uvm_env_review_tests.md`).

Every cell is sampled where the context is known — the downlink packet
log, the control reference model, the stream scoreboard, the link
scoreboards, the pixel driver — at the end of each test, and written into
that test's `cov_summary.json` as `<group>.<cell>` counts.  `make cov_gate`
merges a tier's files and fails on a cell of `GOALS` that no test hit.

A cell that cannot be hit at all is not listed in `GOALS`; `NOT_REACHABLE`
says why, next to its group.
"""

from __future__ import annotations

from collections import Counter

from cxp_protocol import packets as gp


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------
def size_bucket(b: int) -> str:
    if b <= 0:
        return "0"
    if b <= 3:
        return "1_3"
    if b == 4:
        return "4"
    if b <= 104:
        return "5_104"
    if b < 256:
        return "105_255"
    if b == 256:
        return "max"
    return "over"


def run_bucket(n: int) -> str:
    return "lt50" if n < 50 else ("50_98" if n < 99 else "99")


def stall_bucket(n: int) -> str:
    return "1" if n <= 1 else ("2_7" if n < 8 else ("8_63" if n < 64 else "ge64"))


# ---------------------------------------------------------------------------
def collect(env) -> Counter:
    bins: Counter = Counter()
    log = env.pkt_log

    # cg_insertion: short packet kind x the packet it sits in x where.
    names = {"TRIG_RISE": "trig_rise", "TRIG_FALL": "trig_fall", "IOACK": "ioack"}
    ctx = {"0x01": "stream", "0x03": "ack", "0x04": "linktest"}
    for s in log.short:
        k = names.get(s.kind.name)
        if k is None:
            continue
        w = log.where(s.tx_cycle - 1)
        if w == "idle":
            bins[f"cg_insertion.{k}.idle"] += 1
        else:
            t, part = w.split(":")
            bins[f"cg_insertion.{k}.{ctx.get(t, t)}.{part}"] += 1

    # cg_idle_stretch: an IDLE inside a long packet; runs before an IDLE.
    for p in log.long:
        if p.cycles and p.cycles[-1] - p.cycles[0] + 1 > len(p.cycles):
            bins[f"cg_idle_stretch.inside.{ctx.get(f'0x{p.type_byte:02x}', 'other')}"] += 1
    for n, c in getattr(env.sb_linkpro, "run_hist", {}).items():
        bins[f"cg_idle_stretch.run.{run_bucket(n)}"] += c

    # cg_ctrl: op x region x size bucket, and every Table 22 code seen.
    for op, region, size, code, waits, raw in env.sb_control.cov:
        if raw is not None and raw not in (gp.OP_READ, gp.OP_WRITE, gp.OP_RESET):
            op = "undefined"
        bins[f"cg_ctrl.{op}.{region}.{size_bucket(size) if op in ('read', 'write') else 'na'}"] += 1
        bins[f"cg_ctrl.code.{code:02x}"] += 1
        if waits:
            # A Wait acknowledgment (0x04) went out before the final one.
            bins["cg_ctrl.code.04"] += 1
            bins[f"cg_ctrl_timing.wait.{region}"] += 1

    # cg_ctrl_timing: user-slave latency against what ended the command.
    apb = env.apb_ag.mon
    if apb.max_wait:
        b = ("gt_timeout" if apb.abandoned else
             "gt_wait" if env.sb_control.waits else
             "1_15" if apb.max_wait <= 15 else "16_wait")
        bins[f"cg_ctrl_timing.stall.{b}"] += 1
    if apb.transfers and not apb.max_wait:
        bins["cg_ctrl_timing.stall.0"] += 1
    if env.sb_control.reset_expected:
        bins["cg_ctrl_timing.event.ctrl_reset"] += 1
    if env.sb_control.reset_aborted_access:
        bins["cg_ctrl_timing.event.reset_aborted_access"] += 1
    if env.sb_control.orphans_drained:
        bins["cg_ctrl_timing.event.reset_drained_access"] += 1

    # cg_stream: format x width mod 4 x rect/arbitrary x source; packets.
    for fmt, xsize, arb, tpg in env.sb_stream.frame_cov:
        bins[f"cg_stream.fmt.{fmt:04x}"] += 1
        bins[f"cg_stream.width_mod4.{xsize % 4}"] += 1
        bins[f"cg_stream.kind.{'arbitrary' if arb else 'rect'}"] += 1
        bins[f"cg_stream.source.{'tpg' if tpg else 'sensor'}"] += 1
    for n, dsizep in env.sb_stream.pkt_cov:
        last = "one" if n == 1 else ("full" if n >= dsizep else "short")
        bins[f"cg_stream.packet.{last}"] += 1
        bins[f"cg_stream.dsizep.{'le16' if dsizep <= 16 else 'le256' if dsizep <= 256 else 'gt256'}"] += 1

    # cg_tag: wrap and restarts.
    for e in env.sb_stream.tag_events:
        bins[f"cg_tag.{e}"] += 1
    if env.sb_linkreset.active_windows and "reset" in env.sb_stream.tag_events:
        bins["cg_tag.reset_by.connection_reset"] += 1
    if getattr(env.sb_stream, "conn_cfg_writes", 0) and "reset" in env.sb_stream.tag_events:
        bins["cg_tag.reset_by.connection_config"] += 1

    # cg_uplink_err: what the host injected.
    for k, n in env.sb_linkerr.injected.items():
        if n:
            bins[f"cg_uplink_err.{k}"] += n
    for code in (gp.ACK_CRC, gp.ACK_MALFORMED, gp.ACK_SIZE_MISMATCH, gp.ACK_BAD_OP):
        if env.sb_control.codes_seen.get(code):
            bins[f"cg_uplink_err.answered.{code:02x}"] += 1

    # cg_link: link state transitions.
    ls = env.sb_linkstate
    if ls.history:
        bins["cg_link.up"] += 1
    if ls.drops:
        bins["cg_link.loss"] += ls.drops
        if any(lv for _, lv in ls.history[1:]):
            bins["cg_link.relock"] += 1

    # cg_reset: resets seen and what was running.
    if env.sb_linkreset.active_windows:
        bins["cg_reset.connection_reset"] += env.sb_linkreset.active_windows
        if env.sb_stream.frames_flushed or env.sb_stream.frames_torn:
            bins["cg_reset.connection_reset.stream_mid_image"] += 1
    if env.sb_control.reset_expected:
        bins["cg_reset.ctrl_reset"] += env.sb_control.reset_expected
    for d in getattr(env.clkrst_ag, "resets_done", []):
        bins[f"cg_reset.domain.{d}"] += 1

    # cg_cdc: clock ratio points run.
    for a, t, x in getattr(env.clkrst_ag, "ratios", []):
        bins[f"cg_cdc.tx_rx.{'slow' if t > x else 'fast' if t < x else 'eq'}"] += 1
        bins[f"cg_cdc.app_tx.{'slow' if a > t else 'fast' if a < t else 'eq'}"] += 1

    # cg_backpressure: s_pix_ready low, how long and where.
    drv = env.video_ag.drv
    if drv.stalls:
        bins[f"cg_backpressure.len.{stall_bucket(drv.max_stall)}"] += 1
        for pos, n in (("eol", drv.stalls_at_eol), ("eof", drv.stalls_at_eof),
                       ("sof", drv.stalls_at_sof)):
            if n:
                bins[f"cg_backpressure.at.{pos}"] += n
        mid = drv.stalls - drv.stalls_at_eol - drv.stalls_at_eof - drv.stalls_at_sof
        if mid > 0:
            bins["cg_backpressure.at.mid_line"] += mid
    return bins


# ---------------------------------------------------------------------------
# Goals: every cell a tier must hit (nightly).
# ---------------------------------------------------------------------------
GOALS = [
    # cg_insertion
    *(f"cg_insertion.{k}.{c}.{p}" for k in ("trig_rise", "trig_fall", "ioack")
      for c in ("stream",) for p in ("header", "payload", "tail")),
    *(f"cg_insertion.{k}.{c}.payload" for k in ("trig_rise", "trig_fall", "ioack")
      for c in ("ack", "linktest")),
    *(f"cg_insertion.{k}.idle" for k in ("trig_rise", "trig_fall", "ioack")),
    # cg_idle_stretch
    "cg_idle_stretch.inside.stream", "cg_idle_stretch.inside.linktest",
    "cg_idle_stretch.run.lt50", "cg_idle_stretch.run.50_98",
    # cg_ctrl
    *(f"cg_ctrl.read.{r}.{b}" for r in ("boot_ro", "boot_rw", "user")
      for b in ("1_3", "4", "5_104", "max")),
    "cg_ctrl.read.user.over", "cg_ctrl.read.unmapped.4",
    *(f"cg_ctrl.write.{r}.4" for r in ("boot_ro", "boot_rw", "user", "unmapped")),
    "cg_ctrl.write.user.5_104", "cg_ctrl.write.user.max", "cg_ctrl.write.user.over",
    "cg_ctrl.reset.boot_ro.na", "cg_ctrl.undefined.boot_ro.na",
    *(f"cg_ctrl.code.{c:02x}" for c in (0x00, 0x01, 0x03, 0x04, 0x40, 0x41, 0x42,
                                        0x43, 0x44, 0x45, 0x46, 0x47, 0x80)),
    # cg_ctrl_timing
    "cg_ctrl_timing.stall.0", "cg_ctrl_timing.stall.gt_wait",
    "cg_ctrl_timing.stall.gt_timeout", "cg_ctrl_timing.event.ctrl_reset",
    "cg_ctrl_timing.event.reset_aborted_access", "cg_ctrl_timing.wait.user",
    # cg_stream
    *(f"cg_stream.fmt.{f:04x}" for f in (0x101, 0x102, 0x103, 0x104, 0x105)),
    *(f"cg_stream.width_mod4.{m}" for m in range(4)),
    "cg_stream.kind.rect", "cg_stream.kind.arbitrary",
    "cg_stream.source.tpg", "cg_stream.source.sensor",
    "cg_stream.packet.one", "cg_stream.packet.short", "cg_stream.packet.full",
    "cg_stream.dsizep.le16", "cg_stream.dsizep.le256", "cg_stream.dsizep.gt256",
    # cg_tag
    "cg_tag.wrap", "cg_tag.reset", "cg_tag.reset_by.connection_reset",
    "cg_tag.reset_by.connection_config",
    # cg_uplink_err
    "cg_uplink_err.code", "cg_uplink_err.disp", "cg_uplink_err.glitch",
    *(f"cg_uplink_err.answered.{c:02x}" for c in (0x80, 0x47, 0x46, 0x42)),
    # cg_link
    "cg_link.up", "cg_link.loss", "cg_link.relock",
    # cg_reset
    "cg_reset.connection_reset", "cg_reset.connection_reset.stream_mid_image",
    "cg_reset.ctrl_reset", "cg_reset.domain.app", "cg_reset.domain.tx",
    "cg_reset.domain.rx",
    # cg_cdc
    "cg_cdc.tx_rx.slow", "cg_cdc.tx_rx.fast", "cg_cdc.app_tx.slow", "cg_cdc.app_tx.fast",
    # cg_backpressure
    "cg_backpressure.len.8_63", "cg_backpressure.at.mid_line",
    "cg_backpressure.at.eol",
]

NOT_REACHABLE = {
    "cg_ctrl_timing.event.reset_drained_access": "the APB bridge aborts on a "
        "control reset, and the register bus answers in one cycle; both "
        "finish before the reset acknowledgment reaches the wire",
    "cg_insertion.*.ack.header / .tail": "a two-word short packet lands inside a "
        "3-word acknowledgment header or its CRC/EOP only by chance; the payload "
        "cell carries the insertion rule",
    "cg_insertion.*.linktest.header / .tail": "Table 23 has a 2-word header and no "
        "CRC: an insertion there is a matter of one word, covered by payload",
    "cg_idle_stretch.inside.ack": "an acknowledgment is at most 70 words: the "
        "99-word IDLE rule never falls inside one after a clean IDLE",
    "cg_idle_stretch.run.99": "the inserter's soft threshold (95) yields the IDLE "
        "before 99 whenever a word boundary allows; 99 is the SVA's hard limit",
    "cg_stream.kind x fmt / source crosses": "the arbitrary form and the formats "
        "are driven from the sensor port; the TPG is rectangular Mono8..16",
    "cg_cdc.* equal ratio": "the default point; not a goal",
    "cg_backpressure.at.eof / .sof": "never held in 1392 stalls (C-11): the "
        "port takes an image's first and last pixel at once",
}
