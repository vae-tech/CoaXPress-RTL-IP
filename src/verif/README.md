# `src/verif/` — top-level PyUVM verification

Closed-loop PyUVM environment on the product shell, `cxp_device_top`
(interface top plus register file), with Verilator 5.046 and cocotb 2.0.1.
The unit benches in `src/tb_unit/` hold each block (see its README); `src/verif/uvm/`
holds the device against a reactive host and a reference model of every
channel.  `src/verif/common/` (cocotb_sim.mk, host and uplink models, FSM coverage)
serves both.

```
src/verif/
├── Makefile               # PyUVM tiers, build knobs, gate and cov_gate
├── common/                # shared cocotb helpers (unit benches and PyUVM)
└── uvm/
    ├── sv/tb_cxp_top.sv   # cxp_device_top + a user-window APB slave (latency,
    │                      # PSLVERR, Wait) + register-bus / value taps
    ├── common/            # cxp_pkg, clocks, build knobs, seed, decisions (D1-D10)
    ├── agents/            # host_uplink (char-level serializer, ppm / jitter,
    │                      # insertion lane), host_ctrl (closed-loop host),
    │                      # tx_wire, video, cfg, io, apb_slave, reg_bus,
    │                      # sideband, clk_rst
    ├── scoreboards/       # control (RegRef model), reg, stream, link_protocol,
    │                      # link_state, link_error, link_reset, linktest,
    │                      # io_ack, rx_trigger, tx_trigger
    ├── coverage/          # model.py (cells, GOALS, NOT_REACHABLE), gate.py
    └── tests/             # all_tests.py, spec_tests.py (T-01..T-26),
                           # conc_tests.py (C-01..C-16), base_test.py, env.py
```

## Documentation

* [`docs/verification/cxp_verification_plan.md`](../../docs/verification/cxp_verification_plan.md)
  — every test (VP IDs) with description, stimulus and pass / fail criteria,
  the common criteria, coverage goals, traceability and open items.
* [`docs/verification/cxp_uvm_map.html`](../../docs/verification/cxp_uvm_map.html)
  — interactive: component graphs and TLM wiring, harness ports, test
  catalogue, tiers, error kinds, coverage goals, decisions.  Regenerate with
  `python3 tools/gen_uvm_map.py` after changing the environment.

## Running

From the repo root:

```
make pyuvm                                 # ci tier, gated
make pyuvm TEST=test_stream_video          # one test
make pyuvm TEST=test_x SEED=1234           # one test, given seed
make pyuvm TIER=nightly                    # smoke | ci | feature | xifc | nightly | weekly
make pyuvm TEST=test_x WAVE=vcd            # waves (WAVE=fsdb converts with vcd2fsdb)
make pyuvm-list                            # test names
```

In this directory:

```
cd src/verif
make UVM_TESTNAME=test_stream_video run    # one test
make smoke                                 # 4 tests
make ci                                    # smoke + read, write, ConnectionReset, TestMode (8)
make nightly                               # 74 tests, then gate and cov_gate
make weekly                                # nightly with random seeds + soak + full ratio matrix
make cov_gate TIER_TESTS="..."             # coverage goals over archived results
```

* `WAVES=0` for tiers (`/mnt/c` is nearly full); a WAVES change needs a
  fresh `sim_build*`.  Never `make -n` a tier: it runs it.
* Build knobs (`OS_RATIO`, `RX_CLK_KHZ`, `RX_LOSS_WORDS`, `FIFO_DEPTH`)
  select their own `sim_build_<cfg>`; `KNOBS_<test>` in the Makefile sets
  them per test (OS_RATIO 4 for the link-test and IDLE-limit tests and the
  OS 4 jitter pair, FIFO_DEPTH 256 for back-pressure).
* One seed, `CXP_SEED` (default 1), drives every random stream; each
  results.xml records it.
* Results are archived per test under `00_test_results/<test>/`
  (results.xml, cov_summary.json, downlink dump).  `src/verif/cov_summary.json`
  is rewritten by every run: do not commit it.
* `make report` rolls the archive up into `src/verif/uvm_test_report.html`.

## Checks

* **Control**: `host_ctrl` sends one command at a time, resends after the
  200 ms timeout (device time), follows Wait acknowledgments.  The control
  scoreboard predicts every acknowledgment from the golden `RegRef`
  (Table 22 codes with decision D8 priority, D3 extension-connection rules,
  D7 serialisation) and matches each bus access to its command.
* **Stream**: every image golden-packed from the pixels driven or the TPG
  reference; header, line markers, DsizeL, PacketTag run, StreamPacketSizeMax,
  flushes at ConnectionReset and resets.
* **Link**: golden deframer on the downlink (framing, K-code lexicon,
  insertion never split, IDLE runs), LinkState (Detected up / drop / relock
  timing), LinkError (every error pulse explained by an injected error).
* **Trigger / I/O**: host trigger level and Delay law (latency less Delay
  constant), I/O acknowledgments per accepted packet, device trigger level
  and acknowledgment gating.
* A test fails on any scoreboard error.  An `EXPECT_FAIL` tag names an RTL
  finding and passes only while that error is present.

## Decisions

A check whose expected value is a choice reads it from
`uvm/common/decisions.py`; the table in
[`docs/verification/cxp_verification_plan.md`](../../docs/verification/cxp_verification_plan.md)
(§3.4) states each one.  An undecided constant (`None`) logs and does not gate.

## Coverage

`uvm/coverage/model.py` samples the cells at the end of each test (insertion
position x kind, IDLE runs, control op x region x size, codes, stall and
Wait timing, formats and widths, tags, uplink errors, link and reset events,
clock ratios, back-pressure).  `cov_gate` fails on any `GOALS` cell no test
of the tier hit; `NOT_REACHABLE` lists the cells left out and why.
