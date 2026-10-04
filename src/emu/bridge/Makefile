# =============================================================================
# src/emu/bridge — pure-SystemVerilog CoaXPress IP environment, FIFO-bridged
# to the C++ host in src/emu/host.
#
#   make build                 # compile DUT + DPI bridge (Verilator --binary)
#   make fifos                  # create the named pipes ($(FIFO_DIR))
#   make run                    # build + create FIFOs + run the camera RTL
#   make run OPT_LEVEL=0        # no-optimization build (fast compile/debug)
#   make run TRACE=1            # also dump waves to $(MDIR)/cxp_hw_env.vcd
#   make run REGLOG=1           # log every control cmd / APB reg access
#   make clean
#
# OPT_LEVEL (default 1) / TRACE / REGLOG apply to both backends and relink
# the Verilator binary when changed.  The knob is OPT_LEVEL, not OPT: a
# command-line `OPT=` is exported via MAKEFLAGS into Verilator's generated
# verilated.mk (which owns its own `OPT`) and would corrupt the C++ build.
#
# ModelSim / QuestaSim (native vsim -c, no cocotb — sidesteps the cocotb
# VPI crash that affects the src/tb_unit flow):
#
#   make run SIM=vsim              # full-optimization build (vopt, default)
#   make run SIM=vsim OPT_LEVEL=0  # no-optimization build  (vopt -O0 +acc)
#   make run SIM=vsim TRACE=1      # also dump build/cxp_hw_env.vcd
#
# Then, from another terminal, point the C++ host at the pipes:
#
#   src/emu/host/build/cxp --fifo-dir /tmp/cxp read 0x0000
#   src/emu/host/build/cxp --fifo-dir /tmp/cxp stream-capture --frames 4 --out caps
#
# Or, from the repo root, camera and host in one command: make emu-cli / emu-gui.
#
# Default simulator is Verilator (the rest of this repo's flow); any other
# DPI-capable simulator can reuse bridge/tb/cxp_hw_env.sv + bridge/dpi/cxp_fifo_dpi.c.
# =============================================================================

HW_DIR    := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
RTL_DIR   := $(abspath $(HW_DIR)/../../rtl)
MDIR      := $(HW_DIR)/build
BIN       := $(MDIR)/cxp_hw_env

FIFO_DIR  ?= /tmp/cxp
H2C       := $(FIFO_DIR)/cxp.h2c
C2H       := $(FIFO_DIR)/cxp.c2h

# -----------------------------------------------------------------------------
# Test-pattern generator setup.
#
#   WIDTH/HEIGHT (the TPG's maximum image) and PIXFMT/STREAMID (power-on
#   values of PixelFormat / Image1StreamID) are cxp_device_top
#   elaboration parameters — applied at *build* time as top-module
#   overrides (verilator -G / vopt -g).  PIXFMT is a GenICam PFNC value
#   (§11.2.1.6: 0x01080001 Mono8 .. 0x01100007 Mono16).  PIXFMT etc. accept
#   hex (0x01100005) or decimal.  Offsets, SourceTag, TapGeometry and StreamFlags are
#   registers.
#
#   RUN/USE_TPG/ARBITRARY are runtime +plusargs (no rebuild needed):
#       make run RUN=0                 # hold the TPG idle
#       make run WIDTH=128 HEIGHT=64 PIXFMT=0x01100005
#
#   Stream-packet size is host-controlled: the bootstrap
#   StreamPacketSizeMax (0x4010); nothing streams until the host writes it.
# -----------------------------------------------------------------------------
# The maximum image; the XML's Width / Height Max (4096) must not exceed it.
WIDTH     ?= 4096
HEIGHT    ?= 4096
PIXFMT    ?= 0x01080001
STREAMID  ?= 0x0001
RUN       ?= 1
USE_TPG   ?= 1
ARBITRARY ?= 0

# Build-affecting sim knobs (shared by both backends; changing any of
# them relinks the Verilator binary via the .vlcfg stamp below):
#   OPT_LEVEL=1  full optimization  (g++ -O3 / vopt)             [default]
#   OPT_LEVEL=0  no optimization    (g++ -O0 / vopt -O0 +acc)
#   TRACE=1      generate a VCD     (build/cxp_hw_env.vcd)
#   REGLOG=1     enable register-operation $display logs (cmd / APB)
#
# NOTE: the knob is OPT_LEVEL, *not* OPT — a command-line `OPT=` is
# exported through MAKEFLAGS into Verilator's generated verilated.mk,
# which already owns an `OPT` variable, and would corrupt the C++ build.
OPT_LEVEL ?= 1
TRACE     ?= 0
REGLOG    ?= 0

# Top-module parameter overrides, shared by both backends.  verilator -G
# / vopt -g want a plain Verilog/decimal literal, not C-style 0x...; the
# knobs accept either form and are normalised to decimal here.
dec = $(shell printf '%d' $(1))
TPG_PARAMS := \
    TPG_X_SIZE=$(call dec,$(WIDTH))      TPG_Y_SIZE=$(call dec,$(HEIGHT)) \
    TPG_PIXFMT=$(call dec,$(PIXFMT))     TPG_STREAMID=$(call dec,$(STREAMID))
VL_GFLAGS  := $(addprefix -G,$(TPG_PARAMS))
VOPT_GENS  := $(addprefix -g,$(TPG_PARAMS))
TPG_PLUSARGS := +run=$(RUN) +use_tpg=$(USE_TPG) +arbitrary=$(ARBITRARY)

# The register file's GenICam XML ROM loads the one image
# src/rtl/gen/cxp_camera_xml.mem by absolute path: cxp_hw_env forwards
# p_XML_BLOB_MEM to it, set here from src/regmap/regmap.mk.
include $(abspath $(HW_DIR)/../../regmap/regmap.mk)
VL_GFLAGS += $(CXP_XML_MEM_VL_G)
VOPT_GENS += $(CXP_XML_MEM_VSIM_G)

# The whole IP from the single file list (src/rtl/cxp_ip.f), plus this
# env's own SV top.
include $(RTL_DIR)/cxp_ip.mk
VERILOG_SOURCES := $(RTL_SOURCES) $(SVA_SOURCES) $(HW_DIR)/tb/cxp_hw_env.sv

# REGLOG=1 adds the bound register-access trace (no RTL define needed).
REGLOG_SV := $(abspath $(HW_DIR)/../../verif/common/cxp_reglog.sv)
ifeq ($(REGLOG),1)
  VERILOG_SOURCES += $(REGLOG_SV)
endif

DPI_SOURCES := $(HW_DIR)/dpi/cxp_fifo_dpi.c

# Pick the simulator backend: verilator (default) | vsim (ModelSim/QuestaSim).
SIM ?= verilator

.PHONY: build run fifos clean check_8b10b

# The DPI 8B/10B encoder (dpi/cxp_8b10b.h) against the golden vectors.
CXP_PROTOCOL_DIR := $(abspath $(HW_DIR)/../../model/cxp_protocol)
check_8b10b:
	@mkdir -p $(MDIR)
	$(CC) -O2 -Wall -Werror -o $(MDIR)/check_8b10b $(HW_DIR)/dpi/check_8b10b.c
	cd $(CXP_PROTOCOL_DIR) && python3 -m cxp_protocol.vectors --8b10b-text | $(MDIR)/check_8b10b

fifos:
	@mkdir -p $(FIFO_DIR)
	@[ -p $(H2C) ] || mkfifo $(H2C)
	@[ -p $(C2H) ] || mkfifo $(C2H)
	@echo "FIFOs ready: $(H2C)  $(C2H)"

# TPG params are baked into the binary at elaboration (verilator -G /
# vopt -g); force a rebuild when they change by keying the build on a
# content-stable stamp (rewritten only when the param string differs).
.PHONY: FORCE
$(MDIR)/.tpgparams: FORCE
	@mkdir -p $(MDIR)
	@echo '$(TPG_PARAMS) $(CXP_XML_MEM)' | cmp -s - $@ 2>/dev/null || \
	    echo '$(TPG_PARAMS) $(CXP_XML_MEM)' > $@

# -----------------------------------------------------------------------------
# Verilator backend (--binary, the rest of this repo's flow).
# -----------------------------------------------------------------------------
ifeq ($(SIM),verilator)

VERILATOR ?= verilator
VFLAGS := --binary --timing -j 0 --Mdir $(MDIR) -o cxp_hw_env \
          --top-module cxp_hw_env \
          -Wall -Wno-fatal -Werror-USERERROR --assert -Wno-UNUSEDSIGNAL -Wno-UNUSEDPARAM \
          -Wno-DECLFILENAME -Wno-WIDTHEXPAND -Wno-WIDTHTRUNC

# OPT_LEVEL: optimization of the generated C++ model.  =1 full (-O3,
# fastest sim); =0 none (-O0, fast compile / step-debuggable).  Driven
# through -CFLAGS so it sets the g++ optimization directly (Verilator's
# own -O<n> mis-builds with --binary --timing on 5.046).
ifeq ($(OPT_LEVEL),0)
  VFLAGS += -CFLAGS -O0
else
  VFLAGS += -CFLAGS -O3
endif

# TRACE: only pay the trace-codegen cost when a VCD is wanted.  The
# +define+CXP_TRACE compiles the env's $dumpfile/$dumpvars block; the
# run recipe arms it with +trace.
ifeq ($(TRACE),1)
  VFLAGS += --trace --trace-structs +define+CXP_TRACE
endif

# REGLOG: opt-in register-operation $display logs, bound into
# cxp_ctrl_cmd_parser / cxp_rx_ctrl_apb_master (REGLOG_SV, above).

# These knobs are baked into the binary; force a relink when they change.
VLCFG := OPT_LEVEL=$(OPT_LEVEL) TRACE=$(TRACE) REGLOG=$(REGLOG)
$(MDIR)/.vlcfg: FORCE
	@mkdir -p $(MDIR)
	@echo '$(VLCFG)' | cmp -s - $@ 2>/dev/null || echo '$(VLCFG)' > $@

build: $(BIN)

$(BIN): $(VERILOG_SOURCES) $(DPI_SOURCES) $(MDIR)/.tpgparams $(MDIR)/.vlcfg
	$(VERILATOR) $(VFLAGS) $(VL_GFLAGS) $(VERILOG_SOURCES) $(DPI_SOURCES)

run: build fifos
	@echo "[cxp-hw] camera RTL running (verilator, OPT_LEVEL=$(OPT_LEVEL) TRACE=$(TRACE) REGLOG=$(REGLOG)) — Ctrl-C to stop"
	cd $(HW_DIR) && $(BIN) +h2c=$(H2C) +c2h=$(C2H) \
	    $(TPG_PLUSARGS) $(if $(filter 1,$(TRACE)),+trace,)

clean:
	rm -rf $(MDIR)

# -----------------------------------------------------------------------------
# ModelSim / QuestaSim backend (native vsim -c batch; pure SV, no cocotb).
#
#   OPT_LEVEL=1 (default) : `vopt cxp_hw_env`          — full optimization
#   OPT_LEVEL=0           : `vopt -O0 +acc cxp_hw_env` — no optimization,
#                                                        full debug visibility
#
# The DPI C is compiled to a shared object and bound with `vsim -sv_lib`;
# the bridge functions keep C linkage so name-based resolution works.
# -----------------------------------------------------------------------------
else ifeq ($(SIM),vsim)

VLIB    ?= vlib
VMAP    ?= vmap
VLOG    ?= vlog
VOPT    ?= vopt
VSIM    ?= vsim
CC      ?= gcc

VWORK   := $(MDIR)/work
DPI_SO  := $(MDIR)/cxp_fifo_dpi.so
VLOG_FLAGS := -sv -work $(VWORK) -suppress 2583,2892 +acc

# REGLOG: opt-in register-operation $display logs (bound monitor, REGLOG_SV).

ifeq ($(OPT_LEVEL),1)
  VOPT_TOP   := cxp_hw_env_opt
  VOPT_FLAGS := -work $(VWORK)
else
  VOPT_TOP   := cxp_hw_env_noopt
  VOPT_FLAGS := -work $(VWORK) -O0 +acc
endif

# A VCD needs signal visibility *through* vopt; +acc on vlog alone is stripped
# by the optimizer. When TRACE=1, force +acc and key a distinct optimized top
# so the dump captures signals even under the default OPT_LEVEL=1 build.
ifeq ($(TRACE),1)
  VOPT_FLAGS += +acc
  VOPT_TOP   := $(VOPT_TOP)_acc
endif

build: $(MDIR)/.vopt.$(VOPT_TOP)

$(DPI_SO): $(DPI_SOURCES)
	@mkdir -p $(MDIR)
	$(CC) -fPIC -shared -O2 -o $@ $(DPI_SOURCES)

$(MDIR)/.vlog.stamp: $(VERILOG_SOURCES)
	@mkdir -p $(MDIR)
	[ -d $(VWORK) ] || $(VLIB) $(VWORK)
	$(VLOG) $(VLOG_FLAGS) $(VERILOG_SOURCES)
	@touch $@

$(MDIR)/.vopt.$(VOPT_TOP): $(MDIR)/.vlog.stamp $(MDIR)/.tpgparams
	$(VOPT) $(VOPT_FLAGS) $(VOPT_GENS) cxp_hw_env -o $(VOPT_TOP)
	@touch $@

run: build $(DPI_SO) fifos
	@echo "[cxp-hw] camera RTL running (vsim, OPT_LEVEL=$(OPT_LEVEL)) — Ctrl-C to stop"
	cd $(HW_DIR) && $(VSIM) -c -work $(VWORK) $(VOPT_TOP) \
	    -sv_lib $(MDIR)/cxp_fifo_dpi \
	    +h2c=$(H2C) +c2h=$(C2H) \
	    $(TPG_PLUSARGS) \
	    $(if $(filter 1,$(TRACE)),+trace,) \
	    -do "$(if $(filter 1,$(TRACE)),vcd file $(MDIR)/cxp_hw_env.vcd; vcd add -r /*;,) run -all; quit -f"

clean:
	rm -rf $(MDIR)
	rm -f transcript vsim.wlf modelsim.ini

else
$(error Unknown SIM='$(SIM)' — use 'verilator' or 'vsim')
endif
