# =============================================================================
# Makefile for the cxp_tx_ctrl_ack testbench.
#
#   make                    # default: Verilator
#   make SIM=vsim           # QuestaSim, headless batch (alias of SIM=questa)
#   make gui                # QuestaSim, GUI mode with waves auto-loaded
#   make clean
#
# Uses a thin wrapper to fix BUF_DEPTH = 16 (smaller than the RTL default of
# 64 — enough for every test case in this TB and keeps the wave addr signal
# narrow) and expose a TESTCASE label for waveforms.
#
# RTL comes from src/rtl/cxp_ip.f and the tb_*.sv wrapper in this directory
# is added automatically; see src/verif/common/cocotb_sim.mk.
# =============================================================================

TOPLEVEL := tb_cxp_tx_ctrl_ack_top
MODULE   := test_cxp_tx_ctrl_ack

include $(dir $(lastword $(MAKEFILE_LIST)))../../../verif/common/cocotb_sim.mk
