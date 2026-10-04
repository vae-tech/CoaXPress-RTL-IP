"""Common scoreboard base — per-kind error accounting.

Every scoreboard reports through :meth:`CxpScoreboard.err`, which takes a
short *kind* alongside the message and counts the two separately.  The
kind is what a test's ``EXPECT_FAIL`` tag names, so a tag can tolerate
"this scoreboard loses a frame" without also tolerating a wrong
acknowledgment code in the same run.

Kinds are short, stable, lower-case identifiers (``lost_frame``,
``ack_code``, ``tag_continuity``).  They appear in the log line, in the
report, and in the tag, so renaming one breaks the tags that name it —
which is the point.

``_final_check`` is where a scoreboard turns "nothing arrived" into an
error.  It runs exactly once, from :meth:`finalize`, whether the caller
came in through ``finalize()`` or the legacy ``ok`` property.
"""

from __future__ import annotations

from typing import Dict

from pyuvm import uvm_scoreboard


class CxpScoreboard(uvm_scoreboard):
    """A scoreboard that counts its errors per kind."""

    def build_phase(self):
        self._err_counts: Dict[str, int] = {}
        self._err_first: Dict[str, str] = {}
        self._finalized = False

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def err(self, kind: str, msg: str) -> None:
        """Record one error of `kind` and log it."""
        self._err_counts[kind] = self._err_counts.get(kind, 0) + 1
        self._err_first.setdefault(kind, msg)
        self.logger.error(f"[{kind}] {msg}")

    @property
    def err_counts(self) -> Dict[str, int]:
        """kind -> number of errors recorded, in first-seen order."""
        return dict(self._err_counts)

    @property
    def errors(self) -> int:
        return sum(self._err_counts.values())

    def err_summary(self) -> str:
        if not self._err_counts:
            return "clean"
        return " ".join(f"{k}={n}" for k, n in self._err_counts.items())

    def pending_count(self) -> int:
        """Expectations still outstanding.

        `CxpEnv.quiesce` waits for this to reach zero across every
        scoreboard, so a test ends when the device has answered rather
        than when a `Timer` says so.  Zero by default: a scoreboard that
        cannot tell does not hold the test up.
        """
        return 0

    # ------------------------------------------------------------------
    # End of test
    # ------------------------------------------------------------------
    def _final_check(self) -> None:
        """Override: turn missing / pending expectations into errors."""

    def finalize(self) -> Dict[str, int]:
        """Run `_final_check` once and return the per-kind counts."""
        if not self._finalized:
            self._finalized = True
            self._final_check()
        return self.err_counts

    @property
    def ok(self) -> bool:
        self.finalize()
        return self.errors == 0


class TestChecks(CxpScoreboard):
    """The checks a test makes itself, reported like any scoreboard's so a
    tag can scope them: `env.sb_test.check(cond, kind, msg)`."""

    def check(self, cond, kind: str, msg: str) -> bool:
        if not cond:
            self.err(kind, msg)
        return bool(cond)

    def report_phase(self):
        self.logger.info(f"test checks: {self.err_summary()}")
