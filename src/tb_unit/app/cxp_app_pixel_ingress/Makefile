# =============================================================================
# Makefile for the cxp_app_pixel_ingress testbench.
#
#   make                    # default: Verilator
#   make SIM=vsim           # QuestaSim, headless batch
#   make gui                # QuestaSim, GUI mode with waves auto-loaded
#   make clean
#
# RTL comes from src/rtl/cxp_ip.f and the tb_*.sv wrapper in this directory
# is added automatically; see src/verif/common/cocotb_sim.mk.
# =============================================================================

TOPLEVEL := tb_cxp_app_pixel_ingress_top
MODULE   := test_cxp_app_pixel_ingress

include $(dir $(lastword $(MAKEFILE_LIST)))../../../verif/common/cocotb_sim.mk
