"""One seed for the whole environment (`CXP_SEED`).

Every random stream in `src/verif/uvm` comes from `rng(tag)`, so one number
reproduces a run: the fixed default for the nightly tier, a fresh one per
test in the weekly tier (src/verif/Makefile), printed at the start of every
test and written to results.xml (`cxp_seed`).
"""

from __future__ import annotations

import os
import random

DEFAULT_SEED = 1
SEED = int(os.environ.get("CXP_SEED", "") or DEFAULT_SEED)


def rng(tag: str, salt: object = None) -> random.Random:
    """An independent, reproducible stream for one actor."""
    return random.Random(f"{SEED}:{tag}:{salt}")
