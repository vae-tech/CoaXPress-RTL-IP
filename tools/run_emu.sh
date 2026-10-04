#!/usr/bin/env bash
# =============================================================================
# One interactive emulator session: a camera on a private FIFO pair, and the
# C++ host (CLI command or Qt GUI) talking to it.  Stops the camera on exit.
#
#   tools/run_emu.sh cli|gui rtl|vcam [CLI command and options ...]
#
#   cli rtl  discover --params     RTL camera (src/emu/bridge), one CLI command
#   cli vcam read 0x0000           C++ virtual camera (`cxp sim`), one command
#   gui rtl                        RTL camera, device explorer GUI
#   gui vcam                       virtual camera, device explorer GUI
#
# Environment (set by the top-level Makefile, all optional):
#   FIFO_DIR     FIFO directory (default: a fresh /tmp/cxp_emu.XXXXXX)
#   EMU_TIMEOUT  host control-ack timeout in seconds (default 30)
#   TRACE        1 = the RTL camera dumps src/emu/bridge/build/cxp_hw_env.vcd
#   OPT_LEVEL    RTL camera build optimisation (0 / 1), see src/emu/bridge
#   LOG_DIR      where the camera log goes (default src/emu/host/build/emu_logs)
#
# Build first (`make emu-build`); this script only runs.
# =============================================================================
set -u
UI=${1:-cli}; DEV=${2:-rtl}
shift 2 2>/dev/null || shift $#
ROOT=$(cd "$(dirname "$0")/.." && pwd)
HOST=$ROOT/src/emu/host/build
LOG_DIR=${LOG_DIR:-$HOST/emu_logs}
TIMEOUT=${EMU_TIMEOUT:-30}
mkdir -p "$LOG_DIR"

case $UI in cli|gui) ;; *) echo "run_emu: UI must be cli or gui (got '$UI')" >&2; exit 2;; esac
case $DEV in rtl|vcam) ;; *) echo "run_emu: DEV must be rtl or vcam (got '$DEV')" >&2; exit 2;; esac
[ -x "$HOST/cxp" ] || { echo "run_emu: $HOST/cxp not built — run 'make emu-build'" >&2; exit 2; }
if [ "$UI" = gui ] && [ ! -x "$HOST/cxp-gui" ]; then
    echo "run_emu: $HOST/cxp-gui not built — run 'make emu-build' with EMU_GUI=ON" >&2; exit 2
fi

OWN_FIFO=0
if [ -z "${FIFO_DIR:-}" ]; then FIFO_DIR=$(mktemp -d /tmp/cxp_emu.XXXXXX); OWN_FIFO=1; fi
CAM_LOG=$LOG_DIR/camera_$DEV.log

# ---- camera ------------------------------------------------------------------
if [ "$DEV" = rtl ]; then
    make -C "$ROOT/src/emu/bridge" run FIFO_DIR="$FIFO_DIR" TRACE="${TRACE:-0}" \
        ${OPT_LEVEL:+OPT_LEVEL=$OPT_LEVEL} >"$CAM_LOG" 2>&1 &
    READY='camera RTL running'
else
    "$HOST/cxp" --fifo-dir "$FIFO_DIR" sim >"$CAM_LOG" 2>&1 &
    READY=''
fi
CAM=$!

cleanup() {
    # The RTL simulator is make's grandchild: stop every process whose
    # command line names this session's FIFO dir, by PID, never by name.
    for p in $(pgrep -f -- "$FIFO_DIR/" 2>/dev/null); do kill "$p" 2>/dev/null; done
    pkill -TERM -P "$CAM" 2>/dev/null
    kill "$CAM" 2>/dev/null
    wait "$CAM" 2>/dev/null
    [ "$OWN_FIFO" = 1 ] && rm -rf "$FIFO_DIR"
    echo "run_emu: camera stopped (log $CAM_LOG)"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# Wait until the camera is up: the RTL build/run banner, then the FIFOs.
for _ in $(seq 1 600); do
    kill -0 "$CAM" 2>/dev/null || { echo "run_emu: camera exited early, see $CAM_LOG" >&2; tail -20 "$CAM_LOG" >&2; exit 2; }
    if { [ -z "$READY" ] || grep -q "$READY" "$CAM_LOG"; } && [ -p "$FIFO_DIR/cxp.h2c" ] && [ -p "$FIFO_DIR/cxp.c2h" ]; then
        break
    fi
    sleep 0.5
done
sleep 2
echo "run_emu: $DEV camera on $FIFO_DIR (log $CAM_LOG)"

# ---- host --------------------------------------------------------------------
if [ "$UI" = gui ]; then
    "$HOST/cxp-gui" --fifo-dir "$FIFO_DIR"
else
    [ $# -gt 0 ] || set -- discover --params
    "$HOST/cxp" --fifo-dir "$FIFO_DIR" --timeout "$TIMEOUT" "$@"
fi
