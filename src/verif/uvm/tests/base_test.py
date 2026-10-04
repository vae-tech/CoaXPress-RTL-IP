"""cxp_top_test base class.

Every concrete test class (test_*) extends this; the env is built once,
clocks are kicked off, async reset is fired, then the test's
``run_phase`` starts the virtual sequence.  At the end, ``check_phase``
asserts every scoreboard is clean.
"""

from __future__ import annotations

import os
import re

import cocotb
from cocotb.triggers import Timer
from pyuvm import ConfigDB, uvm_test

from uvm.common.clocks   import start_clocks, reset
from uvm.common.cxp_pkg  import (
    CFG_DSIZE_P_KEY, CFG_HOST_JITTER_UI_KEY, CFG_HOST_PHASE_PS_KEY,
    CFG_HOST_PPM_KEY, DEFAULT_PKT_DSIZE_P,
)
from uvm.common.defaults import apply_defaults
from uvm.common.handles  import get_dut
from uvm.common.seed     import SEED
from uvm.tests.env       import CxpEnv

from cxp_protocol import packets as gp
from cxp_protocol import regmap


_PLAN_ID = re.compile(r"^CXP-CAM-[A-Z]+-\d{3}$")


def result_property(name: str, value) -> None:
    """Add a ``<property>`` to this run's results.xml.

    One UVM test runs per simulation, so the testsuite cocotb writes holds
    exactly this test and its properties are the test's.  cocotb 2.0 has
    no public call for it; the reporter it writes ``random_seed`` with is
    the one used here.  Outside a cocotb run this is a no-op.
    """
    xunit = getattr(getattr(cocotb, "_regression_manager", None), "xunit", None)
    if xunit is not None:
        xunit.add_property(name=name, value=str(value))


class CxpTopTest(uvm_test):
    """Common shell — pulls the env up, drives reset, leaves run_phase
    open for concrete tests to start their virtual sequence."""

    # Subclasses override these.
    APP_PERIOD = 10
    TX_PERIOD  = 10
    RX_PERIOD  = 10
    RESET_CYCLES = 8

    # Stream-packet payload size in 32-bit words.  Tests override the
    # class attribute to retune wire packetisation; the value is
    # published to ConfigDB in build_phase and consumed by the cfg_agent
    # driver and the stream scoreboard's adaptive packet check.
    # The Makefile's `psz=` knob exports CXP_PKT_DSIZE_P, which (when
    # set) wins over the class attribute so a single run can be retuned
    # without editing test code.
    PKT_DSIZE_P = DEFAULT_PKT_DSIZE_P

    # Host clock offsets, pushed into the uplink driver after env build.
    # The host has a bit clock of its own, so these are real offsets from
    # the device's nominal rate, not a retune that moves both ends.
    # HOST_PHASE_PS = None draws a random phase.
    # How long run_phase waits for the device to go quiet before giving
    # up and letting the scoreboards report what was still owed.
    QUIESCE_TIMEOUT_NS = 400_000

    HOST_PPM = 0.0
    HOST_PHASE_PS: float | None = None
    HOST_JITTER_UI = 0.0

    # What the host does after reset, before main_seq (§10.1.5, Table 44):
    # wait for the link, then write StreamPacketSizeMax for PKT_DSIZE_P
    # payload words.  The device powers up with StreamPacketSizeMax = 0
    # and sends no stream packet until it is written (§10.3.28).  A test
    # of the power-up state itself sets BRINGUP = False.
    BRINGUP = True

    # Build knobs the test needs (src/verif/Makefile KNOBS_<test> passes them):
    # {"OS_RATIO": 4}.  A run on another build fails at once rather than
    # testing something else.
    REQUIRES: dict = {}

    # Scoped tolerance: ``{(scoreboard, error kind): "the finding it waits
    # for"}``.  Only that scoreboard's errors of that kind are tolerated —
    # everything else in the test still gates.  A tagged kind that does not
    # fire fails the test too, so a tag cannot outlive its finding.
    #
    # Scoreboard names come from CxpEnv.scoreboards(); error kinds are the
    # first argument of the scoreboard's self.err() call.
    EXPECT_FAIL: dict[tuple[str, str], str] = {}

    # Validation-plan traceability (docs/verification/validation/cxp_camera_validation_
    # plan.md).  PLAN names every plan test case this class serves.
    # PLAN_PARTIAL maps the ones it runs only part of the procedure for to
    # what is missing; a row served only partially never reports PASS.
    # Both land in results.xml, where gen_uvm_test_report.py reads them —
    # the plan mapping lives here, not in prose.
    PLAN: tuple[str, ...] = ()
    PLAN_PARTIAL: dict[str, str] = {}

    def _record_plan(self):
        bad = [p for p in self.PLAN if not _PLAN_ID.match(p)]
        bad += [p for p in self.PLAN_PARTIAL if p not in self.PLAN]
        if bad:
            raise ValueError(f"{type(self).__name__}: bad PLAN entries {bad}")
        result_property("uvm_test", type(self).__name__)
        result_property("cxp_seed", SEED)
        result_property("plan", ",".join(self.PLAN))
        result_property("plan_partial", ",".join(self.PLAN_PARTIAL))
        result_property("expect_fail",
                        ",".join(f"{s}/{k}" for s, k in self.EXPECT_FAIL))

    def build_phase(self):
        # First thing: a test that dies later still says what it served.
        self._record_plan()
        from uvm.common import build as _b
        wrong = {k: (v, getattr(_b, k)) for k, v in self.REQUIRES.items()
                 if getattr(_b, k) != v}
        if wrong:
            raise ValueError(f"{type(self).__name__} needs the build "
                             + " ".join(f"{k}={v}" for k, (v, _) in wrong.items())
                             + " (src/verif/Makefile KNOBS_<test>); this one has "
                             + " ".join(f"{k}={b}" for k, (_, b) in wrong.items()))
        # Publish the packet size before env build so child components
        # (cfg_agent, stream_scoreboard) can read it in their own
        # build_phase.  "*" matches any inst path under uvm_test_top.
        env_override = os.environ.get("CXP_PKT_DSIZE_P")
        pkt_dsize = int(env_override) if env_override else int(self.PKT_DSIZE_P)
        ConfigDB().set(None, "*", CFG_DSIZE_P_KEY, pkt_dsize)

        # The env's children are built after this phase, so the host
        # clock offsets go through the ConfigDB the driver reads in its
        # own build_phase.
        ConfigDB().set(None, "*", CFG_HOST_PPM_KEY,       float(self.HOST_PPM))
        ConfigDB().set(None, "*", CFG_HOST_PHASE_PS_KEY,  self.HOST_PHASE_PS)
        ConfigDB().set(None, "*", CFG_HOST_JITTER_UI_KEY, float(self.HOST_JITTER_UI))

        self.env = CxpEnv("env", self)
        self.pkt_dsize = pkt_dsize
        # Drive cleansed defaults so cfg_* don't float at simulation start.
        apply_defaults(get_dut(), pkt_dsize=pkt_dsize)
        self.logger.warning(f"CXP_SEED={SEED}")

    async def run_phase(self):
        self.raise_objection()
        dut = get_dut()
        start_clocks(dut, self.APP_PERIOD, self.TX_PERIOD, self.RX_PERIOD)
        await reset(dut, cycles=self.RESET_CYCLES)
        if self.BRINGUP:
            await self.bringup()
        await self.main_seq()
        # End when the device has nothing left to say, not when a timer
        # expires: with a free-running source the two are not the same.
        await self.env.quiesce(timeout_ns=self.QUIESCE_TIMEOUT_NS)
        self.drop_objection()

    @staticmethod
    def spsm_for(dsize_words: int) -> int:
        """StreamPacketSizeMax (bytes, the whole packet) for a payload of
        `dsize_words` words: Table 19 adds 8 framing words."""
        return 4 * (int(dsize_words) + 8)

    async def bringup(self):
        """Link up, then StreamPacketSizeMax for PKT_DSIZE_P."""
        await self.env.host.link_up()
        code = await self.env.host.write(regmap.STREAM_PACKET_SIZE_MAX,
                                         [self.spsm_for(self.pkt_dsize)])
        if code != gp.ACK_OK_WRITE:
            raise AssertionError(f"bring-up: StreamPacketSizeMax write "
                                 f"acknowledged {code!r}")

    async def main_seq(self):
        """Override me in concrete tests."""
        await Timer(100, unit="ns")

    def check_phase(self):
        # Collect the per-kind counts first (this is what finalises every
        # scoreboard) BEFORE raising, so the report_phase that follows
        # still prints diagnostics rather than being swallowed under the
        # AssertionError.
        self._err_kinds = self.env.error_kinds()

    def final_phase(self):
        counts = getattr(self, "_err_kinds", {})
        fired = {k for k, n in counts.items() if n}
        tagged = set(self.EXPECT_FAIL)

        untagged = sorted(fired - tagged)
        silent   = sorted(tagged - fired)
        result_property("sb_errors", ",".join(
            f"{s}/{k}={counts[(s, k)]}" for s, k in sorted(fired)))

        for k in sorted(fired & tagged):
            self.logger.warning(
                f"expect_fail (tolerated): {k[0]}/{k[1]} ×{counts[k]} — "
                f"{self.EXPECT_FAIL[k]}"
            )

        problems = []
        if untagged:
            problems.append(
                "untagged scoreboard errors: "
                + ", ".join(f"{s}/{k}×{counts[(s, k)]}" for s, k in untagged)
            )
        if silent:
            problems.append(
                "tagged but never fired (remove the tag or fix the test): "
                + ", ".join(f"{s}/{k}" for s, k in silent)
            )
        if problems:
            for p in problems:
                self.logger.error(p)
            raise AssertionError("; ".join(problems))
