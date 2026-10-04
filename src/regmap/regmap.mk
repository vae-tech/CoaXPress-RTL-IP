# =============================================================================
# regmap.mk — where the one XML ROM image is, for every simulation build.
#
# `make regmap` writes the GenICam XML ROM image to src/rtl/gen/cxp_camera_xml.mem.
# cxp_ctrl_bootstrap_regs loads it with $readmemh(p_XML_BLOB_MEM); every
# bench top forwards a p_XML_BLOB_MEM parameter to it (through
# cxp_device_top), and every build sets that top parameter to the absolute
# path below, so no build copies the image into its run directory.
#
#   include .../src/regmap/regmap.mk
#   CXP_XML_MEM          absolute path of the image
#   CXP_XML_MEM_VL_G     Verilator top-parameter flag  (-G)
#   CXP_XML_MEM_VSIM_G   Questa vsim / vopt flag       (-g)
#
# Only a top that declares p_XML_BLOB_MEM may get the flag: Verilator stops
# on a -G the top does not have.  cocotb benches set CXP_XML_ROM := 1
# before including src/verif/common/cocotb_sim.mk, which adds the flag for
# the selected simulator.
# =============================================================================

CXP_REGMAP_DIR     := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
CXP_XML_MEM        := $(abspath $(CXP_REGMAP_DIR)/../rtl/gen/cxp_camera_xml.mem)
CXP_XML_MEM_VL_G   := -Gp_XML_BLOB_MEM='"$(CXP_XML_MEM)"'
CXP_XML_MEM_VSIM_G := -gp_XML_BLOB_MEM='"$(CXP_XML_MEM)"'
