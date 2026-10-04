#!/usr/bin/env bash
# =============================================================================
# Convert the VCD dumps under the given directories to FSDB (Verdi).
#
#   tools/vcd_to_fsdb.sh DIR [DIR ...]
#
# Every <name>.vcd found (recursively) gets a <name>.fsdb next to it; a dump
# whose .fsdb is already newer than the .vcd is skipped, so the script can
# run after every regression without redoing old work.  Verilator writes
# VCD or FST only, so `make ... WAVE=fsdb` traces as VCD and calls this.
#
# The converter is $VCD2FSDB (default: vcd2fsdb from $VERDI_HOME/bin or
# PATH).  KEEP_VCD=0 deletes each VCD once its FSDB is written.
# =============================================================================
set -u
CONV=${VCD2FSDB:-}
if [ -z "$CONV" ]; then
    if [ -n "${VERDI_HOME:-}" ] && [ -x "$VERDI_HOME/bin/vcd2fsdb" ]; then
        CONV=$VERDI_HOME/bin/vcd2fsdb
    else
        CONV=$(command -v vcd2fsdb || true)
    fi
fi
if [ -z "$CONV" ]; then
    echo "vcd_to_fsdb: vcd2fsdb not found (set VERDI_HOME or VCD2FSDB); the VCD dumps are kept" >&2
    exit 2
fi

rc=0; n=0
while IFS= read -r -d '' vcd; do
    fsdb=${vcd%.vcd}.fsdb
    if [ -f "$fsdb" ] && [ "$fsdb" -nt "$vcd" ]; then
        continue
    fi
    echo "vcd_to_fsdb: $vcd -> $fsdb"
    if "$CONV" "$vcd" -o "$fsdb" >"${fsdb}.log" 2>&1; then
        n=$((n + 1))
        rm -f "${fsdb}.log"
        [ "${KEEP_VCD:-1}" = 0 ] && rm -f "$vcd"
    else
        echo "vcd_to_fsdb: conversion failed, see ${fsdb}.log" >&2
        rc=1
    fi
done < <(find "$@" -name '*.vcd' -print0 2>/dev/null)
echo "vcd_to_fsdb: $n file(s) converted"
exit $rc
