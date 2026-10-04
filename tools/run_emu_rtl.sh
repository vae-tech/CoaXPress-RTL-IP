#!/usr/bin/env bash
# =============================================================================
# The emulator's validation campaign against the RTL (src/emu/bridge DPI bridge).
#
#   tools/run_emu_rtl.sh [OUT_DIR]
#   QUIET=1 tools/run_emu_rtl.sh      # case output to validate.log only
#
# Builds the RTL bridge (src/emu/bridge) and the C++ host (src/emu/host/build), starts the
# RTL simulation on a private pair of FIFOs, runs every runnable case with
# `cxp validate --runnable`, stops the simulation and hands the verdicts to
# tools/check_emu_results.py, whose exit code is this script's.
#
# About 25 minutes on one simulator.  `make emu-rtl` calls it; the nightly
# job (`make nightly`) runs it after `make ci`.
# =============================================================================
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-$ROOT/src/emu/host/build/validation_logs/emu_rtl}
FIFO=$(mktemp -d /tmp/cxp_emu_rtl.XXXXXX)
mkdir -p "$OUT"

make -C "$ROOT/src/emu/bridge" build >"$OUT/hw_build.log" 2>&1 || { echo "emu-rtl: src/emu/bridge build failed ($OUT/hw_build.log)"; exit 2; }
make -C "$ROOT/src/emu/host/build" cxp >"$OUT/cpp_build.log" 2>&1 || { echo "emu-rtl: src/emu/host build failed ($OUT/cpp_build.log)"; exit 2; }

make -C "$ROOT/src/emu/bridge" run FIFO_DIR="$FIFO" >"$OUT/sim.log" 2>&1 &
SIM=$!
cleanup() {
    # The simulator is make's grandchild (make -> sh -> cxp_hw_env), so
    # killing make's children left it running.  Stop the processes whose
    # command line names this run's private FIFO dir, by PID, never by name.
    for p in $(pgrep -f -- "h2c=$FIFO/" 2>/dev/null); do kill "$p" 2>/dev/null; done
    pkill -TERM -P "$SIM" 2>/dev/null
    kill "$SIM" 2>/dev/null
    wait "$SIM" 2>/dev/null
    rm -rf "$FIFO"
}
trap cleanup EXIT
sleep 5

# The per-case progress goes to the terminal and to validate.log; QUIET=1
# sends it to the log only.
if [ "${QUIET:-0}" = 1 ]; then
    "$ROOT/src/emu/host/build/cxp" --fifo-dir "$FIFO" --timeout 30 validate --runnable \
        --json "$OUT/results.json" --log-dir "$OUT" >"$OUT/validate.log" 2>&1
    RC=$?
else
    "$ROOT/src/emu/host/build/cxp" --fifo-dir "$FIFO" --timeout 30 validate --runnable \
        --json "$OUT/results.json" --log-dir "$OUT" 2>&1 | tee "$OUT/validate.log"
    RC=${PIPESTATUS[0]}
fi
echo "emu-rtl: cxp validate exited $RC (log $OUT/validate.log)"

python3 "$ROOT/tools/check_emu_results.py" "$OUT/results.json" \
    --expect-fail "$ROOT/src/emu/host/validation/expected_fail.json" \
    --allow-not-run "CXP-CAM-GEN-009"
