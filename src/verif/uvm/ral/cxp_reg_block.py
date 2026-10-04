"""Hand-rolled RAL for cxp_ctrl_bootstrap_regs (CXP-001-2015 Table 45).

PyUVM doesn't ship a full uvm_reg_block — we provide just enough to
support frontdoor (uplink ctrl-cmd) and backdoor (direct APB poke)
read/write, and uvm_reg_hw_reset_seq / uvm_reg_bit_bash_seq surrogates
used by test_ral_sweep.  Field semantics follow the v1.1.1 RTL; reset
values are the generated map's (cxp_protocol.regmap).

Fields & access:
  STANDARD, REVISION, XML_MFST_SIZE, XML_VERSION, XML_SCHEMA, XML_URL,
  DEV_LINK_ID, CTRL_PKT_DSIZE, LINK_CONFIG_DEF — RO.
  XML_MFST_SEL, MST_HOST_ID, STR_PKT_DSIZE, LINK_CONFIG, TEST_MODE,
  TEST_ERR_SEL — RW.
  LINK_RESET — RW1C (writes of 1 trigger a self-clearing pulse).

Note: the reg_scoreboard maintains its own write-observed mirror and is
the authoritative read-checker; this block is the declarative register
model (access policy + reset values) used by directed RAL sequences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from cxp_protocol import regmap as rm
from uvm.common.cxp_pkg import BootstrapAddr


@dataclass
class RegField:
    name: str
    addr: int
    access: str          # "RO" / "RW" / "RW1C"
    reset: int = 0
    mirror: int = 0


def _make_field(name: str, addr: int, access: str, reset: int = 0) -> RegField:
    return RegField(name=name, addr=addr, access=access, reset=reset, mirror=reset)


class CxpRegBlock:
    """Software mirror + access-policy enforcement."""

    def __init__(self):
        self.fields: Dict[int, RegField] = {
            BootstrapAddr.STANDARD:       _make_field("STANDARD",       BootstrapAddr.STANDARD,       "RO", rm.STANDARD_VALUE),
            BootstrapAddr.REVISION:       _make_field("REVISION",       BootstrapAddr.REVISION,       "RO", rm.REVISION_VALUE),
            BootstrapAddr.XML_MFST_SIZE:  _make_field("XML_MFST_SIZE",  BootstrapAddr.XML_MFST_SIZE,  "RO", rm.XML_MANIFEST_SIZE_VALUE),
            BootstrapAddr.XML_MFST_SEL:   _make_field("XML_MFST_SEL",   BootstrapAddr.XML_MFST_SEL,   "RW"),
            BootstrapAddr.XML_VERSION:    _make_field("XML_VERSION",    BootstrapAddr.XML_VERSION,    "RO", rm.XML_VERSION_VALUE),
            BootstrapAddr.XML_SCHEMA:     _make_field("XML_SCHEMA",     BootstrapAddr.XML_SCHEMA,     "RO", rm.XML_SCHEMA_VERSION_VALUE),
            BootstrapAddr.XML_URL:        _make_field("XML_URL",        BootstrapAddr.XML_URL,        "RO", rm.XML_URL_ADDRESS_VALUE),
            BootstrapAddr.LINK_RESET:     _make_field("LINK_RESET",     BootstrapAddr.LINK_RESET,     "RW1C"),
            BootstrapAddr.DEV_LINK_ID:    _make_field("DEV_LINK_ID",    BootstrapAddr.DEV_LINK_ID,    "RO"),
            BootstrapAddr.MST_HOST_ID:    _make_field("MST_HOST_ID",    BootstrapAddr.MST_HOST_ID,    "RW"),
            BootstrapAddr.CTRL_PKT_DSIZE: _make_field("CTRL_PKT_DSIZE", BootstrapAddr.CTRL_PKT_DSIZE, "RO", rm.CONTROL_PACKET_SIZE_MAX_VALUE),
            BootstrapAddr.STR_PKT_DSIZE:  _make_field("STR_PKT_DSIZE",  BootstrapAddr.STR_PKT_DSIZE,  "RW", rm.STREAM_PACKET_SIZE_MAX_RESET),
            BootstrapAddr.LINK_CONFIG:    _make_field("LINK_CONFIG",    BootstrapAddr.LINK_CONFIG,    "RW", rm.CONNECTION_CONFIG_RESET),
            BootstrapAddr.LINK_CONFIG_DEF:_make_field("LINK_CONFIG_DEF",BootstrapAddr.LINK_CONFIG_DEF,"RO", rm.CONNECTION_CONFIG_DEFAULT_VALUE),
            BootstrapAddr.TEST_MODE:      _make_field("TEST_MODE",      BootstrapAddr.TEST_MODE,      "RW"),
            BootstrapAddr.TEST_ERR_SEL:   _make_field("TEST_ERR_SEL",   BootstrapAddr.TEST_ERR_SEL,   "RW"),
        }

    def reset(self):
        for f in self.fields.values():
            f.mirror = f.reset

    def write_mirror(self, addr: int, value: int) -> bool:
        """Apply a host write to the mirror.  Returns True if the value
        landed (RW fields); False on RO writes (no mutation)."""
        f = self.fields.get(addr & 0xFFFF)
        if f is None or f.access == "RO":
            return False
        if f.access == "RW":
            f.mirror = value & 0xFFFF_FFFF
        elif f.access == "RW1C":
            if value & 1:
                f.mirror = 0   # self-clearing register
        return True

    def read_mirror(self, addr: int) -> int:
        f = self.fields.get(addr & 0xFFFF)
        if f is None:
            return 0
        return f.mirror & 0xFFFF_FFFF
