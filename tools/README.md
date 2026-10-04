# `tools/` — regression gates, checkers and report generators

Small standalone tools the repo-root `Makefile`, `src/verif/Makefile`,
`src/tb_unit/Makefile` and CI call. The gates turn what a run left behind into
a single exit code.

| Tool | Purpose | Called by |
|------|---------|-----------|
| `check_results.py` | Fails if any cocotb JUnit `results.xml` has a failure / error, or one is missing. `--benches DIR` requires one per `DIR/<group>/cxp_*/` bench (`--pending` parks benches on purpose); `--results-dir` + `--expect` requires named tests. | `make tb`, `make uvm`, every `src/verif/` tier |
| `check_fsm_coverage.py` | Fails if a registered FSM collected no state, saw a state outside its enum (stale registration), or never took a designed arc. | `make tb` |
| `check_style.py` | Mechanical rules of `docs/style/rtl_coding_style.sv`: line length, `default_nettype`, header block, `p_` params, `_i` / `_o` ports, `<module>_i` instance names, no inline lint pragmas. No args = every file in `src/rtl/cxp_ip.f`. | `make style` |
| `check_emu_results.py` | Gate on a `cxp validate` `results.json`: FAIL / ERROR fail unless tagged in an expected-fail file (then a pass is a stale tag); NOT RUN fails unless allow-listed; an empty campaign fails. | `run_emu_rtl.sh` |
| `run_emu_rtl.sh` | Builds `src/emu/bridge` and `src/emu/host/build`, starts the RTL simulation on a private FIFO pair, runs `cxp validate --runnable`, gates with `check_emu_results.py`. About 25 min. | `make emu-rtl`, `make nightly` |
| `run_emu.sh` | One interactive emulator session: RTL camera (`src/emu/bridge`) or virtual camera on a private FIFO pair, then one `cxp` CLI command or `cxp-gui`; stops the camera on exit. | `make emu-cli`, `make emu-gui` |
| `vcd_to_fsdb.sh` | Converts every VCD under the given directories to FSDB with Verdi's `vcd2fsdb` (`VERDI_HOME` / `VCD2FSDB`), skipping up-to-date ones. | `WAVE=fsdb` on `make unit` / `pyuvm` / `emu-*` |
| `gen_rtl_conn.py` | Elaborates `cxp_device_top` with Verilator (`--json-only`) and rewrites the `CONN` block of `docs/design/cxp_rtl_map.html`: per-level instance graphs (nets, widths, drivers, loads) and the instance tree. Prints every disagreement between the page's module entries (ports, parameters, children) and the netlist; `--check` exits 1 if the block is stale. | by hand after an RTL change |
| `gen_uvm_map.py` | Reads `src/verif/` through Python's `ast` (no simulator or pyuvm needed), the Makefile tiers, `tb_cxp_top.sv` and `docs/verification/cxp_verification_plan.md`, and rewrites the `UVM` block of `docs/verification/cxp_uvm_map.html`: component graphs, harness ports, tests, tiers, error kinds, coverage goals, decisions. Prints every disagreement between code, Makefile, harness and plan; `--check` exits 1 if the block is stale. | by hand after an environment change |
| `gen_test_report.py` | Unit-bench HTML report from every `results.xml` + `fsm_coverage.json` under `src/tb_unit`, written to `src/tb_unit/tb_unit_test_report.html`. | `src/tb_unit/Makefile` (`report`) |
| `gen_uvm_test_report.py` | PyUVM HTML report and per-plan-test verdicts (`src/verif/plan_status.json`) from `src/verif/00_test_results`; the page is `src/verif/uvm_test_report.html`. | `src/verif/Makefile` (`report`, `plan_status`) |

```
python3 tools/check_results.py --benches src/tb_unit
python3 tools/check_style.py src/rtl/tx/cxp_tx_inserter.sv
python3 tools/gen_test_report.py
python3 tools/gen_rtl_conn.py [--check]
python3 tools/gen_uvm_map.py [--check]
tools/run_emu_rtl.sh [OUT_DIR]
```

`src/regmap/gen_regmap.py` (the register-map generator) stays with the map it
generates from.
