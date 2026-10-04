# =============================================================================
# cxp_ip.mk — turns cxp_ip.f into RTL_SOURCES for Makefile consumers.
#
#   RTL_DIR := <path to rtl>        # set before including
#   include $(RTL_DIR)/cxp_ip.mk
#   VERILOG_SOURCES := $(RTL_SOURCES) $(SVA_SOURCES) <your top / TB files>
#
# Strips // comments and blank lines from the list and prefixes RTL_DIR.
# =============================================================================

CXP_IP_F    ?= $(RTL_DIR)/cxp_ip.f
RTL_SOURCES := $(addprefix $(RTL_DIR)/,$(shell sed -e 's|//.*||' $(CXP_IP_F)))

# Bound SVA checkers (src/sva/cxp_sva.f), for simulation and lint only.
SVA_DIR     ?= $(abspath $(RTL_DIR)/../sva)
SVA_SOURCES := $(addprefix $(SVA_DIR)/,$(shell sed -e 's|//.*||' $(SVA_DIR)/cxp_sva.f))

ifeq ($(strip $(RTL_SOURCES)),)
$(error cxp_ip.mk: no RTL sources read from '$(CXP_IP_F)' (is RTL_DIR set?))
endif
