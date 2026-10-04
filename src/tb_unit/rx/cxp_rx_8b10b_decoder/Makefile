# =============================================================================
# Makefile for the cxp_rx_8b10b_decoder testbench.
#
#   make                    # default: Verilator
#   make SIM=vsim           # QuestaSim, headless batch (alias of SIM=questa)
#   make gui                # QuestaSim, GUI mode with waves auto-loaded
#   make clean
#
# RTL comes from src/rtl/cxp_ip.f and the tb_*.sv wrapper in this directory
# is added automatically; see src/verif/common/cocotb_sim.mk.
# =============================================================================

TOPLEVEL := tb_cxp_rx_8b10b_decoder_top
MODULE   := test_cxp_rx_8b10b_decoder

include $(dir $(lastword $(MAKEFILE_LIST)))../../../verif/common/cocotb_sim.mk
