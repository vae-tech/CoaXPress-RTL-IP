"""Module-level handle to the cocotb DUT.

pyuvm's ConfigDB has hierarchy-resolution semantics that bit us at
build_phase time (uvm_test_top not in DB before the test instance
exists).  Storing the dut in a module-level slot is simpler and
reliable: run_test.py calls set_dut(dut) at cocotb entry, every
component pulls it via get_dut().
"""

from __future__ import annotations

_DUT = None


def set_dut(dut):
    global _DUT
    _DUT = dut


def get_dut():
    if _DUT is None:
        raise RuntimeError(
            "get_dut() called before set_dut() — uvm.tests.run_test must "
            "stash the cocotb dut handle at the very top of the test."
        )
    return _DUT
