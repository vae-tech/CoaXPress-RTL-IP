# =============================================================================
# Makefile for the cxp_interface_top integration testbench.
#
#   make                    # Verilator (default)
#   make SIM=vsim           # QuestaSim headless
#   make gui                # QuestaSim GUI
#   make clean
#
# A small wrapper (`tb_cxp_interface_top`) pins TPG geometry to 8x4 and
# ties all clock domains together, matching the single-PLL MVP build the
# RTL documents.  The bootstrap register file is wired to the device's
# APB master out via an inline APB3-slave bridge in the wrapper.
#
# RTL comes from src/rtl/cxp_ip.f and the tb_*.sv wrapper in this directory
# is added automatically; see src/verif/common/cocotb_sim.mk.
# =============================================================================

TOPLEVEL := tb_cxp_interface_top
MODULE   := test_cxp_interface_top

# The bench's register file loads the XML ROM image by absolute path
# (src/regmap/regmap.mk).
CXP_XML_ROM := 1

include $(dir $(lastword $(MAKEFILE_LIST)))../../../verif/common/cocotb_sim.mk
