# CoaXPress device IP

## License

This project is licensed under the
[Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0).

## Overview

A **CoaXPress 1.1.1 (JIIA CXP-001-2015) device-side (camera-side) link-layer
IP** in SystemVerilog, with a complete development and verification environment.
The IP handles low-speed host commands, bootstrap and camera registers, image
streaming, triggers and connection tests. Images come from an external sensor
interface or an internal test-pattern generator.

The repository combines the RTL with cocotb unit benches, closed-loop PyUVM
verification, bound SystemVerilog assertions, a golden Python protocol model
and a C++/Qt host application. A shared YAML register map generates consistent
RTL, software and documentation views.

The environment is deliberately **split into two parts: RTL simulation and an
emulated host written in software**. The simulator runs only the device RTL,
while everything on the host side (protocol handling, register access, image
reconstruction, test sequencing and checking) runs as compiled C++ or Python
outside it. The simulator therefore stops modelling the host side cycle by
cycle, which **greatly reduces simulation time, in some scenarios by a factor
of ten or more** compared with a monolithic HDL testbench.

The split also changes who can contribute. Verification tests, reference
models and host-side workflows are written in **Python and C++**, with thin
SystemVerilog wrappers and assertions connecting them to the RTL. Engineers
from software, validation, test automation or data-processing backgrounds can
write tests, models and analysis tools in the languages they already use.
They do not need to specialize in HDL verification first.

### Emulation and its advantages

The emulation environment makes the **RTL behave as a camera connected to a
host**. A SystemVerilog/DPI-C bridge runs the device in Verilator or QuestaSim
and exchanges traffic with the host over named pipes. The host provides a CLI,
a graphical device explorer, image capture and an automated validation campaign.
It can also run against a software virtual camera.

- **Faster simulation.** Moving the host side out of the simulator keeps the
  simulated design small. Long scenarios like full acquisition runs, register
  sweeps and multi-frame captures become practical to run routinely rather
  than only overnight.
- **A wider team.** People with Python or C++ experience can work directly on
  tests, golden models, host tools and result analysis. HDL specialists can
  focus on the RTL and its assertions.
- **Validate before hardware is available.** Exercise register access, camera
  configuration, acquisition and image reconstruction against the actual RTL.
- **Fast feedback for host software.** The virtual camera lets host-side
  changes be checked without RTL simulation at all. Scripted campaigns and
  repeatable logs shorten diagnosis and reruns.
- **Reuse the environment for real hardware.** The same host workflows, protocol
  checks and applicable validation cases can be used with a physical camera
  through a hardware transport bridge. The repository currently supplies the
  named-pipe transport for RTL and virtual cameras; hardware validation requires
  that bridge and access to any additional bench signals a case needs.

## Folder structure

```text
.
├── Makefile                 Common build, simulation and regression commands
├── README.md                Project overview and documentation entry point
├── src/
│   ├── rtl/                 SystemVerilog IP and shared file list (cxp_ip.f)
│   │   ├── top/             Device and interface wrappers
│   │   ├── app/             Pixel input, acquisition and image formatting
│   │   ├── rx/              Low-speed uplink receiver and packet parsing
│   │   ├── ctrl/            Control commands, bootstrap registers and APB bridge
│   │   ├── tx/              Packet generation, arbitration and insertion
│   │   ├── cdc/             Clock crossings, stream FIFO and reset sequencing
│   │   ├── pkg/             Protocol constants, types and utilities
│   │   ├── gen/             Generated register package and GenICam XML ROM
│   │   └── lib/             Shared RTL building blocks
│   ├── regmap/              YAML register map, generators and GenICam XML
│   ├── model/cxp_protocol/  Golden Python protocol model and reference vectors
│   ├── sva/                 Bound SystemVerilog assertions
│   ├── tb_unit/             Cocotb unit benches, grouped like the RTL
│   ├── verif/
│   │   ├── common/          Shared simulation helpers and host models
│   │   └── uvm/             PyUVM agents, scoreboards, coverage and tests
│   └── emu/
│       ├── bridge/          RTL simulation wrapper and DPI-C transport bridge
│       └── host/            C++/Qt host, CLI, GUI, virtual camera and validation
├── tools/                   Regression gates, report generators and emu runners
├── docs/                    Specification, design, verification, style and reviews

```

## Documentation

### Design and integration

- [RTL guide](src/rtl/README.md)  
- [Interactive RTL map](docs/design/cxp_rtl_map.html).
- [Module list](docs/design/modules_list.md)
- [Register-map guide](src/regmap/README.md)  
- [Register table](docs/design/modules/pkg/cxp_regmap.md).

### Verification

- [Unit Test benches](src/tb_unit/README.md)  
- [PyUVM guide](src/verif/README.md)  
- [Verification plan](docs/verification/cxp_verification_plan.md)  
- [Golden protocol model](src/model/cxp_protocol/README.md)  

### Emulation

- [RTL bridge](src/emu/bridge/README.md)  
- [Host application](src/emu/host/README.md)
- [Validation catalogue](src/emu/host/validation/README.md)  
- [Interactive emulator guide](docs/emulation/cxp_emu_guide.html)  

### Tools and project records

- [Tools guide](tools/README.md) 


## Getting started

Run from the repository root:

```bash
make help                       # targets and options
make lint                       # RTL lint with assertions bound
make unit TB=tx_arbiter          # one unit bench
make pyuvm TEST=test_stream_tpg  # one PyUVM test
make emu-build                  # build the RTL bridge and host
make emu-cli                    # discover the RTL camera
make ci                         # standard repository checks
```

The simulation flows use Verilator, Python, cocotb and pyuvm. The emulator host
also needs CMake, a C++17 compiler and Qt. See [Tool versions](#tool-versions)
and the component guides for setup details.

## RTL — `src/rtl`

`cxp_device_top` is the integration boundary: register file, reset controller
and `cxp_interface_top`. All build and verification flows share `cxp_ip.f`.
Every module is listed in the [module list](docs/design/modules_list.md).

| Block | Clock domain | Role |
|---|---|---|
| `cxp_rx_domain` | `rx_clk` | Uplink sampling, 8B/10B decoding, packet parsing and control commands |
| `cxp_ctrl_bootstrap_regs` | `rx_clk` | Bootstrap registers, camera features and GenICam XML ROM |
| `cxp_app_domain` | `app_clk` | Sensor or test-pattern input, acquisition, pixel packing and image markers |
| `cxp_cdc_layer` | Cross-domain | Configuration, events, responses and app-to-tx stream FIFO |
| `cxp_tx_domain` | `tx_clk` | Stream/control/test packets, triggers, I/O acknowledgments and scheduling |

Key parameters select asynchronous clock crossings (`p_ASYNC_CLOCKS`), uplink
oversampling (`p_OS_RATIO`), FIFO depth, test-pattern geometry and the optional
APB user-register window. The downlink interface is a 32-bit word plus a 4-bit
K-mask. Downlink 8B/10B encoding and the SerDes belong to the surrounding hardware;
high-speed uplink and multi-link operation are outside the current IP scope.

```bash
make lint                       # device/interface tops, async build and style example
make style                      # enforce RTL coding conventions
```

## Register map — `src/regmap`

`cxp_regmap.yaml` defines register addresses, access rules, reset values and
limits. The generator produces the SV package, Python models, C header,
register documentation and GenICam XML ROM image, and checks XML consistency.
The hand-written register file's `ROWS` table and `value_ok()` must also stay
in step with the YAML.

```bash
make regmap                     # regenerate after a register-map change
make regmap-check               # detect stale generated files
```

## Golden protocol model — `src/model/cxp_protocol`

The Python reference model implements 8B/10B, CRC-32, packet codecs, pixel
packing and stream reconstruction against the specification. It supplies
reference behavior to the benches and scoreboards, plus vectors for the C/C++
ports. `quirks.DEVICE` records known RTL deviations from the specification.

Run `python3 -m pytest -q` from `src/model/cxp_protocol`, or `make emu` from the
repository root to include catalogue freshness and DPI 8B/10B vector checks.

## Assertions — `src/sva`

Bound assertions check packet ordering, arbitration, IDLE spacing, FIFO safety,
control-command completion and reset sequencing. They are compiled into the
unit, PyUVM and emulator flows and included in lint. Simulation assertion
failures stop the run (`--assert` in Verilator; `CXP_SVA_FATAL` in Questa).

## Unit testbenches — `src/tb_unit`

Cocotb benches cover individual blocks and top-level integration. They share
protocol models and helpers from `src/verif/common`, and gate on test results
and FSM state/transition coverage. Unit tests track both visited FSM states
and exercised state-to-state transitions, with coverage reports highlighting
any uncovered states or transitions.

```bash
make unit                       # all benches, gated
make unit-list                  # available bench names
make unit TB=tx_arbiter TEST=test_03  # filter tests within one bench
make unit TB=rx_link WAVE=vcd    # capture waveforms
make unit JOBS=4                # limit parallel benches
```

## PyUVM verification — `src/verif`

The closed-loop environment drives `cxp_device_top` with a reactive host,
sensor stimulus, triggers, an APB slave and clock/reset controls. Scoreboards
check control, registers, images, link behavior and trigger timing against
reference models. Functional coverage tracks the verification-plan goals.
Tests include feature, specification and concurrency scenarios.

```bash
make pyuvm                      # CI tier, gated
make pyuvm-list                 # available tests
make pyuvm TEST=test_stream_video SEED=1234  # reproducible individual run
make pyuvm TIER=nightly          # extended regression and coverage gate
make pyuvm TIER=weekly           # randomized seeds and longer scenarios
```

Per-test build settings live in `src/verif/Makefile`; results are archived under
`src/verif/00_test_results/<test>/`. Expected failures name open RTL findings.

## Emulator — `src/emu`

The bridge wraps `cxp_device_top` with three clock generators, asynchronous
crossings, a test-pattern generator or bench-driven pixel port, and controls
for trigger, reset and fault scenarios. DPI-C translates named-pipe traffic
into the serial uplink and reconstructs packets from the downlink.

The C++17/Qt host provides the `cxp` CLI, `cxp-gui`, protocol logging and the
validation campaign. Cases produce per-case logs and JSON verdicts; unsupported
capabilities are reported as NOT RUN. The regression gate tracks allowed skips
and expected failures separately.

```bash
make emu-build                  # bridge + host; EMU_GUI=OFF for CLI only
make emu-gui                    # RTL camera and device explorer
make emu-cli CMD="read 0x0000"  # a host command against RTL
make emu-cli CMD="stream-capture --frames 4 --out caps"
make emu-cli CMD="validate BOOT-002 CT-004 --log-dir logs"
make emu-cli DEV=vcam           # use the software virtual camera
make emu-rtl                    # run and gate the RTL validation campaign
```

## Waveforms

Simulation targets accept `WAVE=none|vcd|fsdb` (default: `none`). FSDB output
requires Verdi's `vcd2fsdb`, found through `VERDI_HOME` or `VCD2FSDB`.

| Flow | VCD location |
|---|---|
| Unit bench | `src/tb_unit/<group>/cxp_<bench>/dump.vcd` |
| PyUVM test | `src/verif/00_test_results/<test>/<test>.vcd` |
| RTL emulator | `src/emu/bridge/build/cxp_hw_env.vcd` |

## Regressions and CI

```bash
make ci                         # regmap check, lint, style, unit, PyUVM CI tier, emu checks
make nightly                    # CI plus PyUVM nightly tier and RTL emulation campaign
```

`make emu` checks the model, catalogue and DPI vectors; `make emu-rtl` runs the
host validation campaign against RTL. Avoid `make -n` on regression targets:
recursive makes can still execute work.

## Tools — `tools`

Tools gate test results, FSM coverage, coding style and emulator verdicts;
generate unit/PyUVM HTML reports; launch emulator sessions; and convert VCD to
FSDB. Use the root Makefile targets for routine work and the
[tools guide](tools/README.md) for individual scripts.

## Tool versions

The CI workflow pins Verilator 5.046, cocotb 2.0.1 and pyuvm 4.0.1. Python 3.12
is used by its standalone Python jobs. The emulator host requires CMake ≥ 3.16,
a C++17 compiler and Qt 6 or Qt 5.15. QuestaSim is an optional simulator; the
bridge guide documents its native DPI flow and the known cocotb/VPI limitation.
