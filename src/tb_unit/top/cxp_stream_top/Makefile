# =============================================================================
# Makefile for the cxp_stream_top integration testbench.
#
#   make                    # default: Verilator
#   make SIM=vsim           # QuestaSim, headless batch (alias of SIM=questa)
#   make gui                # QuestaSim, GUI mode with waves auto-loaded
#   make clean
#
# A small wrapper (`tb_cxp_stream_top`) pins the TPG frame size to 8x4 so
# the integration test finishes in a few thousand cycles.
#
# RTL comes from src/rtl/cxp_ip.f and the tb_*.sv wrapper in this directory
# is added automatically; see src/verif/common/cocotb_sim.mk.
# =============================================================================

TOPLEVEL := tb_cxp_stream_top
MODULE   := test_cxp_stream_top

include $(dir $(lastword $(MAKEFILE_LIST)))../../../verif/common/cocotb_sim.mk
