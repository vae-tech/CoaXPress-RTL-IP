# cxp_protocol — golden CoaXPress 1.1.1 model

One Python package with the protocol knowledge that the testbenches, the
PyUVM environment and the emulator used to carry in four separate copies.
It is written against CXP-001-2015 and IEEE 802.3 Clause 36 and tested
against their vectors (`tests/`), never against the RTL.

| Module | Contents |
|---|---|
| `kcodes` | K-characters, IDLE word, lane helpers, 3-of-4 vote |
| `enc8b10b` | IEEE 8B/10B encoder (with the D.x.A7 rule) and table-inverse decoder |
| `crc` | §8.2.2.2 CRC-32: register, wire word (`crc_wire`) |
| `packets` | Tables 15–23: trigger (LS characters / HS words), I/O ack, control command, acknowledgment, stream packet, connection test; downlink `Deframer`; uplink `UplinkReceiver` (character-level trigger extraction) |
| `stream` | §9.4: PixelF descriptors (Table 25/26), pixel packing (Figures 27–31), image headers and line markers (Tables 38–41), `StreamReassembler` |
| `quirks` | Named deviations of the RTL from the spec; `DEVICE` is what `rtl` does today |
| `vectors` | Golden vectors for the C/C++ ports (`python -m cxp_protocol.vectors`) |

## Quirks

Every codec takes `q=` (default `SPEC`).  Where the RTL does something
other than the specification on the wire, the difference is a flag in
`quirks.Quirks` and part of `quirks.DEVICE`.  Consumers that talk to the
RTL use `DEVICE`; an RTL fix removes its flag, and the consumers follow
without further edits.

## Consumers

* `src/tb_unit` — `src/verif/common/cocotb_sim.mk` puts this directory on
  `PYTHONPATH`; `src/verif/common/cxp_8b10b.py` is a view of `enc8b10b`.
* `src/verif/` — the uplink agent builds its character stream here; the
  stream and control scoreboards decode with `packets` / `stream`.
* `src/regmap/gen_regmap.py` — writes `regmap.py` / `regmodel.py` here from
  `src/regmap/cxp_regmap.yaml`.
* C / C++ — `src/emu/bridge/dpi/cxp_8b10b.h` is checked with
  `make -C src/emu/bridge check_8b10b`; the src/emu/host codecs with the
  `test_golden_vectors` ctest.

## Tests

    cd src/model/cxp_protocol && python3 -m pytest -q
    make emu                                    # repo root: the same, plus the catalogue check and DPI vectors
