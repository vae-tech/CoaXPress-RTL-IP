#!/usr/bin/env python3
"""Generate cxp_validation_cases.json from the validation plan.

    python3 gen_validation_cases.py            # write the catalogue
    python3 gen_validation_cases.py --check    # exit 1 if it is out of date

Every one of the plan's test cases (src/emu/host/validation/cxp_camera_validation_plan.md,
section 8 onward) becomes one entry holding the plan's own text: objective,
preconditions, equipment, procedure, stimulus, expected result, PASS and
FAIL criteria, evidence, and the requirements it verifies with their
wording.  EMULATOR below adds what the C++ host (src/emu/host) does with the
case over its FIFO link: either the exact procedure and PASS/FAIL criteria
of the check in src/cxp/validation/cases/<area>/<ID>.cpp, or why it cannot run.
Keep EMULATOR and the checks in lock-step.

EXTRA_CASES adds the cases the emulator runs beyond the plan, each a whole
entry of its own in the section EXTRA_SECTION: a finer variant of a plan case
(the plan ID with a letter suffix, CXP-CAM-NEG-007b), which inherits that
case's requirements and clauses, or an emulator-only case (CXP-EMU-<AREA>-1nn)
that names the plan cases whose requirements it verifies.

Every PyUVM test in src/verif/uvm/tests/all_tests.py becomes one more entry,
"UVM-<test>", in its own section: the test's docstring, the plan cases it
serves, its EXPECT_FAIL findings and regression tiers (read from src/verif/,
never run), and in UVM_EMULATOR what cases/uvm/UVM-<test>.cpp reproduces of it on the
emulator, with the device's test bench for what UVM drives besides the link.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
PLAN = HERE / "cxp_camera_validation_plan.md"
OUT = HERE / "cxp_validation_cases.json"

# ---------------------------------------------------------------------------
# Why a case cannot run on the emulator (FIFO word link, one connection,
# no trigger codec).  Hardware-class cases get HW_REASON unless listed.
# ---------------------------------------------------------------------------
HW_REASON = ("Hardware test: needs the lab equipment listed under Test equipment. The emulator link "
             "is a word-level FIFO with no electrical, connector, power or indicator layer.")
ELECTRICAL_CASES = {"CXP-CAM-PERF-005", "CXP-CAM-REC-001", "CXP-CAM-IOP-004"}
CHAR_LEVEL = ("The FIFO link carries 32-bit words after 8B/10B decoding, without IDLE words or "
              "K-character flags, so {what} cannot be observed or injected.")
NO_TRIGGER = ("This host has no trigger-packet codec: the FIFO envelope carries no LS/HS trigger "
              "packets and no I/O acknowledgments.")
SINGLE_LINK = ("The emulator link is a single connection, so extension-connection behaviour cannot "
               "be exercised.")
NO_HSUP = "Needs a high-speed upconnection (HSUP); the FIFO link has one uplink and no bit rate."

NOT_RUNNABLE = {
    "CXP-CAM-INIT-003": SINGLE_LINK,
    "CXP-CAM-INIT-005": SINGLE_LINK + " Topology permutations need several Host ports.",
    "CXP-CAM-INIT-006": ("A bit-rate change has no meaning on the FIFO link (no serial rate, no receiver "
                         "lock). ConnectionConfig register behaviour is covered by INIT-004, INIT-009 "
                         "and the tag reset by DATA-003."),
    "CXP-CAM-INIT-008": NO_HSUP,
    "CXP-CAM-PROT-001": CHAR_LEVEL.format(what="10-bit symbols and running disparity"),
    "CXP-CAM-PROT-003": CHAR_LEVEL.format(what="IDLE words and their spacing"),
    "CXP-CAM-CTRL-007": SINGLE_LINK,
    "CXP-CAM-NEG-009": CHAR_LEVEL.format(what="invalid 10-bit symbols and disparity errors"),
    "CXP-CAM-DATA-004": "Needs two or more streams (MSTREAM or MTAP); the reference camera sends one.",
    "CXP-CAM-ML-001": SINGLE_LINK + " Round-robin distribution needs several connections.",
    "CXP-CAM-IMG-006": "Conditional on a line-scan Device (LINESCAN); the reference camera is area scan.",
    "CXP-CAM-IMG-008": "Conditional on an interlaced Device (INTERLACED); the reference camera is progressive.",
    "CXP-CAM-PIX-004": "Conditional on a colour Device (COLOR); the reference camera's XML lists only Mono formats.",
    "CXP-CAM-TRIG-005": NO_TRIGGER + " It also needs an HS upconnection.",
    "CXP-CAM-GEN-008": ("Camera-functional test: needs the DUT datasheet (exposure, frame rate and trigger "
                        "tolerances, NOT PROVIDED) and trigger packets."),
    "CXP-CAM-BND-002": NO_TRIGGER,
    "CXP-CAM-REC-004": CHAR_LEVEL.format(what="loss of IDLE on the upconnection"),
    "CXP-CAM-REC-005": NO_HSUP,
    "CXP-CAM-IOP-002": ("Needs an independent GenTL consumer; the only GenICam client here is this host's "
                        "own node map (see GEN-002)."),
}


# ---------------------------------------------------------------------------
# emulator.params: the stimulus counts, lists and durations of a check.  The
# check reads them (Context::iparam("trials"), ...) and the prose below names
# them as {trials}, {test_packets.clean}, ..., rendered from the same values,
# so every number exists once.  validation/check_params.py (a ctest) fails
# when a check reads a key its case does not declare, or never reads one it
# does.  Numbers a check shares with the standard (Table 45 lengths, the
# §10.1.2 200 ms) stay in the text; run options (--host-spsm, ...) are named,
# never repeated.
# ---------------------------------------------------------------------------
# The StreamPacketSizeMax a host programs to stream: Options::host_spsm.  In
# an spsm_list the string "host_spsm" stands for it.
HOST_SPSM = "the host maximum (--host-spsm)"
# Keys whose numbers read as hex (addresses, register patterns).
# Keys whose numbers read as hex (addresses, register patterns), with their
# least number of digits.
HEX_KEYS = {"unused_addresses": 4, "values": 8}
PLACEHOLDER = re.compile(r"\{([a-z_0-9]+(?:\.[a-z_0-9]+)?)\}")


def fmt_param(key: str, v) -> str:
    if isinstance(v, dict):
        raise SystemExit(f"{{{key}}}: a group renders only through its members ({{{key}.<name>}})")
    if isinstance(v, str):
        return HOST_SPSM if v == "host_spsm" else v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if isinstance(v, float):
            return f"{v:g}"
        digits = HEX_KEYS.get(key.split(".")[0], 4 if v >= 0x100000 else 0)
        if digits:
            return f"0x{v:0{max(digits, 4 if v <= 0xFFFF else 8)}X}"
        return str(v)
    if isinstance(v, list):
        if key.endswith("_range"):
            return f"{fmt_param(key, v[0])}..{fmt_param(key, v[1])}"
        if key == "roi_list":
            items = [f"{w}x{h}" + (f" at offset ({x},{y})" if x or y else "") for w, h, x, y in v]
        else:
            items = [fmt_param(key, x) for x in v]
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
    raise SystemExit(f"{{{key}}}: cannot render {v!r}")


def render(text: str, params: dict) -> str:
    def one(m: re.Match) -> str:
        key = m.group(1)
        head, _, member = key.partition(".")
        if head not in params or (member and member not in params[head]):
            raise SystemExit(f"prose names {{{key}}}, which the case's params do not declare: {text[:80]!r}")
        return fmt_param(key, params[head][member] if member else params[head])
    return PLACEHOLDER.sub(one, text)


def as_json(key: str, v):
    """A param as the catalogue stores it: hex numbers as "0x..." strings (Context parses them)."""
    if isinstance(v, dict):
        return {m: as_json(f"{key}.{m}", x) for m, x in v.items()}
    if isinstance(v, list):
        return [as_json(key, x) for x in v]
    if isinstance(v, int) and not isinstance(v, bool) and (key.split(".")[0] in HEX_KEYS or v >= 0x100000):
        return fmt_param(key, v)
    return v


def E(scope: str, procedure: list[str], pass_: str, fail: str, params: dict | None = None) -> dict:
    params = params or {}
    r = lambda t: render(t, params)  # noqa: E731
    e = {"runnable": True, "scope": r(scope), "procedure": [r(p) for p in procedure],
         "pass_criteria": r(pass_), "fail_criteria": r(fail)}
    if params:
        e["params"] = {k: as_json(k, v) for k, v in params.items()}
    return e


RESTORE = "Registers the check changes are restored afterwards."
BENCH = "Needs the device's test bench ({what}); a device without it shows the case NOT RUN."
# The bench's own clock (CAP_TIMES): on a simulator every millisecond a case
# judges is the device's, not the host's.
BENCH_TIME = ("Times are the bench's (the device's milliseconds, from the time stamps of the packets on "
              "both links), not the host's wall clock.")
# Bootstrap addresses Table 45 leaves unused (BOOT-001, NEG-002).
UNUSED_BOOTSTRAP = [0x0020, 0x1FFC, 0x20D0, 0x2FFC]
# The plan's command-to-acknowledgment limit: Options::ack_latency_ms, a
# verdict limit that --timeout-scale does not touch.
LATENCY = "the plan's latency limit (--ack-latency)"

# emulator.timeout_scale: multiplies every host-side wait of the check (ack,
# image, quiet link; never a spec limit it measures).  1.0 is the standard
# budget; a case listed here needs longer against the slow RTL simulation
# (src/emu/bridge).  The CLI --timeout-scale multiplies in on top.
# CT-004 and CT-005 are computed from their test_packets below the table.
TIMEOUT_SCALE: dict[str, float] = {
    # 20 / 50 default-size (640x480) test images: the RTL sim streams about
    # 1.5 images/s, slower than the image waits of BOOT-007, IMG-005 and
    # PERF-004 allow at x1.
    "CXP-CAM-BOOT-007": 3.0,
    "CXP-CAM-IMG-005": 3.0,
    "CXP-CAM-PERF-004": 4.0,
    # AcquisitionStop completes the image in flight: up to one 640x480
    # image (~0.7 s, more on a loaded host) before the 1.5 s quiet wait.
    "CXP-CAM-GEN-005": 3.0,
    # SPSM 36: one data word per packet, ~77k packets per image in 20 s.
    "CXP-CAM-BND-003": 3.0,
}

EMULATOR = {
    # -- link initialisation -------------------------------------------------
    "CXP-CAM-INIT-001": E(
        "Power-up through the bench's power-on reset: §10.3.28 has the device execute a connection reset at "
        "power-up, so no ConnectionReset is written. Rate, extension and internal-trigger observations are "
        "outside the FIFO link. Needs the device's test bench (power-on reset); a device without it shows "
        "the case NOT RUN.",
        ["Power-cycle the device through the bench (RESET, every input at 0) and wait for a quiet link.",
         "Read ConnectionReset, MasterHostConnectionID, StreamPacketSizeMax, TestMode, TestErrorCountSelector, "
         "TestErrorCount, XmlManifestSelector (4 bytes) and TestPacketCountTx/Rx (8 bytes).",
         "Read ConnectionConfig and HsUpconnection.",
         "Record the downlink for {idle_ms} ms without sending commands.",
         RESTORE],
        "Every listed register reads 0; ConnectionConfig = 1 connection at discovery code 0x28 or 0x38; "
        "HsUpconnection bits 31:1 = 0; no stream or test packet in the {idle_ms} ms recording.",
        "Any register differs from its reset value; ConnectionConfig is not a discovery configuration; a "
        "stream or test packet arrives; a read is not acknowledged with 0x00.",
        {"idle_ms": 500}),
    "CXP-CAM-INIT-002": E(
        "{trials} trials instead of 100; the rate fallback is judged by the ConnectionConfig value (no serial rate).",
        ["Program StreamPacketSizeMax if it is 0, start continuous acquisition, wait for an image header.",
         "Wait {reset_delay_range} ms, write ConnectionReset = 1 without waiting for an ack (t0), wait 200 ms.",
         "Read ConnectionReset, ConnectionConfig, StreamPacketSizeMax; keep recording for {after_ms} ms.",
         "Repeat {trials} times.", RESTORE],
        "In every trial: ConnectionReset = 0, ConnectionConfig = discovery configuration, "
        "StreamPacketSizeMax = 0, and no stream packet arrives later than t0 + 200 ms.",
        "Register not cleared, ConnectionConfig not at discovery, SPSM not 0, streaming continues past "
        "200 ms, or a read fails.",
        {"trials": 5, "reset_delay_range": [0, 20], "after_ms": 300}),
    "CXP-CAM-INIT-004": E(
        "The §10.1 Host order on one connection; rate locking steps collapse to register writes.",
        ["ConnectionReset, wait 200 ms.",
         "Read DeviceConnectionID; write MasterHostConnectionID = 1 and read it back.",
         "Read HsUpconnection and ControlPacketSizeMax; write StreamPacketSizeMax = " + HOST_SPSM + " "
         "and read it back.",
         "Read ConnectionConfigDefault; write it to ConnectionConfig and read it back.",
         "Start acquisition through the XML and receive {images} image(s).", RESTORE],
        "Every command answered 0x00/0x01 within " + LATENCY + "; DeviceConnectionID = 0; read-backs equal the "
        "written values; ControlPacketSizeMax >= 128 and a multiple of 4; ConnectionConfigDefault has a "
        "Table 46 speed code; the first image is complete with no CRC error.",
        "Any error ack, missing ack or ack later than " + LATENCY + ", a wrong read-back or ID, or no complete image.",
        {"images": 1}),
    "CXP-CAM-INIT-007": E(
        "The largest SPSM tried is " + HOST_SPSM + "; the SPSM = 0 write during streaming is not repeated.",
        ["Write StreamPacketSizeMax = 0, start acquisition, record {gate_ms} ms, stop.",
         "For each StreamPacketSizeMax of {spsm_list} bytes: write it, acquire {packets} stream packets, measure "
         "every stream packet from K27.7 to K29.7.", RESTORE],
        "No stream packet while SPSM = 0; for every SPSM value packets arrive and none is larger than SPSM bytes.",
        "A stream packet while SPSM = 0, a packet larger than SPSM, or no stream at a legal SPSM.",
        {"gate_ms": 1000, "spsm_list": [36, 40, 128, 1024, "host_spsm"], "packets": 2000}),
    "CXP-CAM-INIT-009": E(
        "The MMODE reprogramming and power cycle need the vendor mechanism and are not run.",
        ["Read ConnectionConfigDefault.",
         "Write it to ConnectionConfig and read ConnectionConfig back.", RESTORE],
        "Connection count >= 1, speed code in Table 46, write acknowledged 0x01, read-back equal.",
        "Invalid count or speed code, error ack, or a different read-back."),
    # -- bootstrap -----------------------------------------------------------------
    "CXP-CAM-BOOT-001": E(
        "Full Table 45 sweep (35 registers, ElectricalComplianceTest optional and skipped). ConnectionReset "
        "is not written (it resets the link; see INIT-002).",
        ["Read each register with Size = its length.",
         "Read-only registers: write the value just read, then read again.",
         "R/W registers: write a legal value (DeviceUserID 'CXPVALID-USERID', MasterHostConnectionID "
         "0x12345678, SPSM a quarter of " + HOST_SPSM + " (at least 36), selectors/TestMode/counters 0, ConnectionConfig its own value), read back, restore.",
         "Check HsUpconnection[31:1], XmlVersion[31:24], XmlSchemaVersion[31:24] are 0.",
         "Read unused addresses {unused_addresses} and record the ack.", RESTORE],
        "Every register answers 0x00 with Size = length, the right number of data words and a valid CRC; "
        "every read-only write answers 0x43 and leaves the value; every R/W write answers 0x01 and reads "
        "back; the reserved bits are 0. Unused-address acks are recorded only (clarification item).",
        "A missing register, wrong length, read-only register writable or not answering 0x43, R/W register "
        "not writable, non-zero reserved bits.",
        {"unused_addresses": UNUSED_BOOTSTRAP}),
    "CXP-CAM-BOOT-002": E(
        "As the plan.",
        ["Read Standard (0x0000, 4 bytes) and look at the first character on the wire (P0).",
         "Read Revision (0x0004)."],
        "P0 = 0xC0; Standard = 0xC0A79AE5; Revision = 0x00010001 (the plan's assumed v1.1.1 value).",
        "Any other value or byte order."),
    "CXP-CAM-BOOT-003": E(
        "Selectors 0..min(XmlManifestSize,{max_manifests})-1; a size above {max_manifests} is flagged as a probable byte count.",
        ["Read XmlManifestSize.",
         "For each selector: write it, read XmlVersion, XmlSchemaVersion and XmlUrlAddress, then read the "
         "URL string 4 bytes at a time until NUL (at most {max_url_bytes} bytes) and parse it.",
         "Write XmlManifestSelector = XmlManifestSize and read the selector back.", RESTORE],
        "Size >= 1; bits 31:24 of both versions 0; URL address >= 0x6000 and aligned; URL NUL-terminated and "
        "parses as Local:name;addr;len, Web: or File:; the out-of-range selector write is not acked 0x01 "
        "and the selector keeps its value.",
        "Size 0, bad version bits, URL address in bootstrap space, unparsable URL, or an accepted "
        "out-of-range selector.",
        {"max_manifests": 16, "max_url_bytes": 256}),
    "CXP-CAM-BOOT-004": E(
        "As the plan.",
        ["Read 0x2000 (32), 0x2020 (32), 0x2040 (48), 0x2070 (32), 0x20B0 (16) and 0x20C0 (16), one command each.",
         "Check the bytes up to NUL are ASCII 0x20-0x7E; record non-zero bytes after NUL.",
         "Write DeviceUserID with 15 characters + NUL, then 16 characters, reading back each time.",
         "Read Iidc2Address; if non-zero, read 4 bytes there.", RESTORE],
        "Each string read answers 0x00 with Size = length, is printable ASCII, and the five device strings "
        "are not empty; both DeviceUserID round trips are exact; a non-zero Iidc2Address is readable.",
        "Non-ASCII bytes, an empty mandatory string, a round-trip mismatch, or an unreadable IIDC2 space."),
    "CXP-CAM-BOOT-006": E(
        "Where the XML lacks the SFNC name the vendor alias is compared (TapGeometry, StreamId) with a warning.",
        ["Read WidthAddress .. Image1StreamIDAddress (0x3000-0x301C).",
         "Check each is >= 0x6000 and 4-byte aligned, compare it with the XML feature's register address, "
         "and read through it (not for AcquisitionStart/Stop).",
         "Read Image<n>StreamIDAddress for n = 2..16."],
        "All eight addresses in manufacturer space, aligned, equal to the XML register address and readable; "
        "Image<n>StreamIDAddress = 0 for every stream the XML does not declare.",
        "An address that is 0, in bootstrap space, misaligned, different from the XML, unreadable, or non-zero "
        "for an unsupported stream."),
    "CXP-CAM-BOOT-007": E(
        "Waits for {images} images (not 100) and accepts {min_images}: without the XML the host cannot select free-run mode.",
        ["Read the eight *Address registers; stop if any is 0.",
         "Read Width, Height, PixelFormat and Image1StreamID through them.",
         "Program StreamPacketSizeMax if 0; write 1 to the AcquisitionStart target; wait up to {acquire_timeout_ms} ms for {images} headers.",
         "Write 1 to the AcquisitionStop target; wait for a quiet link; record {after_ms} ms more.", RESTORE],
        "At least {min_images} complete images whose header Xsize/Ysize/PixelF/StreamID equal the values read; after the "
        "stop write the image in flight completes, no image begins and the link goes quiet (§11.2.1.5).",
        "A zero *Address, no stream, a header disagreeing with the registers, or streaming past the stop.",
        {"images": 20, "min_images": 10, "acquire_timeout_ms": 5000, "after_ms": 300}),
    "CXP-CAM-BOOT-008": E(
        "Registers at or above 0x6000 declared in the XML.",
        ["Enumerate XML registers with access RW or WO.",
         "Read each; record the ack of every WO register."],
        "Every RW register reads 0x00. Each WO register is reported as a warning (SHOULD, §10.3.3).",
        "An RW register that cannot be read. WO registers alone give warnings, not a failure."),
    # -- protocol ----------------------------------------------------------------------
    "CXP-CAM-PROT-002": E(
        "Word-level audit of this session's own traffic (reads of Standard, Revision and DeviceVendorName, "
        "one write, {images} images): K-code positions inside words cannot be seen without K flags.",
        ["Record the downlink during those reads, the write and a {images}-image acquisition.",
         "Check every frame starts with 4xK27.7, ends with 4xK29.7 and has a 4x replicated type.",
         "Check each type is 0x01, 0x03 or 0x04; count 0x02 and reserved types; note 0x05/0x06 host-stack "
         "extensions and SOP/EOP-valued words inside frame bodies."],
        "Every frame correctly framed; no type 0x02 or reserved type from the Device.",
        "A mis-framed packet, or a command/reserved type sent by the Device.",
        {"images": 2}),
    "CXP-CAM-PROT-004": E(
        "As the plan.",
        ["Read Standard; check P0 = 0xC0.",
         "Write MasterHostConnectionID = 0x11223344; read it back; check the wire characters P0..P3.",
         "Acquire {images} image(s); check DsizeP against the payload length and the header Xsize/Ysize against "
         "Width/Height.", RESTORE],
        "P0 = 0xC0; wire characters 11 22 33 44; DsizeP equals the payload word count in every packet; "
        "Xsize/Ysize equal the Width/Height features.",
        "Any field that only matches when decoded little-endian.",
        {"images": 1}),
    "CXP-CAM-PROT-006": E(
        "Command side only: the K27.7, type and K29.7 characters (the fields §8.2.2.1 replicates). "
        "Header words of this link are CRC-covered and trigger packets are not available.",
        ["For each of the 12 positions (SOP, TYPE, EOP x P0..P3) send a read of Standard with that one "
         "character corrupted, up to {repeats} times (a position that fails once is not repeated), each followed "
         "by a valid read; wait {ack_wait_ms} ms for each ack."],
        "All corrupted reads ({repeats} per position) return 0x00 with 0xC0A79AE5, and every following valid "
        "read succeeds.",
        "A read dropped, NACKed or answered wrongly because of one corrupted replicated character.",
        {"repeats": 3, "ack_wait_ms": 500}),
    "CXP-CAM-PROT-007": E(
        "CRC model is this host's crc.h (the project wire convention; see docs/design/modules/lib/cxp_lib_crc32.md for how it "
        "relates to the §8.2.2.2 worked example). Thousands of packets, not 10^6.",
        ["Read 4..104 bytes (step 4) from 0x2000 and recompute each ack CRC.",
         "Acquire {images} images and recompute the CRC of every stream packet, including those with K28.3 markers."],
        "No CRC mismatch in any read acknowledgment or stream packet.",
        "Any mismatch.",
        {"images": 3}),
    "CXP-CAM-PROT-010": E(
        "{commands} commands, then a ConnectionReset and a {silent_ms} ms silent period.",
        ["Send {commands} commands, writes and reads alternating; collect acks for {collect_ms} ms after each.",
         "Write ConnectionReset = 1; collect acks for {reset_collect_ms} ms.",
         "Record {silent_ms} ms without commands.", RESTORE],
        "Exactly one ack per command; at most one ack for the ConnectionReset write; no ack in the silent period.",
        "A missing, duplicate or unsolicited acknowledgment.",
        {"commands": 20, "collect_ms": 150, "reset_collect_ms": 400, "silent_ms": 1000}),
    # -- connection test ---------------------------------------------------------------
    "CXP-CAM-CT-001": E(
        "One connection (m = 0), {testmode_ms} ms of Test Mode.",
        ["Stop acquisition; write 0 to TestPacketCountTx (8 bytes).",
         "Write TestMode = 1; record {testmode_ms} ms; write TestMode = 0; wait {after_ms} ms.",
         "Compare every type-0x04 packet with Table 23; read TestPacketCountTx."],
        "At least one test packet; every packet is 1027 words with the 0x00..0xFF counting sequence; "
        "TestPacketCountTx equals the packets counted +-1.",
        "No test packet, a content error, or a counter mismatch.",
        {"testmode_ms": 1000, "after_ms": 200}),
    "CXP-CAM-CT-002": E(
        "The >= 16-word gap and the IDLE rule are not observable (no IDLE words on the FIFO link).",
        ["Start acquisition and wait for an image header.",
         "Write TestMode = 1; issue {reads} reads {read_gap_ms} ms apart; write TestMode = 0; stop acquisition.",
         "Count stream packets between {grace_ms} ms after the TestMode write and TestMode = 0."],
        "No stream packet in Test Mode ({grace_ms} ms grace), test packets present, all {reads} reads answered.",
        "A stream packet in Test Mode, no test packets, or an unanswered read.",
        {"reads": 5, "read_gap_ms": 100, "grace_ms": 20}),
    "CXP-CAM-CT-003": E(
        "{trials} exit trials instead of 100.",
        ["Write TestMode = 1; issue {reads} reads, each {read_gap_range} ms after the last, and measure ack "
         "latency; TestMode = 0.",
         "{trials} times: TestMode = 1, wait {on_range} ms, TestMode = 0, record {record_ms} ms."],
        "All {reads} reads answered within " + LATENCY + "; test packets recorded during the Test Mode periods, every one "
        "complete and correct; no test packet later than {grace_ms} ms after TestMode = 0.",
        "An unanswered or late read, no test packets at all, a truncated test packet, or test packets "
        "continuing after exit.",
        {"reads": 20, "read_gap_range": [0, 10], "trials": 5, "on_range": [5, 40], "record_ms": 400, "grace_ms": 100}),
    "CXP-CAM-CT-004": E(
        "Counts {test_packets.clean} clean then {test_packets.one_bad_word} + {test_packets.two_bad_words} "
        "corrupted packets (the plan: 1000 + 300).",
        ["Selector 0; write 0 to TestErrorCount and TestPacketCountRx; start acquisition.",
         "Send {test_packets.clean} Table 23 test packets; read both counters.",
         "Send {test_packets.one_bad_word} packets with 1 corrupted word and {test_packets.two_bad_words} with 2; "
         "read both counters.",
         "Check the stream is still running."],
        "Rx = {test_packets.clean} and errors = 0 after the clean packets; then Rx counts every packet sent and "
        "errors every corrupted word; the stream keeps running.",
        "Any miscount, packets ignored while TestMode = 0, or the stream disturbed.",
        {"test_packets": {"clean": 100, "one_bad_word": 10, "two_bad_words": 10}}),
    "CXP-CAM-CT-005": E(
        "Single connection: only m = 0 is valid (m = 1 too if HsUpconnection bit 0 is set).",
        ["Reset the counters; send {test_packets.first} test packets with 1 corrupted word each.",
         "Read selector, TestErrorCount and TestPacketCountRx.",
         "Write 0 to TestErrorCount; read both counters.",
         "Write out-of-range selectors (1 or 2, and 2); read the selector back.",
         "Send {test_packets.before_reset} more packets; ConnectionReset; read all counters and the selector.",
         RESTORE],
        "Selector 0; both counts {test_packets.first}; clearing TestErrorCount leaves Rx at {test_packets.first}; "
        "out-of-range selectors not acked 0x01 and the selector stays 0; after ConnectionReset every counter and "
        "the selector are 0.",
        "Wrong counts, a clear affecting the other counter, an accepted invalid selector, or counters "
        "surviving ConnectionReset.",
        {"test_packets": {"first": 5, "before_reset": 3}}),
    # -- control -------------------------------------------------------------------------
    "CXP-CAM-CTRL-001": E(
        "As the plan, at 0x2000 (vendor, model and manufacturer-info strings).",
        ["Read 104 bytes at 0x2000 as the reference.",
         "For B = 1..104: read B bytes; check code 0x00, Size = B, ceil(B/4) data words, zero padding, "
         "valid CRC and data equal to the reference."],
        "All 104 sizes correct.",
        "A wrong Size field (e.g. 4N instead of B), wrong word count, non-zero padding, CRC error or data mismatch."),
    "CXP-CAM-CTRL-002": E(
        "Target DeviceUserID (16 bytes). The non-zero-padding write is recorded, not judged.",
        ["For B = 1..16: fill DeviceUserID with 0x55, write B bytes of pattern with Size = B, read 16 bytes back.",
         "Check the ack is exactly K27.7x4, 0x03x4, 0x01x4, K29.7x4.",
         "Write 1 byte with non-zero padding and record the ack.", RESTORE],
        "Every write acked by the 4-word short form 0x01; the first B bytes hold the pattern and bytes B..15 "
        "still read 0x55.",
        "A long-form or wrong ack, or bytes beyond B changed."),
    "CXP-CAM-CTRL-003": E(
        "{reads_idle} reads (+{write_backs} write-backs) per register idle and {reads_streaming} reads under "
        "streaming; triggers not available. ConnectionReset, TestMode and TpgRun are read but not written.",
        ["For every Table 45 register and every XML register at or above 0x6000: read {reads_idle} times and "
         "write the value back after the first {write_backs} reads where writable; measure command-to-ack time.",
         "Start acquisition and read every register {reads_streaming} times."],
        "Every command acknowledged and the maximum latency within " + LATENCY + " in both passes.",
        "A missing final ack or any latency above " + LATENCY + ".",
        {"reads_idle": 5, "write_backs": 2, "reads_streaming": 3}),
    "CXP-CAM-CTRL-004": E(
        "The long operation is a register access to the bench's user window (0x20000) behind a slave that "
        "answers late (REG_STALL); {trials} trials per delay instead of 20. " + BENCH_TIME + " "
        + BENCH.format(what="REG_STALL and bench times"),
        ["With the slave answering at once: write and read back a user-window word (0x01, 0x00 with the value).",
         "For each slave delay of {stall_ms} ms, {trials} times: a write, then a read of the value written.",
         "With the slave at {slow_ms} ms: read Standard (bootstrap).",
         "With the slave at {slow_ms} ms, then with a slave that never answers: a read and a write of the "
         "user word; a bootstrap read; wait {late_window_ms} ms; read the user word back with the slave at once.",
         "With the slave answering PSLVERR: read the user word."],
        "Every access either ends with its final acknowledgment within 200 ms of the command, or gets exactly "
        "one Wait (0x04, Size 4, a good CRC, a value of 100..10000 ms) within 200 ms and then exactly one final "
        "acknowledgment within the time it gives; the accesses with a slave within that time end 0x01 / 0x00 "
        "with the value written; with a slower or hung slave the final acknowledgment is a Table 22 error, "
        "nothing arrives after it, the write it answered is not made and the next bootstrap read answers at "
        "once; the bootstrap read never gets a Wait; a slave error answers a Table 22 error.",
        "A missing, second or late Wait, a Wait value outside 100..10000, a missing or late final "
        "acknowledgment, a success for an access the slave never completed, an acknowledgment after the final "
        "one, a write made although answered with an error, a Wait on a bootstrap register, or success on a "
        "slave error.",
        {"stall_ms": [150, 250], "trials": 3, "slow_ms": 2000, "late_window_ms": 2500, "host_wait_ms": 60000}),
    "CXP-CAM-CTRL-005": E(
        "{passes} passes instead of 100.",
        ["Start acquisition.",
         "Read every Table 45 register {passes} times; write ConnectionConfig with its own value and the first "
         "word of DeviceUserID with its own value.",
         "Scan every ack for code 0x04."],
        "No wait acknowledgment.",
        "Any wait acknowledgment to a bootstrap address.",
        {"passes": 10}),
    "CXP-CAM-CTRL-006": E(
        "No long operation is declared, so the abort-after-wait scenario is not run.",
        ["Idle: send control channel reset (Cmd 0xFF, Size 0, Addr 0); then a read.",
         "Send a truncated read followed by a reset; then a read.",
         "Streaming: note ConnectionConfig, MasterHostConnectionID, SPSM; send a reset; record {after_ms} ms; "
         "compare registers and packet tags."],
        "Every reset answered 0x03 (short form); reads afterwards succeed; the registers are unchanged, "
        "stream packets keep arriving after the reset and their tags stay continuous.",
        "No 0x03, a failing read, reset registers, a stopped stream, or a tag restart (a Device reset).",
        {"after_ms": 300}),
    "CXP-CAM-CTRL-008": E(
        "The read target is the XML region; the maximum-size write needs a documented writable area "
        "that large and is not run.",
        ["Read ControlPacketSizeMax (CPSM); stop here if it is not >= 128 and a multiple of 4.",
         "Read CPSM-24 bytes; measure the ack packet size.",
         "Read CPSM-24+1 and CPSM-24+4 bytes.",
         "Write CPSM-20 bytes to DeviceUserID.",
         "Read 104 bytes at 0x2000."],
        "CPSM is usable; the CPSM-24 read answers 0x00 in a packet no larger than CPSM; the larger reads "
        "and the CPSM-20 write answer 0x45; the 104-byte read answers 0x00.",
        "An unusable CPSM (the boundary cases then cannot run), an ack larger than CPSM, a wrong code, or "
        "a refused 104-byte read."),
    "CXP-CAM-CTRL-009": E(
        "The CPSM-24 chunk size is not repeated (CTRL-008). A plain (not zipped) XML is read in word-sized "
        "chunks, since §10.3.2 only requires odd sizes for zipped XML.",
        ["Parse the XML URL (Local:).",
         "Read the whole file in chunks of {chunk_list} bytes.",
         "If zipped and its length is not a multiple of 4: read the final remainder alone."],
        "Every chunk answers 0x00 and the file images of every chunk size are byte-identical.",
        "A refused chunk or any content difference.",
        {"chunk_list": [4, 8, 100, 104]}),
    "CXP-CAM-CTRL-010": E(
        "{reads} reads instead of 10^4.",
        ["Start acquisition.",
         "Issue {reads} single-word reads {gap_us_range} us apart; record the latency distribution."],
        "Every read answered and the maximum latency within " + LATENCY + ".",
        "An unanswered read or a latency above " + LATENCY + ".",
        {"reads": 500, "gap_us_range": [0, 3000]}),
    # -- negative --------------------------------------------------------------------------
    "CXP-CAM-NEG-001": E(
        "Target MasterHostConnectionID.",
        ["Write 0xA5A5A5A5 to the target.",
         "Send 32 writes, each with one CRC bit flipped.",
         "Send a write with a payload bit flipped under the original CRC.",
         "Read the target; send a valid read.", RESTORE],
        "All 33 corrupted writes answer 0x80; the target still reads 0xA5A5A5A5; the valid read succeeds.",
        "A corrupted write executed, another ack code, no ack, or a failing valid read."),
    "CXP-CAM-NEG-002": E(
        "Addresses mapped on the device (XML registers, XML file, URL string) are skipped.",
        ["Read and write 4 bytes at {unused_addresses}.",
         "Read 8 bytes from HsUpconnection (0x403C) into the unmapped 0x4040.",
         "Read unaligned 0x0002 (recorded only).",
         "Read Standard."],
        "Every invalid read and write answers 0x40; the spanning read answers 0x40; Standard reads 0xC0A79AE5.",
        "Another code, a side effect, or a hang.",
        {"unused_addresses": UNUSED_BOOTSTRAP + [0x4040, 0x5FFC, 0x00FFFFF0, 0xFFFFFFFC]}),
    "CXP-CAM-NEG-003": E(
        "As the plan; XML features with Max or enumeration entries are included.",
        ["Write ConnectionConfig with speed codes 0x00, 0x29, 0x50, with 0 connections and with one more "
         "connection than the default.",
         "Write XmlManifestSelector = XmlManifestSize, TestErrorCountSelector = 2, TestMode = 2, "
         "ConnectionReset = 2 and StreamPacketSizeMax = 1025.",
         "For every RW manufacturer Integer with Max, write Max+1; for every RW Enumeration, write one "
         "above the largest entry.",
         "Read each register back after its write.", RESTORE],
        "Every invalid write answers 0x41 and the register keeps its value.",
        "An invalid value accepted, another code, or a changed register."),
    "CXP-CAM-NEG-004": E(
        "As the plan.",
        ["For Cmd = 0x02..0xFE send Size 4, Addr 0 with a valid CRC; after each, read Standard.",
         "Stop early if the first {give_up_after} codes all fail (a device that never answers would take 253 "
         "timeouts)."],
        "All 253 answer 0x42 and every following read succeeds.",
        "Any other outcome.",
        {"give_up_after": 8}),
    "CXP-CAM-NEG-005": E(
        "Write-only registers come from the XML (AccessMode WO).",
        ["Write every read-only Table 45 register with its own value; read it again.",
         "Read every XML register declared WO."],
        "Read-only writes answer 0x43 with the value unchanged; write-only reads answer 0x44.",
        "A wrong code or a changed value."),
    "CXP-CAM-NEG-006": E(
        "As the plan; target MasterHostConnectionID.",
        ["Read ControlPacketSizeMax (stop if it is not >= 128 and a multiple of 4), then read that many "
         "bytes (the ack would exceed CPSM).",
         "Write Size = 8 with 1 data word, Size = 8 with 3 words, Size = 4 with 2 words.",
         "Read the target.", RESTORE],
        "The read answers 0x45; the three writes answer 0x46; the target is unchanged.",
        "Execution or another code."),
    "CXP-CAM-NEG-007": E(
        "Acks to each malformed packet are recorded (their codes are a clarification item); the verdict "
        "rests on recovery and on no spurious execution.",
        ["A read without EOP followed by a valid read.",
         "A write truncated after its address, then {idle_ms} ms idle.",
         "A read whose type word has 2 of 4 characters corrupted.",
         "Size = 0 read, Size = 0 write, reset with Size = 4, reset with Addr = 0x10.",
         "After each: read Standard. Finally read MasterHostConnectionID."],
        "The read following the EOP-less packet is answered; every valid read afterwards returns "
        "0xC0A79AE5; MasterHostConnectionID unchanged.",
        "A wedge (no ack to a later valid command) or a spurious write.",
        {"idle_ms": 1000}),
    "CXP-CAM-NEG-008": E(
        "Types 0x00-0xFF except 0x02 (command) and 0x04 (test packet, legal from the Host). 0x06 is this "
        "host stack's heartbeat extension.",
        ["Send each type with {payload_words} payload words and EOP; then read Standard."],
        "Every following read returns 0xC0A79AE5.",
        "A wedge, crash or wrong answer.",
        {"payload_words": 4}),
    "CXP-CAM-NEG-010": E(
        "As the plan.",
        ["Send two reads back to back; collect acks for {collect_ms} ms.",
         "Send the same write twice back to back; collect acks for {collect_ms} ms; read the target.",
         "Read Standard.", RESTORE],
        "No more than one final ack per command; the target holds the written value; the channel responds.",
        "A duplicate or corrupted ack, or a wedge.",
        {"collect_ms": 500}),
    # -- data channel ---------------------------------------------------------------------
    "CXP-CAM-DATA-001": E(
        "SPSM {spsm_list}, {images} images each.",
        ["For each SPSM: acquire {images} images.",
         "Check every stream packet: SOP, 4x type, replicated StreamID/PacketTag/DsizeP, DsizeP = N, "
         "total = N + 8 words, valid CRC, EOP."],
        "No violation in any packet.",
        "Any mismatch.",
        {"spsm_list": [128, 1024, "host_spsm"], "images": 2}),
    "CXP-CAM-DATA-002": E(
        "SPSM = {spsm} bytes so {packets} packets arrive quickly and the tag wraps.",
        ["Write SPSM = {spsm}; acquire until {packets} stream packets.",
         "Check tag[k+1] = tag[k] + 1 mod 256 per StreamID; look for a 0xFF -> 0x00 wrap.", RESTORE],
        "At least {packets} packets, no discontinuity, wrap observed.",
        "A skipped, duplicated or reordered tag.",
        {"spsm": 64, "packets": 600}),
    "CXP-CAM-DATA-003": E(
        "As the plan.",
        ["Acquire {images} image(s); change Width by {width_step}; acquire {images} image(s).",
         "Write ConnectionConfig with its current value; acquire {images} image(s).",
         "ConnectionReset, program SPSM; acquire {images} image(s).", RESTORE],
        "The first tag after stop / Width change / start follows the last one; the first tag after the "
        "ConnectionConfig write and after ConnectionReset is 0.",
        "A tag reset on stop/start or ROI change, or no reset after ConnectionConfig / ConnectionReset.",
        {"images": 1, "width_step": 8}),
    "CXP-CAM-DATA-005": E(
        "Single stream; the power-cycle repetition is not run. The vendor StreamId feature stands in when "
        "Image1StreamID is missing (warning).",
        ["Read Image1StreamID (or StreamId).",
         "Acquire {images} images; collect packet StreamIDs and header StreamIDs."],
        "One StreamID in all packets, equal to the feature and to the header StreamID. A primary ID other "
        "than 0 is a warning (§9.3 SHOULD).",
        "Several IDs, or packet / header / feature disagree.",
        {"images": 3}),
    "CXP-CAM-DATA-006": E(
        "SPSM {spsm_list}. The bit-exact comparison needs a static test pattern (TestPattern = {pattern}).",
        ["Select TestPattern = {pattern} when available.",
         "For each SPSM: acquire {images} images and reassemble them from the marker walk.",
         "Compare the line data of the first complete image across SPSM values.", RESTORE],
        "At every SPSM an image reassembles with Ysize lines of ceil(Xsize x bpp / 32) words; the static "
        "pattern is bit-identical at every SPSM.",
        "An image that does not reassemble, or content that depends on the packet size.",
        {"spsm_list": [40, 128, 1024, "host_spsm"], "images": 2, "pattern": "Bars"}),
    # -- image -------------------------------------------------------------------------------
    "CXP-CAM-IMG-001": E(
        "{images} headers instead of 1000.",
        ["Acquire {images} images; walk the stream for K28.3 + 0x01 headers.",
         "Check all 25 words 4x replicated, Flags[7:2] = 0, Flags[1:0] != 3, PixelF a Table 25 code."],
        "Every header well formed.",
        "Any deviation.",
        {"images": 5}),
    "CXP-CAM-IMG-002": E(
        "{images} images; an image cut by the stop at the end of the recording is excluded.",
        ["Acquire {images} images.",
         "Count line markers per image and the words between markers."],
        "Every image has Ysize line markers and every line is DsizeL words long.",
        "A missing or extra marker, or lines of another length (e.g. DsizeL in bytes).",
        {"images": 5}),
    "CXP-CAM-IMG-003": E(
        "ROIs {roi_list} (a Width below the feature's minimum is raised to it); points the device refuses "
        "are noted and skipped.",
        ["For each ROI: set OffsetX/OffsetY/Width/Height through the XML; acquire {images} image(s).",
         "Compare Xsize/Ysize/Xoffs/Yoffs; compare DsizeL and the words per line with "
         "ceil(Xsize x bpp / 32).", RESTORE],
        "Header geometry equals the ROI; DsizeL and the words on the wire equal ceil(Xsize x bpp / 32).",
        "Any mismatch (e.g. DsizeL in bytes).",
        {"roi_list": [[8, 4, 0, 0], [37, 7, 0, 0], [30, 5, 0, 0], [64, 16, 0, 0], [40, 8, 12, 6]], "images": 1}),
    "CXP-CAM-IMG-004": E(
        "Width {test_width} for every Table 25 PixelFormat entry of the XML.",
        ["Set Width = {test_width}; for each PixelFormat: acquire {images} image(s).",
         "Check every line is ceil({test_width} x bpp / 32) words and the bits after the last pixel in the last word are 0 "
         "(pixels run MSB first from P0 bit 7, Figures 28-30).", RESTORE],
        "No line longer or shorter than that, and zero padding in every line.",
        "Packing across lines or non-zero padding bits (which also shows when the first pixel is not in P0).",
        {"test_width": 13, "images": 1}),
    "CXP-CAM-IMG-005": E(
        "{images} images; the wrap uses the vendor SourceTag preset when the XML offers one.",
        ["Acquire {images} images; check SourceTag increments by 1.",
         "If SourceTag is writable: preset 0xFFFE, acquire {wrap_images} images, look for 0xFFFF -> 0x0000.", RESTORE],
        "Increments by one throughout; wraps when the preset exists.",
        "A skip, repeat or constant SourceTag, or no wrap.",
        {"images": 20, "wrap_images": 4}),
    "CXP-CAM-IMG-009": E(
        "Needs a {pattern} (horizontal ramp) test pattern in {pixel_format}.",
        ["Select TestPattern = {pattern}, PixelFormat = {pixel_format}; acquire {images} images.",
         "Decode each line P0 first and count neighbouring pixels that increase by one.", RESTORE],
        "At least {min_percent} % of neighbouring pixels increase by one from left to right.",
        "Reversed or mixed order (a note says when the ramp only appears MSB-first, i.e. first pixel in P3).",
        {"pattern": "Gradient", "pixel_format": "Mono8", "images": 2, "min_percent": 90}),
    "CXP-CAM-IMG-010": E(
        "Single tap: the current tap geometry only; MTAP per-tap streams not applicable.",
        ["Read DeviceTapGeometry (or vendor TapGeometry).",
         "Acquire {images} images and decode TapG."],
        "Header TapG equals the feature value in every image.",
        "A different code.",
        {"images": 2}),
    "CXP-CAM-IMG-011": E(
        "Golden model: the emulator's copy of the reference TPG (renderTestPattern), {patterns} in "
        "{pixel_format}, {images} images each; the gradient is left out on purpose (it differs between models), and "
        "a listed pattern whose frames change (Gradient, Flat) is noted and not compared.",
        ["For {patterns}: acquire {images} images.",
         "Decode each line P0 first and compare with the golden pattern; count stream CRC errors.", RESTORE],
        "Every complete image bit-exact; no CRC error.",
        "Any mismatched pixel (a note says when the image only matches MSB-first) or a CRC error.",
        {"patterns": ["Bars", "GreyBars"], "pixel_format": "Mono8", "images": 10}),
    "CXP-CAM-IMG-012": E(
        "{trials} stops at {stop_delay_range} ms after the first header, instead of 100.",
        ["Start acquisition, wait for a header and {stop_delay_range} ms, write AcquisitionStop, wait for a quiet "
         "link, record {after_ms} ms.",
         "Check the last image and any later packets."],
        "In every trial the last image is complete (Ysize full lines) and no stream packet arrives later "
        "than {late_ms} ms after the stop.",
        "A truncated last image, a continuing stream or a stuck link.",
        {"trials": 10, "stop_delay_range": [0, 30], "after_ms": 300, "late_ms": 1000}),
    "CXP-CAM-PIX-001": E(
        "As the plan.",
        ["For every PixelFormat enumeration entry: check its name is in Table 25, set it, acquire {images} "
         "image(s), compare header PixelF with the Table 25 code.", RESTORE],
        "Every entry is a Table 25 name and yields its Table 25 PixelF.",
        "An unmapped name or a wrong code (e.g. Mono16 not 0x0105).",
        {"images": 1}),
    # -- GenICam ---------------------------------------------------------------------------------
    "CXP-CAM-GEN-001": E(
        "No XSD validator in this host: well-formedness and the GenApi root and version attributes only. "
        "A zipped XML is unzipped by this host and checked the same way.",
        ["Selector 0; read the URL and the whole file.",
         "If zipped: check every member uses STORE or DEFLATE; inflate the .xml member and check its CRC-32 and size.",
         "Parse the XML; check the root element and its version attributes.",
         "Compare XmlSchemaVersion and XmlVersion with the file's attributes.", RESTORE],
        "Well formed, root RegisterDescription, both version registers equal the file's attributes.",
        "Unretrievable or malformed XML, a zip with no readable .xml member (method, inflate, CRC-32), "
        "another root, or a version mismatch."),
    "CXP-CAM-GEN-002": E(
        "Loads with this host's GenApi subset, not the reference GenApi.",
        ["Read and load the XML.",
         "For SFNC names present, compare the interface type; check PixelFormat entries are PFNC names.",
         "Warn about vendor features duplicating SFNC/CXP ones (TapGeometry, StreamId, StreamPacketSize)."],
        "The XML loads; no SFNC type mismatch; every PixelFormat entry is a PFNC name.",
        "A load error or a type mismatch."),
    "CXP-CAM-GEN-003": E(
        "As the plan.",
        ["Check Width, Height (Integer, writable), AcquisitionMode (Enumeration), AcquisitionStart/Stop "
         "(Command), PixelFormat (Enumeration), DeviceTapGeometry (Enumeration), Image1StreamID (Integer), "
         "each with a 4-byte register.",
         "Check AcquisitionMode offers Continuous."],
        "8 of 8 present with the right type and access; Continuous offered.",
        "A missing feature or wrong type/access."),
    "CXP-CAM-GEN-004": E(
        "Matching is by register address.",
        ["For each Table 45 register marked X, find an XML node at its address; compare length and access.",
         "Flag XML nodes below 0x6000 at addresses that are not in Table 45."],
        "Every X register described with Table 45 length and access; no XML node at a non-Table-45 bootstrap address.",
        "A missing X register, a wrong length/access, or a wrong address."),
    "CXP-CAM-GEN-005": E(
        "{cycles} cycles instead of 100.",
        ["Check the CommandValue of AcquisitionStart/Stop is 1.",
         "{cycles} times: execute Start, wait up to {start_timeout_ms} ms for a stream packet, execute Stop, wait "
         "for a quiet link.",
         "Read the AcquisitionStart register (recorded)."],
        "CommandValue 1; every cycle starts and stops the stream.",
        "Another command value, no stream after Start, or no stop.",
        {"cycles": 10, "start_timeout_ms": 1000}),
    "CXP-CAM-GEN-006": E(
        "Registers with side effects on the link (TestMode, TpgRun, test counters, StreamPacketSizeMax) are "
        "not swept; out-of-range writes are NEG-003; pIsLocked is not modelled.",
        ["For every XML register: read it.",
         "RO: write its value back.",
         "RW Integer with Min/Max: write Min and Max, read back. RW Enumeration: write every entry, read back.",
         RESTORE],
        "Every register readable; RO writes answer 0x43; range ends and every entry accepted and read back.",
        "Any discrepancy."),
    "CXP-CAM-GEN-007": E(
        "Width {widths} x Height {heights} x the first {formats} PixelFormat entries, offsets at a quarter of "
        "the size.",
        ["For each combination: set the features, acquire {images} image(s), decode the header.", RESTORE],
        "Header Xsize/Ysize/Xoffs/Yoffs/PixelF equal the settings in every combination.",
        "Any mismatch.",
        {"widths": [32, 48], "heights": [8, 16], "formats": 2, "images": 1}),
    "CXP-CAM-GEN-009": E(
        "Runs only when Iidc2Address is non-zero.",
        ["Read Iidc2Address; count XML nodes at or above it."],
        "At least one XML node in the IIDC2 space.",
        "None."),
    # -- boundaries ---------------------------------------------------------------------------
    "CXP-CAM-BND-001": E(
        "Width is streamed at its extremes with Height {streamed_height}, and Height with Width {streamed_width}, "
        "to keep frames small.",
        ["For Width and Height: set Min and Max through the XML and stream {images} image(s) at each, waiting up "
         "to {acquire_timeout_ms} ms.",
         "Write 0, 0xFFFFFF, Min-1 and Max+1 (when outside the range) to the register; read it back.", RESTORE],
        "Legal extremes stream with the header showing the value; illegal writes answer 0x41 and leave the value.",
        "An illegal value accepted, or a broken stream at a legal extreme.",
        {"streamed_height": 2, "streamed_width": 16, "images": 1, "acquire_timeout_ms": 20000}),
    "CXP-CAM-BND-003": E(
        "The legal SPSM between the smallest and the register maximum is " + HOST_SPSM + ".",
        ["Write/read MasterHostConnectionID 0x00000001 and 0xFFFFFFFF.",
         "For SPSM {spsm_list}: acquire {images} image(s) (waiting up to {acquire_timeout_ms} ms); check packet "
         "sizes and DsizeP.", RESTORE],
        "IDs read back; at every SPSM packets arrive, none exceeds SPSM, DsizeP <= 0xFFFF, an image "
        "completes; at SPSM 36 every packet carries exactly one data word.",
        "Truncation, overflow, or a missing image.",
        {"spsm_list": [36, 40, "host_spsm", 0xFFFFFFFC], "images": 1, "acquire_timeout_ms": 20000}),
    # -- performance ------------------------------------------------------------------------
    "CXP-CAM-PERF-001": E(
        "--perf-seconds instead of 10 min; no declared throughput, so the rate is recorded, "
        "not judged; the IDLE ratio is not observable.",
        ["Write SPSM = " + HOST_SPSM + "; stream continuously for --perf-seconds.",
         "Count payload bytes/s, packets, CRC errors and tag discontinuities.", RESTORE],
        "Stream sustained, no CRC error, no lost packet.",
        "A CRC error or a lost packet."),
    "CXP-CAM-PERF-002": E(
        "No declared frame rate: the rate is recorded, not judged.",
        ["Set Width/Height to their minimum (Width at least {min_width}); stream for --perf-seconds.",
         "Count images, tag discontinuities and incomplete images.", RESTORE],
        "Images stream with no tag discontinuity and no incomplete image.",
        "Loss or no stream.",
        {"min_width": 8}),
    "CXP-CAM-PERF-003": E(
        "--soak seconds instead of 24 h; analysed in {window_ms} ms windows.",
        ["Stream continuously; read Standard every {read_period_ms} ms.",
         "Track CRC errors, packet-tag and SourceTag continuity, incomplete images and read latency."],
        "No CRC error, no tag or SourceTag discontinuity, no incomplete image, every read answered within " + LATENCY + ".",
        "Any of those.",
        {"window_ms": 5000, "read_period_ms": 1000}),
    "CXP-CAM-PERF-004": E(
        "A sensor rate above link capacity cannot be configured; this checks stream syntax over {images} "
        "back-to-back images.",
        ["Acquire {images} images continuously (waiting up to {acquire_timeout_ms} ms).",
         "Check packets for defects and images for completeness; measure the gap from last line to next header."],
        "No malformed packet and no partial image between complete ones.",
        "A malformed stream or a partial image.",
        {"images": 50, "acquire_timeout_ms": 20000}),
    # -- recovery / interoperability ---------------------------------------------------------
    "CXP-CAM-REC-002": E(
        "{trials} host restarts instead of 20; the restart closes and reopens this host's link threads.",
        ["Start acquisition and wait for a header.",
         "Restart the host session (reconnect), write ConnectionReset, wait 200 ms.",
         "Read ConnectionConfig, program SPSM, acquire {images} images.", "Repeat {trials} times.", RESTORE],
        "Each time ConnectionConfig is back at discovery and acquisition resumes with a complete image.",
        "No return to discovery or no image after the restart.",
        {"trials": 3, "images": 2}),
    "CXP-CAM-REC-006": E(
        "{commands} corrupted commands instead of 100.",
        ["Start acquisition; send {commands} reads with a corrupted CRC.",
         "Send a control channel reset.",
         "Stop; write ConnectionConfig with its own value; acquire {images} images.", RESTORE],
        "All corrupted reads answer 0x80 and the stream continues; the reset answers 0x03 and the stream "
        "continues; after the rewrite an image completes and the first tag is 0.",
        "The stream lost, a wrong ack, or no image afterwards.",
        {"commands": 20, "images": 2}),
    "CXP-CAM-IOP-003": E(
        "The rule checker is this host stack's own second implementation (compliance/checker.cpp), not an "
        "independent analyzer.",
        ["Record {reads} reads and a {images}-image acquisition.",
         "Run the compliance rules (packet, size, CRC, stream, timing, device, SFNC) over the capture."],
        "No rule reports an error (warnings are listed).",
        "Any rule error.",
        {"reads": 10, "images": 5}),
    # -- plan cases the bench made runnable: host triggers (uplink)
    "CXP-CAM-TRIG-006": E(
        "{triggers} triggers instead of 10^4, under the test-pattern stream (StreamPacketSizeMax at the host "
        "maximum). The acceptance is the plan's (pending clarification): one low-speed character of the bench's "
        "host bit rate plus one high-speed word. " + BENCH_TIME + " " + BENCH.format(what="character link and bench "
        "times"),
        ["Bench reset, inputs 0, the test pattern selected; stream it.",
         "{triggers} Table 15 triggers, rising and falling alternately, random Delay, each after the previous "
         "acknowledgment and a random 0..{max_gap_ms} ms host pause.",
         "Latency = start of the Table 17 acknowledgment on the downlink - end of the trigger's sixth character "
         "on the uplink; the stream judged by the stream scoreboard."],
        "Every trigger acknowledged and timed; no acknowledgment before its trigger ended; max latency <= 10 bits "
        "of the host bit rate + {tx_word_ns} ns; some acknowledgments were inserted into stream packets; the "
        "stream intact. The distribution is reported.",
        "A missing acknowledgment, a latency above the acceptance, no acknowledgment meeting a packet, or a "
        "broken stream packet.",
        {"triggers": 100, "max_gap_ms": 20, "tx_word_ns": 10}),
    "CXP-CAM-NEG-012": E(
        "Damaged Table 15 packets over the character link; §8.2.2's single-copy immunity decides which are still "
        "triggers. For a packet with no usable Delay (none alike, 240..255, K characters) the device's documented "
        "handling is reported and must be consistent. " + BENCH.format(what="character link and TRIG_OUT"),
        ["Bench reset, inputs 0.",
         "Before each packet one clean falling trigger; after it a read (retried while the receiver re-aligns).",
         "Packets: clean rising; each leader character of a rising and of a falling leader hit (a data character in "
         "its place); leader K28.4 K28.2 K28.4 (falling by majority); one Delay copy different or a K character; no "
         "two Delay copies alike; Delay 240 and 255; every Delay copy a K character; two leader characters wrong "
         "(two ways); reported only: K28.2 K28.2 K28.4, K28.4 K28.4 K28.4 and K28.2 K28.2 K28.2, whose bad copy "
         "is itself a leader character (a leader of either edge fits, one of them a character early).",
         "A clean rising trigger at the end."],
        "Every packet with a majority leader and a two-alike Delay in 0..239 acknowledged once and recreated as "
        "its edge, and the read right after it answered (the packet taken whole); no packet without one recreates a trigger; one without a majority leader is not acknowledged; "
        "one without a usable Delay is handled consistently (acknowledged and reported, or neither); every read "
        "after answers; the last clean trigger fires.",
        "A false trigger, a repairable packet not taken, an acknowledgment for a non-trigger, or a link that no "
        "longer answers.",
        {}),
    "CXP-CAM-TRIG-003": E(
        "The device's trigger signal is seen as the bench's TRIG_OUT: a level, or a strobe of the edge the device "
        "passes (cfg_trig_polarity 0: rising, 1: falling). With a strobe the de-assertion is visible only as "
        "the falling-edge effect under polarity 1. Link settings are restored afterwards. "
        + BENCH.format(what="character link, TRIG_OUT, TRIG_IN and TRIG_POLARITY"),
        ["Bench reset (power-up), inputs 0: no recreated trigger.",
         "Polarity 0: one falling trigger, a rising trigger (the reading: level or strobe), ConnectionReset, "
         "then a rising trigger.",
         "Strobe devices, polarity 1 (trigger input held at its de-asserted level): a falling trigger, a rising "
         "trigger, ConnectionReset.",
         "Device -> Host: the trigger input asserted (K28.4, acknowledged), ConnectionReset, the packets after "
         "it recorded; input low, then high again."],
        "No recreated trigger at power-up; a level falls at the ConnectionReset, a rising-edge strobe does not "
        "fire; a rising trigger after it is an edge again; with falling edges passed the ConnectionReset fires "
        "the strobe once, as a falling-edge trigger packet would (§8.3.2); the device's own trigger rises again "
        "(K28.4) after the reset.",
        "A recreated trigger at power-up, a trigger left asserted (a level that stays high, no falling-edge "
        "effect under polarity 1), a rising trigger after the reset not recreated, or the device trigger "
        "dead after the reset.",
        {}),
    "CXP-CAM-TRIG-002": E(
        "The trigger event and the device's strobe are time stamps of the bench (the sim time of the Table 15 "
        "leader's first bit, UPLINK_MARK, and of the recreated trigger, TRIG_OUT), so the plan's 10^4 edges "
        "shrink to {triggers} and the §4.7 figures (3.4 us, +-4 ns) are not the device's declaration (none "
        "given): the bench's own bit rate and rx_clk apply. " + BENCH.format(what="character link, TRIG_OUT and "
        "bench times"),
        ["Bench reset, inputs 0; one falling trigger puts the recreated trigger low.",
         "{triggers} times: a rising trigger with a random Delay 0..239 at a random character position of an "
         "IDLE word, then a falling one.",
         "For each rising trigger: event = leader's first bit - (239 - Delay) x bit / 24 (§8.3.2.1, Figure 20); "
         "latency = recreated rising edge - event; also the edge - leader time without the Delay."],
        "Every rising trigger recreated once; the compensated latency varies by no more than {rx_clk_ns} ns "
        "(one rx_clk, the device's stated resolution) + {grain_ns} ns (the bench's time grain); the "
        "uncompensated times spread far wider (the Delay is used). Mean latency reported.",
        "A trigger not recreated, a latency jitter above the limit, or a spread that shows the Delay ignored.",
        {"triggers": 40, "rx_clk_ns": 10, "grain_ns": 1}),
    "CXP-CAM-TRIG-001": E(
        "Host triggers go up as Table 15 packets (CXC1 characters); the recreated trigger is the bench's "
        "TRIG_OUT (a level that follows the trigger, or a strobe per rising trigger: either reading passes, "
        "the case reports which). The plan's extension-connection step is CXP-CAM-TRIG-001b. "
        + BENCH.format(what="character link and TRIG_OUT"),
        ["Bench reset, inputs 0; one falling trigger puts the recreated trigger low.",
         "Idle: at each of the 4 character positions of an IDLE word, a rising and a falling trigger with each "
         "Delay of {delays}.",
         "Idle: a trigger at every character position of a read of MasterHostConnectionID, from before its "
         "SOP to after its EOP, the edge alternating, the Delay cycling through {delays}.",
         "Streaming the test pattern: the 4 IDLE positions and reads with a trigger after {stream_cmd_positions} "
         "characters; the stream judged by the stream scoreboard."],
        "Every trigger answered by exactly one Table 17 I/O acknowledgment (4 x K28.6, 4 x 0x01); the recreated "
        "trigger follows the triggers (as a level or one strobe per rising trigger); every read with a trigger "
        "inside answered 0x00 with the register; the stream intact.",
        "A missing or extra acknowledgment, a recreated edge that follows neither reading, a read spoiled by "
        "the trigger inside it, or a broken stream packet.",
        {"delays": [0, 120, 239], "stream_cmd_positions": [3, 10, 17]}),

    # -- plan cases the bench made runnable: device triggers and TestMode
    "CXP-CAM-TRIG-007": E(
        "Host triggers and their acknowledgments only: the camera-functional part (images per trigger, overlap, "
        "overrun against the datasheet) needs a trigger mode the device does not have. The rate stress is "
        "{bursts} runs of {burst_triggers} back-to-back triggers (every uplink character a trigger's) instead of "
        "60 s. " + BENCH.format(what="the character link; TRIG_OUT when there"),
        ["Reset the device through the bench; one falling trigger to settle the recreated trigger.",
         "Acquisition stopped: {triggers} triggers, rising / falling alternating, {gap_ms} ms apart.",
         "Streaming the test pattern: {triggers} triggers {gap_ms} ms apart, then {bursts} runs of "
         "{burst_triggers} triggers back to back in one character frame."],
        "In every phase one clean Table 17 acknowledgment per trigger (ack count = trigger count) and the "
        "recreated trigger following them (level, or a strobe per rising trigger); the stream around them clean "
        "(Table 19, tags, framing).",
        "A lost or extra acknowledgment, a recreated trigger that does not follow, a damaged stream.",
        {"triggers": 10, "gap_ms": 10, "bursts": 3, "burst_triggers": 40}),
    "CXP-CAM-CT-007": E(
        "The clarification is the device decision D2 (src/verif/uvm/common/decisions.py): triggers and I/O "
        "acknowledgments are allowed in TestMode (§8.3.3 has no exemption, §8.7.4 limits data packets). "
        + BENCH.format(what="the character link and TRIG_IN; TRIG_OUT when there"),
        ["Reset the device through the bench with the trigger input low; note TestPacketCountTx; TestMode = 1.",
         "{host_triggers} rising and falling Table 15 triggers, {gap_ms} ms apart, each waited for.",
         "{device_edges} rising and falling edges of the trigger input, each trigger packet acknowledged (Table 17).",
         "Wait {hold_ms} ms; TestMode = 0; read TestPacketCountTx; a read and two more host triggers."],
        "Every host trigger answered by one clean I/O acknowledgment and the recreated trigger following them "
        "(level or a strobe per rising trigger); one Table 16 packet per input edge; the connection-test packets "
        "intact Table 23 packets, as many as TestPacketCountTx counted (+-1), none later than {grace_ms} ms after "
        "TestMode = 0; afterwards the read and the triggers answered.",
        "A trigger or acknowledgment missing, extra or suppressed in TestMode; a corrupted or miscounted test "
        "packet; a control or I/O channel wedged after TestMode.",
        {"host_triggers": 3, "device_edges": 3, "gap_ms": 20, "hold_ms": 100, "grace_ms": 500}),
    "CXP-CAM-PROT-009": E(
        "{triggers} host triggers per phase instead of 10^4. Control acknowledgments (a few words) cannot be met "
        "by a trigger that takes 60 bits to arrive, so the lower-priority packets are stream packets and "
        "connection-test packets. The bench takes an inserted packet out of the one it interrupted and reports "
        "where it was. " + BENCH_TIME + " " + BENCH.format(what="the character link and bench times"),
        ["Reset the device through the bench, select the test pattern and stream.",
         "After the first image header: {triggers} Table 15 triggers (rising / falling alternating, Delay 0) at "
         "random gaps of {gap_range_ms} ms; wait for the acknowledgments; stop.",
         "TestMode = 1: the same triggers while the device sends connection-test packets; TestMode = 0."],
        "One clean Table 17 acknowledgment per trigger; each starts within one LS character (10 bits) plus one "
        "word ({word_ns} ns) of its trigger's end on the uplink; in each phase at least one was inserted into a "
        "packet in progress; every stream packet matches Table 19 (CRC) and every connection-test packet is an "
        "intact Table 23 packet after the bench removed what was inserted.",
        "A missing or malformed acknowledgment, one deferred to the end of the packet, or a corrupted interrupted "
        "packet.",
        {"triggers": 40, "gap_range_ms": [1, 8], "word_ns": 10}),
    "CXP-CAM-TRIG-004": E(
        "Device events are edges of the bench trigger input (TRIG_IN) while the device streams its test pattern; "
        "{edges} instead of random phases without bound; the host acknowledges every trigger at once. The Delay is "
        "judged against its range only (the bench does not time the input's edge within a character). The timeout "
        "register §8.3.3 recommends is recorded, not required. " + BENCH_TIME + " "
        + BENCH.format(what="TRIG_IN, the character link and bench times"),
        ["Reset the device through the bench with the input de-asserted; select the test pattern and stream.",
         "After the first image header, {edges} times: wait a random {gap_range_ms} ms, toggle the input (bench time "
         "taken), wait for the trigger packet, acknowledge it (Table 17).",
         "Stop; judge the triggers, their bench times against the input's changes and the host's acknowledgments, "
         "and the stream."],
        "One trigger packet per edge, alternating 4 x K28.4 / 4 x K28.2, each clean with Delay 0..3; each whose "
        "predecessor's acknowledgment had reached the device ({ack_decode_ns} ns after its last bit on the uplink) "
        "on the wire within {max_latency_ns} ns of the input's change, inserted into the packet in progress when it "
        "met one (at least one did); none before the acknowledgment of the previous one reached the device (or "
        "{timeout_ns} ns, the device's timeout); the stream around them clean (Table 19, tags, framing).",
        "A missing, extra, malformed or wrong-kind packet; a trigger held back to a packet's end; a trigger before "
        "the previous one's acknowledgment or timeout; a damaged stream packet.",
        {"edges": 20, "gap_range_ms": [5, 40], "max_latency_ns": 1000, "ack_decode_ns": 10000, "timeout_ns": 40960}),

    # -- plan cases the bench made runnable: link test and uplink robustness
    "CXP-CAM-PROT-008": E(
        "Stream packets from the test pattern. The host's decoder computes each CRC over the bytes the link "
        "delivered, a K28.3 marker as 0x7C, with the IDLE words the Device stretched a packet with removed by "
        "the bench, which counts them (DL_STATS). The HSUP part (commands stretched with IDLE on a high speed "
        "upconnection) does not apply: the Device has no HSUP, and on the low speed connection §8.2.5.2 forbids "
        "stretching a packet. " + BENCH.format(what="downlink statistics, for the IDLE part"),
        ["Acquire {images} images of the test pattern, counting the downlink before and after.",
         "Check the CRC of every stream packet; count those that carry K28.3 markers and the IDLE words the "
         "bench removed from inside packets."],
        "Packets with K28.3 markers exist and every stream packet passes its CRC; when the Device stretched "
        "packets with IDLE, those pass too.",
        "A CRC mismatch on a packet with markers (K28.3 not taken as 0x7C) or on a stretched packet (IDLE "
        "counted in the CRC), or no marker-bearing packet at all.",
        {"images": 2}),
    "CXP-CAM-PROT-005": E(
        "The bench disturbs the host's serial line (UPLINK_BITS): bits dropped (every bit and character "
        "phase), bits inserted, the line held low for {hold_bits} bits (1 us, 1 ms and 10 ms of the real "
        "20.83 Mbps line; the plan's 100 ms is scaled down for the simulator). The Device declares no recovery "
        "limit; the case holds it to {recover_bits} bits and reports the slowest. " + BENCH_TIME + " "
        + BENCH.format(what="UPLINK_BITS, the character link and bench times"),
        ["For each disturbance ({drop_bits} bits dropped; {insert_bits} bits inserted; the line low for "
         "{hold_bits} bits): read MasterHostConnectionID, apply the disturbance and send {batch} writes of "
         "distinct values to it, each followed by one IDLE word.",
         "Match each acknowledgment to the write whose EOP left last before it (bench times); read the "
         "register back.",
         "After all of them, read Standard."],
        "After every disturbance some write of its batch is acknowledged 0x01 within {recover_bits} bits of "
        "the disturbance's end; the register holds the value of the last write acknowledged 0x01 (or its value "
        "before when none was); no write is acknowledged twice; the final read answers.",
        "A disturbance after which no write is executed (stuck misaligned), a recovery slower than the limit, "
        "a register value no acknowledged write gave (a spurious or unacknowledged write executed), a "
        "duplicated acknowledgment, or a dead link at the end.",
        {"drop_bits": list(range(1, 40)), "insert_bits": [1, 2, 3], "hold_bits": [21, 20833, 208333],
         "batch": 24, "recover_bits": 10000, "ack_window_ms": 10000, "host_wait_ms": 600000}),

    # -- plan cases the bench made runnable: pixel port
    "CXP-CAM-IMG-007": E(
        "The arbitrary form is the bench's ARBITRARY strap (cfg_arbitrary); the frames go in at the pixel port "
        "(PIXEL_FRAME, Mono8) inside an acquisition. The port's metadata carries one Xsize and Xoffs per frame, so "
        "every line of an image has the frame's geometry: an arbitrary shape with lines of different lengths "
        "needs per-line metadata the bench does not drive, and is not sent. " + BENCH.format(what="pixel port and "
        "ARBITRARY strap"),
        ["Reset the device through the bench, select the pixel port, set the strap to arbitrary.",
         "Send one frame per geometry of {geometries} (Width, Height, OffsetX, OffsetY), each after the previous "
         "image is out.",
         "Switch the strap before each of {toggle_sequence} (1 arbitrary, 0 rectangular) {frame.width} x "
         "{frame.height} frames.",
         "Twice (to rectangular, to arbitrary): send a slow {slow_frame.width} x {slow_frame.height} frame "
         "({slow_frame.valid_permille}/1000 of the cycles valid) and a next frame, and switch the strap "
         "{toggle_after_ms} ms after the send, while the slow frame goes in.",
         "Judge every stream packet and image; compare every image with the frame sent."],
        "Every arbitrary image has a Table 40 header (Ysize, Yoffs, PixelF, TapG, Flags, SourceTag, StreamID) and "
        "a Table 41 marker before every line with that line's Xsize, Xoffs and DsizeL, pixels bit for bit; after "
        "a switch between frames each image is in the strap's form; a switch during an image leaves no image on "
        "the link mixing the two forms (it keeps its header's form or starts over), and the next image is right.",
        "A wrong header or marker field, a missing marker, a pixel difference, an image in the wrong form, or an "
        "image whose lines carry markers of the other form.",
        {"geometries": [[16, 4, 0, 0], [5, 3, 7, 2], [33, 2, 100, 9], [64, 8, 4095, 4095]],
         "toggle_sequence": [1, 0, 1, 0], "frame": {"width": 12, "height": 3},
         "slow_frame": {"width": 64, "height": 32, "valid_permille": 20}, "toggle_after_ms": 150}),
    "CXP-CAM-PIX-003": E(
        "The device's pixel port takes container-width values, so shifting a {depths}-bit sensor into the next "
        "container is the integrator's side of the port; the check sends such MSB-aligned values through the "
        "port (bench PIXEL_FRAME, USE_TPG = 0) and holds the device to keeping them MSB-aligned on the link. "
        + BENCH.format(what="pixel port"),
        ["Reset the device through the bench, select the pixel port.",
         "For each sensor depth of {depths} bits with a container the XML lists: set PixelFormat to the "
         "container; send one {width} x {lines} frame of sensor values (full scale, zero, then a ramp over the "
         "range) shifted into the container's top bits.",
         "Compare the image with the golden packing; unpack every container."],
        "Every image equals the golden packing, and every container carries its sensor value in its top bits "
        "with the unused low bit 0.",
        "A container with the value LSB-aligned or truncated, a non-zero unused bit, or a frame missing.",
        {"depths": [9, 11, 13, 15], "width": 16, "lines": 2}),
    "CXP-CAM-PIX-002": E(
        "The pixels go in at the device's pixel port (bench PIXEL_FRAME, USE_TPG = 0) inside an acquisition, "
        "in every format of {formats} the XML lists, with the host's PixelFormat set to the same format; "
        "lines of {widths} pixels. " + BENCH.format(what="pixel port"),
        ["Reset the device through the bench, select the pixel port.",
         "For each format: set PixelFormat; send one frame of {lines} lines per line length, one at a time "
         "(each after the previous image is out), each pixel unique (the first one full scale), and record "
         "the stream.",
         "Walk the images; compare each line word for word with the Figure 27-31 packing of the pixels "
         "sent (MSB first, the first pixel's MSB in P0 bit 7, the unused bits of the last word 0)."],
        "Every frame comes back with its header (PixelF = the format, Xsize, DsizeL = ceil(Xsize x bits / "
        "32)) and every line equals the golden packing bit for bit, padding included; every stream packet "
        "matches Table 19.",
        "A frame missing, a bit misplaced, a non-zero padding bit, a wrong DsizeL or PixelF, or a broken "
        "stream packet.",
        {"formats": ["Mono8", "Mono10", "Mono12", "Mono14", "Mono16"],
         "widths": [1, 2, 3, 4, 5, 6, 7, 8, 9, 13, 16, 31, 32, 33, 63, 64], "lines": 2}),

}


# Host test packets cross the serial uplink bit by bit: in the RTL sim one
# 1027-word packet takes ~0.55 s on an idle host and ~2 s with five sims side
# by side; budget TEST_PACKET_S each.  A read queued behind a burst waits the
# raw ack wait (Options::ack_timeout_ms, 1 s) times the scale; the §10.1.2
# 200 ms after a ConnectionReset must outlast the packets sent before it.
TEST_PACKET_S = 5.0


def test_packet_scale(burst: int, before_reset: int = 0) -> float:
    return float(max(burst * TEST_PACKET_S / 1.0, before_reset * TEST_PACKET_S / 0.2, 1))


_ct4 = EMULATOR["CXP-CAM-CT-004"]["params"]["test_packets"]
TIMEOUT_SCALE["CXP-CAM-CT-004"] = test_packet_scale(max(_ct4["clean"], _ct4["one_bad_word"] + _ct4["two_bad_words"]))
_ct5 = EMULATOR["CXP-CAM-CT-005"]["params"]["test_packets"]
TIMEOUT_SCALE["CXP-CAM-CT-005"] = test_packet_scale(_ct5["first"], _ct5["before_reset"])


# ---------------------------------------------------------------------------
# The PyUVM tests (src/verif/uvm/tests/all_tests.py), reproduced on the emulator.
#
# Every test class there becomes one catalogue entry "UVM-<test>" in the
# UVM section, next to the plan's cases: its docstring, the plan rows it
# serves (PLAN / PLAN_PARTIAL), the findings its EXPECT_FAIL tags tolerate
# and the regression tiers that run it are read from src/verif/, so the
# catalogue goes stale when a test is added, renamed or retagged there.
# UVM_EMULATOR says what the check in src/cxp/validation/cases/uvm/ does
# instead, UVM_NOT_RUNNABLE why a test has no counterpart (none today).
#
# What UVM drives besides the link (trigger pins, the pixel port, the
# extension-link strap, a faulting register bus, the clocks) the checks ask
# of the device's test bench (src/cxp/protocol/bench.h): the RTL bench in
# src/emu/bridge and the in-process virtual camera carry it out.  A device without
# the bench capability a case needs shows that case NOT RUN, with the reason.
#
# The checks judge against CXP 1.1.1, like every other check: an EXPECT_FAIL
# tag in the UVM test does not relax its mirror.
# ---------------------------------------------------------------------------
UVM_TESTS = REPO / "src" / "verif" / "uvm" / "tests" / "all_tests.py"
UVM_MAKEFILE = REPO / "src" / "verif" / "Makefile"
UVM_SECTION = "UVM testbench (src/verif/uvm) on the emulator"

# Registers the UVM random control sequences draw from (UplinkCtrlRandomSeq).
UVM_RW = "MasterHostConnectionID, StreamPacketSizeMax, ConnectionConfig or TestErrorCountSelector"
LEGAL = ("each value is drawn from the register's legal range: any value for MasterHostConnectionID, a "
         "multiple of 4 in {spsm_range} for StreamPacketSizeMax, the current value for ConnectionConfig, 0 for "
         "TestErrorCountSelector")
# The params behind LEGAL and rand_frames(), for every case that draws them.
LEGAL_PARAMS = {"spsm_range": [64, 4096]}
VIDEO_RANDOM = {"frame_xsizes": [4, 8, 12, 16, 32, 64, 128], "frame_ysizes": [2, 4, 8, 16, 32],
                "valid_percent_range": [50, 100]}
# One VsStressConcurrent round (UVM-test_arbiter_preempt).
PREEMPT_PARAMS = {"frames": 2, "commands": 4, "test_packets": 1, "images": 2, "min_complete": 2,
                  **VIDEO_RANDOM, **LEGAL_PARAMS}
STREAM_SB = ("every stream packet matches Table 19 (DsizeP = payload words, CRC); packet tags continue +1 "
             "mod 256; every complete image carries Ysize line markers of DsizeL = ceil(Xsize x bpp / 32) "
             "words; Bars images equal the golden model bit for bit")
INJECTED_SB = ("every stream packet matches Table 19; packet tags continue +1 mod 256; every frame sent comes "
               "back as one image, in order, with the header its metadata asked for, Ysize line markers of the "
               "spec length, and every pixel equal to the one sent (first pixel in P0)")
CTRL_SB = ("every command answers with the code the register model predicts (0x00 with the modelled value "
           "for a read, 0x01 for a write)")
BARS = "Select PixelFormat Mono8 and TestPattern Bars (the golden model's static pattern)."
LINK_RESTORE = "Link settings (ConnectionConfig, StreamPacketSizeMax, MasterHostConnectionID) are restored afterwards."
PIXEL_PORT = ("With a bench pixel port (USE_TPG = 0) the frames go in there, as the UVM video agent drives "
              "s_pix_*, and every image is judged against the pixels sent; without one the device's own Bars "
              "test pattern stands in and the golden model judges.")
TABLE15 = ("Host triggers go up as Table 15 low-speed triggers (6 characters, CXC1 frames), the form the spec "
           "gives the low-speed uplink.")
IDLE = (" As in UVM (cfg_run = 0 unless a test turns the generator on), the bench stops a free-running "
        "test-pattern generator for the test.")
DEV_TRIG = ("each edge answered by a Table 16 trigger packet: 4 x K28.4 for a rising edge, 4 x K28.2 for a "
            "falling one, delay 0..3 (§8.3.2.2); the host acknowledges each with a Table 17 I/O ack")


def rand_frames() -> str:
    return ("{frames} VideoRandomSeq frame(s): Xsize from {frame_xsizes}, Ysize from {frame_ysizes}, Mono8, "
            "{valid_percent_range} % of the cycles valid, random pixels (UVM: a byte ramp)")


UVM_EMULATOR = {
    "test_idle_baseline": E(
        "UVM holds the uplink at IDLE for 2 us and its link-protocol scoreboard sees only IDLE. The FIFO link "
        "carries no IDLE words, so a quiet link is one without packets; host-stack extension frames "
        "(0x05/0x06) are listed, not judged." + IDLE,
        ["Stop acquisition; wait until the link is quiet.",
         "Record the downlink for {record_ms} ms without sending anything."],
        "No stream (0x01), acknowledgment (0x03) or test (0x04) packet in the recording.",
        "Any such packet.",
        {"record_ms": 500}),
    "test_stream_tpg": E(
        "VsSmoke: the test-pattern generator streams while one control read goes up; the stream scoreboard "
        "checks format, framing and pixels. Bars is used so every pixel has a golden value.",
        [BARS, "Start acquisition and record; read Standard once while the stream runs.",
         "Wait for {images} more image headers and the last one's tail; stop; wait for a quiet link.", RESTORE],
        "The read answers 0x00 with 0xC0A79AE5; " + STREAM_SB + "; at least {min_complete} complete images.",
        "A wrong or missing acknowledgment, a malformed packet, a tag break, a framing error, a pixel "
        "mismatch or fewer than {min_complete} complete images.",
        {"images": 3, "min_complete": 2}),
    "test_stream_video": E(
        "UVM drives one random frame into the pixel port with 64-word packets (StreamPacketSizeMax = {spsm} "
        "bytes here). " + PIXEL_PORT + " Without the port, {geometries} seeded random Width x Height settings "
        "({width_range} x {height_range}) stream the test pattern instead.",
        ["Write StreamPacketSizeMax = {spsm}.",
         "Pixel port: send " + rand_frames() + ", record until the port has taken them and the link is quiet.",
         "Test pattern: " + BARS + " For {geometries} geometries set Width and Height, acquire {images} images.",
         RESTORE],
        "No packet exceeds {spsm} bytes, and " + INJECTED_SB + " (test pattern: " + STREAM_SB + ", the header "
        "shows the programmed geometry).",
        "An oversize packet, a lost frame, a header or marker mismatch, or a pixel mismatch.",
        {"spsm": 288, "frames": 8, "geometries": 3, "width_range": [8, 256], "height_range": [2, 32], "images": 2,
         **VIDEO_RANDOM}),
    "test_stream_video_ragged": E(
        "Eight frames into the pixel port whose last packets are 1, 2 and DsizeP - 1 words long, five at "
        "64-word packets (StreamPacketSizeMax = {spsm_64} bytes), three at 16-word packets ({spsm_16} bytes), "
        "widths 4 and 5 among them. " + BENCH.format(what="pixel port") + IDLE,
        ["Write StreamPacketSizeMax = {spsm_64}; send 4x4, 5x4, 29x4, 49x7 and 57x6 ramp frames; record until "
         "the port has taken them and the link is quiet.",
         "Write StreamPacketSizeMax = {spsm_16}; send 21x1, 25x1 and 13x1 ramp frames; record the same way.",
         RESTORE],
        "No packet exceeds the StreamPacketSizeMax in force, and " + INJECTED_SB + ".",
        "An oversize packet, a lost frame, a header or marker mismatch, or a pixel mismatch.",
        {"spsm_64": 288, "spsm_16": 96}),
    "test_arbitrary_image": E(
        "Same stimulus: one random frame through the pixel port with the arbitrary-image strap set "
        "(cfg_arbitrary), every line of the frame's width. " + BENCH.format(what="pixel port and ARBITRARY strap"),
        ["Bench: USE_TPG = 0, ARBITRARY = 1.",
         "Send " + rand_frames() + " with Xoffs {xoffs}; record until the port has taken them and the link is quiet.",
         "Bench straps are restored afterwards."],
        "The image arrives with a Table 40 arbitrary header (type 0x03) carrying the frame's Ysize, Yoffs, "
        "PixelF, TapG, Flags, SourceTag and StreamID; each line with a Table 41 marker (type 0x04) giving its "
        "Xsize, Xoffs and DsizeL = ceil(Xsize x bpp / 32) words, followed by that many words; pixels equal to "
        "those sent. Constant geometry is noted against REQ-IMG-013 (SHOULD), not judged.",
        "A rectangular header, a wrong field, a marker missing or wrong, a line of the wrong length, or a "
        "pixel mismatch.",
        {"frames": 1, "xoffs": 4, **VIDEO_RANDOM}),
    "test_ctrl_cmd_read": E(
        "UVM reads {commands} randomly chosen full-width RW bootstrap registers straight after reset and compares "
        "them with the register model. A ConnectionReset puts the device in that state first." + IDLE,
        ["Write ConnectionReset = 1 without waiting for an ack; wait 200 ms.",
         "{commands} seeded random reads (4 bytes) of " + UVM_RW + ".", LINK_RESTORE],
        "Every read answers 0x00 with 4 data bytes holding the §10.3.28 reset value: 0 for "
        "MasterHostConnectionID, StreamPacketSizeMax and TestErrorCountSelector, a discovery configuration "
        "for ConnectionConfig.",
        "A wrong code, a wrong value or no answer.",
        {"commands": 4}),
    "test_ctrl_cmd_write": E(
        "UVM writes random 32-bit values; CXP 1.1.1 limits three of the four registers, so " + LEGAL + "." + IDLE,
        ["{commands} seeded random writes to " + UVM_RW + ".", "Read every written register back.", RESTORE],
        "Every write answers 0x01 in the 4-word short form and every register reads back the value written "
        "last.",
        "A wrong code or form, or a register that does not hold the value.",
        {"commands": 4, **LEGAL_PARAMS}),
    "test_ctrl_reset_op": E(
        "Same stimulus: {resets} control channel resets between commands." + IDLE,
        ["Send {resets} control channel resets (0xFF), waiting for each acknowledgment.", "Read Standard."],
        "Every reset answers 0x03; the read answers 0x00 with 0xC0A79AE5.",
        "A wrong or missing acknowledgment.",
        {"resets": 2}),
    "test_trigger_uplink": E(
        "{triggers} seeded random rising/falling host triggers, delay 0, spaced; the device's recreated trigger "
        "(trig_o) is watched through the bench. " + TABLE15 + " " + BENCH.format(what="character link and TRIG_OUT") + IDLE,
        ["Send a falling trigger so the recreated trigger starts low.",
         "Send the {triggers} triggers, {spacing_ms} ms apart, each waiting for its I/O acknowledgment.",
         "Collect the TRIG_OUT edges."],
        "The recreated trigger follows the triggers: either its level (a rising trigger raises it, a falling "
        "one lowers it) or one strobe per rising trigger (the plan's \"strobe output\"; the reading is "
        "reported); every trigger is acknowledged with 4 x K28.6 + 4 x 0x01 (REQ-TRIG-003).",
        "A missing or extra edge, or a trigger not acknowledged.",
        {"triggers": 4, "spacing_ms": 20}),
    "test_linktest_clean": E(
        "UVM sends {test_packets} test packets with 64-word bodies; here they are full Table 23 packets (1024 counting "
        "words). Both hosts run {uplink_ppm} ppm off the nominal bit rate (the bench's UPLINK_PPM; nominal on a "
        "bench without it)." + IDLE,
        ["Set the host bit rate {uplink_ppm} ppm off nominal.",
         "Write TestErrorCountSelector = 0, TestErrorCount = 0 and TestPacketCountRx = 0.",
         "Send {test_packets} host test packets back to back.", "Read TestErrorCount and TestPacketCountRx.",
         "Set the host bit rate back to nominal."],
        "TestPacketCountRx = {test_packets} and TestErrorCount = 0.",
        "Any other count, or a read that fails.",
        {"test_packets": 4, "uplink_ppm": 200}),
    "test_linktest_inject": E(
        "UVM sends {test_packets.clean} clean and {test_packets.corrupted} corrupted test packets with 64-word "
        "bodies; here they are full Table 23 packets." + IDLE,
        ["Write TestErrorCountSelector = 0, TestErrorCount = 0 and TestPacketCountRx = 0.",
         "Send {test_packets.clean} clean host test packet(s) and {test_packets.corrupted} with {bad_words} "
         "corrupted words each.",
         "Read TestErrorCount and TestPacketCountRx."],
        "TestPacketCountRx counts every packet sent and TestErrorCount every corrupted word.",
        "Any other count, or a read that fails.",
        {"test_packets": {"clean": 1, "corrupted": 3}, "bad_words": 3}),
    "test_byte_replication_robust": E(
        "Same stimulus: one bit flipped in one replica of the TYPE word (§8.2.2.1, 3-of-4 vote)." + IDLE,
        ["{reads} reads of Standard, each with bit 0 of one seeded random byte of the TYPE word flipped.",
         "A plain read of Standard."],
        "Every read answers 0x00 with 0xC0A79AE5.",
        "A read rejected, unanswered or answered with the wrong data.",
        {"reads": 4}),
    "test_crc_error": E(
        "Same stimulus: {reads} reads of Standard with a corrupted CRC." + IDLE,
        ["{reads} reads of Standard with an inverted CRC.", "A plain read of Standard."],
        "Each corrupted read answers 0x80; the plain read answers 0x00 with 0xC0A79AE5.",
        "A corrupted read executed, answered otherwise or not at all; the plain read fails.",
        {"reads": 4}),
    "test_pslverr_burst": E(
        "UVM's APB slave answers every access with PSLVERR while {writes} random writes go up; the control "
        "scoreboard predicts 0x40. The bench makes the device's register bus answer every access with 0x40 "
        "(RTL: the register file's error code forced); " + LEGAL + ". " + BENCH.format(what="REG_ERR") + IDLE,
        ["Bench: REG_ERR = 0x40.", "{writes} seeded random writes to " + UVM_RW + ".",
         "Bench: REG_ERR off; read Standard.", RESTORE],
        "Every write on the faulting bus answers 0x40; once the fault is removed a read of Standard answers "
        "0x00 with 0xC0A79AE5.",
        "Another code, no answer, or no recovery.",
        {"writes": 4, **LEGAL_PARAMS}),
    "test_arbiter_preempt": E(
        "VsStressConcurrent: random frames, random commands, a rising and a falling host trigger (Table 15, "
        "between packets) and link-test packets at once; " + LEGAL + ". " + PIXEL_PORT + " Without a bench "
        "character link the triggers are left out (noted).",
        ["Pixel port: send " + rand_frames() + "; test pattern: " + BARS + " start acquisition.",
         "Meanwhile: {commands} seeded random reads or writes of " + UVM_RW + ", {test_packets} host test packet(s).",
         "Wait for the frames ({images} more images, at least {min_complete} complete); stop; wait for a quiet link.",
         "Read TestErrorCount and TestPacketCountRx.",
         "Send a rising and a falling host trigger last, so a device the Table 15 form throws off has shown "
         "everything else.", RESTORE],
        CTRL_SB[0].upper() + CTRL_SB[1:] + "; " + INJECTED_SB + " (test pattern: " + STREAM_SB + "); "
        "TestPacketCountRx = {test_packets} and TestErrorCount = 0; both triggers acknowledged.",
        "Any control, stream, link-test or trigger scoreboard failure.",
        PREEMPT_PARAMS),
    "test_ctrl_reset_storm": E(
        "VsLinkResetStorm: {resets} control channel resets back to back." + IDLE,
        ["Send {resets} control channel resets (0xFF) back to back without waiting.",
         "Collect acknowledgments for {collect_ms} ms.", "Read Standard."],
        "{resets} 0x03 acknowledgments; the read answers 0x00 with 0xC0A79AE5.",
        "Fewer or other acknowledgments, or the read fails.",
        {"resets": 3, "collect_ms": 1000}),
    "test_ral_sweep": E(
        "Same registers and patterns. CXP 1.1.1 does not admit every pattern into every register, so the "
        "check is the register model's: a write the device accepts reads back, one it refuses leaves the "
        "old value, and TestErrorCountSelector refuses a selector with no connection behind it (as CT-005)." + IDLE,
        ["For MasterHostConnectionID, StreamPacketSizeMax and TestErrorCountSelector, for {values}: write, then "
         "read back.", RESTORE],
        "Every write answers 0x01 and reads back, or answers an error code and keeps the old value; "
        "MasterHostConnectionID accepts every pattern; TestErrorCountSelector accepts 0 and refuses the others.",
        "An accepted value that does not read back, a refused write that changed the register, a pattern "
        "refused by MasterHostConnectionID or an out-of-range selector accepted.",
        {"values": [0xFFFFFFFF, 0x00000000, 0xA5A5A5A5]}),
    "test_io_ack": E(
        "{triggers} seeded random host triggers, each of which must come back as a Table 17 I/O acknowledgment. "
        + TABLE15 + " " + BENCH.format(what="character link") + IDLE,
        ["Send a falling trigger so the recreated trigger starts low.",
         "Send the {triggers} triggers, {spacing_ms} ms apart, each waiting for its acknowledgment."],
        "{triggers} I/O acknowledgments, each 4 x K28.6 followed by 4 x 0x01 (REQ-TRIG-003).",
        "A missing, extra or malformed acknowledgment.",
        {"triggers": 6, "spacing_ms": 20}),
    "test_tx_trigger": E(
        "{edges} edges on the device's trigger input (trig_i, the UVM io agent's trigger_in_app), from the low "
        "level. " + BENCH.format(what="TRIG_IN and the character link") + IDLE,
        ["Bench: TRIG_IN = 0; let it settle.",
         "Toggle TRIG_IN {edges} times, {gap_ms} ms apart; after each trigger packet send a Table 17 acknowledgment."],
        "{edges} trigger packets, " + DEV_TRIG + ".",
        "A missing, extra, wrong-kind or malformed trigger packet.",
        {"edges": 6, "gap_ms": 20}),
    "test_tx_linktest_mode": E(
        "Same stimulus: TestMode 1, then 0; UVM holds the link for a few 1027-word packets, here {testmode_ms} ms."
        + IDLE,
        ["Stop acquisition; write TestPacketCountTx = 0.",
         "Record; write TestMode = 1; wait {testmode_ms} ms; write TestMode = 0; wait {after_ms} ms.",
         "Read TestPacketCountTx."],
        "At least one test packet; every one is a Table 23 packet (1027 words, counting 0x00..0xFF); "
        "TestPacketCountTx equals the number received (+-1); none later than {grace_ms} ms after TestMode = 0.",
        "No test packet, a malformed one, a count mismatch, or packets after TestMode = 0.",
        {"testmode_ms": 500, "after_ms": 200, "grace_ms": 100}),
    "test_router_reject": E(
        "The extension-link strap (from_extension_link) is set, {writes} random writes and {reads} random reads go up. "
        "REQ-ERR-012: an extension connection is read-only; the refusal code is open in the spec (plan "
        "clarification), UVM expects 0x43, which is noted when it differs. " + BENCH.format(what="EXT_LINK") + IDLE,
        ["Read the four RW registers (the model).", "Bench: EXT_LINK = 1.",
         "{writes} seeded random writes and {reads} seeded random reads of " + UVM_RW + " (" + LEGAL + ").",
         "Bench: EXT_LINK = 0; read the four registers again.", RESTORE],
        "Every write is refused (an error code, not 0x80) and no register changed; every read answers 0x00 "
        "with the modelled value.",
        "A write accepted or executed, or a read refused or wrong.",
        {"writes": 3, "reads": 2, **LEGAL_PARAMS}),
    "test_link_reset": E(
        "Same stimulus: program a register, ConnectionReset, read it back." + IDLE,
        ["Write MasterHostConnectionID = 0xA5A5A5A5.",
         "Write ConnectionReset = 1 without waiting for an ack; wait 200 ms.",
         "Read MasterHostConnectionID.", LINK_RESTORE],
        "The write answers 0x01; after the reset MasterHostConnectionID reads 0.",
        "The write fails or the register keeps its value."),
    "test_tpg_config": E(
        "UVM rewrites TestPattern (1, 2, 3, 0) while the TPG free-runs. Here each value is set through the "
        "XML between acquisitions.",
        ["With {pixel_format}: for TestPattern {patterns}: acquire {images} images.", RESTORE],
        "Every setting streams complete images in clean packets; Bars and GreyBars in Mono8 equal the golden "
        "model bit for bit.",
        "A setting that does not stream, a framing error or a pixel mismatch.",
        {"pixel_format": "Mono8", "patterns": ["Bars", "Flat", "GreyBars", "Gradient"], "images": 2}),
    "test_tpg_formats": E(
        "UVM rewrites PixelFormat (Mono8 .. Mono16) while the TPG free-runs. Here every PixelFormat entry "
        "the XML lists with a Table 25 code is set through the XML between acquisitions.",
        ["With TestPattern {format_pattern}: for every PixelFormat entry with a Table 25 code: acquire {images} "
         "images.", RESTORE],
        "Every format streams complete images in clean packets; the header PixelF equals the selected "
        "format; lines carry ceil(Xsize x bpp / 32) words; Bars in Mono8 equals the golden model bit for bit.",
        "A format that does not stream, a PixelF mismatch, a framing error or a pixel mismatch.",
        {"format_pattern": "Bars", "images": 2}),
    "test_xifc_stream_ctrl": E(
        "VsStreamPlusCtrl: {commands} random commands while the test pattern streams; " + LEGAL + ".",
        [BARS, "Start acquisition and record; wait for the first image header.",
         "While streaming: {commands} seeded random reads or writes of " + UVM_RW + ".",
         "Wait for {images} more image headers; stop; wait for a quiet link.", RESTORE],
        CTRL_SB[0].upper() + CTRL_SB[1:] + "; " + STREAM_SB + "; at least {min_complete} complete images.",
        "Any control or stream scoreboard failure.",
        {"commands": 8, "images": 2, "min_complete": 2, **LEGAL_PARAMS}),
    "test_xifc_stream_trigger": E(
        "VsStreamPlusTrigger: {edges} trigger-input edges while the test pattern streams; the trigger packets "
        "pre-empt the stream at word boundaries and the stream packets must stay intact. "
        + BENCH.format(what="TRIG_IN and the character link"),
        [BARS, "Start acquisition and record; wait for the first image header.",
         "Toggle TRIG_IN {edges} times, {gap_ms} ms apart, acknowledging each trigger packet.",
         "Wait for {images} more image headers; stop; wait for a quiet link.", RESTORE],
        "{edges} trigger packets, " + DEV_TRIG + "; " + STREAM_SB + "; at least {min_complete} complete images.",
        "A trigger packet missing or wrong, or any stream scoreboard failure.",
        {"edges": 8, "gap_ms": 5, "images": 2, "min_complete": 2}),
    "test_xifc_stream_linkreset": E(
        "VsStreamPlusLinkReset: a ConnectionReset lands in the running stream. After it the host programs "
        "StreamPacketSizeMax again (§10.3.28 sets it to 0) and restarts acquisition.",
        [BARS, "Start acquisition and record; wait for {images_before} image headers.",
         "Write ConnectionReset = 1 without waiting for an ack; wait 200 ms.",
         "Write StreamPacketSizeMax (" + HOST_SPSM + "), start acquisition, wait for {images_after} image headers; "
         "stop; wait for a quiet link.",
         LINK_RESTORE],
        "Every stream packet before and after the reset matches Table 19 (no torn packet); tags continue +1 "
        "within each part; the first tag after the reset is 0; the second part holds a complete image equal "
        "to the golden model.",
        "A torn or malformed packet, a tag break, a tag that does not restart at 0, or no image after the "
        "reset.",
        {"images_before": 2, "images_after": 2}),
    "test_xifc_full": E(
        "VsFullConcurrent: the test pattern streams while {commands} random commands, {triggers} host triggers "
        "(Table 15) and {edges} trigger-input edges go on; " + LEGAL + ". "
        + BENCH.format(what="character link, TRIG_IN and TRIG_OUT"),
        [BARS, "Start acquisition and record; wait for the first image header.",
         "While streaming: {commands} seeded random reads or writes of " + UVM_RW + "; {edges} TRIG_IN edges "
         "{gap_ms} ms apart, each trigger packet acknowledged; {triggers} seeded random host triggers "
         "{spacing_ms} ms apart last.",
         "Wait for {images} more image headers; stop; wait for a quiet link.", RESTORE],
        CTRL_SB[0].upper() + CTRL_SB[1:] + "; " + STREAM_SB + "; at least {min_complete} complete images; every "
        "host trigger acknowledged and recreated; {edges} trigger packets, " + DEV_TRIG + ".",
        "Any control, stream or trigger scoreboard failure.",
        {"commands": 4, "triggers": 2, "spacing_ms": 5, "edges": 6, "gap_ms": 5, "images": 2, "min_complete": 2,
         **LEGAL_PARAMS}),
    "test_arbiter_stream_underflow": E(
        "One {frame.width} x {frame.height} Mono8 frame in 64-word packets leaves the image's last packet short; "
        "then {triggers} host triggers must still be acknowledged (§8.3.3). " + PIXEL_PORT + " "
        + TABLE15.split(". The UVM")[0] + ". " + BENCH.format(what="character link"),
        ["Write StreamPacketSizeMax = {spsm} (64-word packets).",
         "Pixel port: send one {frame.width} x {frame.height} byte-ramp frame; test pattern: Width {frame.width}, "
         "Height {frame.height}, acquire {images} image(s).",
         "Send {triggers} seeded random host triggers, {spacing_ms} ms apart.", RESTORE],
        "The image arrives complete in clean packets (pixels as sent, or the golden model); every trigger "
        "is acknowledged with 4 x K28.6 + 4 x 0x01.",
        "A malformed or missing image, or a trigger not acknowledged.",
        {"spsm": 288, "frame": {"width": 64, "height": 8}, "images": 1, "triggers": 4, "spacing_ms": 20}),
    "test_arbiter_underflow_ctrl": E(
        "One {frame.width} x {frame.height} Mono8 frame in 64-word packets leaves the image's last packet short; "
        "then {reads} spaced reads must still be acknowledged. " + PIXEL_PORT,
        ["Write StreamPacketSizeMax = {spsm} (64-word packets).",
         "Pixel port: send one {frame.width} x {frame.height} byte-ramp frame; test pattern: Width {frame.width}, "
         "Height {frame.height}, acquire {images} image(s).",
         "Read MasterHostConnectionID {reads} times, {read_gap_ms} ms apart.", RESTORE],
        "The image arrives complete in clean packets; every read answers 0x00 with the current value.",
        "A malformed or missing image, or a read unanswered or wrong.",
        {"spsm": 288, "frame": {"width": 64, "height": 8}, "images": 1, "reads": 4, "read_gap_ms": 20}),
    "test_trigger_in_ctrl_packet": E(
        "§8.2.4: a rising trigger inserted {insert_at} characters into a read of MasterHostConnectionID; both must "
        "survive. " + TABLE15 + " " + BENCH.format(what="character link and TRIG_OUT") + IDLE,
        ["Send a falling trigger so the recreated trigger starts low.",
         "Send the read with a Table 15 rising trigger after its first {insert_at} characters, as one CXC1 frame.",
         "Collect the command's acknowledgment, the I/O acknowledgment and the TRIG_OUT edges."],
        "The read answers 0x00 with the register's value; the trigger is acknowledged once and raises the "
        "recreated trigger once (a level, or one strobe).",
        "The command lost, NACKed or answered wrongly, or the trigger not acknowledged or not recreated.",
        {"insert_at": 12}),
}

UVM_NOT_RUNNABLE: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Cases beyond the plan (see the module docstring).  X() gives one: its title,
# the plan cases whose requirements and clauses it inherits (`refines`), an
# objective in the plan's words, and the emulator block (E(...)) of its check
# in cases/<area>/<ID>.cpp.  The ID's area token picks the directory.
# ---------------------------------------------------------------------------
EXTRA_SECTION = "Emulator cases beyond the plan"


def X(title: str, refines: list[str], objective: str, emulator: dict, clauses: str = "") -> dict:
    return {"title": title, "refines": refines, "objective": objective, "emulator": emulator,
            "clauses": clauses}


EXTRA_CASES: dict[str, dict] = {
    # -- control commands
    "CXP-EMU-CTRL-110": X(
        "Partial writes into the user window",
        ["CXP-CAM-CTRL-002"],
        "Writes whose Size is not a multiple of 4 into the user register window change exactly the bytes they "
        "carry (Table 21: Size is the number of bytes written; §10.3 byte-addressed space): the device drives "
        "the APB byte enables (PSTRB), so the pad bytes of the last word leave the slave's bytes as they were.",
        E("The user window is the bench's APB slave behind REG_STALL (0 ms here); it honours PSTRB. "
          + BENCH.format(what="REG_STALL"),
          ["For each Size of {sizes}: write sentinels to every word the write touches; write Size bytes "
           "(0x11, 0x22, ...) at the first; read the words back."],
          "Every write answered 0x01; every word read back holds the written bytes where the write reached and "
          "the sentinel's bytes elsewhere.",
          "A byte past Size changed (zeroed by the pad), a written byte missing, or a write refused.",
          {"sizes": [1, 2, 3, 5, 6, 7]})),
    "CXP-EMU-CTRL-101": X(
        "Pipelined commands",
        ["CXP-CAM-NEG-010", "CXP-CAM-CTRL-001"],
        "Commands sent back to back without waiting for the acknowledgments (§8.6.1.1 has the Host wait): "
        "every one answered in order with the right data while they do not overlap inside the Device; when "
        "they do, the Device keeps what its decision D7 says (the command executing and one waiting) and "
        "neither answers nor executes the rest.",
        E("A bootstrap access completes long before the next command has crossed the low-speed uplink, so "
          "real overlap needs a slow first command: a user-window access behind the bench's REG_STALL slave. "
          + BENCH_TIME + " Without REG_STALL only the plain bursts run.",
          ["Read registers with distinct values for the model; bursts of {burst} reads of distinct registers "
           "sent back to back, idle; the same numbers of writes of MasterHostConnectionID; the read bursts "
           "again while the test pattern streams.",
           "For a user-window slave of {stall_ms} ms: a user read followed at once by {overlap} minus 1 "
           "bootstrap reads, then a user write followed by writes of MasterHostConnectionID; wait the stall "
           "plus {settle_ms} ms; read MasterHostConnectionID and the user word back; a read of Standard."],
          "Every plain burst answered command for command, in order, with the right data, and the last "
          "write's value in place; behind the slow access exactly two final acknowledgments (the slow "
          "command's with its data, then the waiting one's), at most one Wait, no acknowledgment on the link "
          "later, MasterHostConnectionID holding the waiting write's value (the dropped writes not executed), "
          "the slow write landed, and the next read answered.",
          "A lost, duplicated, reordered or wrong acknowledgment in a plain burst; behind the slow access a "
          "third final acknowledgment, a missing one, an acknowledgment after the burst, a dropped write that "
          "was executed, or no answer afterwards.",
          {"burst": [2, 3, 4, 8], "overlap": [2, 3, 4, 8], "stall_ms": [50, 150], "settle_ms": 100,
           "burst_wait_ms": 5000})),
    "CXP-CAM-NEG-007b": X(
        "Headers damaged beyond a vote",
        ["CXP-CAM-NEG-007"],
        "Commands whose TYPE word has two of four characters damaged (no majority, §8.2.2.1), whose Cmd/Size "
        "word or address word (single characters under the CRC) has two damaged, and a packet that ends before "
        "its command word: each answered with a Table 22 error that fits (or, a TYPE nobody can vote, not "
        "answered), none executed, the next command answered.",
        E("Reads and writes of MasterHostConnectionID, damaged by XOR 0x5A in two byte lanes of one header "
          "word; the Table 22 code among the ones that fit a damage is not fixed by the standard (D8 open).",
          ["Write the sentinel {sentinel} to MasterHostConnectionID.",
           "For the TYPE word (lanes P0+P1, P2+P3, P0+P3), the Cmd/Size word (P0+P1, P2+P3) and the address word "
           "(P0+P1, P2+P3): a damaged read and a damaged write, each followed by a read of Standard; wait "
           "{ack_wait_ms} ms for an acknowledgment.",
           "Writes of the sentinel with one TYPE character damaged, each lane.",
           "SOP, 4 x 0x02, EOP; then a read of Standard.",
           "Read MasterHostConnectionID."],
          "TYPE with two damaged: 0x47 or no acknowledgment; Cmd and Size high byte: 0x42, 0x46 or 0x80; Size low "
          "bytes: 0x46 or 0x80; address: 0x80; every read of Standard afterwards answered; one damaged TYPE "
          "character voted (0x01); the stub packet 0x47; MasterHostConnectionID still the sentinel.",
          "A success for a damaged command, a code that does not fit the damage, no answer to the next "
          "command, a TYPE character not voted, or a damaged write executed.",
          {"sentinel": 0x5E471007, "ack_wait_ms": 500})),
    "CXP-EMU-NEG-102": X(
        "24-bit Size wrap",
        ["CXP-CAM-NEG-006"],
        "Reads whose 24-bit Size needs an acknowledgment beyond the packet size limit even where "
        "ceil(Size / 4) wraps in 24 bits (0xFFFFFD .. 0xFFFFFF): 0x45 each, never data; writes with such a "
        "Size refused; the Device answers afterwards.",
        E("Reads of Standard and MasterHostConnectionID; writes of MasterHostConnectionID carrying one word.",
          ["Write the sentinel {sentinel} to MasterHostConnectionID; read ControlPacketSizeMax.",
           "For each Size of {sizes}: a read at 0x0000 and at 0x4008, each followed by a read of Standard; a "
           "write of MasterHostConnectionID declaring that Size with one data word.",
           "Read MasterHostConnectionID."],
          "Every read answered 0x45 in the short form with no data, every following read of Standard answered, "
          "every write answered 0x45 or 0x46, MasterHostConnectionID still the sentinel.",
          "Data or success for a wrapped Size, no answer afterwards, or an oversize write executed.",
          {"sizes": [0xFFFFFC, 0xFFFFFD, 0xFFFFFE, 0xFFFFFF], "sentinel": 0x5E102102})),
    "CXP-EMU-BOOT-103": X(
        "Full 32-bit address decode",
        ["CXP-CAM-NEG-002", "CXP-CAM-BOOT-001"],
        "The command address is 32 bits (Table 21): every Table 45 register's alias with a high address bit "
        "set answers 0x40 unless it is a register of the Device, and a write there (a ConnectionReset alias "
        "first) has no effect.",
        E("Aliases that fall in the manufacturer window (0x10000 .. 0x10FFF), the XML ROM (0x90000000) or the "
          "bench's user window are this Device's registers and are skipped. " + RESTORE,
          ["Write the sentinel {sentinel} to MasterHostConnectionID; note StreamPacketSizeMax and "
           "ConnectionConfig.",
           "Read every Table 45 register's address with one of the bits {alias_bits} set.",
           "Write 1 (ConnectionReset, TestMode), 64 (StreamPacketSizeMax) or a pattern to the aliases of "
           "ConnectionReset, MasterHostConnectionID, StreamPacketSizeMax, ConnectionConfig, TestMode and "
           "DeviceUserID; wait 200 ms.",
           "Read MasterHostConnectionID, StreamPacketSizeMax, ConnectionConfig, TestMode and Standard."],
          "Every alias read and write answered 0x40; the registers unchanged, TestMode 0, Standard read at 0.",
          "Any alias answered otherwise, or a register changed by an alias write (a ConnectionReset among them).",
          {"alias_bits": [16, 17, 20, 24, 28, 31], "sentinel": 0x5E103103})),
    "CXP-CAM-CTRL-006b": X(
        "Control channel reset while a command executes",
        ["CXP-CAM-CTRL-006", "CXP-CAM-CTRL-004"],
        "A control channel reset (0xFF) sent while a slow command executes, before and after its Wait: 0x03, "
        "the aborted command answered before it or never, nothing after it, and the next commands served by "
        "the §8.6.1.1 rules.",
        E("The slow command is a user-window read behind the bench's REG_STALL slave. " + BENCH_TIME + " "
          + BENCH.format(what="REG_STALL and bench times"),
          ["For a slave of {stall_ms} ms: write a value to the user word; send the user read and at once a 0xFF; "
           "then the same with the 0xFF sent after the read's Wait arrived.",
           "Right after the 0x03: a read of Standard, timed.",
           "Wait the stall plus {settle_ms} ms and count the acknowledgments on the link.",
           "With the slave at {short_stall_ms} ms: read the user word."],
          "The 0x03 in the short form, before it only the read's Wait or its right data; the read of Standard "
          "answered once with Standard, its first acknowledgment within 200 ms of the command; no "
          "acknowledgment in the time after; the user read answered with the value.",
          "No 0x03, the aborted read answered after the 0x03, a Wait after it, the next command answered late "
          "(> 200 ms without a Wait) or wrongly, or a stray acknowledgment.",
          {"stall_ms": [150, 500], "settle_ms": 100, "short_stall_ms": 20, "host_wait_ms": 60000})),

    # -- stream, resets and power-on
    "CXP-EMU-SCN-002": X(
        "Reset storm under stream", ["CXP-CAM-CTRL-006", "CXP-CAM-REC-002", "CXP-CAM-REC-003", "CXP-CAM-DATA-003"],
        "Verify that control channel resets leave a running stream alone, and that after a ConnectionReset or a "
        "reset of any one clock domain nothing from before the reset is sent: no packet until "
        "StreamPacketSizeMax is programmed again, then tag 0 and a fresh image.",
        E("A {width} x {height} test-pattern stream (the bench's generator when there is one). The domain "
          "resets need the bench's RESET with a domain mask; without it they are not run. " + RESTORE,
          ["Stream; send {control_resets} control channel resets, then a truncated read followed by one "
           "(acknowledgments collected for {ack_window_ms} ms); wait for {images} more images; read "
           "MasterHostConnectionID and StreamPacketSizeMax.",
           "ConnectionReset while streaming; record {quiet_ms} ms; read the two registers; program them; "
           "AcquisitionStart; wait for {images} images (at most {images_timeout_ms} ms).",
           "The same after a bench reset of the app, the tx and the rx domain alone, each while streaming.",
           "Record {quiet_ms} ms more; read MasterHostConnectionID."],
          "Every control channel reset answered 0x03 and the stream around them clean (Table 19, tags +1, "
          "complete images) with the registers kept; after each ConnectionReset and domain reset no stream "
          "packet until StreamPacketSizeMax is programmed, both registers 0, then a first packet with tag 0 "
          "opening an image header, a clean stream and complete images; nothing reset afterwards.",
          "A control channel reset unanswered or disturbing the stream or the registers; a stream packet "
          "after a Device reset before StreamPacketSizeMax is programmed (a replay); a first packet with a "
          "non-zero tag or in the middle of an image; a reset register not 0; a later phantom reset.",
          {"width": 128, "height": 32, "control_resets": 8, "ack_window_ms": 500, "images": 2,
           "images_timeout_ms": 20000, "quiet_ms": 300})),
    "CXP-CAM-INIT-001b": X(
        "Power-up values after every register was moved off them", ["CXP-CAM-INIT-001"],
        "Verify the connection-reset state after a power-up (§10.3.28) when every register it sets held "
        "another value before, and the stream after power-up: no data packet while StreamPacketSizeMax is 0, "
        "none before AcquisitionStart, the first one tagged 0.",
        E("Power-up is the bench's power-on reset with the test pattern selected (USE_TPG = 1). INIT-001 "
          "reads the reset values from a fresh device; this case dirties them first. "
          "ElectricalComplianceTest is only reported (CT-006 judges it). " + BENCH.format(what="RESET and the "
          "pixel-port straps") + " " + RESTORE,
          ["Power-on reset; write MasterHostConnectionID 0x5A5A1234 and StreamPacketSizeMax (host maximum), "
           "send {test_packets} host test packets with one bad word (a read behind them waits up to {drain_ms} "
           "ms), set TestMode 1 for {test_mode_ms} ms, "
           "write ConnectionConfigDefault's value to ElectricalComplianceTest; read the counters.",
           "Power-on reset; read the registers §10.3.28 sets and the test counters.",
           "Start an acquisition with StreamPacketSizeMax 0 and record {idle_ms} ms.",
           "Power-on reset; program StreamPacketSizeMax, record {idle_ms} ms without AcquisitionStart, then "
           "acquire one image.",
           "Power-on reset with TPG_RUN = 1; record {idle_ms} ms; program StreamPacketSizeMax and record "
           "{idle_ms} ms more."],
          "The counters are off 0 before the reset; after it ConnectionReset, MasterHostConnectionID, "
          "StreamPacketSizeMax, TestMode, TestErrorCountSelector, TestErrorCount, XmlManifestSelector, "
          "HsUpconnection and both packet counters read 0 and ConnectionConfig a discovery value; no stream "
          "or test packet while StreamPacketSizeMax is 0 (also with TPG_RUN = 1), none before "
          "AcquisitionStart; the first stream packet after power-up carries tag 0.",
          "A register not at its reset value, a data packet with StreamPacketSizeMax 0 or before "
          "AcquisitionStart, or a first tag other than 0.",
          {"test_packets": 1, "drain_ms": 120000, "test_mode_ms": 200, "idle_ms": 500})),
    "CXP-CAM-DATA-003b": X(
        "ConnectionConfig write while a stream packet is on the wire", ["CXP-CAM-DATA-003"],
        "Verify that a ConnectionConfig write made while stream packets are being sent restarts the Packet Tag "
        "at 0 with the next packet and at no other point, and that the packet it meets and the image stream "
        "stay well formed (§8.5.3, §10.3.33).",
        E("A {width} x {height} test-pattern stream at {spsm}-byte packets (the bench's generator when there "
          "is one); DATA-003 writes ConnectionConfig between acquisitions, this case while packets flow. "
          "The same value is written, so the link speed does not change. " + RESTORE,
          ["Start the acquisition; after the first image header write ConnectionConfig with its own value "
           "{writes} times, {gap_ms_range} ms apart (random, seeded).",
           "Wait for one more image; stop; judge the recording against the acknowledgment times."],
          "Every write answered 0x01; every stream packet complete and matching Table 19; the first packet "
          "after each acknowledgment carries tag 0; tags +1 mod 256 everywhere else; every image complete, "
          "or cut at a write and followed by the next image.",
          "A missing tag restart, a tag break away from a write, a torn or cut packet, or an image torn "
          "away from a write.",
          {"width": 512, "height": 128, "spsm": [4096], "writes": 30, "gap_ms_range": [20, 200]})),
    "CXP-CAM-BND-003b": X(
        "StreamPacketSizeMax below 36 bytes and not a multiple of 4", ["CXP-CAM-BND-003", "CXP-CAM-INIT-007"],
        "Verify the Device's answer to a StreamPacketSizeMax that is not a multiple of 4 (§10.3.32) or too small "
        "for one data word (below 36 bytes), idle and while streaming, and that the Host's value streams again "
        "afterwards (decision D4).",
        E("A {width} x {height} test-pattern stream (the bench's generator when there is one) at the host "
          "maximum; decision D4: not a multiple of 4 is refused 0x41 and the value kept, a multiple of 4 below "
          "36 accepted and the stream held. " + RESTORE,
          ["Idle: write StreamPacketSizeMax {not_multiple} (each refused?) and read it back.",
           "Idle, for each of {below_36}: write it, read it back, run the acquisition for {hold_ms} ms, read "
           "Standard, write the host maximum back and acquire {images} images.",
           "Streaming, for each of {below_36}: after the first image header write {not_multiple} (refused), "
           "then the small value; after {hold_ms} ms read Standard, write the host maximum back and wait for "
           "{images} more images (at most {resume_timeout_ms} ms)."],
          "{not_multiple} answered 0x41 with the register unchanged, idle and streaming; each of {below_36} "
          "answered 0x01 and read back; no stream packet during the idle acquisitions; no packet longer than "
          "the small value between its 0x01 and the next write; the device answers reads throughout; the host "
          "maximum brings the stream back: packets within it, Table 19, tags +1, {images} complete images.",
          "A value not a multiple of 4 accepted or the register changed, a small value refused, a packet "
          "longer than the value in force, a wedged stream or control channel, or incomplete images after "
          "the host value came back.",
          {"width": 128, "height": 32, "not_multiple": [35, 1], "below_36": [32, 4], "hold_ms": 500, "images": 2,
           "resume_timeout_ms": 20000})),
    "CXP-EMU-DATA-104": X(
        "StreamPacketSizeMax changed while streaming", ["CXP-CAM-INIT-007", "CXP-CAM-BND-003", "CXP-CAM-DATA-002"],
        "Verify that a StreamPacketSizeMax the Host writes while the Device streams bounds packets after the "
        "write takes effect, and that the stream goes on unbroken (packet tags, complete images).",
        E("A {width} x {height} test-pattern stream (the bench's generator when there is one). The plan's "
          "bounds are INIT-007 and BND-003 between acquisitions; this is the same register written without "
          "stopping. " + RESTORE,
          ["Set Width x Height to {width} x {height}, StreamPacketSizeMax {spsm_start} bytes; start the "
           "acquisition and wait for the first image header.",
           "Without stopping, write StreamPacketSizeMax {spsm_steps} in turn, each after {images_per_step} "
           "more image headers (at most {step_timeout_ms} ms each).",
           "Stop; judge every packet against the last acknowledged value. A first packet after an acknowledgment "
           "may have started before it and may use the preceding limit."],
          "Every write answered 0x01; after at most one prior-limit packet in flight, packets are no longer "
          "than the value written (whole packet, bytes); each value carried packets; every packet matches Table 19 and the "
          "tags go on +1 mod 256 across the writes; the images are complete ({images_per_step} per value).",
          "A write refused, a packet after the one possible in-flight packet longer than the new value, "
          "a Table 19 defect, a tag break, an "
          "incomplete image or a stalled stream.",
          {"width": 256, "height": 64, "spsm_start": [4096], "spsm_steps": [256, 40, 4096], "images_per_step": 2,
           "step_timeout_ms": 20000})),

    # -- host triggers (uplink)
    "CXP-EMU-PROT-107": X(
        "A trigger and a command with no gap", ["CXP-CAM-PROT-006", "CXP-CAM-TRIG-001"],
        "Verify that a Table 15 trigger immediately before a command's SOP or immediately after its EOP (no IDLE "
        "between, §8.2.4: any character boundary) is taken as a trigger and the command as a command.",
        E(BENCH.format(what="character link and TRIG_OUT") + " MasterHostConnectionID is restored afterwards.",
          ["Bench reset, inputs 0.",
           "For each Delay of {delays}, trigger first and command first: one falling trigger, then in one character "
           "frame a rising trigger and a write of a fresh MasterHostConnectionID value; again with a read of it."],
          "Every write answered 0x01 and every read 0x00 with the value written; every trigger answered by one I/O "
          "acknowledgment and recreated once.",
          "A command lost or spoiled by the trigger next to it, or a trigger not acknowledged or not recreated.",
          {"delays": [0, 239]})),
    "CXP-EMU-TRIG-106": X(
        "Triggers back to back", ["CXP-CAM-TRIG-001", "CXP-CAM-TRIG-002"],
        "Verify that Table 15 triggers sent with no IDLE between them are each acknowledged and each recreated "
        "at its own event time (Figure 20): the tightest retrigger the low-speed link allows, since a packet lasts "
        "60 bits and a Delay less than 10.",
        E("Rising, falling and rising triggers in one character frame, no IDLE between them. With bench times the "
          "spacing of the two recreated rising edges is judged. " + BENCH.format(what="character link and TRIG_OUT"),
          ["Bench reset, inputs 0.",
           "For each Delay pair (first, third) of {delay_pairs}: one falling trigger; then rising (first Delay), "
           "falling (Delay 120), rising (third Delay) back to back.",
           "With bench times: the recreated rising edges' spacing against the leaders' spacing plus (third - "
           "first) x bit / 24.",
           "A read."],
          "Three acknowledgments and two recreated rising edges per sequence; their spacing equals the leaders' "
          "spacing plus the Delay difference within {rx_clk_ns} ns (one rx_clk) + {grain_ns} ns; the read "
          "answers.",
          "A trigger not acknowledged or not recreated, a recreated edge moved by the neighbouring packet, or a "
          "link that no longer answers.",
          {"delay_pairs": [[0, 239], [239, 0], [200, 200]], "rx_clk_ns": 10, "grain_ns": 1})),
    "CXP-EMU-TRIG-107": X(
        "A resent trigger packet", ["CXP-CAM-TRIG-001"],
        "Verify that a Table 15 trigger packet the Host resends (§8.3.3: allowed when the acknowledgment does not "
        "come within the timeout) is acknowledged again but recreates no second trigger event: the Host's "
        "trigger signal did not change.",
        E(BENCH.format(what="character link and TRIG_OUT"),
          ["Bench reset, inputs 0; one falling trigger.",
           "A rising trigger and {resends} copies of it back to back in one character frame.",
           "One falling trigger; a rising trigger and {resends} copies of it, each sent after the previous one "
           "was acknowledged."],
          "Every packet acknowledged ({resends} + 1 per step); one recreated rising edge per step.",
          "A packet not acknowledged, or a resent packet recreated as a new trigger event.",
          {"resends": 2})),
    "CXP-CAM-TRIG-001b": X(
        "Triggers on an extension connection", ["CXP-CAM-TRIG-001"],
        "Verify that a Table 15 trigger arriving on an extension connection has no effect: §8.3 defines the I/O "
        "channel (triggers and I/O acknowledgments) for the Master connection (connection 0) only.",
        E("The connection is made an extension one by the bench strap (EXT_LINK = 1, from_extension_link_i). "
          + BENCH.format(what="character link, TRIG_OUT and EXT_LINK"),
          ["Bench reset, inputs 0; one falling trigger puts the recreated trigger low.",
           "EXT_LINK = 1: {triggers} triggers, rising and falling alternately, Delays spread over 0..239.",
           "EXT_LINK = 0: a read; one rising trigger."],
          "No I/O acknowledgment and no recreated rising edge on the extension connection; back on the master "
          "connection the read answers and the rising trigger is acknowledged once and recreated once.",
          "A trigger acknowledged or acted on over the extension connection, or the master connection not "
          "working after.",
          {"triggers": 6})),

    # -- device triggers and TestMode
    "CXP-CAM-CT-003b": X(
        "TestMode entered during a stream and an unacknowledged device trigger",
        ["CXP-CAM-CT-003", "CXP-CAM-CT-007"],
        "Verify that TestMode entered while the Device streams and while its trigger waits for the Host's "
        "acknowledgment completes the stream packet on the wire, starts no new one, keeps the trigger and I/O "
        "channel working (D2) with no lost or stale packet, and that streaming resumes afterwards.",
        E("Device events are edges of the bench trigger input; the stream is the test pattern. "
          + BENCH.format(what="TRIG_IN and the character link"),
          ["Reset the device through the bench with the trigger input low; select the test pattern; record.",
           "AcquisitionStart; after the first image header raise the trigger input and wait for its packet.",
           "Without acknowledging it: TestMode = 1; after {ack_after_ms} ms acknowledge it; two host triggers "
           "(rising, falling); lower the trigger input and acknowledge its packet.",
           "Hold {hold_ms} ms; TestMode = 0; wait for two more image headers; AcquisitionStop."],
          "Every connection-test packet intact; no stream packet between the first and the last test packet; every "
          "stream packet Table 19 (none torn) with tags +1 across TestMode; every image that starts after TestMode "
          "complete; the device's trigger packets R then F (a resend of an unacknowledged level allowed); one clean "
          "I/O acknowledgment per host trigger and no other.",
          "A torn stream packet, a stream packet in TestMode, a tag restart, an incomplete image after TestMode, a "
          "lost, doubled or stale trigger or acknowledgment.",
          {"ack_after_ms": 20, "hold_ms": 100})),
    "CXP-CAM-TRIG-004b": X(
        "Withheld and late I/O acknowledgments, edge bursts",
        ["CXP-CAM-TRIG-004"],
        "Verify §8.3.3 from the device's side when the Host does not keep up: after a trigger packet no new one "
        "until the Host's acknowledgment or the Device's transmission timeout, and whatever edges merge, the Host "
        "ends at the trigger's level.",
        E("Device events are edges of the bench trigger input; the device's transmission timeout is its design "
          "value ({timeout_ns} ns on this bench). " + BENCH_TIME + " "
          + BENCH.format(what="TRIG_IN, the character link and bench times"),
          ["Reset the device through the bench with the input de-asserted.",
           "Host silent: bursts of {bursts} edges, {edge_gap_us} us of the device's time apart (faster than a "
           "host acknowledgment's round trip and the timeout); wait {settle_ms} ms after each; bring the input back low.",
           "Host acknowledging each trigger as soon as it sees it: the same bursts."],
          "Every packet clean Table 16; after each burst the last packet (or none, when the input is back at the "
          "level the host holds) leaves the host at the input's level; each trigger starts no earlier than the end "
          "of the host's next acknowledgment on the uplink, or {timeout_ns} ns after the previous trigger.",
          "A malformed packet, a host left at the wrong level, or a trigger before the acknowledgment or the timeout.",
          {"bursts": [2, 4, 8], "edge_gap_us": 8, "settle_ms": 300, "timeout_ns": 40960})),
    "CXP-EMU-TRIG-105": X(
        "Device trigger input polarity",
        ["CXP-CAM-TRIG-004"],
        "Verify that a device trigger packet carries the edge of the trigger signal (Table 16: K28.4 rising, "
        "K28.2 falling) whatever the sense of the trigger input: with an active-low input the pin's falling edge "
        "is the trigger's rising edge.",
        E("The trigger input and its polarity strap are bench inputs (TRIG_IN, TRIG_POLARITY). "
          + BENCH.format(what="TRIG_IN, TRIG_POLARITY and the character link"),
          ["For polarity 0 (active high) and 1 (active low): reset the device through the bench with the input at "
           "its de-asserted level (or drive the pins without a bench reset); acknowledge what the start sent.",
           "{cycles} times: assert the input, wait for the trigger packet, acknowledge it (Table 17), wait {gap_ms} "
           "ms; de-assert it, the same."],
          "For both polarities every assertion gives one 4 x K28.4 + 4 x Delay packet and every de-assertion one "
          "4 x K28.2 packet, in order, each clean with Delay 0..3.",
          "A missing, extra or wrong-kind packet (the pin level sent instead of the trigger's), or a malformed one.",
          {"cycles": 3, "gap_ms": 20})),

    # -- link test and uplink robustness
    "CXP-EMU-REC-109": X(
        "Uplink bit-rate tolerance", ["CXP-CAM-REC-004", "CXP-CAM-PROT-005"],
        "Verify that the Device's low-speed receiver takes a Host whose bit rate is off the Device's clock by "
        "up to the sum of the two ends' tolerances (§6.7: each +-100 ppm), and measure how far beyond that it "
        "still works.",
        E("The host's bit rate is offset through the bench (UPLINK_PPM); the Device's clock is exact, so the "
          "offset is the relative error of the two. " + BENCH.format(what="UPLINK_PPM"),
          ["For each offset in {ppm} ppm, positive and negative: set it, one read to settle, then {reads} reads "
           "of Standard.",
           "Back at 0 ppm: one read to settle, then {reads} reads."],
          "Every read answered 0x00 with Standard at offsets up to +-{required_ppm} ppm and back at 0 ppm; "
          "beyond, the first offset per sign that loses a read is recorded.",
          "A lost or wrong read at +-{required_ppm} ppm or less, or after the return to 0 ppm.",
          {"ppm": [100, 200, 1000, 5000, 10000], "reads": 20, "required_ppm": 200, "read_wait_ms": 20000})),
    "CXP-CAM-CT-004b": X(
        "Test receiver: the last payload words and a K character in the body", ["CXP-CAM-CT-004"],
        "Verify that the Device's Test Receiver compares every one of the 1024 payload words of a Table 23 "
        "packet, the last two included, and that a K character inside the payload counts as one different "
        "word of a packet that still counts (§8.7.1, §8.7.2).",
        E("Host test packets as character streams (CXC1) with one IDLE word after each. "
          + BENCH.format(what="character link"),
          ["Clear TestErrorCount and TestPacketCountRx.",
           "For each set of payload words in {corrupt_sets}: one Table 23 packet with those words corrupted "
           "(one byte lane each); read both counters.",
           "One packet with the data character in lane P0 of payload word {k_word} replaced by K28.5; read.",
           "One clean packet; read."],
          "After each packet TestPacketCountRx is one more and TestErrorCount grows by the number of words "
          "that differ: the corrupted ones, and one for the word carrying the K28.5.",
          "A corrupted word not counted (the last two in particular), a packet lost or counted twice, or an "
          "error counted for the clean packet.",
          {"corrupt_sets": [[1022, 1023], [1023], [0, 1023]], "k_word": 500, "host_wait_ms": 600000})),
    "CXP-CAM-CT-005b": X(
        "Counter clear in the middle of a test-packet burst", ["CXP-CAM-CT-005", "CXP-CAM-CT-004"],
        "Verify that writing 0 to TestErrorCount and TestPacketCountRx while host test packets are arriving "
        "clears exactly what came before the write and counts everything after it (§8.7.3, §10.3.37, §10.3.39).",
        E("On one low-speed upconnection a command can only sit between two test packets, so the clear falls "
          "exactly between two of them: one character stream (CXC1) carries the burst with one IDLE word after "
          "every packet, the two clearing writes, and the rest of the burst. "
          + BENCH.format(what="character link"),
          ["Clear the counters; send {test_packets} Table 23 packets, each with {bad_words} corrupted words, "
           "with writes of 0 to TestErrorCount and to TestPacketCountRx (8 bytes) between packet {clear_after} "
           "and the next, as one character stream.",
           "Read TestErrorCount and TestPacketCountRx once the burst is through.",
           "Send {after_packets} clean packets; read both again."],
          "Both writes answered 0x01; TestPacketCountRx equals the packets after the clear and TestErrorCount "
          "their corrupted words; the clean packets add to the packet counter only.",
          "A write not answered 0x01, a counter that kept what came before the clear or lost what came after "
          "it, or a clear of one counter that moved the other.",
          {"test_packets": 12, "clear_after": 5, "bad_words": 2, "after_packets": 3, "host_wait_ms": 1200000})),

    # -- pixel port and scenarios
    "CXP-EMU-PIX-108": X(
        "Malformed pixel-port framing",
        ["CXP-CAM-IMG-002", "CXP-CAM-IMG-003", "CXP-CAM-IMG-011"],
        "Verify that a sensor frame whose framing breaks the pixel port's rules (SOF inside a frame, a short or "
        "long line, stray pixels, a missing or early EOF) does not corrupt stream packets or disturb the next "
        "frame; and that short well-formed frames sent back to back keep their own "
        "header metadata.",
        E("The malformed frames go in with bench PIXEL_BEATS (SOF / EOL / EOF given per beat), the good ones with "
          "PIXEL_FRAME, Mono8, inside an acquisition. A broken frame may already have put its header on the "
          "link before a later pixel proves malformed. " + BENCH.format(what="pixel port and PIXEL_BEATS"),
          ["Reset the device through the bench, select the pixel port.",
           "For each kind of {kinds}: send the malformed {frame.width} x {frame.height} frame (for "
           "metadata_back_to_back: {short_frames} well-formed frames of 1..3 x 1..2 pixels back to back instead), "
           "then a well-formed frame with its own SourceTag; record the stream.",
           "Judge every stream packet (Table 19, tags); compare the good frames with what was sent; read Standard."],
          "Every stream packet matches Table 19 with tags +1; every good frame comes back with its own header "
          "and bit for bit; the device answers the read after each kind.",
          "A packet that breaks Table 19, a good frame lost, altered or carrying another frame's metadata, "
          "a PacketTag break, or no answer.",
          {"kinds": ["sof_mid_frame", "short_line", "long_line", "stray_pixels", "missing_eof", "early_eof",
                     "metadata_back_to_back"],
           "frame": {"width": 16, "height": 4}, "short_frames": 6})),
    "CXP-EMU-SCN-001": X(
        "Everything at once",
        ["CXP-CAM-IMG-011", "CXP-CAM-CTRL-001", "CXP-CAM-CT-004", "CXP-CAM-TRIG-001"],
        "Verify that the stream, register access, host connection-test packets and host triggers, all at the same "
        "time, each stay correct: no stream packet or image damaged, no command lost or answered wrongly, every "
        "test packet counted without error, every trigger acknowledged and recreated.",
        E("One host thread interleaves the traffic; the link queues it, so the device sees commands, test packets "
          "and triggers arrive while it streams. The register model covers MasterHostConnectionID (random values) "
          "and read-only registers (StreamPacketSizeMax and ConnectionConfig are left alone: they restart packets "
          "and tags). " + BENCH.format(what="character link and TRIG_OUT"),
          ["Reset the device through the bench; Mono8, TestPattern Bars, {image.width} x {image.height}; clear the "
           "test counters; put the recreated trigger low.",
           "Start the acquisition; once the first image header is in, run {rounds} rounds of: one host test packet, "
           "{commands_per_round} random reads (model registers) or MasterHostConnectionID writes, "
           "{triggers_per_round} Table 15 triggers of random kind and Delay; then wait for {images_after} more "
           "images and stop.",
           "Judge the stream (Table 19, tags, framing, pixels against the golden Bars model), every command against "
           "the model, TestPacketCountRx / TestErrorCount, the I/O acknowledgments and the recreated trigger."],
          "The stream scoreboard is clean with at least {min_complete} complete images equal to the golden model; "
          "every command is answered as the model predicts; TestPacketCountRx equals the test packets sent with "
          "TestErrorCount 0; every trigger gets one Table 17 acknowledgment and the recreated trigger follows them.",
          "A damaged packet or image, a lost or wrong answer, a test packet not counted or counted with errors, a "
          "missing acknowledgment or a wrong recreated edge.",
          {"image": {"width": 64, "height": 32}, "rounds": 6, "commands_per_round": 4, "triggers_per_round": 2,
           "images_after": 2, "min_complete": 2, "cmd_timeout_ms": 20000, "image_timeout_ms": 20000})),

}

EXTRA_TIMEOUT_SCALE: dict[str, float] = {
}


def extra_cases(plan_cases: list[dict]) -> list[dict]:
    by_id = {c["id"]: c for c in plan_cases}
    stale = set(EXTRA_TIMEOUT_SCALE) - set(EXTRA_CASES)
    if stale:
        raise SystemExit(f"EXTRA_TIMEOUT_SCALE names unknown cases: {sorted(stale)}")
    cases = []
    for cid, x in EXTRA_CASES.items():
        m = re.fullmatch(r"CXP-(CAM|EMU)-([A-Z]+)-(\d+)([a-z]?)", cid)
        if not m or (m.group(1) == "CAM") != bool(m.group(4)) or cid in by_id:
            raise SystemExit(f"{cid}: an extra case is CXP-CAM-<AREA>-<n><letter> or CXP-EMU-<AREA>-<n>")
        unknown = [r for r in x["refines"] if r not in by_id]
        if unknown or not x["refines"]:
            raise SystemExit(f"{cid}: refines unknown or no plan cases {unknown}")
        if m.group(1) == "CAM" and x["refines"][0] != cid[:-1]:
            raise SystemExit(f"{cid}: a variant refines its plan case {cid[:-1]} first")
        reqs, seen = [], set()
        for r in x["refines"]:
            for q in by_id[r]["requirements"]:
                if q["id"] not in seen:
                    seen.add(q["id"])
                    reqs.append(q)
        emu = x["emulator"]
        cases.append({
            "id": cid, "title": x["title"], "area": m.group(2), "section": EXTRA_SECTION,
            "class": "RTL/SIM", "automation": "AUTOMATED", "kind": "executable", "hardware_dependent": False,
            "requirements": reqs,
            "clauses": x["clauses"] or ", ".join(by_id[r]["clauses"] for r in x["refines"] if by_id[r]["clauses"]),
            "objective": x["objective"],
            "preconditions": "", "equipment": ["Emulator host (src/emu/host) over the FIFO link"],
            "procedure": [], "stimulus": "", "expected": "",
            "pass_criteria": emu["pass_criteria"], "fail_criteria": emu["fail_criteria"], "evidence": "",
            "refines": list(x["refines"]),
            "emulator": dict(emu, timeout_scale=float(EXTRA_TIMEOUT_SCALE.get(cid, 1.0))),
        })
    return cases

# Host test packets cross the serial uplink bit by bit (TEST_PACKET_S): each
# test's burst ahead of the counter read, from its test_packets.
def _uvm_packets(test: str) -> int:
    tp = UVM_EMULATOR[test]["params"]["test_packets"]
    return sum(tp.values()) if isinstance(tp, dict) else tp


UVM_TIMEOUT_SCALE: dict[str, float] = {
    t: test_packet_scale(_uvm_packets(t))
    for t in ("test_linktest_clean", "test_linktest_inject", "test_arbiter_preempt")
}


# ---------------------------------------------------------------------------
# plan parsing
# ---------------------------------------------------------------------------
def md_text(s: str) -> str:
    """Strip the markdown the GUI does not render: links, bold, code ticks."""
    s = re.sub(r"\[([^\]]+)\]\(#[^)]*\)", r"\1", s)
    s = s.replace("**", "").replace("`", "")
    return s.strip()


def parse_requirements(text: str) -> dict:
    reqs = {}
    for m in re.finditer(r'^\| <a id="req-[^"]+"></a>`(REQ-[A-Z]+-\d+)` \| ([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|',
                         text, re.M):
        rid, _ver, clause, desc, level = (g.strip() for g in m.groups())
        reqs[rid] = {"id": rid, "level": level, "clause": clause, "text": md_text(desc)}
    return reqs


def field(body: str, label: str) -> str:
    m = re.search(r"^- \*\*" + re.escape(label) + r":\*\* (.*)$", body, re.M)
    return md_text(m.group(1)) if m else ""


def sub_list(body: str, label: str, numbered: bool) -> list[str]:
    m = re.search(r"^- \*\*" + re.escape(label) + r":\*\*\n((?:  .*\n)+)", body, re.M)
    if not m:
        return []
    pat = r"^  \d+\. (.*)$" if numbered else r"^  - (.*)$"
    return [md_text(x) for x in re.findall(pat, m.group(1), re.M)]


def parse_cases(text: str, reqs: dict) -> list[dict]:
    sections = [(m.start(), m.group(1).strip()) for m in re.finditer(r"^## (\d+\. .*)$", text, re.M)]
    heads = list(re.finditer(r"^#### (CXP-CAM-([A-Z]+)-\d+) — (.*)$", text, re.M))
    cases = []
    for i, m in enumerate(heads):
        cid, area, title = m.group(1), m.group(2), md_text(m.group(3))
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end():end]
        nxt = re.search(r"^## ", body, re.M)
        if nxt:
            body = body[:nxt.start()]
        badge = body.strip().splitlines()[0]
        ticks = re.findall(r"`([^`]+)`", badge)
        kind = re.search(r"kind: (.*)$", badge)
        section = [s for pos, s in sections if pos < m.start()][-1]
        req_line = field(body, "Requirement(s)")
        rids = re.findall(r"REQ-[A-Z]+-\d+", req_line)
        clauses = req_line.split("CXP 1.1.1 clause(s):")[-1].strip() if "clause" in req_line else ""
        test_class = ticks[1] if len(ticks) > 1 else ""
        hw = "HARDWARE-DEPENDENT" in badge
        case = {
            "id": cid, "title": title, "area": area, "section": section,
            "class": test_class, "automation": ticks[0] if ticks else "",
            "kind": kind.group(1).strip() if kind else "", "hardware_dependent": hw,
            "requirements": [reqs.get(r, {"id": r, "level": "", "clause": "", "text": ""}) for r in rids],
            "clauses": clauses,
            "objective": field(body, "Objective"),
            "preconditions": field(body, "Preconditions"),
            "equipment": sub_list(body, "Test equipment", numbered=False),
            "procedure": sub_list(body, "Procedure", numbered=True),
            "stimulus": field(body, "Stimulus"),
            "expected": field(body, "Expected result"),
            "pass_criteria": field(body, "PASS criteria"),
            "fail_criteria": field(body, "FAIL criteria"),
            "evidence": field(body, "Evidence"),
        }
        if cid in EMULATOR:
            case["emulator"] = dict(EMULATOR[cid], timeout_scale=float(TIMEOUT_SCALE.get(cid, 1.0)))
        elif cid in NOT_RUNNABLE:
            case["emulator"] = {"runnable": False, "reason": NOT_RUNNABLE[cid]}
        elif hw or test_class.startswith("Hardware"):
            case["emulator"] = {"runnable": False, "reason": HW_REASON}
        else:
            raise SystemExit(f"{cid}: no emulator entry and not a hardware test")
        cases.append(case)
    return cases


# ---------------------------------------------------------------------------
# UVM test parsing (read only: nothing under src/verif/ is imported or run)
# ---------------------------------------------------------------------------
def uvm_tiers() -> dict[str, list[str]]:
    """Regression tiers per test, from the src/verif/ Makefile's *_TESTS lists."""
    text = UVM_MAKEFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    lists = {m.group(1).lower(): m.group(2).split()
             for m in re.finditer(r"^([A-Z]+)_TESTS\s*:=\s*(.*)$", text, re.M)}

    def expand(words: list[str]) -> list[str]:
        out = []
        for w in words:
            ref = re.fullmatch(r"\$\(([A-Z]+)_TESTS\)", w)
            out += expand(lists.get(ref.group(1).lower(), [])) if ref else [w]
        return out

    tiers: dict[str, list[str]] = {}
    for tier, words in lists.items():
        for t in expand(words):
            tiers.setdefault(t, []).append(tier)
    return tiers


def uvm_tests() -> list[dict]:
    """The test_* classes of all_tests.py in file order, with their plan tags."""
    src = UVM_TESTS.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()
    # Module constants the class attributes are built from (_PLAN_STREAM, _F_*).
    ns: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                ns[node.targets[0].id] = eval(compile(ast.Expression(node.value), str(UVM_TESTS), "eval"),
                                              {"__builtins__": {}}, dict(ns))
            except Exception:
                pass
    tiers = uvm_tiers()
    tests = []
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name.startswith("test_")):
            continue
        attrs = {}
        for st in node.body:
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                if st.targets[0].id in ("PLAN", "PLAN_PARTIAL", "EXPECT_FAIL"):
                    attrs[st.targets[0].id] = eval(compile(ast.Expression(st.value), str(UVM_TESTS), "eval"),
                                                   {"__builtins__": {}}, dict(ns))
        # The "# <n>. <title>" banner above the class (and its helper
        # sequences, if any) names it; the previous test class ends the search.
        title = node.name
        for ln in reversed(lines[:node.lineno - 1]):
            if ln.startswith("class test_"):
                break
            m = re.match(r"^# (\d+[a-z]?)\. (.+)$", ln)
            if m:
                title = m.group(2).strip().rstrip(".")
                break
        doc = ast.get_docstring(node) or ""
        tests.append({
            "name": node.name, "title": title, "line": node.lineno,
            "doc": " ".join(doc.split()),
            "plan": list(attrs.get("PLAN", ())),
            "plan_partial": dict(attrs.get("PLAN_PARTIAL", {})),
            "expect_fail": [{"scoreboard": sb, "kind": kind, "finding": " ".join(str(why).split())}
                            for (sb, kind), why in attrs.get("EXPECT_FAIL", {}).items()],
            "tiers": tiers.get(node.name, []),
        })
    return tests


def uvm_cases(tests: list[dict], plan_cases: list[dict]) -> list[dict]:
    names = {t["name"] for t in tests}
    stale = (set(UVM_EMULATOR) | set(UVM_NOT_RUNNABLE) | set(UVM_TIMEOUT_SCALE)) - names
    if stale:
        raise SystemExit(f"entries for unknown UVM tests: {sorted(stale)}")
    by_id = {c["id"]: c for c in plan_cases}
    cases = []
    for t in tests:
        name = t["name"]
        unknown = [p for p in t["plan"] if p not in by_id]
        if unknown:
            raise SystemExit(f"{name}: PLAN names unknown plan cases {unknown}")
        reqs, seen = [], set()
        for p in t["plan"]:
            for r in by_id[p]["requirements"]:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    reqs.append(r)
        served = [f"{p} (partly: {t['plan_partial'][p]})" if p in t["plan_partial"] else p for p in t["plan"]]
        case = {
            "id": "UVM-" + name, "title": t["title"], "area": "UVM", "section": UVM_SECTION,
            "class": "RTL/SIM", "automation": "AUTOMATED", "kind": "executable", "hardware_dependent": False,
            "requirements": reqs,
            "clauses": ", ".join(by_id[p]["clauses"] for p in t["plan"] if by_id[p]["clauses"]),
            "objective": t["doc"] or f"The PyUVM test {name}: {t['title']}.",
            "preconditions": "",
            "equipment": ["FPGA/RTL testbench: cocotb + pyuvm on Verilator (src/verif/)"],
            "procedure": [],
            "stimulus": "",
            "expected": "",
            "pass_criteria": "",
            "fail_criteria": "",
            "evidence": "",
            "uvm": {
                "test": name,
                "source": f"src/verif/uvm/tests/all_tests.py:{t['line']}",
                "plan": served,
                "expect_fail": t["expect_fail"],
                "tiers": t["tiers"],
            },
        }
        if name in UVM_EMULATOR:
            case["emulator"] = dict(UVM_EMULATOR[name], timeout_scale=float(UVM_TIMEOUT_SCALE.get(name, 1.0)))
        elif name in UVM_NOT_RUNNABLE:
            case["emulator"] = {"runnable": False, "reason": UVM_NOT_RUNNABLE[name]}
        else:
            raise SystemExit(f"{name}: no UVM_EMULATOR or UVM_NOT_RUNNABLE entry")
        cases.append(case)
    # Each plan case names the UVM tests that serve it.
    for c in plan_cases:
        c["uvm_tests"] = [t["name"] for t in tests if c["id"] in t["plan"]]
    return cases


def build() -> str:
    text = PLAN.read_text(encoding="utf-8")
    reqs = parse_requirements(text)
    cases = parse_cases(text, reqs)
    ids = {c["id"] for c in cases}
    stale = (set(EMULATOR) | set(NOT_RUNNABLE) | set(TIMEOUT_SCALE)) - ids
    if stale:
        raise SystemExit(f"entries for unknown plan cases: {sorted(stale)}")
    uvm = uvm_cases(uvm_tests(), cases)
    extras = extra_cases(cases)
    # Physical/electrical measurements, including the rate, disconnect and
    # cable cases filed in other plan sections, stay in the source plan.
    cases = [c for c in cases if not c["section"].startswith("19. Hardware/Electrical")
             and c["id"] not in ELECTRICAL_CASES]
    cases += extras
    cases += uvm
    ver = re.search(r"^\| Document version \| ([^|]+) \|", text, re.M)
    date = re.search(r"^\| Generation date \| ([^|]+) \|", text, re.M)
    doc = {
        "schema": "cxp-validation-cases/1",
        "source": {
            "plan": "src/emu/host/validation/cxp_camera_validation_plan.md",
            "plan_version": ver.group(1).strip() if ver else "",
            "generated": date.group(1).strip() if date else "",
            "generator": "src/emu/host/validation/gen_validation_cases.py",
            "uvm_tests": "src/verif/uvm/tests/all_tests.py",
        },
        "summary": {
            "cases": len(cases),
            "runnable_on_emulator": sum(c["emulator"]["runnable"] for c in cases),
            "uvm_tests": len(uvm),
            "uvm_runnable_on_emulator": sum(c["emulator"]["runnable"] for c in uvm),
        },
        "cases": cases,
    }
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="fail if the catalogue is out of date")
    args = ap.parse_args()
    out = build()
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != out:
            print(f"{OUT.name} is out of date; run {Path(__file__).name}", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(out, encoding="utf-8")
    doc = json.loads(out)
    print(f"wrote {OUT} ({doc['summary']['cases']} cases, {doc['summary']['runnable_on_emulator']} runnable)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
