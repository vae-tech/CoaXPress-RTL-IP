# =============================================================================
# Makefile for the cxp_tx_arbiter testbench.
#
#   make                    # default: Verilator
#   make SIM=vsim           # QuestaSim, headless batch (alias of SIM=questa)
#   make gui                # QuestaSim, GUI mode with waves auto-loaded
#   make clean
#
# The DUT has all-scalar ports already, so no top-level wrapper is needed —
# `cxp_tx_arbiter` is the TOPLEVEL directly.
#
# RTL comes from src/rtl/cxp_ip.f and the tb_*.sv wrapper in this directory
# is added automatically; see src/verif/common/cocotb_sim.mk.
# =============================================================================

TOPLEVEL := tb_cxp_tx_arbiter_top
MODULE   := test_cxp_tx_arbiter

include $(dir $(lastword $(MAKEFILE_LIST)))../../../verif/common/cocotb_sim.mk
