# `src/regmap/` — the register map

`cxp_regmap.yaml` is the one register map of the IP and the only file to
edit to change a register, a limit, an enumeration entry, a GenICam feature
or the device identity. `gen_regmap.py` generates every other view of it,
the GenICam XML included.

| File | Role |
|------|------|
| `cxp_regmap.yaml` | Source: bootstrap rows (Table 45), use-case features (0x3000 slot), manufacturer window, XML ROM address, limits (`min` / `max` / `allowed` / `multiple`), GenICam metadata (`gc:` per row, `genicam:` block: identity, versions, GUID, category tree) |
| `gen_regmap.py` | Generator; `--check` fails if an output is stale |
| `genicam/cxp_camera.xml` | Generated: the GenICam description the device serves (XML ROM), GenApi schema 1.1; also the C++ host's default XML |
| `cxp_regmap.h` | Generated: addresses for C |
| `cxp_regmap.hpp` | Generated: the map for C++ (`namespace cxp::reg`): addresses, reset values, limits, strings, XML ROM constants and the row tables the virtual camera decodes with |
| `regmap.mk` | Make fragment: `CXP_XML_MEM`, the absolute path of the one ROM image, and the `-G` / `-g` flags that pass it to `p_XML_BLOB_MEM` |

Other generated outputs (listed in the YAML header):

* `src/rtl/gen/cxp_camera_xml.mem` — the XML ROM image (`$readmemh`), the
  same bytes as `genicam/cxp_camera.xml`
* `src/rtl/gen/cxp_regmap_pkg.sv` — addresses, reset values, strings, XML ROM size
* `src/model/cxp_protocol/cxp_protocol/regmap.py`, `regmodel.py` — Python views
* `docs/design/modules/pkg/cxp_regmap.md` — register table

```
make regmap          # python3 src/regmap/gen_regmap.py
make regmap-check    # gen_regmap.py --check + tools/check_regmap_literals.py   (CI)
make genicam-check   # python3 tools/check_genicam_xml.py                        (CI)
```

## Who reads what

```
src/regmap/cxp_regmap.yaml  ── make regmap ──┬─ genicam/cxp_camera.xml   host default XML ($CXP_XML overrides),
                                             │                           virtual camera's XML ROM
                                             ├─ rtl/gen/cxp_camera_xml.mem  $readmemh(p_XML_BLOB_MEM) by absolute
                                             │                           path: tb_unit, src/verif, the RTL bridge
                                             ├─ rtl/gen/cxp_regmap_pkg.sv   RTL register file, benches
                                             ├─ cxp_regmap.hpp / .h      src/emu/host (cxp::reg), C
                                             ├─ model/.../regmap.py, regmodel.py
                                             │                           tb_unit, src/verif, cxp_protocol.regref
                                             └─ docs/.../cxp_regmap.md
```

Nothing else writes an address down: `tools/check_regmap_literals.py`
(part of `make regmap-check`) fails on a number literal in the code of
`src/emu/host`, `src/verif`, `src/tb_unit` or `src/emu/bridge` that equals
a map address above the Support group (0x2000.., 0x3000.., 0x4000..,
0x6000, the manufacturer window, 0x90000000). Comments, docstrings and
strings may name addresses; real exceptions are in its commented `ALLOW`.

## Changing the map

Edit the YAML, run `make regmap`, commit the YAML with every file it
rewrote. A new `max:` for example moves the XML's `Max`, the ROM image,
its size in `XML_BLOB_BYTES` and the XmlUrl string, and every view above.
When the register file must follow (a new row, a new limit), update
`ROWS` / `value_ok()` in `cxp_ctrl_bootstrap_regs.sv` by hand.

## How the XML is made

`gen_regmap.py` builds the XML in memory before anything else, from the
rows that carry `gc:`:

* a row's `min` / `max` / `multiple` become the Integer's `Min` / `Max` /
  `Inc`; an `allowed` mapping (entry name → value) becomes the
  Enumeration; `access: WO` makes a `Command` writing 1 (§11.2.1.4-5);
* address, length, access mode, `pPort`, `Sign` and
  `Endianess` (BigEndian, §10.3.3) come from the row;
* `VendorName` / `ModelName` are `genicam.vendor` / `genicam.model`, which
  the DeviceVendorName / DeviceModelName rows also serve;
* `VersionGuid` is a UUIDv5 of the file content under `product_guid`.

The ROM image, `XML_BLOB_BYTES`, the XmlUrl string (`Local:cxp_camera.xml;
90000000;<size>`) and XmlVersion / XmlSchemaVersion all come from those
bytes and the `genicam:` block, so they cannot disagree. A structural
self-check then parses the result: every `pValue` / `pPort` / `pFeature` /
`pSelected` resolves, every feature hangs under Root, every register node
is a register of the map.

`make genicam-check` loads the file in the EMVA GenICam reference
implementation (`python3 -m pip install genicam`; a missing package fails
the gate). GenApi applies its own schema. The port is then connected
to the reference register model (`cxp_protocol.regref`), every feature is
read, and every Min / Max / Inc / entry is probed against the model's
Table 22 answers.

## The register file

The register file itself, `src/rtl/ctrl/cxp_ctrl_bootstrap_regs.sv`, is
hand-written on top of `cxp_regmap_pkg`: its `ROWS` table and `value_ok()`
must follow the YAML by hand. The YAML's `reg` / `out` / `in` / `nv` fields
name its signals and ports.
