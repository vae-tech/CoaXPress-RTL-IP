# =============================================================================
# src/verif/Makefile — top-level PyUVM regression for CoaXPress device IP.
#
# Selects one UVM test per `make` invocation via the +UVM_TESTNAME plusarg.
# Test names listed under uvm/tests/ (e.g. test_smoke, test_stream_tpg,
# test_arbiter_preempt, ...).  Default goal runs the smoke test.
#
# Usage:
#   make                                    # smoke
#   make UVM_TESTNAME=test_stream_video     # single test
#   make smoke|ci|nightly|weekly            # regression tiers
#   make SIM=vsim ...                       # QuestaSim back-end
#   make WAVES=1 ...                        # trace (off by default)
#   make DUMP=1 ...                         # raw downlink dump (on in nightly/weekly)
# =============================================================================

REPO_ROOT  := $(abspath $(CURDIR)/../..)
RTL_DIR    := $(REPO_ROOT)/src/rtl
TB_DIR     := $(CURDIR)/uvm/sv
UVM_DIR    := $(CURDIR)/uvm
COMMON_DIR := $(REPO_ROOT)/src/verif/common

TOPLEVEL_LANG ?= verilog
TOPLEVEL      ?= tb_cxp_top
MODULE        ?= uvm.tests.run_test

# Default UVM test (override on command line).
UVM_TESTNAME ?= test_idle_baseline

# Cocotb passes args to the simulator via PLUSARGS.  pyuvm picks
# UVM_TESTNAME from the environment, but +UVM_TESTNAME also works.
export UVM_TESTNAME

# RTL comes from src/rtl/cxp_ip.f via cocotb_sim.mk; only the SV top is local.
TB_SOURCES := $(TB_DIR)/tb_cxp_top.sv

# Make Python find the UVM tree and the existing common helpers.
export PYTHONPATH := $(CURDIR):$(COMMON_DIR):$(PYTHONPATH)

# -----------------------------------------------------------------------------
# Code coverage — Verilator line coverage (FSM state arcs / branch reach).
# Enable with `make RTL_COV=1 ...` (force a clean sim_build first so the
# instrumentation is rebuilt).  Each run drops a coverage.dat that `archive`
# files under 00_test_results/<test>/; `make fsm_cov` merges the whole
# regression into an annotated report.
#
# Line coverage (not toggle) is used deliberately: every FSM state's case
# arm and next-state branch is a distinct source line, so line coverage is
# a direct readout of FSM-state reachability — and toggle coverage on the
# 1M-entry stream FIFO would be intractable.  NB: the knob is RTL_COV, not
# COVERAGE — cocotb treats a COVERAGE env var as a request for Python
# coverage of the testbench itself.
# -----------------------------------------------------------------------------
RTL_COV ?= 0
ifeq ($(RTL_COV),1)
EXTRA_ARGS += --coverage-line
endif

# -----------------------------------------------------------------------------
# Clock-crossing configuration of the DUT.
#
# The regression drives three independent clock inputs and vs_cdc_sweep
# retunes their periods mid-test, so the crossings have to be the real
# ones — 1 by default, the same build cxp_device_top uses.  Build the
# single-clock variant with `make ASYNC_CLOCKS=0`.
# -----------------------------------------------------------------------------
ASYNC_CLOCKS ?= 1
EXTRA_ARGS   += -Gp_ASYNC_CLOCKS=$(ASYNC_CLOCKS)

# -----------------------------------------------------------------------------
# Build knobs of the TB shell (tb_cxp_top parameters).  Each is passed to
# Verilator as -G and exported as CXP_<NAME> so the Python side
# (uvm/common/build.py) sees the same value.  A set that differs from the
# defaults gets its own sim_build_<cfg>: make never notices a parameter
# change, so one build per parameter set.
#
#   OS_RATIO       uplink oversampling (16)
#   RX_CLK_KHZ     rx cycles per device millisecond (20: the 100 ms Wait
#                  and 900 ms timeout are 20 us / 180 us at rx 10 ns)
#   RX_LOSS_WORDS  words without IDLE before the link is lost (the RTL
#                  default, 2 x 10 000 low-speed words)
#   FIFO_DEPTH     stream FIFO words (the RTL default, 1024)
# -----------------------------------------------------------------------------
# Tests that need another build (their class REQUIRES it): the tiers pass
# these knobs, and `make UVM_TESTNAME=<t>` picks them up too.
KNOBS_test_uplink_idle_limits    := OS_RATIO=4
KNOBS_test_linktest_rx_full      := OS_RATIO=4
KNOBS_test_linktest_tx_rules     := OS_RATIO=4
KNOBS_test_conc_bidir_linktest   := OS_RATIO=4
KNOBS_test_uplink_ppm_jitter_os4 := OS_RATIO=4
KNOBS_test_uplink_ppm_jitter_os4_neg := OS_RATIO=4
KNOBS_test_conc_backpressure_frames := FIFO_DEPTH=256
# (A value given on the command line still wins over these.)
$(foreach kv,$(KNOBS_$(UVM_TESTNAME)),$(eval $(kv)))

OS_RATIO      ?= 16
RX_CLK_KHZ    ?= 20
RX_LOSS_WORDS ?= 20000
FIFO_DEPTH    ?= 1024
export CXP_OS_RATIO      := $(OS_RATIO)
export CXP_RX_CLK_KHZ    := $(RX_CLK_KHZ)
export CXP_RX_LOSS_WORDS := $(RX_LOSS_WORDS)
export CXP_FIFO_DEPTH    := $(FIFO_DEPTH)
EXTRA_ARGS += -GOS_RATIO=$(OS_RATIO) -GRX_CLK_KHZ=$(RX_CLK_KHZ) \
              -GRX_LOSS_WORDS=$(RX_LOSS_WORDS) -GFIFO_DEPTH=$(FIFO_DEPTH)
BUILD_CFG := $(if $(filter-out 16,$(OS_RATIO)),_os$(OS_RATIO))$(if \
             $(filter-out 20,$(RX_CLK_KHZ)),_khz$(RX_CLK_KHZ))$(if \
             $(filter-out 20000,$(RX_LOSS_WORDS)),_loss$(RX_LOSS_WORDS))$(if \
             $(filter-out 1024,$(FIFO_DEPTH)),_fifo$(FIFO_DEPTH))$(if \
             $(filter-out 1,$(ASYNC_CLOCKS)),_sync)
SIM_BUILD := sim_build$(BUILD_CFG)

# One seed for every random stream of the environment (uvm/common/seed.py).
CXP_SEED ?= 1
export CXP_SEED

# tb_cxp_top forwards p_XML_BLOB_MEM to the register file: it loads the one
# XML ROM image, src/rtl/gen/cxp_camera_xml.mem, by absolute path
# (src/regmap/regmap.mk, applied by cocotb_sim.mk).
CXP_XML_ROM := 1

# -----------------------------------------------------------------------------
# Raw downlink dump — every tx word as (tx_cycle, word, kmask), gzip'd, in the
# cxp_protocol.dump format (`python -m cxp_protocol.dump FILE` summarises it).
# Off for a single run unless DUMP=1; the nightly and weekly tiers turn it on.
# DUMP_MAX_MB caps each test's file on disk (/mnt/c has little room left).
# -----------------------------------------------------------------------------
DUMP        ?= 0
DUMP_MAX_MB ?= 16
export DUMP DUMP_MAX_MB

include $(COMMON_DIR)/cocotb_sim.mk

# -----------------------------------------------------------------------------
# Result archival
#
# Every test run lands its JUnit results.xml plus the coverage JSON in
# 00_test_results/<UVM_TESTNAME>/.  The HTML rollup (gen_uvm_test_report.py)
# walks that directory and writes one summary page, uvm_test_report.html.
#
# `run` wraps cocotb's `sim` target so the archive fires even when a
# test fails.  `run` is the default goal.
#
# Every one of those artifacts is written into this one directory under a
# fixed name, so `run` deletes the previous test's copies before starting
# the simulator.  Without that, `archive` cannot tell this test's output
# from the last test's leftovers: a simulation that dies before writing
# results.xml would be archived with the *previous* test's results.xml and
# sail through `gate`, and a dump.vcd left by an earlier WAVES=1 run would
# be copied into every test's directory (31 × ~175 MB across a weekly).
# -----------------------------------------------------------------------------
RESULTS_DIR := $(CURDIR)/00_test_results
REPORT_HTML := $(CURDIR)/uvm_test_report.html

# Written by the simulator / the testbench, one set per `run`.
RUN_ARTIFACTS := results.xml dump.vcd cov_summary.json cxp_stream_dump.txt \
                 coverage.dat downlink.bin.gz

.DEFAULT_GOAL := run

.PHONY: run archive report gate fsm_cov plan_status

run:
	@rm -f $(RUN_ARTIFACTS)
	@rc=0; $(MAKE) sim || rc=$$?; \
	 $(MAKE) --no-print-directory archive UVM_TESTNAME=$(UVM_TESTNAME); \
	 exit $$rc

archive:
	@mkdir -p $(RESULTS_DIR)/$(UVM_TESTNAME)
	@if [ -f results.xml ]; then \
	    cp -f results.xml $(RESULTS_DIR)/$(UVM_TESTNAME)/results.xml; \
	 fi
	@if [ -f dump.vcd ]; then \
	    cp -f dump.vcd $(RESULTS_DIR)/$(UVM_TESTNAME)/$(UVM_TESTNAME).vcd; \
	 fi
	@if [ -f cov_summary.json ]; then \
	    cp -f cov_summary.json $(RESULTS_DIR)/$(UVM_TESTNAME)/cov_summary.json; \
	 fi
	@if [ -f cxp_stream_dump.txt ]; then \
	    cp -f cxp_stream_dump.txt $(RESULTS_DIR)/$(UVM_TESTNAME)/cxp_stream_dump.txt; \
	 fi
	@if [ -f downlink.bin.gz ]; then \
	    mv -f downlink.bin.gz $(RESULTS_DIR)/$(UVM_TESTNAME)/downlink.bin.gz; \
	 fi
	@if [ -f coverage.dat ]; then \
	    cp -f coverage.dat $(RESULTS_DIR)/$(UVM_TESTNAME)/coverage.dat; \
	    rm -f coverage.dat; \
	 fi
	@echo "archived to $(RESULTS_DIR)/$(UVM_TESTNAME)/"

# rc 1 means "the report lists failing tests" — that is `gate`'s business,
# not a report failure; only rc >= 2 (no results directory, write error) is
# worth a warning.  Either way the HTML step never decides a tier's fate.
report:
	@rc=0; python3 $(REPO_ROOT)/tools/gen_uvm_test_report.py \
	    --results-dir $(RESULTS_DIR) --out $(REPORT_HTML) || rc=$$?; \
	 if [ $$rc -ge 2 ]; then echo "warning: report generation failed (rc=$$rc)"; fi

# One verdict per validation-plan test case (docs/verification/validation/plan_map.json)
# from whatever results.xml files are in 00_test_results/ now.  Runs no
# simulation; writes plan_status.json and prints the table.  `report` writes
# the same JSON and puts the table in the HTML page.
plan_status:
	@python3 $(REPO_ROOT)/tools/gen_uvm_test_report.py \
	    --results-dir $(RESULTS_DIR) --plan-status-only

# The gate.  A tier runs every test to the end (a failure must not hide the
# tests behind it), so nothing in the loop can carry an exit code out; this
# reads the archived results.xml files instead and is the tier's last word.
# TIER_TESTS is the list that had to run: a test whose simulation never
# started leaves no results.xml and fails here as "MISSING".
gate:
	@python3 $(REPO_ROOT)/tools/check_results.py \
	    --results-dir $(RESULTS_DIR) --expect "$(TIER_TESTS)"

# Merge every per-test coverage.dat into one annotated line/FSM-coverage
# report.  Run after a `make COVERAGE=1 nightly` (or weekly).
fsm_cov:
	@dats=$$(ls $(RESULTS_DIR)/*/coverage.dat 2>/dev/null); \
	 if [ -z "$$dats" ]; then \
	    echo "fsm_cov: no coverage.dat found — run 'make COVERAGE=1 nightly' first"; \
	 else \
	    n=$$(echo $$dats | wc -w); echo "fsm_cov: merging $$n coverage.dat files"; \
	    verilator_coverage $$dats --write $(RESULTS_DIR)/merged_coverage.dat \
	        2>/dev/null || true; \
	    verilator_coverage $(RESULTS_DIR)/merged_coverage.dat \
	        --annotate $(RESULTS_DIR)/coverage_annotated --annotate-min 1 \
	        --annotate-all 2>&1 | tail -20 || \
	      echo "warning: verilator_coverage annotate failed"; \
	    echo "FSM/line coverage: $(RESULTS_DIR)/coverage_annotated/ "; \
	 fi

# -----------------------------------------------------------------------------
# Regression tier helpers — they re-invoke `run` (not `sim`) so each test
# archives independently, and every tier ends in `gate`, which is what
# gives the tier its exit code.  At the end of nightly/weekly we render the
# rolled-up HTML.  Lists track §10.1 testplan.
# -----------------------------------------------------------------------------
SMOKE_TESTS   := test_idle_baseline test_stream_tpg test_discovery_bringup \
                 test_conc_ctrl_under_stream_load

# What `make ci` runs at the repo root: the smoke pair plus a control
# read, a control write, a ConnectionReset and the device's connection
# test, so every CI run has a host talking to the RTL through the gate.
CI_TESTS      := $(SMOKE_TESTS) test_ctrl_cmd_read test_ctrl_cmd_write \
                 test_link_reset test_tx_linktest_mode

# v1.1.1 feature tests with no prior top-level coverage.
FEATURE_TESTS := test_io_ack test_tx_trigger test_tx_linktest_mode \
                 test_router_reject test_link_reset test_tpg_config \
                 test_tpg_formats

# Cross-interface interaction tests (simultaneous multi-interface traffic).
XIFC_TESTS    := test_xifc_stream_ctrl test_xifc_stream_trigger \
                 test_xifc_stream_linkreset test_xifc_full

NIGHTLY_TESTS := test_idle_baseline test_stream_tpg test_stream_video \
                 test_stream_video_ragged \
                 test_arbitrary_image test_ctrl_cmd_read \
                 test_ctrl_cmd_write test_ctrl_reset_op \
                 test_trigger_uplink test_linktest_clean test_linktest_inject \
                 test_byte_replication_robust test_crc_error \
                 test_pslverr_burst test_arbiter_preempt \
                 test_arbiter_stream_underflow test_arbiter_underflow_ctrl \
                 test_ctrl_reset_storm test_ral_sweep \
                 test_trigger_in_ctrl_packet \
                 $(FEATURE_TESTS) $(XIFC_TESTS)

# Spec-chapter tests (catalogue T-01 .. T-26, uvm/tests/spec_tests.py).
SPEC_TESTS    := test_discovery_bringup test_bootstrap_ro_map test_ctrl_xml_read \
                 test_conn_reset_postconditions test_conn_config_write \
                 test_spsm_negotiation test_ctrl_size_matrix test_ctrl_invalid_cmds \
                 test_ctrl_wait_ack test_ctrl_reset_during_exec test_ctrl_pipelined_cmds \
                 test_uplink_reserved_types test_uplink_bit_errors \
                 test_uplink_ppm_jitter test_uplink_ppm_jitter_neg \
                 test_uplink_ppm_jitter_os4 test_uplink_ppm_jitter_os4_neg \
                 test_link_loss_relock test_uplink_idle_limits \
                 test_trigger_delay_latency test_tx_trigger_ack_rules \
                 test_trigger_at_discovery test_packet_tag_persistence \
                 test_image_geometry_matrix test_pixel_formats \
                 test_stream_back_to_back_frames test_linktest_rx_full \
                 test_linktest_tx_rules

# Concurrency tests (catalogue C-01 .. C-15, uvm/tests/conc_tests.py).
CONC_TESTS    := test_conc_ctrl_under_stream_load test_conc_ioack_inside_stream \
                 test_conc_trigger_insertion_sweep test_conc_trigger_vs_ioack \
                 test_conc_uplink_trigger_in_packet test_conc_testmode_under_stream \
                 test_conc_conn_reset_everything test_conc_ctrl_reset_under_load \
                 test_conc_uplink_errors_under_stream test_conc_link_loss_under_stream \
                 test_conc_backpressure_frames test_conc_source_and_format_switch \
                 test_conc_single_domain_reset test_conc_clock_ratio_matrix \
                 test_conc_bidir_linktest

NIGHTLY_TESTS += $(SPEC_TESTS) $(CONC_TESTS)

WEEKLY_TESTS  := $(NIGHTLY_TESTS) test_soak_random \
                 test_conc_clock_ratio_matrix_full

.PHONY: smoke ci nightly weekly feature xifc cov_clean cov_report results_clean

smoke: results_clean
	@$(foreach t,$(SMOKE_TESTS),$(MAKE) UVM_TESTNAME=$(t) run ;) true
	@$(MAKE) report
	@$(MAKE) gate TIER_TESTS="$(SMOKE_TESTS)"

ci: results_clean
	@$(foreach t,$(CI_TESTS),$(MAKE) UVM_TESTNAME=$(t) run ;) true
	@$(MAKE) report
	@$(MAKE) gate TIER_TESTS="$(CI_TESTS)"

feature: results_clean
	@$(foreach t,$(FEATURE_TESTS),$(MAKE) UVM_TESTNAME=$(t) run ;) true
	@$(MAKE) report
	@$(MAKE) gate TIER_TESTS="$(FEATURE_TESTS)"

xifc: results_clean
	@$(foreach t,$(XIFC_TESTS),$(MAKE) UVM_TESTNAME=$(t) run ;) true
	@$(MAKE) report
	@$(MAKE) gate TIER_TESTS="$(XIFC_TESTS)"

nightly: cov_clean results_clean
	@$(foreach t,$(NIGHTLY_TESTS),$(MAKE) UVM_TESTNAME=$(t) DUMP=1 run ;) true
	@$(MAKE) cov_report
	@$(MAKE) report
	@rc=0; $(MAKE) gate TIER_TESTS="$(NIGHTLY_TESTS)" || rc=1; \
	 $(MAKE) cov_gate TIER_TESTS="$(NIGHTLY_TESTS)" || rc=1; exit $$rc

# The weekly tier runs every test with a fresh seed (printed, and in each
# results.xml as cxp_seed) on top of what the nightly covers with the fixed
# one.
weekly: cov_clean results_clean
	@$(foreach t,$(WEEKLY_TESTS),s=$$(od -An -N2 -tu2 /dev/urandom | tr -d ' '); \
	   echo "== $(t) CXP_SEED=$$s"; $(MAKE) UVM_TESTNAME=$(t) CXP_SEED=$$s DUMP=1 run ;) true
	@$(MAKE) cov_report
	@$(MAKE) report
	@rc=0; $(MAKE) gate TIER_TESTS="$(WEEKLY_TESTS)" || rc=1; \
	 $(MAKE) cov_gate TIER_TESTS="$(WEEKLY_TESTS)" || rc=1; exit $$rc

# Functional coverage of the tier against uvm/coverage/model.py GOALS:
# fails on a goal cell no test hit and prints the holes.
.PHONY: cov_gate
cov_gate:
	@python3 -m uvm.coverage.gate --results-dir $(RESULTS_DIR) --tests "$(TIER_TESTS)"

cov_clean: 
	rm -rf cov_*.xml cov_summary.json 

cov_report:
	@python3 -c "import os, glob; xs=sorted(glob.glob('cov_*.xml')); print('Merging %d coverage XML files' % len(xs))"
	@python3 -m uvm.coverage.merge cov_*.xml > coverage_report.html || true
	@echo "Coverage report at: $(CURDIR)/coverage_report.html"

results_clean:
	rm -rf $(RESULTS_DIR)
