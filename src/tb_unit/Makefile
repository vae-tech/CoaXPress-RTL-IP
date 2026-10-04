# =============================================================================
# Unit testbench driver.  Every <group>/cxp_<name>/ directory with a Makefile
# is a TB (group = app, cdc, tx, rx, ctrl, lib, top); nothing here needs
# editing when one is added or removed.  TB names are the directory names
# without cxp_, whatever the group.
#
#   make                       # run all TBs on Verilator, then the HTML report
#   make -j8 -k                # same, 8 TBs in parallel, keep going on failure
#   make SIM=vsim              # run all TBs on QuestaSim (headless batch)
#   make <name>                # one TB, e.g. `make tx_arbiter`
#   make gui_<name>            # one TB on QuestaSim, GUI with waves
#   make clean                 # remove all sim_build dirs
#   make list                  # print the TB names
#   make TB_SKIP="a b"         # leave TBs a and b out of `all`
# =============================================================================

SIM ?= verilator
TB_SKIP ?=

TB_DIRS    := $(patsubst %/Makefile,%,$(sort $(wildcard */cxp_*/Makefile)))
TBS        := $(patsubst cxp_%,%,$(notdir $(TB_DIRS)))
tb_dir     = $(filter %/cxp_$(1),$(TB_DIRS))
GUI_TBS    := $(addprefix gui_,$(TBS))
CLEAN_TBS  := $(addprefix clean_,$(TBS))

.PHONY: all report list clean $(TBS) $(GUI_TBS) $(CLEAN_TBS)

all: report

report: $(filter-out $(TB_SKIP),$(TBS))
	python3 ../../tools/gen_test_report.py

list:
	@echo $(TBS)

$(TBS):
	$(MAKE) -C $(call tb_dir,$@) SIM=$(SIM)

# QuestaSim GUI per TB. Run one at a time — opening three GUIs in parallel
# is rarely what anyone wants.
$(GUI_TBS): gui_%:
	$(MAKE) -C $(call tb_dir,$*) gui

clean: $(CLEAN_TBS)

$(CLEAN_TBS): clean_%:
	$(MAKE) -C $(call tb_dir,$*) clean
