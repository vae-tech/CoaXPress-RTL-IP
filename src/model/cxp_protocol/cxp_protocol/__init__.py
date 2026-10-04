"""Golden CoaXPress 1.1.1 (CXP-001-2015) protocol model.

Written against the specification and tested against its vectors, never
against the RTL.  Deliberate RTL deviations are named in
:mod:`cxp_protocol.quirks`; ``quirks.DEVICE`` is what rtl does today.
"""

from . import crc, enc8b10b, kcodes, packets, quirks, stream  # noqa: F401
from .quirks import DEVICE, SPEC, Quirks  # noqa: F401
