"""control_scoreboard — every control command against a reference model (§8.6).

Consumes:
* `UplinkTxn` from the uplink driver (published before the first
  character goes out, `t_done_ns` filled in when the last one left);
* `RegTxn` from the register-bus monitor and `ApbTxn` from the
  user-window monitor — the accesses the device made;
* type-0x03 packets from the downlink monitor;
* `SidebandEvent`s (the control plane's status pulses).

Prediction.  Every command is judged the way the parser does (Table 22,
`cxp_ctrl_cmd_parser`): a CRC or 8B/10B error 0x80, Size 0 0x46, more data
words than ControlPacketSizeMax allows 0x45, a write on an extension
link 0x43 (decision D3 for ConnectionReset / MasterHostConnectionID).
A command that passes is executed on a reference register file
(`cxp_protocol.regref.RegRef`, generated from `src/regmap/`) or, in the user
window, on a model of the TB slave's memory; that gives the code and the
read data.  A control channel reset (0xFF) is answered 0x03 and cancels
whatever command has not been answered yet (§8.6.1.2).

Checks per acknowledgment:
* all four lanes of the type and code words equal (no vote needed on a
  clean link);
* the code is one Table 22 defines and the one predicted;
* long form (0x00): Size = the command's B, N = ceil(B / 4) data words,
  each equal to the predicted register value (big-endian on the wire),
  pad bytes 0, CRC; exactly N + 6 words;
* Wait (0x04): only for a user-window access, at most one per command,
  Size 4, one word of 100 .. 10 000 ms, CRC, 7 words; the final
  acknowledgment follows;
* every other code: exactly 4 words (the short form);
* sent within 200 ms (the host's timeout, §8.6.1.1) of the command's
  last character, a Wait included; the final one within the time the
  Wait announced;
* the accesses: an executed command makes exactly its accesses —
  address, address + 4, ... on the right bus (register file or user
  window), in its direction; a rejected one none.  A read acknowledged
  0x00 carries what the bus returned, word for word.

Commands a host could not have sent (decision D7: a third command while
one executes and one waits) may be dropped: a command published while
two others still wait for their acknowledgment is optional.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Set

import cocotb
from cocotb.utils import get_sim_time
from pyuvm import uvm_tlm_analysis_fifo

from cxp_protocol import packets as gp
from cxp_protocol import regmap as gregmap
from cxp_protocol import regmodel as grm
from cxp_protocol import regmap
from cxp_protocol.kcodes import lanes
from cxp_protocol.regref import RegRef, Unknown

from uvm.agents.apb_slave_agent import (
    USER_BASE, USER_WORDS, in_user_window, user_reset_value,
)
from uvm.common.clocks import ms_ns
from uvm.common.cxp_pkg import PacketType, UplinkKind
from uvm.common.decisions import (
    D3_EXTENSION_MHCID_SILENT, D3_EXTENSION_WRITE_CODE, D7_WAITING_COMMANDS,
)
from uvm.common.handles import get_dut
from uvm.scoreboards.sb_base import CxpScoreboard


TABLE22 = {gp.ACK_OK_DATA, gp.ACK_OK_WRITE, gp.ACK_OK_RESET, gp.ACK_WAIT,
           gp.ACK_BAD_ADDR, gp.ACK_BAD_DATA, gp.ACK_BAD_OP, gp.ACK_RO_WRITE,
           gp.ACK_WO_READ, gp.ACK_OVERSIZE, gp.ACK_SIZE_MISMATCH,
           gp.ACK_MALFORMED, gp.ACK_CRC}

# Data words a command may carry: ControlPacketSizeMax holds Table 21's
# six framing words plus N data words (§8.6.4).
MAX_WORDS = regmap.CONTROL_PACKET_SIZE_MAX_VALUE // 4 - 6

HOST_TIMEOUT_MS = 200          # §8.6.1.1
WAIT_AFTER_MS = 100            # cxp_ctrl_plane p_WAIT_AFTER_MS
TIMEOUT_MS = 900               # cxp_ctrl_plane p_TIMEOUT_MS


def load_xml_blob(path: str = gregmap.XML_MEM) -> Optional[bytes]:
    """The XML ROM image the register file loads ($readmemh words,
    big-endian bytes), cut to its size: by default the one generated
    image, src/rtl/gen/cxp_camera_xml.mem, which the bench passes to the
    register file by absolute path."""
    if not os.path.exists(path):
        return None
    with open(path) as f:
        raw = b"".join(int(l, 16).to_bytes(4, "big")
                       for l in f if l.strip() and not l.startswith("//"))
    return raw[:grm.XML_BLOB_BYTES]


@dataclass
class _Cmd:
    x: object
    op: str                               # "read" | "write" | "reset"
    user: bool = False
    codes: Set[Optional[int]] = field(default_factory=set)  # final codes allowed
    data: Optional[List[Optional[int]]] = None  # predicted read values
    acc: List[int] = field(default_factory=list)  # addresses to access
    acc_seen: int = 0
    acc_err: bool = False
    acc_rdata: List[int] = field(default_factory=list)
    acc_slverr: bool = False
    acc_abandoned: bool = False
    optional: bool = False
    started: bool = False            # an access of it has begun
    waits: int = 0
    t_wait_ns: float = 0.0
    wait_ms: int = 0
    user_writes: List[tuple] = field(default_factory=list)

    @property
    def nwords(self) -> int:
        return gp.nwords_of(self.x.size_bytes)


class ControlScoreboard(CxpScoreboard):
    def build_phase(self):
        super().build_phase()
        self.uplink_fifo   = uvm_tlm_analysis_fifo("uplink_fifo",   self)
        self.reg_fifo      = uvm_tlm_analysis_fifo("reg_fifo",      self)
        self.apb_fifo      = uvm_tlm_analysis_fifo("apb_fifo",      self)
        self.apb_start_fifo = uvm_tlm_analysis_fifo("apb_start_fifo", self)
        self.wire_fifo     = uvm_tlm_analysis_fifo("wire_fifo",     self)
        self.sideband_fifo = uvm_tlm_analysis_fifo("sideband_fifo", self)

        self.uplink_xp   = self.uplink_fifo.analysis_export
        self.reg_xp      = self.reg_fifo.analysis_export
        self.apb_xp      = self.apb_fifo.analysis_export
        self.apb_start_xp = self.apb_start_fifo.analysis_export
        self.wire_xp     = self.wire_fifo.analysis_export
        self.sideband_xp = self.sideband_fifo.analysis_export

        self.ref = RegRef()
        blob = load_xml_blob()
        if blob is not None:
            self.ref.set_xml(blob)
        self.umem = {i: user_reset_value(i) for i in range(USER_WORDS)}
        self.q: Deque[_Cmd] = deque()
        self._orphans: List[int] = []
        self._last_final_ns = 0.0
        # (op, region, Size, code, waits, raw opcode) per answered command.
        self.cov: list = []
        self.orphans_drained = 0
        self.reset_aborted_access = 0
        self.acked = 0
        self.waits = 0
        self.max_latency_ns = 0.0
        self.codes_seen: dict = {}
        self.crc_injected = self.crc_observed = 0
        self.crc_maybe = 0
        self.reset_expected = self.reset_observed = 0
        self.nack_expected = self.nack_observed = 0
        # Kept for tests that set it; the extension-link strap is read from
        # the pin at every command.
        self.extension_link_mode = False
        self.strict_ack_match = True

    def reset_model(self) -> None:
        """The device was reset: power-on register values, fresh slave."""
        self.ref.reset()
        self.umem = {i: user_reset_value(i) for i in range(USER_WORDS)}
        for c in self.q:
            c.optional = True

    async def run_phase(self):
        cocotb.start_soon(self._drain_uplink())
        cocotb.start_soon(self._drain_reg())
        cocotb.start_soon(self._drain_apb())
        cocotb.start_soon(self._drain_apb_start())
        cocotb.start_soon(self._drain_sideband())
        await self._drain_wire()

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------
    def _parser_code(self, x, ext: bool) -> Optional[int]:
        """The code the parser answers without executing, or None."""
        if x.inject_crc_err or x.inject_code_at >= 0 or x.inject_disp_at >= 0:
            return gp.ACK_CRC
        if getattr(x, "opcode", None) is not None and x.opcode not in (
                gp.OP_READ, gp.OP_WRITE, gp.OP_RESET):
            return gp.ACK_BAD_OP
        if x.kind == UplinkKind.CTRL_CMD_RESET:
            return None
        if getattr(x, "extra_words", 0):
            return gp.ACK_SIZE_MISMATCH
        if x.size_bytes == 0:
            return gp.ACK_SIZE_MISMATCH
        if gp.nwords_of(x.size_bytes) > MAX_WORDS:
            return gp.ACK_OVERSIZE
        if ext and x.kind == UplinkKind.CTRL_CMD_WRITE:
            if D3_EXTENSION_MHCID_SILENT and x.address in (
                    regmap.CONNECTION_RESET, regmap.MASTER_HOST_CONNECTION_ID):
                return gp.ACK_OK_WRITE
            return D3_EXTENSION_WRITE_CODE
        return None

    def _outstanding(self) -> int:
        """Commands not answered yet (some may have been dropped: which is
        known only once the device answers, so count them all)."""
        return sum(1 for c in self.q if c.op != "reset")

    def _predict(self, x) -> _Cmd:
        ext = bool(int(get_dut().from_extension_link.value))
        op = {UplinkKind.CTRL_CMD_READ: "read", UplinkKind.CTRL_CMD_WRITE: "write",
              UplinkKind.CTRL_CMD_RESET: "reset"}[x.kind]
        c = _Cmd(x=x, op=op, user=in_user_window(x.address))
        # Decision D7: one command executes, one waits; a third is dropped.
        if self._outstanding() > D7_WAITING_COMMANDS:
            c.optional = True
        if x.expect_codes is not None:
            # A packet the test built by hand: its expectation, no access.
            c.codes = set(x.expect_codes)
            if c.codes == {gp.ACK_CRC}:
                self.crc_injected += 1
            elif gp.ACK_CRC in c.codes:
                self.crc_maybe += 1
            if op == "reset" and c.codes == {gp.ACK_OK_RESET}:
                self.reset_expected += 1
            return c
        if x.may_drop:
            c.optional = True
            c.codes = {gp.ACK_MALFORMED}
            x2 = type(x)(**{**x.__dict__, "may_drop": False, "beats_edit": None})
            e = self._predict(x2)
            e.x, e.optional = x, True
            e.codes |= {gp.ACK_MALFORMED}
            return e
        pc = self._parser_code(x, ext)
        if x.inject_code_at >= 0 or x.inject_disp_at >= 0:
            # A code or disparity error may also hit the framing: then the
            # packet is dropped (0x47, or nothing if the SOP is lost).
            c.codes = {gp.ACK_CRC, gp.ACK_MALFORMED, None}
            self.crc_maybe += 1
            return c
        if pc is not None:
            c.codes = {pc}
            if pc == gp.ACK_CRC:
                self.crc_injected += 1
            if pc not in (gp.ACK_OK_WRITE,):
                self.nack_expected += 1
            return c
        if op == "reset":
            c.codes = {gp.ACK_OK_RESET}
            self.reset_expected += 1
            return c
        n = c.nwords
        c.acc = [x.address + 4 * i for i in range(n)]
        if c.user:
            base = (x.address - USER_BASE) >> 2
            if op == "read":
                c.data = [self.umem.get(base + i, 0) for i in range(n)]
                c.codes = {gp.ACK_OK_DATA, gp.ACK_BAD_ADDR}
            else:
                # Commands execute in order, so the model takes the write
                # now; the slave keeps nothing it answered with PSLVERR.
                # The last word of a write of B bytes keeps the slave's
                # other bytes (PSTRB, Table 21); its value is big-endian, so
                # the first byte is bits 31:24.
                slverr = int(get_dut().usr_slverr.value)
                for i in range(n):
                    v = x.payload[i] if i < len(x.payload) else 0
                    keep = x.size_bytes - 4 * i
                    if 0 < keep < 4:
                        mask = (0xFFFF_FFFF << (8 * (4 - keep))) & 0xFFFF_FFFF
                        v = (v & mask) | (self.umem.get(base + i, 0) & ~mask & 0xFFFF_FFFF)
                    c.user_writes.append((base + i, v))
                    if not slverr:
                        self.umem[base + i] = v
                c.codes = {gp.ACK_OK_WRITE, gp.ACK_BAD_ADDR}
            return c
        if op == "read":
            code, words = self._ref_read(x.address, x.size_bytes)
            c.codes = {code}
            c.data = words if code == gp.ACK_OK_DATA else None
        else:
            vals = list(x.payload[:n]) + [0] * max(0, n - len(x.payload))
            if x.size_bytes % 4:
                c.codes = {self._ref_partial_write(x.address, vals, x.size_bytes)}
            else:
                c.codes = {self.ref.write(x.address, vals)}
        return c

    def _ref_read(self, addr: int, nbytes: int):
        """RegRef.read, with live values left open (None)."""
        if addr & 3:
            return gp.ACK_BAD_ADDR, []
        out: List[Optional[int]] = []
        for i in range(gp.nwords_of(nbytes)):
            a = addr + 4 * i
            try:
                out.append(self.ref.word(a))
            except Unknown:
                out.append(None)             # a live counter
            except KeyError:
                return gp.ACK_BAD_ADDR, []
            except PermissionError:
                return gp.ACK_WO_READ, []
            if a == regmap.CONNECTION_RESET:
                out[-1] = None               # 1 while the reset is in progress
        return gp.ACK_OK_DATA, out

    def _ref_partial_write(self, addr: int, vals: List[int], nbytes: int) -> int:
        """A write whose Size is not a multiple of 4: the last word's bytes
        past B are not written (byte enables).  Data words are big-endian on
        the wire, so the B mod 4 bytes written are the word's most
        significant.  The code is the whole-word write's code for the
        merged value."""
        n = len(vals)
        keep = nbytes % 4
        merged = list(vals)
        try:
            old = self.ref.word(addr + 4 * (n - 1))
        except (Unknown, KeyError, PermissionError):
            old = 0
        mask = ((1 << (8 * keep)) - 1) << (8 * (4 - keep))
        merged[-1] = (old & ~mask) | (vals[-1] & mask)
        return self.ref.write(addr, merged)

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------
    async def _drain_uplink(self):
        while True:
            x = await self.uplink_fifo.get()
            if x.kind not in (UplinkKind.CTRL_CMD_READ, UplinkKind.CTRL_CMD_WRITE,
                              UplinkKind.CTRL_CMD_RESET):
                continue
            self.q.append(self._predict(x))

    def _open_access_cmd(self, addr: int) -> Optional[_Cmd]:
        """The command this access belongs to: the first one still owed
        accesses — past any optional (droppable) command whose next
        address is not this one."""
        first = None
        for c in self.q:
            if c.acc and c.acc_seen < len(c.acc) and not c.acc_err:
                if first is None:
                    first = c
                if c.acc[c.acc_seen] == addr:
                    return c
                if not c.optional and not first.optional:
                    return first
        return first

    def _access(self, addr: int, write: bool, bus: str) -> Optional[_Cmd]:
        if addr in self._orphans:
            self._orphans.remove(addr)
            self.orphans_drained += 1
            return None
        c = self._open_access_cmd(addr)
        if c is None:
            self.err("stray_access",
                     f"{bus} {'write' if write else 'read'} 0x{addr:08x} with "
                     "no command expecting one")
            return None
        exp = c.acc[c.acc_seen]
        if addr != exp or write != (c.op == "write") or (bus == "apb") != c.user:
            self.err("access",
                     f"{bus} {'write' if write else 'read'} 0x{addr:08x}, the "
                     f"{c.op} of 0x{c.x.address:08x} expects 0x{exp:08x} on "
                     f"{'the user window' if c.user else 'the register bus'}")
        c.acc_seen += 1
        return c

    async def _drain_reg(self):
        while True:
            t = await self.reg_fifo.get()
            c = self._access(t.addr, t.write, "reg")
            if c is None:
                continue
            c.acc_rdata.append(t.rdata)
            if t.err:
                c.acc_err = True

    async def _drain_apb(self):
        while True:
            t = await self.apb_fifo.get()
            c = self._access(t.addr, t.write, "apb")
            if c is None:
                continue
            c.acc_rdata.append(t.rdata)
            if t.pslverr:
                c.acc_slverr = True
                c.acc_err = True
            if not t.completed:
                c.acc_abandoned = True
                c.acc_err = True
                if any(p.op == "reset" for p in self.q):
                    self.reset_aborted_access += 1

    async def _drain_apb_start(self):
        """A user-window transfer began: the command it belongs to is
        executing (it was not dropped), though its data is still owed."""
        while True:
            addr, write = await self.apb_start_fifo.get()
            for c in self.q:
                if c.user and c.acc and c.acc_seen < len(c.acc) \
                        and c.acc[c.acc_seen] == addr:
                    c.started = True
                    break

    async def _drain_sideband(self):
        while True:
            evt = await self.sideband_fifo.get()
            if evt.sb_cmd_crc_err_pulse:
                self.crc_observed += 1
            if evt.sb_ctrl_reset_pulse:
                self.reset_observed += 1
            if evt.sb_ctrl_nack_pulse:
                self.nack_observed += 1

    # ------------------------------------------------------------------
    # Acknowledgments
    # ------------------------------------------------------------------
    def _check_shape(self, pkt, ack) -> None:
        w = pkt.words
        for i, what in ((1, "type"), (2, "code")):
            ls = lanes(w[i][0])
            if len(set(ls)) != 1 or w[i][1] != 0:
                self.err("ack_replica",
                         f"acknowledgment {what} word 0x{w[i][0]:08x} "
                         f"(kmask {w[i][1]:x}) is not four equal data bytes")
        code = ack.code
        if code == gp.ACK_OK_DATA:
            n = gp.nwords_of(ack.size)
            if len(w) != n + 6:
                self.err("ack_length",
                         f"0x00 acknowledgment of Size {ack.size} is {len(w)} "
                         f"words, Table 22 makes it {n + 6}")
            if ack.size % 4 and len(w) == n + 6:
                last = w[4 + n - 1][0]
                keep = ack.size % 4
                pad = [b for j, b in enumerate(lanes(last)) if j >= keep]
                if any(pad):
                    self.err("ack_pad",
                             f"last data word 0x{last:08x} of Size {ack.size}: "
                             "pad bytes not 0")
            if not ack.crc_ok:
                self.err("ack_crc", f"0x00 acknowledgment CRC wrong")
        elif code == gp.ACK_WAIT:
            if len(w) != 7 or ack.size != 4:
                self.err("ack_length",
                         f"Wait acknowledgment is {len(w)} words, Size "
                         f"{ack.size}; Table 22 makes it 7 words, Size 4")
            if not ack.crc_ok:
                self.err("ack_crc", "Wait acknowledgment CRC wrong")
            ms = ack.data[0] if ack.data else 0
            if not 100 <= ms <= 10_000:
                self.err("ack_wait_time",
                         f"Wait announces {ms} ms (§8.6.3: 100 .. 10 000)")
        elif len(w) != 4:
            self.err("ack_length",
                     f"acknowledgment code 0x{code:02x} is {len(w)} words, "
                     "the short form is 4")

    def _take(self, code: int) -> Optional[_Cmd]:
        if code == gp.ACK_OK_RESET:
            # §8.6.1.2: the reset cancels every command not answered yet.
            # A bus access one of them had started may still complete (the
            # device drains it before the next command): it is theirs.
            for i, c in enumerate(self.q):
                if c.op == "reset":
                    for _ in range(i):
                        gone = self.q.popleft()
                        if gone.acc and gone.acc_seen < len(gone.acc):
                            self._orphans.append(gone.acc[gone.acc_seen])
                    return c
        while self.q:
            c = self.q[0]
            fits = code in c.codes or (code == gp.ACK_WAIT and c.user) or (
                c.user and c.acc and code in (gp.ACK_OK_DATA, gp.ACK_OK_WRITE,
                                              gp.ACK_BAD_ADDR))
            # A dropped command made no access: an acknowledgment of a
            # command that did belongs to a later one.
            if c.optional and c.acc and not (c.acc_seen or c.started) and code in (
                    gp.ACK_OK_DATA, gp.ACK_OK_WRITE, gp.ACK_WAIT):
                fits = False
            if fits:
                return c
            if c.optional or None in c.codes:
                self.q.popleft()
                continue
            return c
        return None

    async def _drain_wire(self):
        while True:
            pkt = await self.wire_fifo.get()
            if pkt.type_byte != PacketType.CTRL_ACK:
                continue
            self.acked += 1
            body = [w for w, _ in pkt.words[1:-1]]
            try:
                ack = gp.parse_ctrl_ack(body)
            except (ValueError, IndexError) as exc:
                self.err("ack_parse", f"acknowledgment does not parse: {exc}")
                continue
            self.codes_seen[ack.code] = self.codes_seen.get(ack.code, 0) + 1
            if ack.code not in TABLE22:
                self.err("ack_code_unknown",
                         f"code 0x{ack.code:02x} is not in Table 22")
                continue
            self._check_shape(pkt, ack)
            c = self._take(ack.code)
            if c is None:
                self.err("ack_unexpected",
                         f"acknowledgment 0x{ack.code:02x} with no command "
                         "waiting for one")
                continue
            self._check_time(c, ack, pkt)
            if ack.code == gp.ACK_WAIT:
                self._on_wait(c, ack, pkt)
                continue
            self.q.popleft()
            self._last_final_ns = pkt.eop_ns
            self._on_final(c, ack)
            self.cov.append((c.op, self._region(c.x), c.x.size_bytes, ack.code,
                             c.waits, getattr(c.x, "opcode", None)))

    @staticmethod
    def _region(x) -> str:
        a = x.address
        if in_user_window(a):
            return "user"
        try:
            r = RegRef()._row(a)
        except Exception:
            r = None
        if r is not None:
            return "boot_ro" if r[2] in ("RO", "STR", "ZERO") else "boot_rw"
        if grm.MFR_BASE <= a < grm.MFR_BASE + 4 * grm.MFR_WORDS or gregmap.WIDTH_SLOT <= a < gregmap.IMAGE_N_STREAM_ID_ADDRESS:
            return "boot_rw"
        if grm.XML_BLOB_ADDR <= a < grm.XML_BLOB_ADDR + grm.XML_BLOB_BYTES:
            return "boot_ro"
        return "unmapped"

    def _check_time(self, c: _Cmd, ack, pkt) -> None:
        if None in c.codes:
            # A packet cut or lost on the line: the device answers when it
            # sees the damage (its link monitor re-framing), not "at once".
            return
        # A command that waited behind another (decision D7) starts when
        # that one was answered.
        t_done = max(c.x.t_done_ns if c.x.t_done_ns >= 0 else pkt.sop_ns,
                     self._last_final_ns)
        if c.waits:
            limit = ms_ns(c.wait_ms)
            lat = pkt.sop_ns - c.t_wait_ns
            what = f"after the Wait announcing {c.wait_ms} ms"
        else:
            limit = ms_ns(HOST_TIMEOUT_MS)
            lat = pkt.sop_ns - t_done
            what = "after the command"
        self.max_latency_ns = max(self.max_latency_ns, lat)
        if lat > limit:
            self.err("ack_late",
                     f"0x{ack.code:02x} for the {c.op} of 0x{c.x.address:08x} "
                     f"came {lat:.0f} ns {what}, the limit is {limit:.0f} ns")

    def _on_wait(self, c: _Cmd, ack, pkt) -> None:
        self.waits += 1
        if not c.user:
            self.err("wait_bootstrap",
                     f"Wait for the {c.op} of bootstrap/register-file address "
                     f"0x{c.x.address:08x} (§10.3.3)")
        c.waits += 1
        if c.waits > 1:
            self.err("wait_twice",
                     f"second Wait for the {c.op} of 0x{c.x.address:08x}")
        c.t_wait_ns = pkt.sop_ns
        c.wait_ms = ack.data[0] if ack.data else 0

    def _on_final(self, c: _Cmd, ack) -> None:
        code = ack.code
        if c.user and c.acc and code == gp.ACK_BAD_ADDR and c.acc_seen < len(c.acc):
            # Timed out with its access unanswered.  APB has no abort: a
            # transfer that began runs on until PREADY, and that completion
            # belongs to no command.
            c.acc_abandoned = True
            if c.started:
                self._orphans.append(c.acc[c.acc_seen])
        if c.user and c.acc:
            # The user window's result is the bus's: PSLVERR or a transfer
            # abandoned at the timeout is 0x40.
            exp = gp.ACK_BAD_ADDR if (c.acc_slverr or c.acc_abandoned) else (
                gp.ACK_OK_DATA if c.op == "read" else gp.ACK_OK_WRITE)
            codes = {exp}
        else:
            codes = c.codes
        if code not in codes:
            self.err("ack_code",
                     f"the {c.op} of 0x{c.x.address:08x} (Size {c.x.size_bytes}) "
                     f"got 0x{code:02x}, expected "
                     + "/".join("none" if k is None else f"0x{k:02x}" for k in sorted(
                         codes, key=lambda v: -1 if v is None else v)))
        # Accesses.
        if code in (gp.ACK_OK_DATA, gp.ACK_OK_WRITE) and c.acc:
            if c.acc_seen != len(c.acc):
                self.err("access_count",
                         f"the {c.op} of 0x{c.x.address:08x} made {c.acc_seen} "
                         f"accesses, it has {len(c.acc)} words")
        if not c.acc and c.acc_seen:
            self.err("access_rejected",
                     f"rejected {c.op} of 0x{c.x.address:08x} made accesses")
        if code != gp.ACK_OK_DATA:
            return
        # Read data: the acknowledgment carries the bus's words, and those
        # are the model's.
        if ack.size != c.x.size_bytes:
            self.err("ack_size",
                     f"read of 0x{c.x.address:08x}: Size {ack.size}, the "
                     f"command asked {c.x.size_bytes}")
        n = c.nwords
        keep = c.x.size_bytes % 4
        for i in range(min(n, len(ack.data))):
            got = ack.data[i]
            mask = 0xFFFF_FFFF
            if i == n - 1 and keep:
                mask = (0xFFFF_FFFF << (8 * (4 - keep))) & 0xFFFF_FFFF
            if i < len(c.acc_rdata) and (got & mask) != (c.acc_rdata[i] & mask):
                self.err("ack_data_bus",
                         f"read of 0x{c.x.address + 4 * i:08x}: acknowledgment "
                         f"0x{got:08x}, the bus returned 0x{c.acc_rdata[i]:08x}")
            want = c.data[i] if c.data and i < len(c.data) else None
            if want is not None and (got & mask) != (want & mask):
                self.err("ack_data",
                         f"read of 0x{c.x.address + 4 * i:08x}: 0x{got:08x}, "
                         f"the register holds 0x{want:08x}")
        if len(ack.data) != n:
            self.err("ack_length",
                     f"read of Size {c.x.size_bytes}: {len(ack.data)} data words, "
                     f"expected {n}")

    # ------------------------------------------------------------------
    def pending_count(self) -> int:
        return sum(1 for c in self.q if not c.optional and None not in c.codes)

    def _final_check(self):
        owed = [c for c in self.q if not c.optional and None not in c.codes]
        if owed:
            self.err("ack_missing",
                     f"{len(owed)} command(s) never acknowledged: "
                     + ", ".join(f"{c.op} 0x{c.x.address:x}" for c in owed[:6]))
        self.q.clear()
        # A code or disparity error in a command may be answered 0x80 or
        # 0x47 (or not at all): those may add 0x80 pulses.
        if not (self.crc_injected <= self.crc_observed
                <= self.crc_injected + self.crc_maybe):
            self.err("crc_count",
                     f"0x80 pulses {self.crc_observed}, CRC errors sent "
                     f"{self.crc_injected} (+{self.crc_maybe} that may be)")
        if self.reset_expected != self.reset_observed:
            self.err("reset_pulse_count",
                     f"control-reset pulses {self.reset_observed}, 0xFF sent "
                     f"{self.reset_expected}")

    def report_phase(self):
        codes = " ".join(f"0x{k:02x}:{v}" for k, v in sorted(self.codes_seen.items()))
        self.logger.info(
            f"control_scoreboard: acks={self.acked} [{codes}] waits={self.waits} "
            f"max_latency={self.max_latency_ns:.0f}ns "
            f"crc_inj={self.crc_injected} crc_obs={self.crc_observed} "
            f"rst_exp={self.reset_expected} rst_obs={self.reset_observed} "
            f"{self.err_summary()}"
        )
