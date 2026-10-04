// CoaXPress 1.1.1 protocol constants (cxp/protocol/constants.py).
//
// Values mirror the RTL verification package (src/verif/uvm/common/cxp_pkg.py)
// so the host stack and the RTL device agree on the wire format.  Spec
// references use CoaXPress 1.1.1 (CXP-001-2015) numbering.
#pragma once

#include <cstdint>
#include <optional>
#include <vector>

#include "cxp_regmap.hpp"

namespace cxp {

using Words = std::vector<uint32_t>;

// --------------------------------------------------------------------------
// 8B/10B K-code data bytes (pre-encoding).  §9.2.5.
// --------------------------------------------------------------------------
inline constexpr uint8_t K28_0 = 0x1C;
inline constexpr uint8_t K28_1 = 0x3C;
inline constexpr uint8_t K28_2 = 0x5C;
inline constexpr uint8_t K28_3 = 0x7C;
inline constexpr uint8_t K28_4 = 0x9C;
inline constexpr uint8_t K28_5 = 0xBC;
inline constexpr uint8_t K27_7 = 0xFB;  // Start Of Packet marker byte
inline constexpr uint8_t K29_7 = 0xFD;  // End Of Packet marker byte
inline constexpr uint8_t D21_5 = 0xB5;  // IDLE filler data byte

// IDLE word as transmitted on-wire (P0..P3): K28.5 K28.1 K28.1 D21.5.
inline constexpr uint32_t IDLE_WORD =
    uint32_t(K28_5) | (uint32_t(K28_1) << 8) | (uint32_t(K28_1) << 16) |
    (uint32_t(D21_5) << 24);
inline constexpr uint32_t IDLE_KMASK = 0b0111;

// SOP / EOP 32-bit words (4x repeated K-byte).
inline constexpr uint32_t SOP_WORD = 0xFBFBFBFBu;
inline constexpr uint32_t EOP_WORD = 0xFDFDFDFDu;
inline constexpr uint32_t KMARK_FULL = 0b1111;

// CXP packet TYPE byte (§9.2.4 / Table 15, host<->device).
enum class PacketType : uint8_t {
    Stream = 0x01,     // device->host video stream packet
    CtrlCmd = 0x02,    // host->device control command
    CtrlAck = 0x03,    // device->host control acknowledge
    LinkTest = 0x04,   // link-test pattern packet
    Event = 0x05,      // device->host event packet
    Discovery = 0x06,  // host->device discovery / heartbeat (host extension)
};

std::optional<PacketType> toPacketType(uint8_t b);
const char* packetTypeName(PacketType t);  // "STREAM", "CTRL_CMD", ...

// Control-command OPCODE byte (host->device, Table 20).
enum class CtrlOpcode : uint8_t { Read = 0x00, Write = 0x01, Reset = 0xFF };

std::optional<CtrlOpcode> toCtrlOpcode(uint8_t b);
const char* ctrlOpcodeName(CtrlOpcode op);

// Control-acknowledge result code (device->host, CXP-001-2015 §8.6.3 Table 22).
enum class AckCode : uint8_t {
    Ok = 0x00,        // final, read OK, reply data appended
    WriteOk = 0x01,   // final, write OK, no reply data
    ResetOk = 0x03,   // final, control channel reset executed
    Wait = 0x04,      // wait, 4-byte ms reply data
    InvalidAddress = 0x40,
    InvalidData = 0x41,
    InvalidOperation = 0x42,
    WriteProtect = 0x43,  // write to a read-only address
    ReadProtect = 0x44,   // read from a write-only address
    BadSize = 0x45,       // Size field too large
    SizeMismatch = 0x46,  // message size inconsistent with Size
    Malformed = 0x47,
    CrcError = 0x80,
};

std::optional<AckCode> toAckCode(uint8_t b);
const char* ackCodeName(AckCode c);  // "OK", "WRITE_OK", ...

// A few well-known event identifiers (§12.2).
namespace EventCode {
inline constexpr uint32_t HEARTBEAT = 0x0000;
inline constexpr uint32_t LINK_ERROR = 0x8000;
inline constexpr uint32_t OVERTEMP = 0x8001;
inline constexpr uint32_t STREAM_OVERFLOW = 0x8002;
}  // namespace EventCode

// --------------------------------------------------------------------------
// Bootstrap register map: the generated src/regmap/cxp_regmap.hpp
// (namespace cxp::reg, from src/regmap/cxp_regmap.yaml).  The names below
// are the host's older spellings of the same registers.
// --------------------------------------------------------------------------
namespace Bootstrap {
inline constexpr uint32_t STANDARD = reg::STANDARD;
inline constexpr uint32_t REVISION = reg::REVISION;
inline constexpr uint32_t XML_MFST_SIZE = reg::XML_MANIFEST_SIZE;
inline constexpr uint32_t XML_URL_ADDRESS = reg::XML_URL_ADDRESS;  // *pointer* to the URL (§10.3.11)
inline constexpr uint32_t VENDOR_NAME = reg::DEVICE_VENDOR_NAME;
inline constexpr uint32_t MODEL_NAME = reg::DEVICE_MODEL_NAME;
inline constexpr uint32_t DEVICE_LINK_ID = reg::DEVICE_CONNECTION_ID;
inline constexpr uint32_t MASTER_HOST_LINK_ID = reg::MASTER_HOST_CONNECTION_ID;
inline constexpr uint32_t STREAM_PACKET_DATA_SIZE = reg::STREAM_PACKET_SIZE_MAX;
}  // namespace Bootstrap

// Magic value read from Standard to recognise a CXP device (§10.3.5).
inline constexpr uint32_t CXP_MAGIC = reg::STANDARD_VALUE;

// Default target of XmlUrlAddress: the GenICam XML URL string (64 B) lives
// there, not inline at 0x001C (§10.3.11).
inline constexpr uint32_t XML_URL_PTR_DEFAULT = reg::XML_URL_ADDRESS_VALUE;

// Maximum 32-bit words a single control READ/WRITE command may carry.
// The device NAKs anything larger with BAD_SIZE — see
// src/rtl/ctrl/cxp_ctrl_cmd_parser.sv (MAX_N = BUF_DEPTH = 64).
inline constexpr uint32_t CXP_MAX_CTRL_XFER_WORDS = 64;

// Default control-command data size (32-bit words).
inline constexpr uint32_t DEFAULT_CTRL_DSIZE_WORDS = 1;

// The smallest StreamPacketSizeMax that holds a stream packet (Table 19):
// SOP, type, 4 header words, one data word, CRC, EOP = 9 words.
inline constexpr uint32_t SPSM_MIN_BYTES = 36;

// Stream rectangular image-header / line-marker word counts (Tables 37/38).
inline constexpr int RECT_HDR_WORDS = 25;
inline constexpr int RECT_LINE_WORDS = 9;
inline constexpr int ARB_HDR_WORDS = 16;
inline constexpr int ARB_LINE_WORDS = 11;

// Protocol timing budgets (§9.6.3), milliseconds.
inline constexpr int CTRL_ACK_TIMEOUT_MS = 200;
inline constexpr int HEARTBEAT_PERIOD_MS = 200;
inline constexpr int LINK_INIT_TIMEOUT_MS = 1000;

inline constexpr int WORD_BYTES = 4;

}  // namespace cxp
