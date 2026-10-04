"""Cocotb entry point that hands control to pyuvm.

`cocotb-config --makefiles` invokes us as MODULE=uvm.tests.run_test.
We honour the UVM_TESTNAME environment variable (also accepts the
+UVM_TESTNAME plusarg) and dispatch to the matching uvm_test subclass
from uvm.tests.all_tests.
"""

from __future__ import annotations

import os

import cocotb
from cocotb.triggers import Timer

from pyuvm import uvm_root

# Import for side-effect: registers every test_* class with the UVM factory.
from uvm.tests import all_tests  # noqa: F401
from uvm.tests import spec_tests  # noqa: F401
from uvm.tests import conc_tests  # noqa: F401

from uvm.common.handles import set_dut
from cxp_testcase import _publish as _testcase_publish  # for wave-labelling


def _resolve_test_name(dut) -> str:
    """Pick the test name from env var or +UVM_TESTNAME plusarg."""
    name = os.environ.get("UVM_TESTNAME", "").strip()
    if name:
        return name
    # cocotb 2.x: dut._sim accepts plusargs via the testlauncher; we keep
    # this simple and rely on the env var, which the Makefile exports.
    return "test_idle_baseline"


@cocotb.test()
async def main(dut):
    test_name = _resolve_test_name(dut)

    # Publish the test name in waves via the existing TESTCASE port.
    _testcase_publish(dut, 1, test_name)

    # Stash the DUT handle in a module-level slot so every component
    # pulls the same cocotb dut object regardless of UVM hierarchy.
    set_dut(dut)

    # Hand off to pyuvm — this awaits run_phase/check_phase/report_phase
    # via the standard uvm_root().run_test() coroutine.
    await uvm_root().run_test(test_name)

    # Small grace period before sim closes so any final analysis writes
    # have time to land.
    await Timer(200, unit="ns")
