"""Configure the internal test-pattern generator through its registers.

The `cfg_*` pins of the shell are a bench shortcut; a Host has only the
register map.  `TpgCfgSeq` writes Width / Height / PixelFormat /
TestPattern at their feature addresses — the addresses the §10.3.19-27
slots at 0x3000 point to — as control writes on the uplink, the way a
Host that had read those slots would.  Addresses come from
`cxp_protocol.regmap` (generated from `src/regmap/cxp_regmap.yaml`), never
from a copy.

Only the fields that are not ``None`` are written.  Choosing the TPG as
the pixel source (`cfg_use_tpg`) and running it (`cfg_run`) stay on
`CfgToggleSeq`: the shell has no register for either.

Open-loop until the reactive host exists: each write is followed by
`gap_words` IDLE words so the acknowledgment of one command has drained
before the next arrives.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import List, Optional, Tuple

from pyuvm import uvm_sequence

from cxp_protocol import regmap

from uvm.agents.host_uplink_agent import UplinkIdleSeq, UplinkRegSeq


@dataclass
class TpgCfg:
    """What to program; ``None`` leaves a register as it is."""
    width:   Optional[int] = None     # pixels per line
    height:  Optional[int] = None     # lines per frame
    pixfmt:  Optional[int] = None     # PFNC value (§11.2.1.6)
    pattern: Optional[int] = None     # 0 gradient, 1 bars, 2 flat, 3 grey bars

    # field -> feature address
    ADDR = {
        "width":   regmap.WIDTH_ALIAS,
        "height":  regmap.HEIGHT_ALIAS,
        "pixfmt":  regmap.PIXEL_FORMAT_ALIAS,
        "pattern": regmap.TEST_PATTERN,
    }

    def writes(self) -> List[Tuple[int, int]]:
        """(address, value) for every field that is set, in field order."""
        return [(self.ADDR[f.name], int(getattr(self, f.name)) & 0xFFFF_FFFF)
                for f in fields(self) if getattr(self, f.name) is not None]


class TpgCfgSeq(uvm_sequence):
    """Write a `TpgCfg` on the uplink packet lane (`uplink_ag.seqr`)."""

    def __init__(self, name="tpg_cfg_seq", cfg: Optional[TpgCfg] = None,
                 gap_words: int = 16, **kw):
        super().__init__(name)
        self.cfg = cfg if cfg is not None else TpgCfg(**kw)
        self.gap_words = gap_words

    async def body(self):
        for addr, value in self.cfg.writes():
            await UplinkRegSeq(addr=addr, data=value, write=True).start(self.sequencer)
            await UplinkIdleSeq(n=self.gap_words).start(self.sequencer)
