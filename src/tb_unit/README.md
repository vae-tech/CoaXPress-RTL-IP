# Testbenches — `src/tb_unit`

Cocotb 2.x testbenches for the RTL in `src/rtl/`: one directory per bench,
grouped like the RTL (`app`, `cdc`, `tx`, `rx`, `ctrl`, `lib`, `top`).
The shared helpers are in `src/verif/common/`, the PyUVM environment in
`src/verif/uvm/`, the interactive hardware environment for the emulator host in
`src/emu/bridge/`.

```
src/
├── verif/common/                 # shared by every bench and by src/verif/uvm
│   ├── cocotb_sim.mk             # simulator selection, sources, --assert, PYTHONPATH
│   ├── cxp_testcase.py           # @cxp_test(): TESTCASE number + FSM coverage hooks
│   ├── fsm_coverage.py           # Python FSM state / arc coverage
│   ├── cxp_bus.py                # register-file driver (cxp_ctrl_bootstrap_regs bench)
│   ├── cxp_8b10b.py              # view of the golden 8B/10B encoder
│   ├── cxp_uplink.py             # serial uplink driver: golden 8B/10B, IDLE fill
│   ├── cxp_host.py               # golden CoaXPress host: commands, acks, stream reassembly
│   ├── cxp_reglog.sv             # bound register-access trace (opt-in)
│   └── wave.do                   # default QuestaSim wave setup
└── tb_unit/
    ├── Makefile                  # runs every <group>/cxp_*/ bench, then the HTML report
    ├── README.md                 # this file
    ├── <group>/cxp_<module>/     # one bench per RTL block, e.g. tx/cxp_tx_arbiter/
    │   ├── Makefile              #   TOPLEVEL + MODULE + include of cocotb_sim.mk
    │   ├── tb_cxp_<module>_top.sv #  thin SV wrapper (flattens arrays, sets parameters)
    │   └── test_cxp_<module>.py  #   the tests
    ├── cdc/cxp_cdc/              # the four CDC primitives, two unrelated clocks
    └── top/cxp_device_top/       # the whole IP, three unrelated clocks, golden host
```

The HTML report generator is `tools/gen_test_report.py`; it writes
`src/tb_unit/tb_unit_test_report.html`.

## Running

### From the repo root

```
make unit                                  # every bench, in parallel, gated (= make tb)
make unit-list                             # bench names
make unit TB=tx_arbiter                    # one bench
make unit TB=tx_arbiter TEST=test_03       # tests of one bench matching a regex
make unit TB=rx_link WAVE=vcd              # with a VCD (WAVE=fsdb converts with vcd2fsdb)
```

### Default — Verilator

```
cd src/tb_unit
make                          # runs every bench, then the HTML report
make -j8 -k                   # same, 8 benches in parallel, keep going on failure
make app_image_header        # one bench (directory name without cxp_)
make list                     # bench names
make clean                    # remove every sim_build
```

Inside one bench:

```
cd src/tb_unit/top/cxp_interface_top
make WAVES=0 COCOTB_TEST_FILTER=test_10_trigger_preempts_stream
```

`WAVES=0` is the default — no trace, so runs are fast and write nothing.
`WAVES=1` traces to VCD; `WAVES_FMT=fst` switches to FST. Verilator
compiles trace support into the model while cocotb passes `--trace` to that
model at run time, so the two settings must agree: a `sim_build` left by a
`WAVES=0` run makes the next `WAVES=1` run die before the simulation starts
("--trace requires the design to be built with trace support"), and make sees
an up-to-date build, so it never recovers on its own. `src/verif/common/cocotb_sim.mk`
therefore stamps `sim_build` with the settings it was built with and wipes it
when they change; the rebuild is announced on stdout. No `make clean` needed.

From the repo root, `make tb` runs the same set in parallel and fails if any
test failed (`tools/check_results.py` reads the `results.xml` files);
`make lint` lints `cxp_device_top` (also with `p_ASYNC_CLOCKS` = 1) and
`cxp_interface_top`; `make regmap-check` checks the generated register files;
`make ci` runs regmap-check → lint → style → tb → PyUVM ci tier → emu.

### Source lists

RTL files are listed once, in `src/rtl/cxp_ip.f` (packages first). `src/rtl/cxp_ip.mk`
turns it into `RTL_SOURCES`, and the bound checkers in `src/sva/cxp_sva.f` into
`SVA_SOURCES`. `src/verif/common/cocotb_sim.mk` compiles `RTL_SOURCES`, `SVA_SOURCES` and
every `tb_*.sv` in the bench directory, so a per-bench Makefile only sets
`TOPLEVEL` and `MODULE`. `src/tb_unit/Makefile` finds benches by globbing
`cxp_*/Makefile`. To add a bench, create the directory with those three files;
no list needs editing.

Verilator builds use `-Wall -Wno-fatal -Werror-USERERROR`: lint warnings do not
stop a bench (that is `make lint`'s job), but an elaboration-time `$error`
from a parameter guard does.

### Assertions

`src/sva/cxp_sva.sv` holds checkers that are bound into the RTL, not written
inside it, and every Verilator bench builds with `--assert`, so a failing
property stops the test:

The twelve checkers and what they guarantee are listed in
[`src/sva/README.md`](../sva/README.md).

A checker runs in every bench whose DUT contains the module it is bound to.
The same file is compiled by `src/verif/`, `src/emu/bridge` and `make lint`.

### Register-access trace

`src/verif/common/cxp_reglog.sv` binds two monitors into the control plane
(`cxp_ctrl_cmd_parser`: one line per received command; `cxp_ctrl_bus_master`:
one line per register-bus / APB access). It is not in the default source list, so nothing is
printed unless it is compiled: `src/emu/bridge` adds it with `make REGLOG=1`; in a
cocotb bench add it to `TB_SOURCES`.

### Reference models

Protocol knowledge — 8B/10B, CRC, packet and stream codecs, pixel packing,
register map — comes from the golden `cxp_protocol` package at the repo root
(`src/model/cxp_protocol`, put on `PYTHONPATH` by `src/verif/common/cocotb_sim.mk`). It
is written against the specification, not the RTL; where the RTL still
deviates, pass `q=cxp_protocol.DEVICE` so the deviation is named instead
of copied into the test. `cxp_protocol.regmap` is generated from
`src/regmap/cxp_regmap.yaml` together with the register file.

Device-level benches use two drivers built on it:

* `src/verif/common/cxp_uplink.py` — `Uplink` serialises CoaXPress characters onto
  `rx_serial` at `os_ratio` clocks per bit with the golden 8B/10B encoder
  (running disparity kept across calls) and fills the gaps with IDLE words,
  so the soft sampler keeps lock. It can corrupt a symbol or flip the
  disparity at a chosen position.
* `src/verif/common/cxp_host.py` — `Host` drives the uplink and splits the downlink
  word bus (`cxp_if_data`, `cxp_if_kmask`) with the golden deframer:
  `read`/`read1`/`write`/`write_ok` send control commands and wait for the
  acknowledgment (CRC-checked), `trigger` sends a host trigger,
  `link_up` waits for `sb_link_detected`; stream packets go to the golden
  reassembler (`images`, CRC / tag / DsizeP errors), and trigger, I/O-ack
  and connection-test packets are collected. `max_run` is the longest
  non-IDLE run seen.

### Multi-clock benches

* `cxp_cdc` — `cxp_cdc_sync`, `cxp_cdc_pulse`, `cxp_cdc_bus`, `cxp_cdc_req`
  between a 10 ns source clock and a 7 ns or 13 ns destination clock; every
  test runs at both ratios.
* `cxp_device_top` — `cxp_device_top` (`cxp_interface_top` plus the
  register file) built with `p_ASYNC_CLOCKS` = 1 and run at `rx_clk` 10 ns,
  `tx_clk` 8 ns, `app_clk` 12 ns with the three resets released at different
  times. All stimulus goes through the serial uplink from `Host`, so every
  clock crossing carries real traffic: discovery reads, register writes, the
  TPG stream, ConnectionReset, TestMode, a host trigger.

`cxp_cdc_stream_fifo` and `cxp_stream_top` also run separate `app_clk` and
`tx_clk` for the gray-code stream FIFO. All other benches run one clock, or
tie their clocks together (`cxp_interface_top` ties all three).

### GenICam XML image

The register file loads the one ROM image, `src/rtl/gen/cxp_camera_xml.mem`
(written by `make regmap` together with the XML), by absolute path: the bench
top forwards `p_XML_BLOB_MEM`, and a bench Makefile that sets
`CXP_XML_ROM := 1` gets the `-G` / `-g` flag from `src/regmap/regmap.mk`
through `cocotb_sim.mk` (`cxp_ctrl_bootstrap_regs`, `cxp_device_top`,
`cxp_interface_top`; `src/verif/` the same way). No bench copies it.

### QuestaSim / ModelSim

```
make SIM=vsim                 # alias of SIM=questa, also accepts SIM=questasim/modelsim
make gui_interface_top        # one bench in the GUI with waves loaded
```

Per bench:

```
cd src/tb_unit/ctrl/cxp_ctrl_bootstrap_regs
make
make SIM=vsim
make gui
make clean
```

## Test inventory

Counts are the `test_*` functions of each bench (2026-09-30): 29 benches, 406 tests.

| Group | Bench | Tests | DUT / wrapper |
|---|---|---|---|
| `app` | `cxp_app_acq_ctrl` | 7 | acquisition control and pixel gate |
| `app` | `cxp_app_image_header` | 9 | image-header marker generator |
| `app` | `cxp_app_line_marker` | 7 | line-marker generator |
| `app` | `cxp_app_pixel_ingress` | 6 | sensor pixel ingress |
| `app` | `cxp_app_pixel_packer` | 18 | pixel → 32-bit word packer |
| `app` | `cxp_app_tpg` | 13 | test-pattern generator |
| `cdc` | `cxp_cdc` | 8 | the four CDC primitives (`sync`, `pulse`, `bus`, `req`), two unrelated clocks |
| `cdc` | `cxp_cdc_stream_fifo` | 13 | app→tx stream FIFO, two clocks |
| `ctrl` | `cxp_ctrl_bootstrap_regs` | 24 | register file, counters flattened to one link |
| `ctrl` | `cxp_ctrl_bus_master` | 15 | control-command executor (register bus, APB window, Wait, timeout) |
| `ctrl` | `cxp_ctrl_cmd_parser` | 20 | control-command parser |
| `ctrl` | `cxp_ctrl_plane` | 13 | control plane: parser + executor |
| `lib` | `cxp_lib_crc32` | 16 | CRC-32 engine |
| `rx` | `cxp_rx_8b10b_decoder` | 7 | 8B/10B decoder |
| `rx` | `cxp_rx_link` | 19 | uplink receiver (`OS_RATIO` 8, `BUF_DEPTH` 16) |
| `rx` | `cxp_rx_link_mon` | 10 | uplink link monitor (Detected / lost / resync) |
| `rx` | `cxp_rx_linktest` | 8 | connection-test receiver |
| `rx` | `cxp_rx_lspd_sampler` | 12 | low-speed oversampler |
| `rx` | `cxp_rx_packet_parser` | 12 | uplink packet parser |
| `rx` | `cxp_rx_trigger_lspd` | 13 | low-speed trigger reconstruction |
| `top` | `cxp_device_top` | 38 | whole IP, three unrelated clocks, `p_ASYNC_CLOCKS` = 1, golden host |
| `top` | `cxp_interface_top` | 16 | link + datapath, one clock, external register file behind an APB bridge |
| `top` | `cxp_stream_top` | 16 | stream pipeline: `cxp_app_stream` + stream FIFO + `cxp_tx_stream_pkt` |
| `tx` | `cxp_tx_arbiter` | 22 | `cxp_tx_arbiter` + `cxp_tx_inserter`, Python stand-in sources |
| `tx` | `cxp_tx_ctrl_ack` | 16 | control acknowledgment framer |
| `tx` | `cxp_tx_io_ack` | 9 | I/O acknowledgment source |
| `tx` | `cxp_tx_linktest` | 10 | connection-test packet source |
| `tx` | `cxp_tx_stream_pkt` | 17 | stream packet framer |
| `tx` | `cxp_tx_trigger_hs` | 13 | high-speed trigger source |

What each test proves, and what it does not, is described per module in
`docs/design/modules/<group>/cxp_<module>.md`, together with the known deviations from
CXP-001-2015 v1.1.1. `cxp_tx_pkt_framer`, `cxp_tx_short_pkt`, `cxp_tx_inserter`,
`cxp_app_marker_seq`, `cxp_app_stream`, `cxp_ctrl_apb_bridge`, `cxp_cdc_link`,
`cxp_cdc_reset`, the per-clock wrappers (`cxp_app_domain`, `cxp_tx_domain`,
`cxp_rx_domain`, `cxp_cdc_layer`) and `cxp_tx_domain`'s scheduler have no bench of
their own; they are exercised through the benches that contain them.

## FSM coverage

Verilator cannot run SystemVerilog `covergroup`s, so FSM coverage is
collected in Python by `src/verif/common/fsm_coverage.py`.  It samples a DUT's
`state_q` register on every rising clock edge and accumulates the states
and state-to-state transitions exercised across *all* tests in a bench
run.

A testbench opts in with a single call at import time:

```python
from fsm_coverage import register_fsm

register_fsm(
    name="rx_word_aligner",
    states=["ST_HUNT", "ST_ALIGNED"],               # enum, in declaration order
    state_path="cxp_rx_word_aligner_i.state_q",     # dotted path under `dut`
    clk_path="rx_clk",
    arcs=[("ST_HUNT", "ST_ALIGNED"),                # designed transition graph
          ("ST_ALIGNED", "ST_HUNT")],
)
```

`@cxp_test()` (in `src/verif/common/cxp_testcase.py`) does the rest — it starts a
sampler per registered FSM at the head of each test and flushes the
result.

Outputs:

* a compact per-FSM summary printed at the end of each bench run (states
  and transitions covered, uncovered states/arcs);
* `<bench>/fsm_coverage.json` (git-ignored) — picked up by
  `gen_test_report.py` and rendered as an **FSM coverage** panel in
  `tb_unit_test_report.html` (also generated, git-ignored): per FSM, a
  state-coverage bar + state chips and a transition-coverage bar + one
  chip per designed arc (green = exercised, red = never taken).

Reaching an internal `state_q` needs the signal visible to the VPI:
`src/verif/common/cocotb_sim.mk` adds Verilator's `--public-flat-rw` for this;
Questa exposes internals by default.  State coverage is a hard
percentage (the enum is the denominator).  Transition coverage is
`arcs_covered / arcs_total` against the `arcs=` designed transition
graph — the explicit branches of the FSM's next-state case statement.
An arc observed that is *not* in that graph (a global override, or a
genuine bug) is shown as a blue **extra** chip rather than counted
against the total.  States no RTL path can reach can be waived with
`register_fsm(..., unreachable=[...])`.

Status (2026-09-30): every FSM in `rtl` is registered — 11 benches,
13 registrations, 49 states (plus 1 waived) and 70 designed arcs, all
covered, no extra arcs.  The arc lists are the RTL's next-state logic including the
overrides written outside the case statement (the 0xFF in
`cxp_ctrl_bus_master`, `resync_i` in `cxp_rx_lspd_sampler`, the trailer
in `cxp_ctrl_cmd_parser`, `flush_i` in `cxp_rx_packet_parser`); keep them
in step when that logic changes.  The shared submodules are covered
through each wrapper whose parameters give them different arcs:
`cxp_tx_pkt_framer` via `cxp_tx_ctrl_ack` (bare ack, empty read),
`cxp_tx_stream_pkt` and `cxp_tx_linktest` (no CRC: DATA -> EOP; its
`ST_CRC` is waived); `cxp_tx_short_pkt` via `cxp_tx_io_ack` (back-to-back
COD -> HDR) and `cxp_tx_trigger_hs` (`more_i` = 0, so that arc is not
listed).  Those registrations name the path through the instance
(`cxp_tx_ctrl_ack_i.cxp_tx_pkt_framer_i.state_q`).  `cxp_tx_arbiter`
names its numeric `owner_q` (0 none, i+1 = port i) `S_NONE` …
`S_STREAM`, and registers `cxp_tx_inserter`'s `short_q` as well.
`cxp_ctrl_bus_master`'s `ST_DRAIN` needs a register file slower than one
cycle (test 15); the benches' register file otherwise answers at once.

## TB / cocotb notes

* All benches target **cocotb 2.0+** (uses `unit=` keyword on `Timer` /
  `Clock`).
* `COCOTB_TEST_FILTER=<regex>` selects tests; for an alternation quote it
  twice: `COCOTB_TEST_FILTER='"a|b"'`.
* A value written right after `await RisingEdge(...)` is sampled by the DUT
  on the *next* edge, and two writes in the same time step collapse into the
  last one; sequence stimulus accordingly.
* The register-file slave's `rdata` is registered, so the bus driver
  in `src/verif/common/cxp_bus.py` samples in the `ReadOnly` phase and re-syncs
  via `NextTimeStep()` so callers may immediately drive other signals
  after a read returns.
* The image-header / line-marker monitors sample `m_word_data` etc.
  on the rising edge **without** a `ReadOnly` synchronisation — under
  Verilator and Questa this returns the value presented during the
  cycle ending at the edge (i.e. the data being accepted), which is the
  standard AXI-Stream-monitor convention.
* `cxp_ctrl_bootstrap_regs` exposes unpacked array ports
  (`test_err_count_i [p_NUM_LINKS]` and the 64-bit packet counts), which are
  not portable across cocotb back-ends.  `tb_cxp_ctrl_bootstrap_regs_top.sv`
  flattens them to single inputs (`p_NUM_LINKS` = 1) and sets
  `p_LINK_RESET_CLEAR_CYCLES` to 8 so the auto-clear timer runs in a
  handful of cycles.
* The wrappers declare a `TESTCASE` byte that `@cxp_test()` sets to the
  running test's number, so the test is visible in the waves.
