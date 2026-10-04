"""Decisions the checks depend on — one constant per decision, one place.

Where the standard leaves a choice to the device, or where the IP's
behaviour today is not yet accepted as the intended one, the expected
value of a check is a *decision*, not a fact.  D1–D10 come from
`docs/verification/validation/cxp_validation_test_proposal_260921.md` ("Decisions
needed before the checks can be written").

Rule: a constant that is ``None`` is **undecided**.  A check that reads it
logs the behaviour it observes at INFO and does not gate.  Recording a
decision means setting the constant here *and* adding it to the
"Decisions" table of `docs/verification/cxp_top_level_testplan.md`, in the same commit.
Do not decide by editing only one of the two.

Each comment names the requirement and the tests that read the constant.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# D1 — REQ-PROT-019.  Reserved or wrong-direction packet type on the
# uplink: silently discarded, or acknowledged 0x47?
#   None | "discard" | 0x47
#   Read by: T-12.
# Decided: discard.  Table 22's codes answer a control command (Table 21);
# a packet whose type is not the host's command type (Table 18) is not a
# command, so nothing acknowledges it: no acknowledgment, no access, no
# error pulse, and the commands around it are answered as usual.
D1_UPLINK_BAD_TYPE = "discard"

# D2 — REQ-PROT-027.  Triggers and I/O acknowledgments while TestMode = 1:
# allowed, or suppressed?
#   None | "allowed" | "suppressed"
#   Read by: C-06, CXP-CAM-CT-007.
# Decided: allowed.  §8.3.3 has no TestMode exemption ("Trigger packets
# shall be acknowledged") and §8.3.2 inserts a trigger into any
# lower-priority packet; §8.7.4 limits *data* in TestMode to test and
# control packets.  Triggers and I/O acknowledgments go out in TestMode,
# inserted into the test packets; nothing is held for the exit.
D2_TESTMODE_TRIGGERS = "allowed"

# D3 — REQ-ERR-012.  Acknowledgment code for a write that arrives on an
# extension connection (0x43 today), and whether a MasterHostConnectionID
# write there is ignored silently.
#   None | ack code (int)
#   Read by: V-02 test_extension_link_access.
# Decided: any other write there is refused 0x43 (§5.1); ConnectionReset
# and MasterHostConnectionID written there are ignored — acknowledged
# 0x01 like any write (§8.6.1.1: one acknowledgment per command), nothing
# executed (§10.3.28 / §10.3.30 notes); a 0xFF there is executed, as it
# resets the control channel, not the connection.
D3_EXTENSION_WRITE_CODE = 0x43
D3_EXTENSION_MHCID_SILENT = True      # None | True | False
D3_EXTENSION_CTRL_RESET = "execute"   # None | "execute" | "ignore"

# D4 — REQ-INIT-009.  StreamPacketSizeMax written with a value that is not
# a multiple of 4, or below 36 bytes.
#   None | 0x41 | "round_down"
#   Read by: T-06.
# Decided: a value that is not a multiple of 4 is refused 0x41, the
# register keeps its value (§10.3.33: "shall be a multiple of 4").  A
# multiple of 4 below 36 bytes (one header + one data word) is accepted,
# and no image enters until StreamPacketSizeMax is at least 36 (the
# acquisition gate holds the stream; the wire carries IDLE only).
D4_SPSM_BAD_VALUE = 0x41
D4_SPSM_BELOW_MIN = "accept_hold"     # None | 0x41 | "accept_hold"
D4_SPSM_MIN_BYTES = 36

# D5 — REQ-PROT-031, REQ-BOOT-004.  A selector (XmlManifestSelector,
# TestErrorCountSelector) written out of range.
#   None | 0x41 | "clamp"
#   Read by: T-03, T-25.
# Decided: refused 0x41, the selector keeps its value — a selector names
# something that must exist (§10.3.36 TestErrorCountSelector "the
# connection"; XmlManifestSelector indexes XmlManifestSize manifests),
# and 0x41 is Table 22's "invalid data" answer.
D5_SELECTOR_OUT_OF_RANGE = 0x41

# D6 — REQ-TRIG-011.  I/O-acknowledgment latency the IP guarantees, in
# low-speed character times (the Host LS timeout is about one).
#   None | int
#   Read by: C-02, CXP-CAM-TRIG-006.
# Decided: one.  §8.3.3 sets the Host's transmission timeout for a trigger
# sent on the low-speed connection to one low-speed character; an
# acknowledgment later than that lets the Host resend or send a new
# trigger.  Measured from the trigger's last character leaving the Host
# to the acknowledgment's leader on the downlink.
D6_IOACK_LATENCY_CHARS = 1

# D7 — REQ-CTRL-016.  What the device does when a Host overlaps commands.
#   None | "serialise" | "nack" | "drop_second"
#   Read by: T-11.
# Decided: serialise.  §8.6.1.1 has the Host wait for the acknowledgment
# before the next command, except a re-send after its 200 ms timeout, so
# one command may wait behind the one executing; it starts once that
# one's response has been handed to the acknowledgment framer.  A command
# that starts while one already waits is dropped with no acknowledgment
# (a 0xFF always gets through).  Each command gets its own acknowledgment,
# in order, with its own data.
D7_OVERLAPPED_COMMANDS = "serialise"
D7_WAITING_COMMANDS = 1

# D8 — REQ-ERR-010/011.  Which code wins when several apply (0x46 / 0x47 /
# 0x80), and the code for Size = 0.
#   None | tuple of codes, highest priority first
#   Read by: T-08.
# Decided (Table 22 wording): 0x47 "malformed packet" only when the
# framing itself breaks — the trailer lost, or the trailer before the
# command word; a command cut short or run long against its Size is 0x46
# "message size inconsistent with the size indication"; then the opcode
# (0x42), the CRC (0x80), the size limit (0x45); first match wins, as the
# parser checks them.  A read or write with Size 0 is 0x46 (Table 21:
# B >= 1).
D8_CODE_PRIORITY = (0x47, 0x42, 0x46, 0x80, 0x45)
D8_SIZE_ZERO_CODE = 0x46

# D9 — (no requirement ID).  SourceTag owner: does the IP count SourceTag
# itself, or does it pass the sensor's metadata through?
#   None | "ip_counts" | "passthrough"
#   Read by: V-08 test_source_tag_sequence (sensor arm).
# Decided: passthrough on the pixel port.  Table 38 wants the same
# SourceTag in every stream of one image, which only the sensor side
# knows, so an image from the pixel port carries the SourceTag of its
# metadata, taken with its first pixel (device_top 26, 36).  The test
# pattern generator has no sensor behind it and counts its own images.
D9_SOURCETAG_OWNER = "passthrough"

# D10 — validation plan §4.3.  DUT feature profile and the geometry limit
# policy.  The profile is what makes the N/A rows of the proposal N/A.
#   D10_FEATURE_PROFILE: None | dict feature -> bool
#   D10_GEOMETRY_OUT_OF_RANGE: None | 0x41 ("reject, value unchanged") |
#                              "clamp"
#   Read by: every N/A row of the plan map; V-10 test_geometry_bounds.
# Decided: the profile of the validation proposal (§4.3 of the plan),
# except ECT: ElectricalComplianceTest is implemented (§10.3.40; its
# power-up behaviour is the open emu finding CT-006).  Width and Height
# outside 1..4096 and OffsetX / OffsetY above 4095 are refused 0x41, the
# register keeping its value (cxp_ctrl_bootstrap_regs value_ok, T-07).
D10_FEATURE_PROFILE = {
    "MULTI": False, "HSUP": False, "LINESCAN": False, "INTERLACED": False,
    "MSTREAM": False, "MTAP": False, "COLOR": False, "IIDC2": False,
    "ECT": True, "ARB": True, "D2HTRIG": True, "LONGOP": True, "ZIPXML": False,
}
D10_GEOMETRY_OUT_OF_RANGE = 0x41

# --------------------------------------------------------------------------
# Recorded.

# §10.3.28 calls the ConnectionReset write "fire and forget"; the device
# acknowledges it once today.  Recorded in prompts/uvm_env_review_followup.md
# ("Decisions to take and record"): keep exactly one acknowledgment.
#   Read by: sb_linkreset, T-04, V-01.
CONNECTION_RESET_ACKS = 1


def undecided() -> list:
    """Names of the decisions still open, for the report."""
    return sorted(k for k, v in globals().items()
                  if k[:1] == "D" and k[1:2].isdigit() and v is None)
