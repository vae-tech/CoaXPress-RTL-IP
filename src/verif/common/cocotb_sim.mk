# =============================================================================
# Shared cocotb / simulator selection fragment.
#
# Default simulator: Verilator.   `make`                runs on Verilator.
# Questa, batch:                  `make SIM=vsim`       runs on Questa headless.
# Questa, GUI:                    `make gui`            opens QuestaSim, loads
#                                                       waves, runs to end.
# Anything else (e.g. SIM=icarus) is forwarded to cocotb verbatim.
#
# Per-TB Makefiles set only TOPLEVEL and MODULE (and CXP_XML_ROM := 1 when
# the top loads the XML ROM), then `include` this file.
# It supplies the rest and pulls in the cocotb stock Makefile.sim:
#   RTL_DIR          rtl
#   TB_DIR           the including Makefile's directory ($(CURDIR))
#   COMMON_DIR       this directory
#   RTL_SOURCES      every file in src/rtl/cxp_ip.f (via src/rtl/cxp_ip.mk)
#   TB_SOURCES       every tb_*.sv in TB_DIR (the wrapper)
#   VERILOG_SOURCES  RTL_SOURCES + TB_SOURCES
#   PYTHONPATH       TB_DIR:COMMON_DIR prepended
# Any of the first five may be preset by the includer (src/verif/Makefile does).
# =============================================================================

# Immediate (:=) defaults: MAKEFILE_LIST must be read before any other
# include changes it, and a lazy ?= would be evaluated too late.
ifndef COMMON_DIR
COMMON_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
endif
ifndef TB_DIR
TB_DIR     := $(CURDIR)
endif
ifndef RTL_DIR
RTL_DIR    := $(abspath $(COMMON_DIR)/../../rtl)
endif

SIM            ?= verilator
TOPLEVEL_LANG  ?= verilog

# Friendly alias: many users type "vsim" expecting QuestaSim.
ifeq ($(SIM),vsim)
override SIM := questa
endif
ifeq ($(SIM),questasim)
override SIM := questa
endif
ifeq ($(SIM),modelsim)
override SIM := questa
endif

# ----------------------------------------------------------------------------
# Per-simulator argument tweaks
# ----------------------------------------------------------------------------
ifeq ($(SIM),verilator)
    # Wave tracing off by default: a trace costs run time and disk on every
    # test, and a regression tier wants neither — pass WAVES=1 to get a dump
    # when there is actually something to look at.
    # WAVES_FMT selects the dump format: vcd (default) or fst.
    WAVES     ?= 0
    WAVES_FMT ?= vcd
    ifeq ($(WAVES),1)
        EXTRA_ARGS += --trace --trace-structs
        ifeq ($(WAVES_FMT),fst)
            EXTRA_ARGS += --trace-fst
        else ifneq ($(WAVES_FMT),vcd)
            $(error WAVES_FMT must be 'vcd' or 'fst' (got '$(WAVES_FMT)'))
        endif
    endif
    # Lint is `make lint`'s job: warnings do not stop a bench build, but an
    # elaboration-time $error (parameter guard) does.
    EXTRA_ARGS += -Wall -Wno-fatal -Werror-USERERROR
    # Bound SVA contracts (sva) stop the simulation when they fail.
    EXTRA_ARGS += --assert
    EXTRA_ARGS += -Wno-UNUSEDSIGNAL -Wno-UNUSEDPARAM
    EXTRA_ARGS += -Wno-DECLFILENAME
    # Keep simulation timing accurate for `posedge / negedge ... or` resets.
    EXTRA_ARGS += --timing
    # Expose internal signals (FSM `state_q`, etc.) to the VPI so the Python
    # FSM-coverage collector (common/fsm_coverage.py) can sample them.
    # Questa exposes internals by default; only Verilator needs this.
    EXTRA_ARGS += --public-flat-rw

    # ---- Wave settings are baked into the build ---------------------------
    # Verilator compiles trace support into the model, but cocotb passes the
    # same --trace to that model at run time.  A sim_build left by a WAVES=0
    # run therefore kills the next WAVES=1 run outright —
    #
    #   Error: --trace requires the design to be built with trace support
    #
    # — and make sees an up-to-date sim_build, so it never rebuilds itself
    # out of the hole; every test of a tier fails the same way before the
    # simulation starts.  The reverse (WAVES=0 against a traced build) just
    # runs slower.  Nothing in the build tree records which it was, so stamp
    # it: a missing stamp for this WAVES/WAVES_FMT means the tree was built
    # with different settings and has to go.  Done while the makefile is
    # read, before any rule runs, so the wipe cannot race the build.
    SIM_BUILD   ?= sim_build
    WAVES_STAMP := $(SIM_BUILD)/.waves-$(WAVES)-$(WAVES_FMT)
    ifeq ($(filter clean,$(MAKECMDGOALS)),)
        ifeq ($(wildcard $(WAVES_STAMP)),)
            ifneq ($(wildcard $(SIM_BUILD)),)
                $(info cocotb_sim.mk: $(SIM_BUILD) was built with other wave \
settings — rebuilding it for WAVES=$(WAVES) WAVES_FMT=$(WAVES_FMT))
                $(shell rm -rf $(SIM_BUILD))
            endif
            $(shell mkdir -p $(SIM_BUILD) && touch $(WAVES_STAMP))
        endif
    endif
endif

ifeq ($(SIM),questa)
    # Compile each file as SystemVerilog 2012 and disable unused-signal warnings
    # that the RTL deliberately silences with /*verilator lint_off*/ pragmas.
    COMPILE_ARGS += -sv -mfcu -suppress 2275 -suppress 2583
    # Questa only logs a failing $error assertion; the bound SVA contracts
    # (sva) gate a run, so every property failure is made $fatal.
    COMPILE_ARGS += +define+CXP_SVA_FATAL
    SIM_ARGS     += -voptargs="+acc"

    # GUI=1 launches QuestaSim interactively and auto-loads waves; default is
    # headless (cocotb's runsim.do appends `run -all; quit` when GUI=0).
    GUI ?= 0
    ifeq ($(GUI),1)
        # Tell cocotb to log every signal so the wave window is populated.
        WAVES ?= 1
        # Per-TB wave.do (next to the TB Makefile) wins over the shared one.
        WAVE_DO := $(firstword $(wildcard $(TB_DIR)/wave.do) \
                              $(wildcard $(COMMON_DIR)/wave.do))
        ifneq ($(WAVE_DO),)
            # Appended to the TCL `vsim` line in cocotb's autogenerated
            # runsim.do, so the do-file is sourced after the design loads.
            # Note: SIM_ARGS is fed into a `@echo "..."` recipe so we cannot
            # rely on inner double-quotes surviving — pass a bare filename;
            # vsim's -do treats an existing path as a script to source.
            SIM_ARGS += -do $(WAVE_DO)
        endif
    endif
endif

# ----------------------------------------------------------------------------
# XML ROM: a bench whose top forwards p_XML_BLOB_MEM to the register file
# sets CXP_XML_ROM := 1 before including this file; the top then loads the
# one image src/rtl/gen/cxp_camera_xml.mem by absolute path (src/regmap/regmap.mk).
# ----------------------------------------------------------------------------
include $(abspath $(COMMON_DIR)/../../regmap/regmap.mk)
ifeq ($(CXP_XML_ROM),1)
    ifeq ($(SIM),verilator)
        EXTRA_ARGS += $(CXP_XML_MEM_VL_G)
    else ifeq ($(SIM),questa)
        SIM_ARGS   += $(CXP_XML_MEM_VSIM_G)
    endif
endif

# ----------------------------------------------------------------------------
# Sources: the whole IP from the single file list (packages first, in
# dependency order), then the TB wrapper.  Unused modules cost nothing —
# only the TOPLEVEL hierarchy is elaborated.
# ----------------------------------------------------------------------------
include $(RTL_DIR)/cxp_ip.mk
TB_SOURCES      ?= $(wildcard $(TB_DIR)/tb_*.sv)
VERILOG_SOURCES := $(RTL_SOURCES) $(SVA_SOURCES) $(TB_SOURCES)

# cxp_protocol (src/model/cxp_protocol) is the golden protocol model every TB imports.
CXP_PROTOCOL_DIR ?= $(abspath $(COMMON_DIR)/../../model/cxp_protocol)
export PYTHONPATH := $(TB_DIR):$(COMMON_DIR):$(CXP_PROTOCOL_DIR):$(PYTHONPATH)

# ----------------------------------------------------------------------------
# Forward to cocotb's stock simulator Makefile (must come before any local
# targets so its default goal `sim` stays the default).
# ----------------------------------------------------------------------------
include $(shell cocotb-config --makefiles)/Makefile.sim

# ----------------------------------------------------------------------------
# Convenience targets defined AFTER the include so they don't displace the
# default `sim` goal coming from cocotb's stock Makefile.
# ----------------------------------------------------------------------------
.PHONY: gui
gui:
	$(MAKE) SIM=vsim GUI=1
