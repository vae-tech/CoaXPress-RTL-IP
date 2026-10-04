"""Named deviations of the RTL device from CXP-001-2015.

The codecs in this package are written to the specification.  Where the
RTL does something else on the wire, the difference is a named flag here
so it stays explicit and countable.  ``DEVICE`` is the set the RTL in
rtl implements today; each RTL fix removes its flag (and, once no
consumer needs it, the code path behind it).

Testbenches, the PyUVM environment and the emulator talk to the RTL with
``DEVICE``.  Tests of this package itself use ``SPEC`` (no quirks).
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace


@dataclass(frozen=True)
class Quirks:
    # No deviation is left: the RTL device speaks CXP-001-2015 on the wire.
    # A new one is added here as a bool field (default False), set in
    # DEVICE, and removed with the RTL fix.

    def active(self):
        return [f.name for f in fields(self) if getattr(self, f.name)]

    def without(self, *names: str) -> "Quirks":
        return replace(self, **{n: False for n in names})


SPEC = Quirks()

DEVICE = Quirks()
