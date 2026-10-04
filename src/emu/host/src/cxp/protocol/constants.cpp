#include "cxp/protocol/constants.h"

namespace cxp {

std::optional<PacketType> toPacketType(uint8_t b) {
    if (b >= 0x01 && b <= 0x06) return static_cast<PacketType>(b);
    return std::nullopt;
}

const char* packetTypeName(PacketType t) {
    switch (t) {
    case PacketType::Stream:    return "STREAM";
    case PacketType::CtrlCmd:   return "CTRL_CMD";
    case PacketType::CtrlAck:   return "CTRL_ACK";
    case PacketType::LinkTest:  return "LINKTEST";
    case PacketType::Event:     return "EVENT";
    case PacketType::Discovery: return "DISCOVERY";
    }
    return "UNKNOWN";
}

std::optional<CtrlOpcode> toCtrlOpcode(uint8_t b) {
    switch (b) {
    case 0x00: return CtrlOpcode::Read;
    case 0x01: return CtrlOpcode::Write;
    case 0xFF: return CtrlOpcode::Reset;
    default:   return std::nullopt;
    }
}

const char* ctrlOpcodeName(CtrlOpcode op) {
    switch (op) {
    case CtrlOpcode::Read:  return "READ";
    case CtrlOpcode::Write: return "WRITE";
    case CtrlOpcode::Reset: return "RESET";
    }
    return "?";
}

std::optional<AckCode> toAckCode(uint8_t b) {
    switch (b) {
    case 0x00: case 0x01: case 0x03: case 0x04:
    case 0x40: case 0x41: case 0x42: case 0x43:
    case 0x44: case 0x45: case 0x46: case 0x47:
    case 0x80:
        return static_cast<AckCode>(b);
    default:
        return std::nullopt;
    }
}

const char* ackCodeName(AckCode c) {
    switch (c) {
    case AckCode::Ok:               return "OK";
    case AckCode::WriteOk:          return "WRITE_OK";
    case AckCode::ResetOk:          return "RESET_OK";
    case AckCode::Wait:             return "WAIT";
    case AckCode::InvalidAddress:   return "INVALID_ADDRESS";
    case AckCode::InvalidData:      return "INVALID_DATA";
    case AckCode::InvalidOperation: return "INVALID_OPERATION";
    case AckCode::WriteProtect:     return "WRITE_PROTECT";
    case AckCode::ReadProtect:      return "READ_PROTECT";
    case AckCode::BadSize:          return "BAD_SIZE";
    case AckCode::SizeMismatch:     return "SIZE_MISMATCH";
    case AckCode::Malformed:        return "MALFORMED";
    case AckCode::CrcError:         return "CRC_ERROR";
    }
    return "?";
}

}  // namespace cxp
