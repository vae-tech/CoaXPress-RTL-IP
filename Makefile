# =============================================================================
# Repo-root driver: lint, regressions, the CI gate, and the everyday runs.
#
#   make help          # the everyday targets and their options
#
#   make lint          # verilator --lint-only -Wall on the real tops + style guide
#   make style         # mechanical coding-style rules (tools/check_style.py)
#   make tb            # every unit TB in src/tb_unit, JOBS in parallel, gated
#   make uvm           # src/verif/ ci tier (PyUVM on tb_cxp_top: smoke + control
#                      #   read / write, ConnectionReset, TestMode), gated
#   make emu           # golden cxp_protocol pytest, validation catalogue, DPI vectors
#   make host          # build the C++ host (CLI, GUI, QtTest) and run its ctest suite
#   make emu-rtl       # emulator validation campaign on the RTL (DPI bridge),
#                      #   gated on tools/check_emu_results.py (~25 min)
#   make nightly       # ci, then the PyUVM nightly tier and emu-rtl: the job
#                      #   to run once a day (not part of ci: ~1.5 h)
#   make regmap        # regenerate the register map from src/regmap/cxp_regmap.yaml
#   make regmap-check  # fail if a generated register-map file is stale or an
#                      #   address is hand-written (tools/check_regmap_literals.py)
#   make genicam-check # load the GenICam XML in the EMVA reference GenApi
#   make ci            # regmap-check -> genicam-check -> lint -> style -> tb -> uvm -> emu -> host
#
# Everyday runs (options in `make help`):
#   make unit  [TB=<bench>] [TEST=<regex>] [WAVE=vcd|fsdb]     RTL unit benches
#   make pyuvm [TEST=<test> | TIER=<tier>] [SEED=n] [WAVE=...] PyUVM tests
#   make emu-build / emu-cli / emu-gui [DEV=rtl|vcam]          emulator
#
# RTL is listed once, in src/rtl/cxp_ip.f; lint waivers live in
# src/rtl/cxp_ip.vlt (reviewed — each names the finding it belongs to).
# =============================================================================

RTL_DIR   := src/rtl
VERILATOR ?= verilator
JOBS      ?= $(shell nproc 2>/dev/null || echo 4)

# cxp_device_top holds the whole IP; cxp_interface_top is linted on its
# own because the benches instantiate it without the register file.
LINT_TOPS  ?= cxp_device_top cxp_interface_top
LINT_FLAGS := --lint-only -Wall -Wpedantic \
              $(RTL_DIR)/cxp_ip.vlt -F $(RTL_DIR)/cxp_ip.f -F src/sva/cxp_sva.f

# -----------------------------------------------------------------------------
# Waves, for every simulation target below: WAVE=none (default), vcd or fsdb.
# Verilator traces VCD only (or FST); WAVE=fsdb traces VCD and converts each
# dump with Verdi's vcd2fsdb afterwards (tools/vcd_to_fsdb.sh; set VERDI_HOME
# or VCD2FSDB).  KEEP_VCD=0 deletes the VCD once its FSDB exists.
# -----------------------------------------------------------------------------
WAVE ?= none
ifeq ($(filter $(WAVE),none vcd fsdb),)
$(error WAVE must be none, vcd or fsdb (got '$(WAVE)'))
endif
WAVES_ON := $(if $(filter none,$(WAVE)),0,1)
# Run the conversion after a simulation, whatever its result; keep its rc.
to_fsdb = $(if $(filter fsdb,$(WAVE)),tools/vcd_to_fsdb.sh $(1),true)

.PHONY: all ci nightly lint lint_async lint_guide style tb uvm emu host emu-rtl regmap regmap-check genicam-check $(addprefix lint_,$(LINT_TOPS))

all: ci

ci: regmap-check genicam-check lint style tb uvm emu host

# The GenICam XML, the XML ROM image, the register-map package and the
# Python / C / Markdown views are generated from src/regmap/cxp_regmap.yaml.
regmap:
	python3 src/regmap/gen_regmap.py

# ... and no register-map address is written out by hand in src/emu/host,
# src/verif, src/tb_unit or src/emu/bridge.
regmap-check:
	python3 src/regmap/gen_regmap.py --check
	python3 tools/check_regmap_literals.py

# The XML in the EMVA GenICam reference implementation (pip package
# `genicam`; missing = failure), every feature read and its limits probed
# against the reference register model.
genicam-check:
	python3 tools/check_genicam_xml.py

lint: $(addprefix lint_,$(LINT_TOPS))

$(addprefix lint_,$(LINT_TOPS)): lint_%:
	$(VERILATOR) $(LINT_FLAGS) --top-module $*

# The same IP with every clock crossing synchronised.
lint: lint_async
lint_async:
	$(VERILATOR) $(LINT_FLAGS) --top-module cxp_device_top -Gp_ASYNC_CLOCKS=1

# The coding-style guide is a module too; it must stay lint-clean.
lint: lint_guide
lint_guide:
	$(VERILATOR) $(LINT_FLAGS) docs/style/rtl_coding_style.sv --top-module cxp_style_example

style:
	python3 tools/check_style.py
	python3 tools/check_style.py docs/style/rtl_coding_style.sv

# Benches parked on purpose: not run, printed by the gate on every run.
TB_PENDING ?=

# Keep going past failing TBs (-k) so one run reports all of them, then gate
# on the JUnit files the runs left behind (shell globs, not $(wildcard):
# make expands a whole recipe before running its first line).  Every bench
# with a Makefile must leave a results.xml: one that fails to build fails
# the gate.
# A COCOTB_TEST_FILTER left in the shell would narrow every bench
# silently, so the full regression runs without it.  Tests skipped on
# purpose are named in TB_ALLOW_SKIP; any other skip fails the gate.
TB_ALLOW_SKIP ?=

tb:
	rm -f src/tb_unit/*/cxp_*/results.xml src/tb_unit/*/cxp_*/fsm_coverage.json src/tb_unit/*/cxp_*/expected_fail.json
	-env -u COCOTB_TEST_FILTER $(MAKE) -C src/tb_unit -k -j$(JOBS) WAVES=$(WAVES_ON) WAVES_FMT=vcd TB_SKIP="$(TB_PENDING)" all
	-$(call to_fsdb,src/tb_unit)
	python3 tools/check_results.py --benches src/tb_unit --pending "$(TB_PENDING)" \
	    --allow-skip "$(TB_ALLOW_SKIP)" src/tb_unit/*/cxp_*/results.xml
	python3 tools/check_fsm_coverage.py --benches src/tb_unit src/tb_unit/*/cxp_*/fsm_coverage.json

# The tier itself ends in its own `gate` step (src/verif/Makefile), so a failed
# or never-started PyUVM test comes back as a non-zero exit from here.
uvm:
	$(MAKE) -C src/verif WAVES=0 ci

# The emulator's case campaign against the RTL.  A FAIL of a case listed
# in src/emu/host/validation/expected_fail.json (open RTL finding) is tolerated
# and listed; a NOT RUN plan case fails unless the script allow-lists it.
emu-rtl:
	tools/run_emu_rtl.sh

# The daily job: everything ci runs, then the long tiers.  Each step runs
# even when an earlier one failed; the exit code is non-zero if any did.
nightly:
	@rc=0; \
	 $(MAKE) ci || rc=1; \
	 $(MAKE) -C src/verif WAVES=0 nightly || rc=1; \
	 $(MAKE) emu-rtl || rc=1; \
	 exit $$rc

emu:
	cd src/model/cxp_protocol && python3 -m pytest -q
	cd src/emu/host && python3 validation/gen_validation_cases.py --check
	$(MAKE) -C src/emu/bridge check_8b10b

# The C++ host's ctest suite: protocol, transport, GenICam, end-to-end
# against the virtual camera, golden vectors, the validation framework and
# catalogue checks (and the GUI validation window with EMU_GUI=ON).
host: host-build
	ctest --test-dir $(EMU_HOST)/build --output-on-failure -j$(JOBS)

# =============================================================================
# Everyday runs
# =============================================================================
.PHONY: help unit unit-list pyuvm pyuvm-list host-build emu-build emu-cli emu-gui emu-run emu-clean

help:
	@echo 'RTL unit benches (src/tb_unit, cocotb + Verilator)'
	@echo '  make unit                          every bench, in parallel, gated (= make tb)'
	@echo '  make unit TB=tx_arbiter            one bench (names: make unit-list)'
	@echo '  make unit TB=tx_arbiter TEST=test_03   tests of that bench matching a regex'
	@echo ''
	@echo 'PyUVM (src/verif, closed-loop env on cxp_device_top)'
	@echo '  make pyuvm                         the ci tier (8 tests), gated (= make uvm)'
	@echo '  make pyuvm TIER=nightly            smoke | ci | feature | xifc | nightly | weekly'
	@echo '  make pyuvm TEST=test_stream_video  one test (names: make pyuvm-list)'
	@echo '  make pyuvm TEST=... SEED=30176     one test with a given seed (default 1)'
	@echo ''
	@echo 'Emulator (src/emu: RTL DPI bridge or virtual camera + C++/Qt host)'
	@echo '  make emu-build                     RTL bridge + host CLI and GUI (EMU_GUI=OFF: CLI only)'
	@echo '  make emu-cli                       RTL camera, run CMD (default "discover --params")'
	@echo '  make emu-cli CMD="read 0x0000"     any cxp command: read, write, stream-capture, validate ...'
	@echo '  make emu-gui                       RTL camera, device explorer GUI'
	@echo '  make emu-cli DEV=vcam              the same against the virtual camera (no RTL)'
	@echo '  make emu-rtl                       the validation campaign on the RTL, gated (~25 min)'
	@echo ''
	@echo 'Options for every simulation'
	@echo '  WAVE=none|vcd|fsdb                 waves off by default; fsdb converts VCD with vcd2fsdb'
	@echo '  JOBS=n                             parallel unit benches (default: nproc)'
	@echo '  SIM=vsim                           unit / PyUVM on QuestaSim instead of Verilator'
	@echo ''
	@echo 'Where the waves go'
	@echo '  unit   src/tb_unit/<group>/cxp_<bench>/dump.vcd (.fsdb)'
	@echo '  pyuvm  src/verif/00_test_results/<test>/<test>.vcd (.fsdb)'
	@echo '  emu    src/emu/bridge/build/cxp_hw_env.vcd (.fsdb), RTL camera only'

# ---- RTL unit benches --------------------------------------------------------
# No TB: the whole gated regression (`tb`).  TB=<name>: that bench alone,
# TEST=<regex> narrowing it to the matching tests (COCOTB_TEST_FILTER).
TB   ?=
TEST ?=

unit-list:
	@$(MAKE) --no-print-directory -C src/tb_unit list

ifeq ($(strip $(TB)),)
unit: tb
else
unit:
	@rc=0; COCOTB_TEST_FILTER='$(TEST)' $(MAKE) -C src/tb_unit $(TB) WAVES=$(WAVES_ON) WAVES_FMT=vcd || rc=$$?; \
	 $(call to_fsdb,src/tb_unit/*/cxp_$(TB)) || rc=$$?; exit $$rc
endif

# ---- PyUVM -------------------------------------------------------------------
# TEST=<test>: one test (its KNOBS_<test> build knobs apply); otherwise the
# tier TIER (default ci).  SEED sets CXP_SEED for a single test.  WAVE with
# a tier traces every test of it: expect ~175 MB of VCD per test.
TIER ?= ci
SEED ?= 1
PYUVM_TIERS := smoke ci feature xifc nightly weekly

pyuvm-list:
	@grep -h '^class test_' src/verif/uvm/tests/all_tests.py src/verif/uvm/tests/spec_tests.py \
	    src/verif/uvm/tests/conc_tests.py | sed 's/^class \(test_[a-z0-9_]*\).*/\1/'

pyuvm:
ifneq ($(strip $(TEST)),)
	@rc=0; $(MAKE) -C src/verif run UVM_TESTNAME=$(TEST) CXP_SEED=$(SEED) WAVES=$(WAVES_ON) WAVES_FMT=vcd || rc=$$?; \
	 $(call to_fsdb,src/verif/00_test_results/$(TEST)) || rc=$$?; exit $$rc
else
	@case " $(PYUVM_TIERS) " in *" $(TIER) "*) ;; *) echo "TIER must be one of: $(PYUVM_TIERS)"; exit 2;; esac
	@rc=0; $(MAKE) -C src/verif $(TIER) WAVES=$(WAVES_ON) WAVES_FMT=vcd || rc=$$?; \
	 $(call to_fsdb,src/verif/00_test_results) || rc=$$?; exit $$rc
endif

# ---- Emulator ----------------------------------------------------------------
# The camera is the RTL (DEV=rtl, src/emu/bridge, Verilator + DPI over two
# named pipes) or the C++ virtual camera (DEV=vcam, `cxp sim`); the host is
# the `cxp` CLI or the `cxp-gui` explorer.  tools/run_emu.sh starts the
# camera on a private FIFO pair, runs the host and stops the camera after.
DEV         ?= rtl
CMD         ?= discover --params
EMU_GUI     ?= ON
EMU_TIMEOUT ?= 30
EMU_HOST    := src/emu/host
EMU_TRACE   := $(WAVES_ON)

emu-build: host-build
	$(MAKE) -C src/emu/bridge build TRACE=$(EMU_TRACE)

# The C++ host alone (CLI, GUI when EMU_GUI=ON, the QtTest suite).
host-build:
	@# A cache configured from another source tree (a moved checkout) stops
	@# cmake; drop the cache only, the build tree's logs and results stay.
	@# Compare real paths: the same checkout may be reached through a symlink.
	@cached=$$(sed -n 's/^CMAKE_HOME_DIRECTORY:INTERNAL=//p' $(EMU_HOST)/build/CMakeCache.txt 2>/dev/null); \
	 if [ -n "$$cached" ] && [ "$$(realpath -m "$$cached")" != "$$(realpath $(EMU_HOST))" ]; then \
	    echo "emu-build: $(EMU_HOST)/build was configured elsewhere, reconfiguring"; \
	    rm -rf $(EMU_HOST)/build/CMakeCache.txt $(EMU_HOST)/build/CMakeFiles; \
	 fi
	@# Configure only when there is no cache or EMU_GUI changed (~1 min on
	@# /mnt/c); `cmake --build` reconfigures by itself on a CMakeLists edit.
	@grep -qx "CXP_BUILD_GUI:BOOL=$(EMU_GUI)" $(EMU_HOST)/build/CMakeCache.txt 2>/dev/null || \
	    cmake -S $(EMU_HOST) -B $(EMU_HOST)/build -DCXP_BUILD_GUI=$(EMU_GUI)
	cmake --build $(EMU_HOST)/build -j$(JOBS)

emu-cli: UI := cli
emu-gui: UI := gui
emu-cli emu-gui: emu-run

emu-run: emu-build
	@rc=0; EMU_TIMEOUT=$(EMU_TIMEOUT) TRACE=$(EMU_TRACE) \
	    tools/run_emu.sh $(or $(UI),cli) $(DEV) $(if $(filter gui,$(UI)),,$(CMD)) || rc=$$?; \
	 $(if $(filter rtl,$(DEV)),$(call to_fsdb,src/emu/bridge/build) || rc=$$?;) exit $$rc

emu-clean:
	$(MAKE) -C src/emu/bridge clean
	rm -rf $(EMU_HOST)/build
