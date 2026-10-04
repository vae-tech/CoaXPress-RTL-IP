# `src/emu/host/validation/` — validation catalogue and its checkers

Python tools that keep the C++ validation campaign (`cxp validate`, checks in
`src/cxp/validation/cases/<area>/<ID>.cpp`) in step with the validation plan
(`src/emu/host/validation/cxp_camera_validation_plan.md`) and the
PyUVM tests.

| File | Purpose |
|------|---------|
| `gen_validation_cases.py` | Generates `cxp_validation_cases.json`: one entry per plan case (objective, procedure, PASS / FAIL, requirements), emulator procedure and params (`EMULATOR`), extra emulator-only cases, and one `UVM-<test>` entry per PyUVM test. `--check` exits 1 if stale. |
| `cxp_validation_cases.json` | Generated catalogue the C++ runner loads. |
| `check_case_files.py` | Each case file is named after the one ID it registers with `CXP_CHECK`, sits in the right area directory, and no ID is registered twice. |
| `check_params.py` | Every parameter a check reads (`iparam()`, `slist()`, `rows()`, …) is declared by its case in the catalogue, and vice versa. |
| `expected_fail.json` | Cases red for an open RTL finding (`{"ID": "finding: …"}`); read by `tools/check_emu_results.py`. |

```
python3 src/emu/host/validation/gen_validation_cases.py          # after a plan change
python3 src/emu/host/validation/gen_validation_cases.py --check
python3 src/emu/host/validation/check_case_files.py
python3 src/emu/host/validation/check_params.py
```

The last three also run as CTest tests of the `src/emu/host` build.
