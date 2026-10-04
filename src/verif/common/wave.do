# =============================================================================
# Generic QuestaSim wave/run script for cocotb GUI mode (`make gui`).
#
# Sourced by vsim after the design has been loaded. Adds every signal under
# the toplevel to the wave window, runs the simulation to completion, and
# leaves the GUI open for inspection (no quit). Per-TB overrides: drop a
# wave.do alongside the testbench Makefile and the build will prefer it.
# =============================================================================

# Make sure every signal is captured before the run so the wave window shows
# real values instead of X. `add wave` alone does this for the displayed set,
# but explicit `log` covers anything not auto-displayed too.
catch {log -recursive /*}

if {[catch {add wave -position insertpoint -recursive sim:/*} err]} {
    puts "wave.do: add wave failed: $err"
}

configure wave -namecolwidth    250
configure wave -valuecolwidth   120
configure wave -justifyvalue    left
configure wave -signalnamewidth 1
configure wave -timelineunits   ns

onbreak {resume}
run -all

catch {wave zoom full}
