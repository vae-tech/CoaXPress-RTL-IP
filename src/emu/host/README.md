# `src/emu/host/` — CoaXPress host (C++17 / Qt)

A CoaXPress host in C++17: the protocol library (`libcxpcore`), the `cxp`
command-line tool, the `cxp-gui` Qt Widgets device explorer, a built-in
virtual camera and the validation campaign. It talks over two named pipes
(FIFO envelope, CRC / replication, Table 22 acknowledgments, §9.4 image
headers) to either its own virtual camera (`cxp sim`) or the RTL running in
`src/emu/bridge`.

## Build

From the repo root: `make emu-build` (this host with its GUI, plus the RTL
bridge; `EMU_GUI=OFF` for the CLI only). By hand:

Needs CMake ≥ 3.16, a C++17 compiler, and Qt 6 or Qt 5.15 (Core, Gui, Widgets, Test).

```bash
cmake -S . -B build
cmake --build build -j
ctest --test-dir build --output-on-failure
```

Options: `-DCXP_BUILD_GUI=OFF`, `-DCXP_BUILD_TESTS=OFF`,
`-DCXP_DEFAULT_XML=<path>` (the XML the virtual camera serves; `$CXP_XML`
overrides it at run time).

## Run

```bash
build/cxp discover       --spawn-sim --params
build/cxp read 0x0000    --spawn-sim
build/cxp write 0x10000 320 --spawn-sim
build/cxp stream-capture --spawn-sim --frames 4 --out caps
build/cxp image-extract  --spawn-sim --out frame.png
build/cxp compliance     --spawn-sim --inject-crc
build/cxp trace          --spawn-sim --pcap cxp.pcap --seconds 2
build/cxp xml-parse ../../regmap/genicam/cxp_camera.xml --tree
build/cxp validate       --spawn-sim --all --json results.json

build/cxp sim --fifo-dir /tmp/cxp        # terminal 1: virtual camera (Ctrl-C stops)
build/cxp-gui --fifo-dir /tmp/cxp        # terminal 2: device explorer
```

`--spawn-sim` runs the virtual camera in-process. It serves the generated
register map (`src/regmap/cxp_regmap.hpp`, `namespace cxp::reg`, on the
include path): identity, power-on values, the XML and the Table 22 answer
to every access match the RTL camera's, so `discover --params` prints the
same on both. Host code names registers through `cxp::reg` (or the
`Bootstrap::` / validation `Reg::` aliases of it), never by number
(`make regmap-check`). Against the RTL camera,
from the repo root (the camera starts and stops with the command):

```bash
make emu-cli                             # cxp discover --params on the RTL
make emu-cli CMD="write 0x10000 320"     # any subcommand
make emu-gui                             # device explorer on the RTL
make emu-cli DEV=vcam CMD="read 0x0000"  # the same through the virtual camera
make emu-rtl                             # validation campaign on the RTL, gated
```

Global options
(`--fifo-dir`, `--log`, `--timeout`, `--protocol-log FILE`) can go anywhere on the command line.
`--protocol-log` writes every frame the host sends and receives to FILE, decoded and
timestamped. Control frames, and any packet that does not decode, are followed by their
raw words. Retries and ack timeouts appear as event lines between the frames.

## Validation plan

**Tools → Validation plan…** (Ctrl+Shift+V) in `cxp-gui` opens the validation
window. It lists the 166 cases of the catalogue: the 113 non-electrical test cases of
`src/emu/host/validation/cxp_camera_validation_plan.md` grouped by plan section,
22 emulator cases beyond the plan, and the 31 PyUVM tests of
`src/verif/uvm/tests/all_tests.py` as `UVM-<test>` cases in their own section
(139 of the 166 are runnable over the FIFO link). The selector next to the filter shows **All** cases, only
the **Runnable** ones, or only the **UVM based** ones (remembered). Click a case to see its objective, requirements, procedure, and PASS/FAIL
criteria. The *Run on the emulator* box says what the check does here and how
it decides the verdict, or why the emulator cannot run the case. **Run
selected** (or a double-click) runs the chosen cases. **Run all** plays every
listed case one by one against the Explorer's current device. **Stop** ends
the run after the current step and restores any registers the check changed.
**Export results…** saves the session's verdicts and logs as JSON.

A GUI run is the same run as `cxp validate` (both are a `validation::Campaign`):
it writes the same per-case protocol logs and `results.json`, into a new
`<folder>/<date_time>` directory per run. **Logs…** chooses the folder (default
`validation_logs` in the working directory; remembered). **Soak** and **Waits**
are the `--soak` and `--timeout-scale` of the CLI; **Options…** holds the other
run options below. Hovering over any option or its label shows what it does,
which checks use it, its CLI flag, default and range. When the device stops
answering, the summary line says after which case.

`cxp validate` runs the same checks from the command line. It takes case IDs
or ID suffixes (`BOOT-002`, `test_crc_error`), `--all`, `--list`, `--json FILE`
and the run options, and exits with 2 if any case fails. `--runnable` and `--uvm`
narrow the chosen cases, or choose all such cases when no ID is given.

Run options (`validation::optionSpecs()`; `cxp --help` lists them with their
defaults, `validate --list` and every case log show the values in effect):

| Option | Default | What it sets |
|--------|---------|--------------|
| `--soak N` | 60 s (5 .. 86400) | PERF-003 soak duration |
| `--perf-seconds N` | 5 s | PERF-001 / PERF-002 measurement window |
| `--host-spsm N` | 4096 bytes | StreamPacketSizeMax the host programs to stream (whole packet, a multiple of 4); the largest SPSM the packet-size cases try |
| `--ack-timeout N` | 1000 ms | wait for the acknowledgment of a raw command a check sends |
| `--timeout-scale X` | 1.0 | multiplies every host-side wait (see Timeouts) |
| `--quiet-ms N` | 150 ms | a link without stream packets this long counts as quiet (scaled) |
| `--quiet-timeout N` | 3000 ms | longest wait for a quiet link after a stop (scaled) |
| `--first-image-timeout N` | 5000 ms | wait for the first image header after AcquisitionStart (scaled) |
| `--ack-latency N` | 200 ms | the plan's command-to-acknowledgment limit (INIT-004, CT-003, CTRL-003, CTRL-010, PERF-003); a verdict limit, never scaled, so relax it only for a slow simulator |
| `--seed N` | 0 | random seed of every check; 0 = each check's own default (each case logs the seed it used) |
| `--max-reported N` | 3 | FAIL lines a check prints per kind of item before it only counts |
| `--keep-logs N` | 10 | timestamped run directories kept under the log folder |

Two acknowledgment waits exist. `--ack-timeout` bounds the raw commands a
check builds itself (`Context::exchange`). The global `--timeout` (seconds) is
the session's own wait, used by register calls through the XML (Width,
AcquisitionStart, ...) and at connect. Inside a case both are multiplied by
the timeout scale.

```bash
build/cxp --fifo-dir /tmp/cxp validate CT-004 BOOT-002 --log-dir logs
build/cxp --fifo-dir /tmp/cxp validate --uvm --runnable --spawn-sim
```

* Each run writes to `--log-dir DIR` (default `validation_logs/<date_time>`;
  only the newest `--keep-logs` of those timestamped directories are kept).
  `DIR/<ID>.log` holds the case's protocol log: every frame, with the check's
  PASS/FAIL lines and the verdict in time order. `DIR/connect.log` holds the
  discovery at connect. `DIR/results.json` holds the verdicts (`--json`
  overrides the path).
* After each case the host reads Standard. If the device no longer answers,
  the run says after which case it stopped, because every later "no
  acknowledgment" comes from that one.
* Timeouts. `emulator.timeout_scale` in the catalogue multiplies every
  host-side wait of one case: ack, image, quiet link, settle time, and the
  pauses and recording windows a check makes (`Context::sleepMs`, `record`).
  It never changes a spec limit that the check measures, nor stimulus timing
  (random delays, trigger spacing) or a window whose length is the
  measurement (the PERF windows): those use `sleepRawMs`. It is 1.0 unless
  the case needs longer against the RTL simulation (`src/emu/bridge`). Host test
  packets cross the serial uplink bit by bit, so the link-test cases (CT-004,
  CT-005 and four UVM mirrors) get a scale computed from their `test_packets`
  parameter (`TEST_PACKET_S` per packet); the others are set in
  `TIMEOUT_SCALE` in `gen_validation_cases.py`. `--timeout-scale X`
  multiplies it again for the whole run, for example for a slower simulator
  build. `validate --list` shows every scale other than 1.

* Case parameters. The stimulus counts, lists and durations of a check (trials,
  images, packets, `test_packets`, `spsm_list`, `roi_list`, patterns, ...) are
  its case's `emulator.params` in the catalogue. `validate --list` prints them
  under each case and every case log starts with them. The check reads them
  with `Context::iparam()`, `ilist()`, `spsmList()` (where `host_spsm` stands
  for `--host-spsm`) and friends; the generator renders the catalogue prose
  from the same values (`{trials}`), so each number exists once. Change one in
  `gen_validation_cases.py` and regenerate. The `validation_params_declared`
  test (`validation/check_params.py`) fails when a check reads a key its case
  does not declare, or never reads one it does.
* The cases live in `validation/cxp_validation_cases.json`. Regenerate it with
  `python3 validation/gen_validation_cases.py` whenever the plan changes (the
  `validation_catalogue_fresh` test fails until you do). `$CXP_VALIDATION_JSON`
  loads a different file.
* 86 of the 113 plan cases run over the FIFO link. The other 27 need lab hardware,
  character-level access (8B/10B, IDLE), trigger packets, several connections,
  or a feature the reference camera does not have. Those cases show NOT RUN.
* UVM tests. The generator reads each test's docstring, the plan cases it
  serves (`PLAN`, `PLAN_PARTIAL`), its `EXPECT_FAIL` findings and its tiers
  from `src/verif/` (nothing there is run), so the catalogue goes stale when a test
  is added or retagged. `UVM_EMULATOR` says what `cases/uvm/` reproduces:
  the test's stimulus over the link, judged like its scoreboards (stream
  format, tags, framing and pixels against the golden model; acks against a
  register model; link-test counters), against CXP 1.1.1 like every other
  check: an `EXPECT_FAIL` tag does not relax it. All 31 run.
* The device's test bench (`src/cxp/protocol/bench.h`). What UVM drives
  besides the link, the checks ask of the device's bench over the same pipes:
  trigger input and output, the pixel port (with the arbitrary-image strap),
  the extension-link strap, a faulting register bus, the three clock periods
  and the host bit rate. Character frames (`src/cxp/protocol/chars.h`) carry
  what whole words cannot: Table 15 triggers, a trigger inside a command, and
  the downlink trigger and I/O-ack packets. The RTL bench (`src/emu/bridge`) has all
  of it; the in-process virtual camera all but the clocks. A device without
  what a case needs shows it NOT RUN with the reason.
* The checks (`src/cxp/validation/cases/<area>/<ID>.cpp`, one file per case,
  named by the ID it registers with `CXP_CHECK`) expect what CXP 1.1.1
  requires, not what the reference camera does. A FAIL against the in-process
  virtual camera is a real difference between that camera and the standard.
  Helpers every area uses are in `cases/_common.*`, those of one area in
  `cases/<area>/_helpers.*`. A new case file is picked up by the next build
  (CMake glob); the `validation_case_files` test fails when a file's name is
  not its ID, when it registers two cases or none, or when it sits outside its
  area's directory. `test_validation` takes its counts from the catalogue's
  `summary` block, so adding a case edits no test.
* Verdicts: PASS, FAIL (an expectation failed, or the device did not answer a
  plain access as a spec device must), NOT RUN, ERROR (host-side fault in the
  check), and STOPPED.

## Layout

| Path | Contents |
|---|---|
| `src/cxp/protocol/` | Wire format: constants, CRC, packets, character frames (`chars.*`), bench ops (`bench.h`) |
| `src/cxp/transport/fifo.*` | FIFO endpoint: envelope framing, reader / writer threads, resync |
| `src/cxp/parser/stream_parser.*` | Stream packet parser and image reassembly |
| `src/cxp/image/` | Pixel formats, frame reconstruction and saving, test patterns |
| `src/cxp/genicam/sfnc.*` | GenICam XML and SFNC features |
| `src/cxp/compliance/` | Compliance checker (`cxp compliance`) |
| `src/cxp/camera/` | `client.*` discovery and register access, `session.*` FIFO pair + optional virtual camera (shared by CLI, GUI, tests), `protocol_log.*` (`--protocol-log`) |
| `src/cxp/sim/virtual_camera.*` | Built-in virtual camera (`cxp sim`, `--spawn-sim`) |
| `src/cxp/utils/` | Logging, hex dump, pcap writer (`cxp trace`) |
| `src/cxp/validation/` | Validation runner (target `cxpvalidation`): catalogue, `Context`, `runCase`, `Campaign`, `cases/<area>/<ID>.cpp` |
| `apps/cli/main.cpp` | `cxp` command-line tool |
| `apps/gui/` | `cxp-gui`: explorer, image, statistics and validation windows |
| `tests/test_*.cpp` | QtTest suites (`ctest --test-dir build`) |
| `validation/` | Catalogue generator and checkers ([README](validation/README.md)) |

## Implementation notes

* **Threads.** Each `FifoEndpoint` has a reader thread and a writer
  thread. `CameraControl` has a receive-dispatch thread and blocking,
  mutex-serialised register calls. The GUI runs device I/O on a one-slot
  worker (`IoBridge`) and feeds stream traffic on a per-device drain thread
  (`LinkStats`).
* `StreamParser::feedFrame` returns every frame completed by a packet, so
  two tiny frames closing inside one packet are both delivered.
* The transport reader doesn't spin on repeated EOF while no writer is
  attached. `reopens` counts only sessions that actually carried data.
* The virtual camera streams an unknown `PixelFormat` code as Mono8 with
  a warning.
* `ReconstructedFrame::save` writes `.png` / `.bmp` / `.jpg` through
  QImage, so it needs no external image library. Other extensions fall
  back to Netpbm.
* The GUI register tree shows values from the discovery sweep. It never
  does register I/O on the GUI thread; use **Read** to refresh a value.
