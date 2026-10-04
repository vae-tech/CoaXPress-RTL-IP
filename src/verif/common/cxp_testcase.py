"""Drop-in replacement for ``@cocotb.test()`` that publishes the running
test-case index to a top-level ``TESTCASE`` signal on the DUT.

Why this exists
---------------
When debugging long regression waves, knowing *which* test produced a given
slice of activity saves time. Each ``tb_<module>_top.sv`` declares::

    int unsigned TESTCASE /*verilator public_flat_rw*/ = 0;

and every ``@cxp_test()``-decorated coroutine writes its sequential index
into that signal at entry. The number is then visible in any waveform.

The counter is module-scoped (resets to 1 on each ``make`` invocation, since
cocotb spawns a fresh Python process per TB run) and indexes tests in the
order cocotb dispatches them.

If a TB has no ``TESTCASE`` signal (e.g. an RTL-direct TOPLEVEL without a
wrapper), the write is silently skipped.

FSM coverage
------------
``@cxp_test()`` also drives :mod:`fsm_coverage`: at the head of each test it
starts a sampler for every FSM the testbench registered (via
``fsm_coverage.register_fsm``), and at the end it stops the samplers and
flushes ``fsm_coverage.json``. TBs that register no FSM are unaffected.

Teardown checks
---------------
A test (usually its setup helper) can register end-of-test assertions with
:func:`at_teardown`; they run after the body, in registration order, and the
registry is emptied for the next test either way. They run only when the body
passed — a teardown check must never hide the failure that produced it.
"""

from __future__ import annotations

import json
from functools import wraps

import cocotb

try:  # coverage is best-effort — never let it break a testbench
    import fsm_coverage
except Exception as _exc:  # pragma: no cover  (common/ always on PYTHONPATH)
    fsm_coverage = None
    cocotb.log.warning("cxp_test: fsm_coverage unavailable (%s)", _exc)


_counter = 0
_teardown: list = []


def at_teardown(check) -> None:
    """Register a zero-argument callable to run after the current test body.

    The callable asserts; anything it raises fails the test.  Registrations
    are dropped at the end of every test, so a setup helper re-registers per
    test without accumulating.
    """
    _teardown.append(check)


def _run_teardown() -> None:
    for check in list(_teardown):
        check()


def _publish(dut, idx: int, name: str) -> None:
    handle = getattr(dut, "TESTCASE", None)
    if handle is not None:
        try:
            handle.value = idx
        except Exception as exc:  # pragma: no cover  (sim-back-end specific)
            cocotb.log.warning("cxp_test: could not write TESTCASE=%d (%s)", idx, exc)
    cocotb.log.info("[TESTCASE %d] %s", idx, name)


_findings: dict[str, str] = {}


def _write_findings() -> None:
    """Record every tagged test of this bench in ``expected_fail.json``.

    The gate (``tools/check_results.py``) lists them on every run, so a
    red test that is tolerated is never silent."""
    try:
        with open("expected_fail.json", "w") as f:
            json.dump(_findings, f, indent=1, sort_keys=True)
    except OSError as exc:  # pragma: no cover
        cocotb.log.warning("cxp_test: could not write expected_fail.json (%s)", exc)


def cxp_test(*dargs, finding: str | None = None, **dkwargs):
    """Drop-in replacement for :func:`cocotb.test`.

    Usage mirrors cocotb exactly::

        @cxp_test()
        async def test_foo(dut): ...

        @cxp_test(skip=True)
        async def test_bar(dut): ...

    ``finding="<ID>: <one line>"`` marks a test that is red today for an
    open RTL finding: cocotb's ``expect_fail`` inverts its verdict, so it
    passes while the check fails and fails once the RTL is fixed — the tag
    then has to come off.  The ID names the finding (``N-nn`` or a work
    package), never an environment gap.
    """
    # Bare @cxp_test (no parens): dargs == (coro,)
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs and finding is None:
        return cxp_test()(dargs[0])
    if finding is not None:
        dkwargs["expect_fail"] = True

    def decorator(coro):
        @wraps(coro)
        async def wrapper(*args, **kwargs):
            global _counter
            _counter += 1
            dut = args[0] if args else kwargs.get("dut")
            _publish(dut, _counter, coro.__name__)
            if finding is not None:
                _findings[coro.__name__] = finding
                cocotb.log.info("[EXPECTED FAIL] %s — %s", coro.__name__, finding)
            _write_findings()
            cov_tasks = fsm_coverage.start_all(dut) if fsm_coverage else []
            _teardown.clear()
            try:
                result = await coro(*args, **kwargs)
                # Only on a passing body: a teardown check must not mask
                # the failure that caused it.
                _run_teardown()
                return result
            finally:
                _teardown.clear()
                if fsm_coverage:
                    fsm_coverage.stop_all(cov_tasks)
                    fsm_coverage.flush()

        return cocotb.test(*dargs, **dkwargs)(wrapper)

    return decorator
